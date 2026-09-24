"""Job queue, worker handlers and heartbeat."""

from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta

import pytest

from lj_signage import jobs
from lj_signage.extensions import db
from lj_signage.models import AuditLog, Device, DeviceFile, Job, Video
from lj_signage.reconciler.cycle import acquire_lock
from lj_signage.status import worker_status, write_heartbeat
from lj_signage.timeutil import utcnow
from lj_signage.worker import (
    Worker,
    handle_adopt,
    handle_discover,
    handle_reconcile,
    handle_transcode,
)


def test_enqueue_deduplicates_waiting_jobs(app):
    first = jobs.enqueue("reconcile", device_id=None, payload={"x": 1})
    again = jobs.enqueue("reconcile", device_id=None, payload={"x": 1})
    other = jobs.enqueue("reconcile", device_id=None, payload={"x": 2})
    db.session.commit()
    assert first.id == again.id and other.id != first.id


def test_claim_order_types_and_delays(app):
    a = jobs.enqueue("transcode", video_id=None, payload={"n": 1}, dedupe=False)
    b = jobs.enqueue("reconcile", payload={"n": 2}, dedupe=False)
    c = jobs.enqueue("reconcile", payload={"n": 3}, dedupe=False)
    c.not_before = utcnow() + timedelta(minutes=5)
    db.session.commit()
    assert jobs.claim(jobs.FTP_JOBS, "w1").id == b.id
    assert jobs.claim(jobs.FTP_JOBS, "w1") is None  # c is delayed
    claimed = jobs.claim(jobs.MEDIA_JOBS, "w1")
    assert claimed.id == a.id and claimed.status == "running" and claimed.attempts == 1


def test_finish_retry_and_postpone(app):
    job = jobs.enqueue("reconcile", payload={}, max_attempts=2)
    db.session.commit()
    jobs.claim(jobs.FTP_JOBS, "w1")
    jobs.finish(job, error="falhou", retry_in=timedelta(seconds=0))
    assert job.status == "pending"
    jobs.claim(jobs.FTP_JOBS, "w1")
    jobs.postpone(job, timedelta(seconds=0), "à espera")
    assert job.status == "pending" and job.attempts == 1
    jobs.claim(jobs.FTP_JOBS, "w1")
    jobs.finish(job, error="falhou outra vez", retry_in=timedelta(seconds=0))
    assert job.status == "failed" and job.finished_at is not None


def test_jobs_left_running_by_a_previous_worker_are_recovered(app):
    job = jobs.enqueue("reconcile", payload={})
    db.session.commit()
    jobs.claim(jobs.FTP_JOBS, "old-worker")
    assert jobs.recover_stale("new-worker") == 1
    assert job.status == "pending"


def test_heartbeat(app):
    assert worker_status().alive is False
    write_heartbeat(worker_id="w", started_at=utcnow(), next_reconcile_at=None, dry_run=True)
    status = worker_status()
    assert status.alive and status.dry_run is True
    assert worker_status(utcnow() + timedelta(minutes=5)).alive is False
    write_heartbeat(
        worker_id="w", started_at=utcnow(), next_reconcile_at=None, dry_run=True, stopped=True
    )
    assert worker_status().alive is False  # a clean shutdown is visible at once


def test_reconcile_job_waits_while_the_device_is_busy(app, synced):
    device = db.session.execute(db.select(Device).filter_by(code="ubbo")).scalar_one()
    job = jobs.enqueue("reconcile", device_id=device.id)
    db.session.commit()
    acquire_lock(device.id, "another-cycle")
    jobs.claim(jobs.FTP_JOBS, "w")
    handle_reconcile(Worker(app), job)
    assert job.status == "pending" and job.not_before is not None


