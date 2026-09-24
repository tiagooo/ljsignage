"""One reconciliation cycle for one device (SPEC §6).

Used by the worker (every RECONCILE_INTERVAL_MIN and for "Aplicar agora") and by
the CLI. A per-device lease in the database guarantees one cycle at a time per
Pi, even across processes (worker and CLI).

Writing requires all three: ``execute=True`` (the caller wants to apply),
``DRY_RUN=false`` and ``enabled: true`` for the device. Otherwise the cycle only
reads the Pi (read-only FTP client) and records the plan.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, update
from sqlalchemy.exc import InvalidRequestError

from .. import audit
from ..config import Settings
from ..discovery import DiscoveryResult, discover
from ..extensions import db
from ..ftp import FtpAuthError, FtpClient, FtpConnectError, FtpError
from ..models import Device, DeviceFile, Video
from ..timeutil import utcnow
from . import planner as pl
from .executor import ExecutionError, Executor, LeaseLost, Outcome, read_remote_state
from .state import library, planner_input, store_snapshot

log = logging.getLogger(__name__)

LOCK_TTL = timedelta(minutes=30)
LEASE_RENEW_S = 30.0  # renew the lease this often while a file is being sent


@dataclass
class CycleResult:
    device_code: str
    status: str  # online | offline | error | busy | unknown
    message: str = ""
    plan: pl.Plan | None = None
    outcomes: list[Outcome] = field(default_factory=list)
    wrote: bool = False
    read_only_reason: str | None = None


def default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


def client_for(device: Device, settings: Settings, *, read_only: bool) -> FtpClient:
    user, password = settings.ftp_credentials(device.code)
    return FtpClient(
        device.host,
        device.ftp_port,
        user,
        password,
        timeout=settings.ftp_timeout_s,
        read_only=read_only,
    )


ClientFactory = Callable[..., FtpClient]


# --- per-device lease -------------------------------------------------------------


def acquire_lock(device_id: int, owner: str, now: datetime | None = None) -> bool:
    now = now or utcnow()
    result = db.session.execute(
        update(Device)
        .where(
            Device.id == device_id,
            or_(
                Device.lock_owner.is_(None),
                Device.lock_expires_at < now,
                Device.lock_owner == owner,
            ),
        )
        .values(lock_owner=owner, lock_expires_at=now + LOCK_TTL)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return result.rowcount == 1


def renew_lock(device_id: int, owner: str) -> bool:
    """Extend the lease. False if it was lost (expired and taken by someone else)."""
    result = db.session.execute(
        update(Device)
        .where(Device.id == device_id, Device.lock_owner == owner)
        .values(lock_expires_at=utcnow() + LOCK_TTL)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return result.rowcount == 1


def release_lock(device_id: int, owner: str) -> None:
    db.session.execute(
        update(Device)
        .where(Device.id == device_id, Device.lock_owner == owner)
        .values(lock_owner=None, lock_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()


# --- the cycle ----------------------------------------------------------------------


def run_cycle(
    device_id: int,
    settings: Settings,
    *,
    execute: bool,
    actor: audit.Actor | None = None,
    owner: str | None = None,
    client_factory: ClientFactory = client_for,
) -> CycleResult:
    owner = owner or default_owner()
    device = db.session.get(Device, device_id)
    if device is None:
        raise ValueError(f"unknown device {device_id}")
    code = device.code
    if not acquire_lock(device_id, owner):
        return CycleResult(code, "busy", "Já está a decorrer uma sincronização desta loja.")
    try:
        db.session.refresh(device)
        return _cycle(device, settings, execute, actor, owner, client_factory)
    except Exception as exc:  # a bug must not leave the device looking healthy
        log.exception("reconciliation cycle crashed for %s", code)
        db.session.rollback()
        device = db.session.get(Device, device_id)
        device.status = "error"
        device.last_error = f"Erro interno na sincronização ({exc.__class__.__name__})."
        db.session.commit()
        return CycleResult(code, "error", device.last_error)
    finally:
        db.session.rollback()  # drop anything left uncommitted by a failure
        release_lock(device_id, owner)


def _cycle(
    device: Device,
    settings: Settings,
    execute: bool,
    actor: audit.Actor | None,
    owner: str,
    client_factory: ClientFactory,
) -> CycleResult:
    started = utcnow()
    can_write = execute and not settings.dry_run and device.enabled and device.in_config
    reason = None
    if not can_write:
        if settings.dry_run:
            reason = "Modo simulação (DRY_RUN=true): nada é escrito nos Pis."
        elif not device.in_config:
            reason = "Esta loja já não está no devices.yaml: só leitura."
        elif not device.enabled:
            reason = "Escrita desativada para esta loja (enabled: false no devices.yaml)."
        else:
            reason = "Só leitura (pedido sem execução)."
    result = CycleResult(device.code, "unknown", read_only_reason=reason)
    previous = device.status
    device.last_checked_at = started

    try:
        with client_factory(device, settings, read_only=not can_write) as client:
            device.ftp_banner = (client.welcome or "")[:200] or None
            # Never hold a SQLite write transaction while waiting on FTP: commit
            # pending changes before every block of FTP reads.
            db.session.commit()
            if device.video_path is None and device.config_video_path:
                _adopt_configured_path(client, device, started)
                db.session.commit()
            if device.video_path is None:
                if device.video_path_source == "config":
                    result.message = "A pasta definida no devices.yaml não foi encontrada."
                    result.status = "error"
                    _set_status(device, previous, "error", result.message, started)
                else:
                    result.message = "Pasta de vídeos por confirmar."
                    result.status = "online"
                    _set_status(device, previous, "online", None, started)
                db.session.commit()
                return result

            lib = library()
            remote = read_remote_state(client, device.video_path)
            first_read = device.snapshot_at is None
            changes = store_snapshot(device, remote, started, lib=lib)
            if first_read and changes.new_legacy:
                audit.record(
                    "legacy_registered",
                    f"Primeira leitura: {len(changes.new_legacy)} ficheiro(s) existente(s) "
                    "registado(s) como legado(s). Não são apagados.",
                    device=device,
                    result="info",
                    data={"files": [f.display_name for f in changes.new_legacy]},
                )
            for name in changes.dropped_requests:
                audit.record(
                    "legacy_delete_cancelled",
                    f"Pedido de remoção de {name} anulado: o ficheiro mudou no Pi desde o pedido.",
                    device=device,
                    actor=audit.SYSTEM,
                    result="info",
                )
            plan = pl.plan(planner_input(device, remote, started, settings, lib=lib))
            result.plan = plan
            db.session.commit()

            if plan.actions and can_write:
                result.outcomes = _execute(client, device, plan, actor, owner, settings)
                result.wrote = any(o.ok for o in result.outcomes)
                fatal = next((o for o in result.outcomes if o.fatal), None)
                if fatal is not None:  # do not touch this FTP session again
                    raise ExecutionError(f"{pl.describe(fatal.action, plan)} {fatal.error}")
                remote = read_remote_state(client, device.video_path)
                store_snapshot(device, remote, utcnow(), lib=lib)
                failed = next((o for o in result.outcomes if not o.ok), None)
                after = pl.plan(planner_input(device, remote, utcnow(), settings, lib=lib))
                device.plan_digest = after.digest
                db.session.commit()  # keep the post-execution snapshot even on failure
                if failed is not None:
                    raise ExecutionError(
                        f"{pl.describe(failed.action, plan)} Falhou: {failed.error}"
                    )
                if after.is_empty:
                    device.last_reconcile_at = utcnow()
            elif plan.actions:
                if plan.digest != device.plan_digest or actor is not None:
                    audit.record(
                        "plan",
                        f"{len(plan.actions)} ação(ões) por aplicar. {reason}",
                        actor=actor or audit.SYSTEM,
                        device=device,
                        result="dry_run",
                        data={"actions": [pl.describe(a, plan) for a in plan.actions]},
                    )
                device.plan_digest = plan.digest
            else:
                device.plan_digest = plan.digest
                device.last_reconcile_at = started

            _set_status(device, previous, "online", None, started)
            result.status = "online"
            result.message = _summary(plan, result)
    except FtpConnectError as exc:
        db.session.rollback()
        _set_status(device, previous, "offline", str(exc), started)
        result.status, result.message = "offline", str(exc)
    except (FtpAuthError, FtpError, ExecutionError) as exc:
        db.session.rollback()
        _set_status(device, previous, "error", str(exc), started)
        result.status, result.message = "error", str(exc)
        if isinstance(exc, ExecutionError):
            audit.record(
                "reconcile_failed",
                str(exc),
                actor=actor or audit.SYSTEM,
                device=device,
                result="error",
            )
    db.session.commit()
    return result


def _summary(plan: pl.Plan, result: CycleResult) -> str:
    if result.wrote:
        return f"{sum(o.ok for o in result.outcomes)} ação(ões) aplicada(s)."
    if plan.actions:
        return f"{len(plan.actions)} ação(ões) por aplicar."
    return "Em dia: nada a fazer."


def _set_status(
    device: Device, previous: str, status: str, error: str | None, now: datetime
) -> None:
    device.status = status
    device.last_error = error
    device.last_checked_at = now
    if status == "online":
        device.last_seen_at = now
        device.consecutive_failures = 0
    else:
        device.consecutive_failures = (device.consecutive_failures or 0) + 1
    if status != previous and (status != "online" or previous in ("offline", "error")):
        labels = {"online": "Online", "offline": "Offline", "error": "Erro"}
        audit.record(
            "device_status",
            f"{labels.get(previous, 'Por verificar')} → {labels[status]}"
            + (f": {error}" if error else ""),
            actor=audit.SYSTEM,
            device=device,
            result="ok" if status == "online" else "error",
        )


def _adopt_configured_path(client: FtpClient, device: Device, now: datetime) -> None:
    """video_path from devices.yaml: validate it exists and use it (SPEC §7.1)."""
    found: DiscoveryResult = discover(
        client, login_home=device.login_home, now=now, config_video_path=device.config_video_path
    )
    device.discovery = found.to_dict()
    device.discovery_at = now
    if found.status == "found":
        device.video_path = found.candidates[0].ftp_path
        device.video_path_source = "config"
        device.video_path_confirmed_by = "devices.yaml"
        device.video_path_confirmed_at = now
        audit.record(
            "video_path_confirmed",
            f"Pasta definida no devices.yaml validada: {device.video_path_display}",
            device=device,
            actor=audit.SYSTEM,
        )


def _media_path(settings: Settings) -> Callable[[int], Path | None]:
    def resolve(video_id: int) -> Path | None:
        video = db.session.get(Video, video_id)
        if video is None or not video.media_path:
            return None
        return settings.data_dir / video.media_path

    return resolve


def _execute(
    client: FtpClient,
    device: Device,
    plan: pl.Plan,
    actor: audit.Actor | None,
    owner: str,
    settings: Settings,
) -> list[Outcome]:
    requested_by = {
        f.remote_name: f.delete_requested_by
        for f in device.files
        if f.location == "root" and f.delete_requested_by
    }

    def on_outcome(outcome: Outcome, plan: pl.Plan) -> None:
        action = outcome.action
        detail = pl.describe(action, plan)
        if action.kind is pl.ActionKind.DELETE_LEGACY and requested_by.get(action.src or ""):
            detail += f" Pedido por {requested_by[action.src]}."
        if not outcome.ok:
            detail += f" Erro: {outcome.error}"
        audit.record(
            action.kind.value,
            detail,
            actor=actor or audit.SYSTEM,
            device=device,
            result="ok" if outcome.ok else "error",
            data={"src": action.src, "dst": action.dst, "seconds": round(outcome.seconds, 1)},
        )
        if outcome.ok and action.kind is pl.ActionKind.UPLOAD:
            _mark_uploaded(device, action)
        db.session.commit()

    last_renewal = [time.monotonic()]

    def keep_lease(*, now: bool = False) -> None:
        if not now and time.monotonic() - last_renewal[0] < LEASE_RENEW_S:
            return
        if not renew_lock(device.id, owner):
            raise LeaseLost(
                "A sincronização desta loja passou para outro processo: parei sem escrever mais."
            )
        last_renewal[0] = time.monotonic()

    def before_action(action: pl.Action) -> None:
        keep_lease(now=True)
        if action.kind is pl.ActionKind.DELETE_LEGACY:
            _confirm_legacy_delete(client, device, action)

    executor = Executor(
        client,
        device.video_path,
        media_path=_media_path(settings),
        on_outcome=on_outcome,
        before_action=before_action,
        progress=lambda _action, _sent: keep_lease(),
    )
    return executor.run(plan)


def _confirm_legacy_delete(client: FtpClient, device: Device, action: pl.Action) -> None:
    """Re-check a legacy deletion right before DELE: the request must still stand
    (it may have been cancelled while this cycle ran) and the file on the Pi must
    still be the one the admin saw."""
    row = next(
        (f for f in device.files if f.location == "root" and f.remote_name == action.src), None
    )
    try:
        if row is not None:
            db.session.refresh(row)
    except InvalidRequestError:
        row = None
    name = row.display_name if row is not None else action.src
    if row is None or row.delete_requested_at is None:
        raise ExecutionError(f"O pedido de remoção de {name} foi cancelado: nada foi apagado.")
    size = client.size(client.join(device.video_path, action.src))
    if size != row.size_bytes:
        raise ExecutionError(f"{name} mudou no Pi desde o pedido de remoção: não foi apagado.")


def _mark_uploaded(device: Device, action: pl.Action) -> None:
    now = utcnow()
    name = (action.dst or "").split("/", 1)[-1]
    row = next((f for f in device.files if f.location == "staging" and f.remote_name == name), None)
    if row is None:
        row = DeviceFile(device=device, location="staging", remote_name=name, state="staged")
        db.session.add(row)
    row.video_id = action.video_id
    row.size_bytes = action.size
    row.uploaded_at = now
    row.verified_at = now
