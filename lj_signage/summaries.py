"""What the panel shows about a store: state, loop, alerts, "entra no ar até HH:MM".

Everything here reads the database only (last snapshot of each Pi), so the web
process never talks FTP.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Settings
from .extensions import db
from .models import Assignment, Device, DeviceFile, Group
from .naming import BLOCKING_ISSUES
from .reconciler import planner as pl
from .reconciler.state import plan_from_snapshot
from .status import WorkerStatus
from .timeutil import fmt_duration, fmt_relative

STATUS_LABELS = {
    "online": "Online",
    "offline": "Offline",
    "error": "Erro",
    "unknown": "Por verificar",
}
UNKNOWN_DURATION_S = 60.0  # estimate for legacy files, whose duration is unknown


def target_label(assignment: Assignment) -> str:
    if assignment.target_type == "all":
        return "Todas as lojas"
    if assignment.target_type == "group":
        group = db.session.get(Group, assignment.target_id)
        return f"Grupo {group.name}" if group else "Grupo removido"
    device = db.session.get(Device, assignment.target_id)
    return device.label if device else "Loja removida"


@dataclass
class Alert:
    level: str  # error | warning | info
    message: str


@dataclass
class DeviceSummary:
    device: Device
    plan: pl.Plan | None
    playing: list[DeviceFile] = field(default_factory=list)  # visible files, player order
    staged: list[DeviceFile] = field(default_factory=list)
    legacy: list[DeviceFile] = field(default_factory=list)
    loop_known_s: float = 0.0
    loop_unknown: int = 0
    alerts: list[Alert] = field(default_factory=list)
    write_mode: str = ""

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.device.status, self.device.status)

    @property
    def playable_count(self) -> int:
        return sum(1 for f in self.playing if _playable(f))

    @property
    def pending_actions(self) -> int:
        return len(self.plan.actions) if self.plan else 0

    @property
    def loop_label(self) -> str:
        """Short loop duration: "≥ 1 min" when legacy files of unknown length also play."""
        if self.device.snapshot_at is None or (not self.loop_known_s and self.loop_unknown):
            return "—"
        text = fmt_duration(self.loop_known_s)
        return f"≥ {text}" if self.loop_unknown else text

    @property
    def loop_estimate_s(self) -> float:
        return self.loop_known_s + UNKNOWN_DURATION_S * self.loop_unknown

    @property
    def worst(self) -> str | None:
        for level in ("error", "warning", "info"):
            if any(a.level == level for a in self.alerts):
                return level
        return None


def _playable(f: DeviceFile) -> bool:
    return f.state == "active" or (
        f.state == "legacy" and not BLOCKING_ISSUES.intersection(f.issues or [])
    )


def write_mode(device: Device, settings: Settings) -> str:
    if settings.dry_run:
        return "Simulação"
    return "Escrita ativa" if device.enabled else "Só leitura"


def summarize(device: Device, now: datetime, settings: Settings) -> DeviceSummary:
    plan = plan_from_snapshot(device, now, settings)
    root = sorted(
        (f for f in device.files if f.location == "root" and not f.remote_name.startswith(".")),
        key=lambda f: f.remote_name,
    )
    summary = DeviceSummary(
        device=device,
        plan=plan,
        playing=root,
        staged=[f for f in device.files if f.location == "staging"],
        legacy=[f for f in root if f.state == "legacy"],
        write_mode=write_mode(device, settings),
    )
    for f in root:
        if f.state == "active" and f.video is not None and f.video.duration_s:
            summary.loop_known_s += f.video.duration_s
        elif _playable(f):
            summary.loop_unknown += 1
    summary.alerts = _alerts(summary, now, settings)
    return summary


def _alerts(summary: DeviceSummary, now: datetime, settings: Settings) -> list[Alert]:
    device = summary.device
    alerts: list[Alert] = []
    if device.status == "offline":
        since = fmt_relative(device.last_seen_at, now, settings.tz)
        alerts.append(Alert("error", f"Sem ligação ao Pi (visto pela última vez: {since})."))
    elif device.status == "error" and device.last_error:
        alerts.append(Alert("error", device.last_error))
    if device.video_path is None:
        if device.config_video_path:
            alerts.append(Alert("warning", "A validar a pasta definida no devices.yaml."))
        else:
            alerts.append(Alert("warning", "Pasta de vídeos por confirmar."))
    if device.snapshot_at is not None and not summary.playable_count:
        alerts.append(Alert("error", "A pasta não tem nenhum vídeo reproduzível: ecrã preto."))
    broken = [f for f in summary.legacy if BLOCKING_ISSUES.intersection(f.issues or [])]
    if broken:
        alerts.append(
            Alert(
                "warning", f"{len(broken)} ficheiro(s) legado(s) que o leitor não consegue abrir."
            )
        )
    if summary.plan is not None:
        seen = set()
        for notice in summary.plan.notices:
            if notice.level == "info" or notice.code in seen:
                continue
            seen.add(notice.code)
            alerts.append(Alert(notice.level, notice.message))
        if summary.plan.actions:
            reason = (
                "modo simulação"
                if settings.dry_run
                else ("escrita desativada" if not device.enabled else "no próximo ciclo")
            )
            alerts.append(
                Alert("info", f"{len(summary.plan.actions)} alteração(ões) por aplicar ({reason}).")
            )
    return alerts


# --- "entra no ar até HH:MM" (SPEC §6) ------------------------------------------------------


@dataclass
class OnAir:
    at: datetime | None
    note: str


def first_cycle_after(moment: datetime, next_cycle: datetime, interval: timedelta) -> datetime:
    if moment <= next_cycle:
        return next_cycle
    steps = math.ceil((moment - next_cycle) / interval)
    return next_cycle + steps * interval


def on_air_estimate(
    assignment: Assignment,
    devices: list[Device],
    summaries: dict[int, DeviceSummary],
    worker: WorkerStatus,
    now: datetime,
    settings: Settings,
) -> OnAir:
    """Upper bound: next cycle after the start + one loop of the current carousel."""
    if assignment.end_at is not None and assignment.end_at <= now:
        return OnAir(None, "Terminada.")
    if settings.dry_run:
        return OnAir(None, "Modo simulação: não é enviado para as lojas.")
    writable = [d for d in devices if d.enabled]
    if not writable:
        return OnAir(None, "Não é enviado: nenhuma destas lojas tem a escrita ativa.")
    interval = timedelta(minutes=settings.reconcile_interval_min)
    next_cycle = worker.next_reconcile_at if worker.alive and worker.next_reconcile_at else None
    next_cycle = next_cycle or now + interval
    cycle = first_cycle_after(max(assignment.start_at, now), next_cycle, interval)
    loop = max(
        (summaries[d.id].loop_estimate_s for d in writable if d.id in summaries), default=0.0
    )
    return OnAir(cycle + timedelta(seconds=loop), "")
