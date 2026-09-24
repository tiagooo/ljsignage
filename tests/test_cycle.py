"""Full reconciliation cycles against the simulated FTP server (never a real Pi)."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import timedelta

import pytest

from lj_signage.config import parse_devices
from lj_signage.extensions import db
from lj_signage.inventory import sync_devices
from lj_signage.models import Assignment, AuditLog, Device, DeviceFile, Video
from lj_signage.naming import STAGING_DIR
from lj_signage.reconciler import cycle as cycle_module
from lj_signage.reconciler.cycle import acquire_lock, run_cycle
from lj_signage.timeutil import utcnow

VIDEOS = "/home/tmagalhaes/Videos"


def tree(root) -> dict:
    """Snapshot of the simulated Pi: path -> (size, mtime)."""
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def names(folder) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


@pytest.fixture
def pi(app, ftp_server, settings):
    """A device pointing at the simulated server, with its video folder confirmed."""
    videos = ftp_server.home / "Videos"
    videos.mkdir()
    (videos / "Promo Antiga.mp4").write_bytes(b"legacy" * 100)
    (videos / "clip.mp4").write_bytes(b"clip" * 100)
    config = parse_devices(
        {
            "defaults": {"login_home": "/home/tmagalhaes", "enabled": False},
            "devices": [
                {
                    "store": "02",
                    "name": "Loja de testes",
                    "code": "teste",
                    "hostname": "127.0.0.1",
                    "ftp_port": ftp_server.port,
                }
            ],
        }
    )
    sync_devices(config)
    device = db.session.execute(db.select(Device).filter_by(code="teste")).scalar_one()
    device.video_path = VIDEOS
    device.video_path_source = "discovery"
    db.session.commit()
    return device


def add_video(settings, title: str, size: int = 4096, duration: float = 20.0) -> Video:
    content = os.urandom(size)
    sha = hashlib.sha256(content).hexdigest()
    video = Video(
        title=title,
        slug=title.lower().replace(" ", "-"),
        original_filename=f"{title}.mov",
        status="ready",
        sha256=sha,
        size_bytes=size,
        duration_s=duration,
    )
    db.session.add(video)
    db.session.flush()
    path = settings.data_dir / "media" / "videos" / f"{video.id}-{sha[:12]}.mp4"
    path.write_bytes(content)
    video.media_path = str(path.relative_to(settings.data_dir))
    db.session.commit()
    return video


def schedule(video, *, fallback=False, start=None, end=None, position=100):
    assignment = Assignment(
        video=video,
        target_type="all",
        start_at=start or utcnow() - timedelta(hours=1),
        end_at=end,
        position=position,
        is_fallback=fallback,
    )
    db.session.add(assignment)
    db.session.commit()
    return assignment


def writable(settings):
    return replace(settings, dry_run=False)


def actions_logged(*kinds):
    rows = db.session.execute(db.select(AuditLog).where(AuditLog.action.in_(kinds))).scalars()
    return [r.action for r in rows]


def test_dry_run_reads_but_never_writes(pi, settings, ftp_server):
    add_video(settings, "Natal")
    schedule(db.session.execute(db.select(Video)).scalar_one())
    before = tree(ftp_server.root)
    result = run_cycle(pi.id, settings, execute=True)
    assert tree(ftp_server.root) == before
    assert result.status == "online" and not result.wrote
    assert "DRY_RUN" in result.read_only_reason
    assert [a.kind.value for a in result.plan.actions] == ["mkdir_staging", "upload", "activate"]
    assert pi.status == "online" and pi.snapshot_at is not None
    legacy = sorted(f.remote_name for f in pi.files if f.state == "legacy")
    assert legacy == ["Promo Antiga.mp4", "clip.mp4"]
    assert actions_logged("legacy_registered", "plan") == ["legacy_registered", "plan"]
    # the same plan again is not logged twice
    run_cycle(pi.id, settings, execute=True)
    assert actions_logged("plan") == ["plan"]


def test_disabled_device_is_never_written(pi, settings, ftp_server):
    schedule(add_video(settings, "Natal"))
    before = tree(ftp_server.root)
    result = run_cycle(pi.id, writable(settings), execute=True)
    assert tree(ftp_server.root) == before
    assert "enabled: false" in result.read_only_reason


def test_enabled_device_is_reconciled_and_idempotent(pi, settings, ftp_server):
    pi.enabled = True
    db.session.commit()
    natal = add_video(settings, "Natal", duration=15)
    filigrana = add_video(settings, "Filigrana", duration=25)
    reserva = add_video(settings, "Reserva", duration=10)
    schedule(natal, position=10)
    schedule(filigrana, position=20)
    schedule(reserva, fallback=True)
    videos = ftp_server.home / "Videos"

    result = run_cycle(pi.id, writable(settings), execute=True)
    assert result.status == "online" and result.wrote
    assert all(o.ok for o in result.outcomes)
    assert names(videos) == sorted(
        [
            ".lj-staging",
            "Promo Antiga.mp4",
            "clip.mp4",
            f"010_natal-{natal.sha256[:6]}.mp4",
            f"020_filigrana-{filigrana.sha256[:6]}.mp4",
        ]
    )
    assert names(videos / STAGING_DIR) == [f"{reserva.sha256[:12]}.mp4"]
    assert (videos / f"010_natal-{natal.sha256[:6]}.mp4").read_bytes() == (
        settings.data_dir / natal.media_path
    ).read_bytes()
    assert (videos / "Promo Antiga.mp4").read_bytes() == b"legacy" * 100  # untouched
    assert pi.last_reconcile_at is not None
    logged = actions_logged("mkdir_staging", "upload", "activate")
    assert logged.count("upload") == 3 and logged.count("activate") == 2
    staged = [f for f in pi.files if f.location == "staging"]
    assert staged[0].video_id == reserva.id

    # Repeating the cycle changes nothing on the Pi.
    before = tree(ftp_server.root)
    again = run_cycle(pi.id, writable(settings), execute=True)
    assert again.plan.actions == () and tree(ftp_server.root) == before


def test_end_of_campaign_switches_to_the_fallback(pi, settings, ftp_server):
    pi.enabled = True
    db.session.commit()
    natal = add_video(settings, "Natal")
    reserva = add_video(settings, "Reserva")
    campaign = schedule(natal)
    schedule(reserva, fallback=True)
    run_cycle(pi.id, writable(settings), execute=True)

    campaign.end_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    result = run_cycle(pi.id, writable(settings), execute=True)
    kinds = [o.action.kind.value for o in result.outcomes]
    assert kinds == ["activate", "delete_active"]
    videos = ftp_server.home / "Videos"
    assert f"010_reserva-{reserva.sha256[:6]}.mp4" in names(videos)
    assert f"010_natal-{natal.sha256[:6]}.mp4" not in names(videos)


def test_requested_legacy_deletion(pi, settings, ftp_server):
    pi.enabled = True
    db.session.commit()
    schedule(add_video(settings, "Natal"))
    run_cycle(pi.id, writable(settings), execute=True)
    row = next(f for f in pi.files if f.remote_name == "clip.mp4")
    row.delete_requested_by = "admin@lugardajoia.com"
    row.delete_requested_at = utcnow()
    db.session.commit()
    run_cycle(pi.id, writable(settings), execute=True)
    videos = ftp_server.home / "Videos"
    assert "clip.mp4" not in names(videos) and "Promo Antiga.mp4" in names(videos)
    entry = db.session.execute(db.select(AuditLog).filter_by(action="delete_legacy")).scalar_one()
    assert "admin@lugardajoia.com" in entry.detail
    assert (
        db.session.execute(db.select(DeviceFile).filter_by(remote_name="clip.mp4")).scalar() is None
    )


def test_offline_device(pi, settings, monkeypatch):
    pi.ftp_port = 1  # nothing listens there
    db.session.commit()
    result = run_cycle(pi.id, settings, execute=True)
    assert result.status == "offline"
    assert pi.status == "offline" and pi.consecutive_failures == 1
    run_cycle(pi.id, settings, execute=True)
    assert pi.consecutive_failures == 2
    assert actions_logged("device_status") == ["device_status"]


def test_failed_size_check_stops_and_cleans_up(pi, settings, ftp_server, monkeypatch):
    pi.enabled = True
    db.session.commit()
    natal = add_video(settings, "Natal")
    schedule(natal)
    real_size = cycle_module.FtpClient.size

    def lying_size(self, path):
        if path.endswith(".part"):
            return 1
        return real_size(self, path)

    monkeypatch.setattr(cycle_module.FtpClient, "size", lying_size)
    result = run_cycle(pi.id, writable(settings), execute=True)
    assert result.status == "error"
    assert "Verificação do envio falhou" in pi.last_error
    staging = ftp_server.home / "Videos" / STAGING_DIR
    assert names(staging) == []  # the .part was removed
    assert f"010_natal-{natal.sha256[:6]}.mp4" not in names(ftp_server.home / "Videos")


def test_corrupted_local_media_is_not_sent(pi, settings, ftp_server):
    pi.enabled = True
    db.session.commit()
    natal = add_video(settings, "Natal")
    (settings.data_dir / natal.media_path).write_bytes(os.urandom(natal.size_bytes))
    schedule(natal)
    result = run_cycle(pi.id, writable(settings), execute=True)
    assert result.status == "error" and "corrompido" in pi.last_error
    assert names(ftp_server.home / "Videos" / STAGING_DIR) == []


def test_busy_device_is_skipped(pi, settings):
    assert acquire_lock(pi.id, "someone-else")
    assert run_cycle(pi.id, settings, execute=True).status == "busy"


def test_folder_from_devices_yaml_is_validated_on_the_first_cycle(app, ftp_server, settings):
    (ftp_server.home / "Videos").mkdir()
    config = parse_devices(
        {
            "devices": [
                {
                    "store": "03",
                    "name": "Loja YAML",
                    "code": "yaml",
                    "hostname": "127.0.0.1",
                    "ftp_port": ftp_server.port,
                    "login_home": "/home/tmagalhaes",
                    "video_path": "/home/tmagalhaes/Videos",
                }
            ]
        }
    )
    sync_devices(config)
    db.session.commit()
    device = db.session.execute(db.select(Device).filter_by(code="yaml")).scalar_one()
    assert device.video_path is None and device.config_video_path == "/home/tmagalhaes/Videos"
    result = run_cycle(device.id, settings, execute=True)
    assert result.status == "online"
    assert device.video_path == "/home/tmagalhaes/Videos"
    assert device.video_path_confirmed_by == "devices.yaml"


def test_unconfirmed_folder_only_checks_connectivity(app, ftp_server, settings):
    config = parse_devices(
        {
            "devices": [
                {
                    "store": "03",
                    "name": "Nova",
                    "code": "nova",
                    "hostname": "127.0.0.1",
                    "ftp_port": ftp_server.port,
                }
            ]
        }
    )
    sync_devices(config)
    db.session.commit()
    device = db.session.execute(db.select(Device).filter_by(code="nova")).scalar_one()
    result = run_cycle(device.id, settings, execute=True)
    assert result.status == "online" and result.plan is None
    assert "por confirmar" in result.message
    assert "pyftpdlib" in device.ftp_banner


def test_no_database_write_lock_is_held_during_ftp_reads(pi, settings, monkeypatch):
    """Other writers (the panel, other stores) must never wait on a slow Pi."""
    import sqlite3

    real_read = cycle_module.read_remote_state
    checks = []

    def read_and_probe(client, video_path):
        probe = sqlite3.connect(settings.db_path, timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE")  # fails at once if a writer holds the lock
            probe.rollback()
            checks.append(True)
        finally:
            probe.close()
        return real_read(client, video_path)

    monkeypatch.setattr(cycle_module, "read_remote_state", read_and_probe)
    pi.enabled = True
    pi.last_error = "pendente"  # a dirty attribute before the cycle starts
    db.session.commit()
    schedule(add_video(settings, "Natal"))
    result = run_cycle(pi.id, writable(settings), execute=True)
    assert result.status == "online" and checks == [True, True]


# --- regressions from the independent review ----------------------------------------------


def _yaml_device(port, video_path, *, enabled=True, code="teste"):
    return parse_devices(
        {
            "defaults": {"login_home": "/home/tmagalhaes", "enabled": enabled},
            "devices": [
                {
                    "store": "02",
                    "name": "Loja de testes",
                    "code": code,
                    "hostname": "127.0.0.1",
                    "ftp_port": port,
                    "video_path": video_path,
                }
            ],
        }
    )


def _request_delete(device, name):
    row = next(f for f in device.files if f.location == "root" and f.remote_name == name)
    row.delete_requested_by = "admin@lugardajoia.com"
    row.delete_requested_at = utcnow()
    db.session.commit()
    return row


def test_delete_request_does_not_follow_the_name_into_another_folder(app, ftp_server, settings):
    old, new = ftp_server.home / "Videos", ftp_server.home / "Videos2"
    old.mkdir()
    new.mkdir()
    (old / "promo.mp4").write_bytes(b"o" * 600)
    (new / "promo.mp4").write_bytes(b"N" * 900)
    schedule(add_video(settings, "Reserva"), fallback=True)
    sync_devices(_yaml_device(ftp_server.port, "/home/tmagalhaes/Videos"))
    db.session.commit()
    device = db.session.execute(db.select(Device).filter_by(code="teste")).scalar_one()
    run_cycle(device.id, settings, execute=True)  # simulation: read only
    _request_delete(device, "promo.mp4")

    sync_devices(_yaml_device(ftp_server.port, "/home/tmagalhaes/Videos2"))  # another folder
    db.session.commit()
    assert device.snapshot_at is None and device.files == []
    run_cycle(device.id, writable(settings), execute=True)
    assert (old / "promo.mp4").exists() and (new / "promo.mp4").exists()


def test_delete_request_is_dropped_when_the_file_changes(app, ftp_server, settings):
    folder = ftp_server.home / "Videos"
    folder.mkdir()
    (folder / "promo.mp4").write_bytes(b"o" * 600)
    schedule(add_video(settings, "Reserva"), fallback=True)
    sync_devices(_yaml_device(ftp_server.port, "/home/tmagalhaes/Videos"))
    db.session.commit()
    device = db.session.execute(db.select(Device).filter_by(code="teste")).scalar_one()
    run_cycle(device.id, settings, execute=True)
    row = _request_delete(device, "promo.mp4")

    (folder / "promo.mp4").write_bytes(b"N" * 900)  # replaced on the Pi by someone else
    run_cycle(device.id, writable(settings), execute=True)
    assert (folder / "promo.mp4").read_bytes() == b"N" * 900
    assert row.delete_requested_at is None
    assert "mudou no Pi" in " ".join(
        e.detail
        for e in db.session.execute(
            db.select(AuditLog).filter_by(action="legacy_delete_cancelled")
        ).scalars()
    )


def _raw(settings, sql, *params):
    import sqlite3

    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(sql, params)


def test_request_cancelled_during_the_cycle_is_not_executed(pi, settings, ftp_server, monkeypatch):
    pi.enabled = True
    db.session.commit()
    schedule(add_video(settings, "Natal"))
    run_cycle(pi.id, settings, execute=True)  # simulation: registers the legacy files
    row = _request_delete(pi, "clip.mp4")
    real_store = cycle_module.FtpClient.store

    def store_then_cancel(self, path, fp, **kwargs):
        sent = real_store(self, path, fp, **kwargs)
        # the admin clicks "Cancelar remoção" while the upload is running
        _raw(settings, "UPDATE device_files SET delete_requested_at = NULL WHERE id = ?", row.id)
        return sent

    monkeypatch.setattr(cycle_module.FtpClient, "store", store_then_cancel)
    result = run_cycle(pi.id, writable(settings), execute=True)
    assert (ftp_server.home / "Videos" / "clip.mp4").exists()
    assert result.status == "error" and "cancelado" in result.message


def test_file_replaced_during_the_cycle_is_not_deleted(pi, settings, ftp_server, monkeypatch):
    pi.enabled = True
    db.session.commit()
    schedule(add_video(settings, "Natal"))
    run_cycle(pi.id, settings, execute=True)
    _request_delete(pi, "clip.mp4")
    clip = ftp_server.home / "Videos" / "clip.mp4"
    real_store = cycle_module.FtpClient.store

    def store_then_replace(self, path, fp, **kwargs):
        sent = real_store(self, path, fp, **kwargs)
        clip.write_bytes(b"outro ficheiro")
        return sent

    monkeypatch.setattr(cycle_module.FtpClient, "store", store_then_replace)
    result = run_cycle(pi.id, writable(settings), execute=True)
    assert clip.read_bytes() == b"outro ficheiro"
    assert "mudou no Pi" in result.message


def test_device_removed_from_devices_yaml_is_never_written(app, ftp_server, settings):
    folder = ftp_server.home / "Videos"
    folder.mkdir()
    sync_devices(_yaml_device(ftp_server.port, "/home/tmagalhaes/Videos", code="al"))
    db.session.commit()
    schedule(add_video(settings, "Campanha"))
    other = parse_devices(
        {"devices": [{"store": "03", "name": "Outra", "code": "ub", "hostname": "raspub"}]}
    )
    sync_devices(other)  # the pilot is taken out of the file
    db.session.commit()
    device = db.session.execute(db.select(Device).filter_by(code="al")).scalar_one()
    assert not device.in_config and not device.enabled
    device.enabled = True  # even if someone flips it by hand
    device.video_path = "/home/tmagalhaes/Videos"
    db.session.commit()
    result = run_cycle(device.id, writable(settings), execute=True)
    assert not result.wrote and "já não está no devices.yaml" in result.read_only_reason
    assert list(folder.iterdir()) == []


def test_lease_lost_during_an_upload_aborts_the_transfer(pi, settings, ftp_server, monkeypatch):
    pi.enabled = True
    db.session.commit()
    natal = add_video(settings, "Natal", size=600_000)
    schedule(natal)
    monkeypatch.setattr(cycle_module, "LEASE_RENEW_S", 0.0)
    calls = {"n": 0}
    real_renew = cycle_module.renew_lock

    def renew_then_steal(device_id, owner):
        calls["n"] += 1
        if calls["n"] == 4:  # another process takes the device mid-transfer
            _raw(settings, "UPDATE devices SET lock_owner = 'outro' WHERE id = ?", device_id)
        return real_renew(device_id, owner)

    monkeypatch.setattr(cycle_module, "renew_lock", renew_then_steal)
    result = run_cycle(pi.id, writable(settings), execute=True)
    assert result.status == "error" and "outro processo" in result.message
    videos = ftp_server.home / "Videos"
    assert not any(n.startswith("0") for n in names(videos))  # nothing activated
    db.session.refresh(pi)
    assert pi.lock_owner == "outro"  # the new owner's lease was not released by us


def test_renew_lock_reports_a_lost_lease(pi):
    assert acquire_lock(pi.id, "A")
    assert cycle_module.renew_lock(pi.id, "A") is True
    db.session.execute(db.text("UPDATE devices SET lock_owner = 'B' WHERE id = :i"), {"i": pi.id})
    db.session.commit()
    assert cycle_module.renew_lock(pi.id, "A") is False
