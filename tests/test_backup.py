"""Daily backup with the SQLite backup API, 30-day retention, and a tested restore."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from lj_signage.backup import list_backups, prune, restore, run_backup
from lj_signage.extensions import db
from lj_signage.models import Video


def count_videos(path) -> int:
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT count(*) FROM videos").fetchone()[0]


def test_backup_and_restore_round_trip(app, settings):
    db.session.add(Video(title="Antes", slug="antes", original_filename="a.mov"))
    db.session.commit()
    backup = run_backup(settings)
    assert backup.exists() and count_videos(backup) == 1

    db.session.add(Video(title="Depois", slug="depois", original_filename="b.mov"))
    db.session.commit()
    assert count_videos(settings.db_path) == 2

    db.session.remove()
    db.engine.dispose()
    restore(backup, settings)
    assert count_videos(settings.db_path) == 1


def test_retention_keeps_30_days(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    now = datetime(2026, 9, 24, 3, 30, tzinfo=UTC)
    for days in (0, 10, 29, 31, 60):
        taken = now - timedelta(days=days)
        (folder / f"lj-signage-{taken:%Y%m%d-%H%M%S}.db").write_bytes(b"x")
    (folder / "outro-ficheiro.db").write_bytes(b"x")
    removed = prune(folder, now)
    assert len(removed) == 2
    assert len(list_backups(folder)) == 3
    assert (folder / "outro-ficheiro.db").exists()