def test_discover_job_records_the_result(app, ftp_server, settings):
    from lj_signage.config import parse_devices
    from lj_signage.inventory import sync_devices

    (ftp_server.home / "player.sh").write_text('VIDEOPATH="/home/tmagalhaes/Videos"\n')
    (ftp_server.home / "Videos").mkdir()
    (ftp_server.home / "Videos" / "a.mp4").write_bytes(b"1")
    sync_devices(
        parse_devices(
            {
                "devices": [
                    {
                        "store": "02",
                        "name": "Teste",
                        "code": "teste",
                        "hostname": "127.0.0.1",
                        "ftp_port": ftp_server.port,
                        "login_home": "/home/tmagalhaes",
                    }
                ]
            }
        )
    )
    device = db.session.execute(db.select(Device).filter_by(code="teste")).scalar_one()
    job = jobs.enqueue("discover", device_id=device.id, created_by="admin@lugardajoia.com")
    db.session.commit()
    jobs.claim(jobs.FTP_JOBS, "w")
    handle_discover(Worker(app), job)
    assert job.status == "done"
    assert device.discovery["status"] == "found"
    assert device.discovery["candidates"][0]["ftp_path"] == "/home/tmagalhaes/Videos"
    assert device.video_path is None  # never saved without an admin's confirmation

    # adopting a legacy file downloads it (read-only) and queues its conversion
    device.video_path = "/home/tmagalhaes/Videos"
    row = DeviceFile(
        device=device, location="root", remote_name="a.mp4", size_bytes=1, state="legacy"
    )
    db.session.add(row)
    db.session.flush()
    adopt = jobs.enqueue("adopt", payload={"file_id": row.id}, device_id=device.id)
    db.session.commit()
    jobs.claim(jobs.FTP_JOBS, "w")
    handle_adopt(Worker(app), adopt)
    assert adopt.status == "done", adopt.error
    video = db.session.execute(db.select(Video)).scalar_one()
    assert video.origin == "legacy" and row.adopted_video_id == video.id
    assert (settings.data_dir / video.original_path).read_bytes() == b"1"
    assert db.session.execute(db.select(Job).filter_by(type="transcode")).scalar_one()
    assert (ftp_server.home / "Videos" / "a.mp4").exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg não instalado")
def test_transcode_job_end_to_end(app, settings, tmp_path):
    source = settings.media_dir / "originals" / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", "testsrc2=size=640x360:rate=25:duration=1", "-pix_fmt", "yuv420p", str(source),
        ],
        check=True,
        timeout=60,
    )  # fmt: skip
    video = Video(
        title="Clip",
        slug="clip",
        original_filename="clip.mp4",
        original_path=str(source.relative_to(settings.data_dir)),
        created_by="editor@lugardajoia.com",
    )
    db.session.add(video)
    db.session.flush()
    job = jobs.enqueue("transcode", video_id=video.id)
    db.session.commit()
    jobs.claim(jobs.MEDIA_JOBS, "w")
    handle_transcode(Worker(app), job)
    assert job.status == "done" and video.status == "ready"
    assert video.width == 1920 and video.sha256
    assert db.session.execute(db.select(AuditLog).filter_by(action="video_ready")).scalar_one()


def test_transcode_failure_marks_the_video(app, settings):
    source = settings.media_dir / "originals" / "falso.mp4"
    source.write_bytes(b"nada")
    video = Video(
        title="Falso",
        slug="falso",
        original_filename="falso.mp4",
        original_path=str(source.relative_to(settings.data_dir)),
    )
    db.session.add(video)
    db.session.flush()
    job = jobs.enqueue("transcode", video_id=video.id)
    db.session.commit()
    jobs.claim(jobs.MEDIA_JOBS, "w")
    handle_transcode(Worker(app), job)
    assert video.status == "failed" and "vídeo" in video.error
    assert job.status == "failed"


def test_worker_ids_are_unique_per_start(app):
    first, second = Worker(app), Worker(app)
    assert first.id != second.id  # same hostname and PID (as in Docker), different run
    job = jobs.enqueue("reconcile", payload={})
    db.session.commit()
    jobs.claim(jobs.FTP_JOBS, first.id)
    assert jobs.recover_stale(second.id) == 1  # the restarted worker picks it up
    assert job.status == "pending"


def test_a_store_still_busy_is_skipped_by_the_next_round(app, synced):
    worker = Worker(app)
    submitted = []
    worker.ftp_pool.submit = lambda fn, device_id: submitted.append(device_id)
    busy = db.session.execute(db.select(Device).filter_by(code="ubbo")).scalar_one()
    worker._cycling.add(busy.id)
    worker.heartbeat = lambda: None
    worker.reconcile_all()
    assert busy.id not in submitted and len(submitted) == 2
    worker.reconcile_all()  # nothing new: the other two are still queued
    assert len(submitted) == 2
