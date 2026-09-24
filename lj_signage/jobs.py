"""Worker task queue stored in the database (no Redis).

The web process only creates jobs; the worker claims and runs them. Claiming is
a single conditional UPDATE, so two worker threads never take the same job.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, update

from .extensions import db
from .models import Job
from .timeutil import utcnow

MEDIA_JOBS = ("transcode",)
FTP_JOBS = ("reconcile", "discover", "adopt")
ACTIVE = ("pending", "running")

JOB_LABELS = {
    "transcode": "Conversão do vídeo",
    "reconcile": "Aplicar agora",
    "discover": "Descoberta da pasta",
    "adopt": "Adoção de ficheiro legado",
}
STATUS_LABELS = {
    "pending": "Em espera",
    "running": "A decorrer",
    "done": "Concluída",
    "failed": "Falhou",
    "cancelled": "Cancelada",
}


def enqueue(
    job_type: str,
    *,
    payload: dict | None = None,
    device_id: int | None = None,
    video_id: int | None = None,
    created_by: str | None = None,
    dedupe: bool = True,
    max_attempts: int = 3,
) -> Job:
    """Create a job, or return the same kind of job already waiting or running."""
    payload = payload or {}
    if dedupe:
        candidates = db.session.execute(
            select(Job).where(
                Job.type == job_type,
                Job.status.in_(ACTIVE),
                Job.device_id.is_(None) if device_id is None else Job.device_id == device_id,
                Job.video_id.is_(None) if video_id is None else Job.video_id == video_id,
            )
        ).scalars()
        for job in candidates:
            if job.payload == payload:
                return job
    job = Job(
        type=job_type,
        payload=payload,
        device_id=device_id,
        video_id=video_id,
        created_by=created_by,
        max_attempts=max_attempts,
    )
    db.session.add(job)
    db.session.flush()
    return job


def claim(types: tuple[str, ...], worker_id: str, now: datetime | None = None) -> Job | None:
    now = now or utcnow()
    next_id = (
        select(Job.id)
        .where(
            Job.status == "pending",
            Job.type.in_(types),
            or_(Job.not_before.is_(None), Job.not_before <= now),
        )
        .order_by(Job.id)
        .limit(1)
        .scalar_subquery()
    )
    claimed = db.session.execute(
        update(Job)
        .where(Job.id == next_id, Job.status == "pending")
        .values(
            status="running",
            started_at=now,
            heartbeat_at=now,
            worker_id=worker_id,
            attempts=Job.attempts + 1,
        )
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    db.session.commit()
    return db.session.get(Job, claimed) if claimed is not None else None


def set_progress(job: Job, percent: float | None, message: str | None = None) -> None:
    job.progress = None if percent is None else round(max(0.0, min(100.0, percent)), 1)
    if message is not None:
        job.message = message[:300]
    job.heartbeat_at = utcnow()
    db.session.commit()


def finish(job: Job, *, error: str | None = None, retry_in: timedelta | None = None) -> None:
    now = utcnow()
    if error is None:
        job.status = "done"
        job.progress = 100.0
        job.error = None
        job.finished_at = now
    elif retry_in is not None and job.attempts < job.max_attempts:
        job.status = "pending"
        job.error = error
        job.not_before = now + retry_in
    else:
        job.status = "failed"
        job.error = error
        job.finished_at = now
    db.session.commit()


def postpone(job: Job, delay: timedelta, message: str) -> None:
    """Put a claimed job back in the queue without counting an attempt."""
    job.status = "pending"
    job.attempts = max(0, job.attempts - 1)
    job.not_before = utcnow() + delay
    job.message = message
    db.session.commit()


def recover_stale(worker_id: str) -> int:
    """Jobs left 'running' by a previous worker process go back to the queue."""
    stale = db.session.execute(
        select(Job).where(Job.status == "running", Job.worker_id != worker_id)
    ).scalars()
    count = 0
    for job in stale:
        count += 1
        if job.attempts < job.max_attempts:
            job.status = "pending"
            job.message = "Retomada após reinício do serviço."
        else:
            job.status = "failed"
            job.error = "Interrompida demasiadas vezes (reinícios do serviço)."
            job.finished_at = utcnow()
    db.session.commit()
    return count


def purge_finished(older_than: timedelta = timedelta(days=30)) -> int:
    cutoff = utcnow() - older_than
    result = db.session.execute(
        delete(Job).where(Job.status.in_(("done", "failed", "cancelled")), Job.finished_at < cutoff)
    )
    db.session.commit()
    return result.rowcount or 0
