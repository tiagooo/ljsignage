"""Activity log (SPEC §4 AuditLog). Users are recorded by name and email only (RGPD)."""

from __future__ import annotations

from dataclasses import dataclass

from flask import has_request_context
from flask_login import current_user

from .extensions import db
from .models import AuditLog, Device, User


@dataclass(frozen=True)
class Actor:
    email: str | None
    name: str | None

    @property
    def label(self) -> str:
        return self.name or self.email or "Sistema"


SYSTEM = Actor(None, "Sistema")


def actor_for_email(email: str | None) -> Actor:
    if not email:
        return SYSTEM
    user = db.session.execute(db.select(User).filter_by(email=email)).scalar_one_or_none()
    return Actor(email, user.name if user else None)


def current_actor() -> Actor:
    if has_request_context() and current_user.is_authenticated:
        return Actor(current_user.email, current_user.name)
    return SYSTEM


def record(
    action: str,
    detail: str = "",
    *,
    actor: Actor | None = None,
    device: Device | None = None,
    result: str = "ok",
    data: dict | None = None,
) -> AuditLog:
    """Add an entry to the session (the caller commits)."""
    actor = actor or current_actor()
    entry = AuditLog(
        user_email=actor.email,
        user_name=actor.name,
        device_id=device.id if device else None,
        device_code=device.code if device else None,
        action=action,
        detail=detail[:4000],
        data=data,
        result=result,
    )
    db.session.add(entry)
    return entry


ACTION_LABELS = {
    "login": "Entrada no painel",
    "login_denied": "Entrada recusada",
    "logout": "Saída do painel",
    "user_authorized": "Acesso autorizado",
    "user_revoked": "Acesso removido",
    "devices_sync": "Leitura do devices.yaml",
    "device_status": "Estado da loja",
    "discovery_requested": "Descoberta pedida",
    "discovery": "Descoberta da pasta",
    "video_path_confirmed": "Pasta de vídeos confirmada",
    "video_path_cleared": "Pasta de vídeos anulada",
    "legacy_registered": "Ficheiros legados registados",
    "legacy_delete_requested": "Remoção de legado pedida",
    "legacy_delete_cancelled": "Remoção de legado cancelada",
    "legacy_adopt_requested": "Adoção de legado pedida",
    "legacy_adopted": "Legado adotado",
    "video_uploaded": "Vídeo enviado",
    "video_ready": "Vídeo pronto",
    "video_failed": "Conversão falhou",
    "video_updated": "Vídeo alterado",
    "video_deleted": "Vídeo apagado",
    "assignment_created": "Programação criada",
    "assignment_updated": "Programação alterada",
    "assignment_ended": "Programação terminada",
    "assignment_deleted": "Programação apagada",
    "reconcile_requested": "Aplicar agora",
    "plan": "Plano calculado",
    "mkdir_staging": "Pasta de preparação criada",
    "upload": "Envio para a preparação",
    "activate": "Vídeo ativado",
    "reorder": "Ordem alterada",
    "deactivate": "Vídeo retirado (guardado)",
    "delete_active": "Vídeo retirado",
    "delete_legacy": "Legado apagado",
    "delete_staged": "Limpeza da preparação",
    "delete_part": "Envio incompleto apagado",
    "reconcile_failed": "Sincronização falhou",
    "backup": "Backup da base de dados",
}

RESULT_LABELS = {
    "ok": "Concluído",
    "error": "Erro",
    "dry_run": "Simulação",
    "info": "Informação",
    "denied": "Recusado",
}
