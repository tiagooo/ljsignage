"""Atividade: filterable audit log (SPEC §9.6)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from flask import Blueprint, current_app, render_template, request

from ..audit import ACTION_LABELS, RESULT_LABELS
from ..extensions import db
from ..models import AuditLog, Device

bp = Blueprint("activity", __name__, url_prefix="/atividade")

PER_PAGE = 50


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@bp.get("/")
def index():
    tz = current_app.config["LJ_SETTINGS"].tz
    args = request.args
    select = db.select(AuditLog)
    device_code = args.get("loja") or None
    user = args.get("utilizador") or None
    action = args.get("acao") or None
    result = args.get("resultado") or None
    start = _parse_date(args.get("de"))
    end = _parse_date(args.get("ate"))
    if device_code:
        select = select.where(AuditLog.device_code == device_code)
    if user == "sistema":
        select = select.where(AuditLog.user_email.is_(None))
    elif user:
        select = select.where(AuditLog.user_email == user)
    if action:
        select = select.where(AuditLog.action == action)
    if result:
        select = select.where(AuditLog.result == result)
    if start:
        select = select.where(AuditLog.created_at >= datetime.combine(start, time(0), tzinfo=tz))
    if end:
        limit = datetime.combine(end + timedelta(days=1), time(0), tzinfo=tz)
        select = select.where(AuditLog.created_at < limit)
    page = db.paginate(
        select.order_by(AuditLog.id.desc()), per_page=PER_PAGE, max_per_page=PER_PAGE
    )
    users = (
        db.session.execute(
            db.select(AuditLog.user_email, AuditLog.user_name)
            .where(AuditLog.user_email.is_not(None))
            .distinct()
            .order_by(AuditLog.user_email)
        )
        .tuples()
        .all()
    )
    devices = db.session.execute(db.select(Device).order_by(Device.store_number)).scalars().all()
    filters = {
        "loja": device_code or "",
        "utilizador": user or "",
        "acao": action or "",
        "resultado": result or "",
        "de": args.get("de", ""),
        "ate": args.get("ate", ""),
    }
    return render_template(
        "activity/index.html",
        page=page,
        users=_unique_users(users),
        devices=devices,
        actions=sorted(ACTION_LABELS.items(), key=lambda item: item[1]),
        results=RESULT_LABELS,
        filters=filters,
        query={k: v for k, v in filters.items() if v},
    )


def _unique_users(rows: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for email, name in rows:
        if email not in seen or (name and seen[email] == email):
            seen[email] = name or email
    return sorted(seen.items(), key=lambda item: item[1].lower())
