"""Health check (for Docker) and a small JSON status endpoint."""

from __future__ import annotations

from flask import Blueprint, current_app
from sqlalchemy import text

from ..extensions import db
from ..models import Device
from ..status import worker_status
from ..summaries import summarize
from ..timeutil import utcnow

bp = Blueprint("api", __name__)


@bp.get("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:
        return {"status": "error", "database": "indisponível"}, 503
    return {"status": "ok"}


@bp.get("/api/estado")
def state():
    settings = current_app.config["LJ_SETTINGS"]
    now = utcnow()
    worker = worker_status(now)
    devices = db.session.execute(
        db.select(Device).where(Device.in_config.is_(True)).order_by(Device.store_number)
    ).scalars()
    result = []
    for device in devices:
        summary = summarize(device, now, settings)
        result.append(
            {
                "code": device.code,
                "store": device.store_number,
                "name": device.name,
                "status": device.status,
                "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
                "consecutive_failures": device.consecutive_failures,
                "playing": summary.playable_count,
                "pending_actions": summary.pending_actions,
                "alerts": [a.message for a in summary.alerts if a.level != "info"],
            }
        )
    return {
        "dry_run": settings.dry_run,
        "worker": {
            "alive": worker.alive,
            "beat_at": worker.beat_at.isoformat() if worker.beat_at else None,
            "next_reconcile_at": (
                worker.next_reconcile_at.isoformat() if worker.next_reconcile_at else None
            ),
        },
        "devices": result,
    }
