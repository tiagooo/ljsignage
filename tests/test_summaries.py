"""Store summaries and the "entra no ar até HH:MM" estimate (SPEC §6)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from lj_signage.extensions import db
from lj_signage.models import Assignment, Device, DeviceFile, Video
from lj_signage.status import WorkerStatus
from lj_signage.summaries import first_cycle_after, on_air_estimate, summarize
from lj_signage.timeutil import fmt_time

LISBON = ZoneInfo("Europe/Lisbon")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_first_cycle_after():
    interval = timedelta(minutes=5)
    next_cycle = utc(2026, 9, 24, 10, 3)
    assert first_cycle_after(utc(2026, 9, 24, 10, 0), next_cycle, interval) == next_cycle
    assert first_cycle_after(utc(2026, 9, 24, 10, 4), next_cycle, interval) == utc(
        2026, 9, 24, 10, 8
    )
    assert first_cycle_after(utc(2026, 9, 24, 10, 8), next_cycle, interval) == utc(
        2026, 9, 24, 10, 8
    )


def store(code="ubbo") -> Device:
    return db.session.execute(db.select(Device).filter_by(code=code)).scalar_one()


def playing(device: Device, seconds: float) -> None:
    video = Video(
        title="A passar",
        slug="a-passar",
        original_filename="a.mov",
        sha256="b" * 64,
        size_bytes=10,
        duration_s=seconds,
        status="ready",
    )
    db.session.add(video)
    db.session.flush()
    device.snapshot_at = utc(2026, 10, 24, 12, 0)
    device.video_path = "/home/tmagalhaes/Videos"
    db.session.add(
        DeviceFile(
            device=device,
            location="root",
            remote_name=f"010_a-passar-{'b' * 6}.mp4",
            size_bytes=10,
            state="active",
            video=video,
        )
    )
    db.session.commit()


def test_estimate_is_next_cycle_plus_one_loop(app, synced, settings):
    device = store()
    device.enabled = True
    playing(device, 90)
    now = utc(2026, 9, 24, 10, 0)
    assignment = Assignment(video_id=1, target_type="all", start_at=now - timedelta(minutes=1))
    worker = WorkerStatus(alive=True, next_reconcile_at=utc(2026, 9, 24, 10, 3))
    live = replace(settings, dry_run=False)
    summaries = {device.id: summarize(device, now, live)}
    estimate = on_air_estimate(assignment, [device], summaries, worker, now, live)
    assert estimate.at == utc(2026, 9, 24, 10, 4, 30)  # 10:03 cycle + 90 s loop


def test_estimate_across_the_autumn_clock_change(app, synced, settings):
    """Computed in UTC; the wall-clock label may go "backwards" and still be right."""
    device = store()
    device.enabled = True
    playing(device, 300)
    now = utc(2026, 10, 25, 0, 50)  # 01:50 summer time
    assignment = Assignment(video_id=1, target_type="all", start_at=now)
    worker = WorkerStatus(alive=True, next_reconcile_at=utc(2026, 10, 25, 0, 55))
    live = replace(settings, dry_run=False)
    estimate = on_air_estimate(
        assignment, [device], {device.id: summarize(device, now, live)}, worker, now, live
    )
    assert estimate.at == utc(2026, 10, 25, 1, 0)  # 00:55 cycle + 5 min loop
    assert fmt_time(now, LISBON) == "01:50"
    assert fmt_time(estimate.at, LISBON) == "01:00"  # winter time, ten minutes later


def test_no_estimate_in_simulation_or_without_writable_stores(app, synced, settings):
    device = store()
    now = utc(2026, 9, 24, 10, 0)
    assignment = Assignment(video_id=1, target_type="all", start_at=now)
    worker = WorkerStatus(alive=True, next_reconcile_at=now)
    summaries = {device.id: summarize(device, now, settings)}
    assert on_air_estimate(assignment, [device], summaries, worker, now, settings).at is None
    live = replace(settings, dry_run=False)
    estimate = on_air_estimate(assignment, [device], summaries, worker, now, live)
    assert estimate.at is None and "escrita ativa" in estimate.note


def test_summary_counts_and_alerts(app, synced, settings):
    device = store()
    device.status = "online"
    device.video_path = "/home/tmagalhaes/Videos"
    device.snapshot_at = utc(2026, 9, 24, 10, 0)
    db.session.add_all(
        [
            DeviceFile(device=device, location="root", remote_name="Promo X.mp4", size_bytes=5,
                       state="legacy", issues=["unsafe_name", "non_standard_name"]),
            DeviceFile(device=device, location="root", remote_name="ok.mp4", size_bytes=5,
                       state="legacy", issues=["non_standard_name"]),
            DeviceFile(device=device, location="root", remote_name=".oculto", size_bytes=5,
                       state="other"),
        ]
    )  # fmt: skip
    db.session.commit()
    summary = summarize(device, utc(2026, 9, 24, 10, 5), settings)
    assert summary.playable_count == 1
    assert summary.loop_label == "—"  # only a legacy file of unknown length
    messages = " ".join(a.message for a in summary.alerts)
    assert "não consegue abrir" in messages
    assert "Sem vídeo de reserva" in messages
