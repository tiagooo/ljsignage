"""Daily database backup with the SQLite backup API (SPEC §11), kept for 30 days."""

from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Settings
from .timeutil import utcnow

_NAME_RE = re.compile(r"^lj-signage-(\d{8}-\d{6})\.db$")
RETENTION = timedelta(days=30)


class BackupError(RuntimeError):
    pass


def run_backup(settings: Settings, now: datetime | None = None) -> Path:
    now = now or utcnow()
    folder = settings.backups_dir
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"lj-signage-{now.astimezone(UTC):%Y%m%d-%H%M%S}.db"
    tmp = final.with_suffix(".db.tmp")
    source = sqlite3.connect(settings.db_path, timeout=30)
    target = sqlite3.connect(tmp)
    try:
        with target:
            source.backup(target)
        check = target.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        target.close()
        source.close()
    if check != "ok":
        tmp.unlink(missing_ok=True)
        raise BackupError(f"integrity_check falhou: {check}")
    tmp.replace(final)
    prune(folder, now)
    return final


def backup_time(path: Path) -> datetime | None:
    match = _NAME_RE.match(path.name)
    if not match:
        return None
    return datetime.strptime(match[1], "%Y%m%d-%H%M%S").replace(tzinfo=UTC)


def list_backups(folder: Path) -> list[Path]:
    found = [p for p in folder.glob("lj-signage-*.db") if backup_time(p)]
    return sorted(found, key=backup_time, reverse=True)


def prune(folder: Path, now: datetime, retention: timedelta = RETENTION) -> list[Path]:
    removed = []
    for path in list_backups(folder):
        taken = backup_time(path)
        if taken and now - taken > retention:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def restore(backup: Path, settings: Settings) -> None:
    """Replace the database with a backup. Both services must be stopped first."""
    with sqlite3.connect(backup) as conn:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if check != "ok":
        raise BackupError(f"O backup está danificado: {check}")
    db_path = settings.db_path
    for suffix in ("-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    tmp = db_path.with_suffix(".db.restoring")
    shutil.copy2(backup, tmp)
    tmp.replace(db_path)
