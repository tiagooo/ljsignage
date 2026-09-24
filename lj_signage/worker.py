"""Entrypoint of the worker service: ``python -m lj_signage.worker``.

Everything that talks to the Pis or takes long runs here, never in the web
processes (with several gunicorn workers each would schedule its own cycles):

- reconciliation of every device every RECONCILE_INTERVAL_MIN;
- the job queue: "Aplicar agora", discovery and legacy adoption (FTP lane, at
  most MAX_PARALLEL Pis at once) and video conversions (media lane, one at a
  time so the docker-server is not overloaded);
- the daily database backup and housekeeping;
- a heartbeat in the database, shown in the panel.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask
from sqlalchemy import inspect

from . import audit, create_app, jobs
from .backup import prune, run_backup
from .config import ConfigError, load_devices_file
from .discovery import DiscoveryResult, discover
from .extensions import db
from .ftp import FtpError
from .inventory import sync_devices
from .media.transcode import MediaError, process_video
from .models import Device, DeviceFile, Job, Video
from .naming import slugify
from .reconciler.cycle import acquire_lock, client_for, default_owner, release_lock, run_cycle
from .status import write_heartbeat
from .timeutil import fmt_duration, utcnow

log = logging.getLogger("lj_signage.worker")

BUSY_RETRY = timedelta(seconds=20)


class Worker:
    def __init__(self, app: Flask, *, poll_interval: float = 2.0) -> None:
        self.app = app
        self.settings = app.config["LJ_SETTINGS"]
        # Unique per start: in Docker the hostname and PID (1) repeat after a restart,
        # and jobs left "running" by the previous run must be recognised as stale.
        self.id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.poll_interval = poll_interval
        self.stopping = threading.Event()
        self.started_at = utcnow()
        self.ftp_pool = ThreadPoolExecutor(self.settings.max_parallel, thread_name_prefix="ftp")
        self.media_pool = ThreadPoolExecutor(1, thread_name_prefix="media")
        self._ftp_jobs = 0
        self._media_busy = False
        self._cycling: set[int] = set()  # devices with a scheduled cycle queued or running
        self._lock = threading.Lock()
        self.scheduler = BackgroundScheduler(timezone=UTC)

    # --- lifecycle ------------------------------------------------------------------
    def start(self) -> None:
        with self.app.app_context():
            wait_for_schema()
            self.sync_config()
            recovered = jobs.recover_stale(self.id)
            if recovered:
                log.info("recovered %d interrupted job(s)", recovered)
        settings = self.settings
        self.scheduler.add_job(
            self.reconcile_all,
            "interval",
            minutes=settings.reconcile_interval_min,
            id="reconcile",
            next_run_time=utcnow() + timedelta(seconds=10),
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.backup,
            CronTrigger(hour=3, minute=30, timezone=settings.tz),
            id="backup",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=6 * 3600,
        )
        self.scheduler.add_job(
            self.housekeeping,
            CronTrigger(hour=4, minute=15, timezone=settings.tz),
            id="housekeeping",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.heartbeat, "interval", seconds=30, id="heartbeat", next_run_time=utcnow()
        )
        self.scheduler.start()
        log.info(
            "worker %s started (DRY_RUN=%s, interval=%s min, MAX_PARALLEL=%s)",
            self.id,
            settings.dry_run,
            settings.reconcile_interval_min,
            settings.max_parallel,
        )

    def run_forever(self) -> None:
        self.start()
        while not self.stopping.is_set():
            try:
                self.dispatch()
            except Exception:
                log.exception("dispatch failed")
            self.stopping.wait(self.poll_interval)
        self.shutdown()

    def stop(self, *_args) -> None:
        self.stopping.set()

    def shutdown(self) -> None:
        log.info("worker stopping")
        self.scheduler.shutdown(wait=False)
        self.ftp_pool.shutdown(wait=True, cancel_futures=True)
        self.media_pool.shutdown(wait=True, cancel_futures=True)
        with self.app.app_context():
            write_heartbeat(
                worker_id=self.id,
                started_at=self.started_at,
                next_reconcile_at=None,
                dry_run=self.settings.dry_run,
                stopped=True,
            )
        log.info("worker stopped")

    # --- job queue --------------------------------------------------------------------
    def dispatch(self) -> None:
        with self.app.app_context():
            with self._lock:
                media_free = not self._media_busy
            if media_free:
                job = jobs.claim(jobs.MEDIA_JOBS, self.id)
                if job is not None:
                    with self._lock:
                        self._media_busy = True
                    self.media_pool.submit(self._run_job, job.id, True)
            while True:
                with self._lock:
                    if self._ftp_jobs >= self.settings.max_parallel:
                        break
                job = jobs.claim(jobs.FTP_JOBS, self.id)
                if job is None:
                    break
                with self._lock:
                    self._ftp_jobs += 1
                self.ftp_pool.submit(self._run_job, job.id, False)

    def _run_job(self, job_id: int, media: bool) -> None:
        try:
            with self.app.app_context():
                job = db.session.get(Job, job_id)
                handler = HANDLERS.get(job.type)
                if handler is None:
                    jobs.finish(job, error=f"Tipo de tarefa desconhecido: {job.type}")
                    return
                try:
                    handler(self, job)
                except Exception as exc:
                    log.exception("job %s (%s) crashed", job_id, job.type)
                    db.session.rollback()
                    job = db.session.get(Job, job_id)
                    jobs.finish(job, error=f"Erro interno ({exc.__class__.__name__}).")
        finally:
            with self._lock:
                if media:
                    self._media_busy = False
                else:
                    self._ftp_jobs -= 1

    # --- scheduled work ---------------------------------------------------------------------
    def reconcile_all(self) -> None:
        with self.app.app_context():
            ids = (
                db.session.execute(
                    db.select(Device.id)
                    .where(Device.in_config.is_(True))
                    .order_by(Device.store_number)
                )
                .scalars()
                .all()
            )
        # Do not wait for the whole round: a slow Pi (a long upload) must not hold
        # back the other stores. A store still busy from a previous round is skipped.
        for device_id in ids:
            with self._lock:
                if device_id in self._cycling:
                    continue
                self._cycling.add(device_id)
            self.ftp_pool.submit(self._scheduled_cycle, device_id)
        self.heartbeat()

    def _scheduled_cycle(self, device_id: int) -> None:
        with self.app.app_context():
            try:
                result = run_cycle(device_id, self.settings, execute=True)
                log.info("cycle %s: %s %s", result.device_code, result.status, result.message)
            except Exception:
                log.exception("scheduled cycle failed for device %s", device_id)
            finally:
                with self._lock:
                    self._cycling.discard(device_id)

    def heartbeat(self) -> None:
        job = self.scheduler.get_job("reconcile")
        with self.app.app_context():
            write_heartbeat(
                worker_id=self.id,
                started_at=self.started_at,
                next_reconcile_at=job.next_run_time if job else None,
                dry_run=self.settings.dry_run,
            )

    def backup(self) -> None:
        with self.app.app_context():
            try:
                path = run_backup(self.settings)
            except Exception as exc:
                log.exception("backup failed")
                audit.record("backup", f"Falhou: {exc}", result="error", actor=audit.SYSTEM)
            else:
                audit.record("backup", f"Backup criado: {path.name}", actor=audit.SYSTEM)
            db.session.commit()

    def housekeeping(self) -> None:
        with self.app.app_context():
            jobs.purge_finished()
            prune(self.settings.backups_dir, utcnow())
            cutoff = time.time() - 24 * 3600
            for path in self.settings.tmp_dir.glob("*"):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)

    def sync_config(self) -> None:
        try:
            config = load_devices_file(self.settings.devices_file)
        except ConfigError as exc:
            log.error("devices file not loaded: %s", exc)
            return
        report = sync_devices(config)
        db.session.commit()
        log.info("devices.yaml: %s", report.summary())


# --- job handlers -------------------------------------------------------------------------


def handle_transcode(worker: Worker, job: Job) -> None:
    video = db.session.get(Video, job.video_id) if job.video_id else None
    actor = audit.actor_for_email(job.created_by)
    if video is None or video.deleted_at is not None:
        jobs.finish(job, error="O vídeo foi apagado.")
        return
    video.status = "processing"
    video.error = None
    jobs.set_progress(job, 0, "A converter…")
    last = [0.0]

    def progress(percent: float) -> None:
        if percent - last[0] >= 1:
            last[0] = percent
            jobs.set_progress(job, percent, "A converter…")

    try:
        process_video(video, worker.settings, progress)
    except MediaError as exc:
        video.status = "failed"
        video.error = str(exc)
        audit.record("video_failed", f"«{video.title}»: {exc}", actor=actor, result="error")
        jobs.finish(job, error=str(exc))
        return
    video.status = "ready"
    audit.record(
        "video_ready",
        f"«{video.title}» pronto ({fmt_duration(video.duration_s)}, 1920×1080).",
        actor=actor,
    )
    jobs.finish(job)


def handle_reconcile(worker: Worker, job: Job) -> None:
    actor = audit.actor_for_email(job.created_by)
    jobs.set_progress(job, None, "A ligar à loja…")
    result = run_cycle(job.device_id, worker.settings, execute=True, actor=actor)
    if result.status == "busy":
        jobs.postpone(job, BUSY_RETRY, "À espera do fim da sincronização em curso…")
        return
    message = result.message
    if result.plan is not None and result.plan.actions and not result.wrote:
        message = f"{message} {result.read_only_reason or ''}".strip()
    job.message = message[:300]
    if result.status == "online":
        jobs.finish(job)
    else:
        jobs.finish(job, error=message)


def handle_discover(worker: Worker, job: Job) -> None:
    device = db.session.get(Device, job.device_id)
    actor = audit.actor_for_email(job.created_by)
    owner = default_owner()
    if not acquire_lock(device.id, owner):
        jobs.postpone(job, BUSY_RETRY, "À espera do fim da sincronização em curso…")
        return
    try:
        jobs.set_progress(job, None, "A procurar a pasta de vídeos…")
        now = utcnow()
        try:
            with client_for(device, worker.settings, read_only=True) as client:
                result = discover(
                    client,
                    login_home=device.login_home,
                    now=now,
                    config_video_path=device.config_video_path,
                )
        except FtpError as exc:
            result = DiscoveryResult(
                status="error", candidates=[], at=now.isoformat(), error=str(exc)
            )
        device.discovery = result.to_dict()
        device.discovery_at = now
        audit.record(
            "discovery",
            result.summary,
            actor=actor,
            device=device,
            result="error" if result.status == "error" else "info",
            data={"candidates": [c.ftp_path for c in result.candidates], "notes": result.notes},
        )
        job.message = result.summary[:300]
        if result.status == "error":
            jobs.finish(job, error=result.summary)
        else:
            jobs.finish(job)
    finally:
        release_lock(device.id, owner)


def handle_adopt(worker: Worker, job: Job) -> None:
    """Download a legacy file (read-only for the Pi) and queue its conversion."""
    settings = worker.settings
    row = db.session.get(DeviceFile, job.payload.get("file_id"))
    actor = audit.actor_for_email(job.created_by)
    if row is None or row.state != "legacy" or row.is_dir:
        jobs.finish(job, error="O ficheiro já não existe no Pi.")
        return
    device = row.device
    owner = default_owner()
    if not acquire_lock(device.id, owner):
        jobs.postpone(job, BUSY_RETRY, "À espera do fim da sincronização em curso…")
        return
    display = row.display_name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", display)[-120:] or "video"
    target = settings.media_dir / "originals" / f"legacy-{device.code}-{row.id}-{safe}"
    try:
        expected = row.size_bytes or 0

        def progress(received: int) -> None:
            if expected and received % (8 * 1024 * 1024) < 65536:
                jobs.set_progress(job, 100 * received / expected, "A descarregar do Pi…")

        try:
            with (
                client_for(device, settings, read_only=True) as client,
                target.open("wb") as fh,
            ):
                received = client.retrieve(
                    client.join(device.video_path, row.remote_name), fh, callback=progress
                )
        except (FtpError, OSError) as exc:
            target.unlink(missing_ok=True)
            jobs.finish(job, error=f"Não foi possível descarregar o ficheiro: {exc}")
            return
        if expected and received != expected:
            target.unlink(missing_ok=True)
            jobs.finish(job, error="O ficheiro descarregado está incompleto.")
            return
        title = Path(display).stem.strip() or "Vídeo legado"
        video = Video(
            title=title[:200],
            slug=slugify(title),
            original_filename=display[:255],
            original_path=str(target.relative_to(settings.data_dir)),
            status="processing",
            origin="legacy",
            origin_detail=f"Loja {device.store_number} · {display}"[:500],
            created_by=job.created_by,
        )
        db.session.add(video)
        db.session.flush()
        row.adopted_video_id = video.id
        jobs.enqueue("transcode", video_id=video.id, created_by=job.created_by)
        audit.record(
            "legacy_adopted",
            f"{display} descarregado para a biblioteca como «{video.title}».",
            actor=actor,
            device=device,
        )
        jobs.finish(job)
    finally:
        release_lock(device.id, owner)


HANDLERS = {
    "transcode": handle_transcode,
    "reconcile": handle_reconcile,
    "discover": handle_discover,
    "adopt": handle_adopt,
}


def wait_for_schema(timeout: float = 300.0) -> None:
    """The web service runs the migrations; wait until the tables exist."""
    deadline = time.monotonic() + timeout
    while True:
        tables = inspect(db.engine).get_table_names()
        if {"alembic_version", "devices", "jobs"} <= set(tables):
            return
        if time.monotonic() > deadline:
            raise RuntimeError("database schema not found: run 'flask db upgrade'")
        log.info("waiting for the database schema…")
        time.sleep(3)
        db.session.remove()


def main() -> None:
    app = create_app()
    worker = Worker(app)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
