"""Worker heartbeat, stored in the database and shown in the panel (SPEC §11)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .extensions import db
from .models import SystemState
from .timeutil import utcnow

HEARTBEAT_KEY = "worker"
STALE_AFTER = timedelta(minutes=2)


@dataclass(frozen=True)
class WorkerStatus:
    alive: bool
    beat_at: datetime | None = None
    started_at: datetime | None = None
    next_reconcile_at: datetime | None = None
    dry_run: bool | None = None


def write_heartbeat(
    *,
    worker_id: str,
    started_at: datetime,
    next_reconcile_at: datetime | None,
    dry_run: bool,
    stopped: bool = False,
) -> None:
    row = db.session.get(SystemState, HEARTBEAT_KEY)
    if row is None:
        row = SystemState(key=HEARTBEAT_KEY)
        db.session.add(row)
    row.value = {
        "worker_id": worker_id,
        "beat_at": utcnow().isoformat(),
        "started_at": started_at.isoformat(),
        "next_reconcile_at": next_reconcile_at.isoformat() if next_reconcile_at else None,
        "dry_run": dry_run,
        "stopped": stopped,
    }
    db.session.commit()


def worker_status(now: datetime | None = None) -> WorkerStatus:
    now = now or utcnow()
    row = db.session.get(SystemState, HEARTBEAT_KEY)
    if row is None or not row.value:
        return WorkerStatus(alive=False)
    value = row.value

    def parse(key: str) -> datetime | None:
        raw = value.get(key)
        return datetime.fromisoformat(raw) if raw else None

    beat = parse("beat_at")
    return WorkerStatus(
        alive=beat is not None and now - beat < STALE_AFTER and not value.get("stopped"),
        beat_at=beat,
        started_at=parse("started_at"),
        next_reconcile_at=parse("next_reconcile_at"),
        dry_run=value.get("dry_run"),
    )
