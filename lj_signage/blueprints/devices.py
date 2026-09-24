"""Loja: what plays, staging, legacy files, history, "Aplicar agora", folder discovery
(SPEC §9.2). Every change here only writes the database and creates worker jobs."""

from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

from .. import audit, jobs
from ..auth import admin_required
from ..discovery import DiscoveryResult
from ..extensions import db
from ..inventory import forget_folder
from ..models import AuditLog, Device, DeviceFile, Job
from ..summaries import summarize
from ..timeutil import utcnow

bp = Blueprint("devices", __name__, url_prefix="/lojas")


def _device(code: str) -> Device:
    device = db.session.execute(db.select(Device).filter_by(code=code)).scalar_one_or_none()
    return device or abort(404)


def _recent_jobs(device: Device, limit: int = 5) -> list[Job]:
    return (
        db.session.execute(
            db.select(Job).where(Job.device_id == device.id).order_by(Job.id.desc()).limit(limit)
        )
        .scalars()
        .all()
    )


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _jobs_response(device: Device, message: str | None = None):
    if _is_htmx():
        return render_template(
            "devices/_jobs.html", device=device, recent_jobs=_recent_jobs(device), message=message
        )
    if message:
        flash(message, "success")
    return redirect(url_for("devices.detail", code=device.code))


@bp.get("/<code>")
def detail(code: str):
    settings = current_app.config["LJ_SETTINGS"]
    device = _device(code)
    summary = summarize(device, utcnow(), settings)
    discovery = DiscoveryResult.from_dict(device.discovery) if device.discovery else None
    history = (
        db.session.execute(
            db.select(AuditLog)
            .where(AuditLog.device_id == device.id)
            .order_by(AuditLog.id.desc())
            .limit(25)
        )
        .scalars()
        .all()
    )
    return render_template(
        "devices/detail.html",
        device=device,
        summary=summary,
        discovery=discovery,
        history=history,
        recent_jobs=_recent_jobs(device),
    )


@bp.get("/<code>/tarefas")
def recent_jobs(code: str):
    device = _device(code)
    return render_template("devices/_jobs.html", device=device, recent_jobs=_recent_jobs(device))


@bp.post("/<code>/aplicar")
def apply_now(code: str):
    device = _device(code)
    if not device.in_config or device.video_path is None:
        message = (
            "Esta loja já não está no devices.yaml: não é sincronizada."
            if not device.in_config
            else "Confirme primeiro a pasta de vídeos desta loja."
        )
        if _is_htmx():
            return _jobs_response(device, message)
        flash(message, "error")
        return redirect(url_for("devices.detail", code=code))
    jobs.enqueue("reconcile", device_id=device.id, created_by=current_user.email)
    audit.record("reconcile_requested", "Pedido de sincronização imediata.", device=device)
    db.session.commit()
    return _jobs_response(device, "Pedido enviado: a loja vai ser sincronizada dentro de momentos.")


@bp.post("/<code>/descobrir")
@admin_required
def discover(code: str):
    device = _device(code)
    jobs.enqueue("discover", device_id=device.id, created_by=current_user.email)
    audit.record("discovery_requested", "Descoberta da pasta de vídeos pedida.", device=device)
    db.session.commit()
    return _jobs_response(device, "A procurar a pasta de vídeos (só leitura)…")


@bp.post("/<code>/pasta")
@admin_required
def confirm_path(code: str):
    device = _device(code)
    path = request.form.get("ftp_path", "")
    candidates = [c["ftp_path"] for c in (device.discovery or {}).get("candidates", [])]
    if device.video_path_source == "config":
        flash("A pasta desta loja está definida no devices.yaml.", "error")
    elif path not in candidates:
        flash("Escolha uma das pastas encontradas pela descoberta.", "error")
    else:
        if device.video_path != path:
            forget_folder(device)
        device.video_path = path
        device.video_path_source = "discovery"
        device.video_path_confirmed_by = current_user.email
        device.video_path_confirmed_at = utcnow()
        audit.record(
            "video_path_confirmed", f"Pasta de vídeos: {device.video_path_display}", device=device
        )
        jobs.enqueue("reconcile", device_id=device.id, created_by=current_user.email)
        db.session.commit()
        flash("Pasta confirmada. A primeira leitura da loja foi pedida.", "success")
    return redirect(url_for("devices.detail", code=code))


@bp.post("/<code>/pasta/anular")
@admin_required
def clear_path(code: str):
    device = _device(code)
    if device.video_path_source == "config":
        flash("A pasta desta loja está definida no devices.yaml.", "error")
        return redirect(url_for("devices.detail", code=code))
    old = device.video_path_display
    device.video_path = None
    device.video_path_source = None
    device.video_path_confirmed_by = None
    device.video_path_confirmed_at = None
    forget_folder(device)
    audit.record("video_path_cleared", f"Pasta anulada (era {old}).", device=device)
    db.session.commit()
    flash("Pasta anulada. Corra a descoberta de novo para escolher a pasta.", "success")
    return redirect(url_for("devices.detail", code=code))


def _legacy_file(device: Device, file_id: int) -> DeviceFile:
    row = db.session.get(DeviceFile, file_id)
    if row is None or row.device_id != device.id or row.location != "root":
        abort(404)
    if row.state != "legacy":
        abort(400)
    return row


@bp.post("/<code>/ficheiros/<int:file_id>/remover")
@admin_required
def request_delete(code: str, file_id: int):
    device = _device(code)
    row = _legacy_file(device, file_id)
    if row.is_dir:
        flash("A app não apaga pastas.", "error")
        return redirect(url_for("devices.detail", code=code))
    row.delete_requested_by = current_user.email
    row.delete_requested_at = utcnow()
    audit.record("legacy_delete_requested", f"Remover {row.display_name} do Pi.", device=device)
    db.session.commit()
    flash(
        f"{row.display_name} será apagado na próxima sincronização "
        "(só se continuar a haver um vídeo a passar).",
        "success",
    )
    return redirect(url_for("devices.detail", code=code))


@bp.post("/<code>/ficheiros/<int:file_id>/cancelar")
@admin_required
def cancel_delete(code: str, file_id: int):
    device = _device(code)
    row = _legacy_file(device, file_id)
    row.delete_requested_by = None
    row.delete_requested_at = None
    audit.record("legacy_delete_cancelled", f"Mantém {row.display_name}.", device=device)
    db.session.commit()
    flash(f"{row.display_name} já não vai ser apagado.", "success")
    return redirect(url_for("devices.detail", code=code))


@bp.post("/<code>/ficheiros/<int:file_id>/adotar")
def adopt(code: str, file_id: int):
    device = _device(code)
    row = _legacy_file(device, file_id)
    if row.is_dir:
        flash("Só é possível adotar ficheiros de vídeo.", "error")
        return redirect(url_for("devices.detail", code=code))
    jobs.enqueue(
        "adopt", payload={"file_id": row.id}, device_id=device.id, created_by=current_user.email
    )
    audit.record(
        "legacy_adopt_requested", f"Adotar {row.display_name} para a biblioteca.", device=device
    )
    db.session.commit()
    flash(f"{row.display_name} vai ser descarregado e convertido para a biblioteca.", "success")
    return redirect(url_for("devices.detail", code=code))
