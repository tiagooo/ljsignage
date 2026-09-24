"""Bridges the database and the pure planner.

- ``planner_input`` gathers what the planner needs from the database.
- ``store_snapshot`` records what was read from a Pi (DeviceFile rows), so the
  web process can show state and preview plans without talking FTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, or_

from ..config import Settings
from ..extensions import db
from ..ftp import ListEntry
from ..models import Assignment, Device, DeviceFile, Video
from . import planner as pl


def planner_video(video: Video) -> pl.Video:
    return pl.Video(
        id=video.id,
        slug=video.slug,
        sha256=video.sha256 or "",
        size_bytes=video.size_bytes or 0,
        title=video.title,
        duration_s=video.duration_s,
    )


def library() -> tuple[pl.Video, ...]:
    """Every video the app ever produced (deleted ones included), to recognise its files."""
    rows = db.session.execute(
        db.select(Video).where(Video.sha256.is_not(None), Video.size_bytes.is_not(None))
    ).scalars()
    return tuple(planner_video(v) for v in rows)


def applicable(device: Device):
    """SQL condition: assignments that target this device (all + its groups + itself)."""
    group_ids = [g.id for g in device.groups]
    conditions = [Assignment.target_type == "all"]
    if group_ids:
        conditions.append(
            and_(Assignment.target_type == "group", Assignment.target_id.in_(group_ids))
        )
    conditions.append(and_(Assignment.target_type == "device", Assignment.target_id == device.id))
    return or_(*conditions)


def wants_for(device: Device, now: datetime) -> tuple[pl.Want, ...]:
    rows = db.session.execute(
        db.select(Assignment)
        .join(Video)
        .where(
            applicable(device),
            Video.status == "ready",
            Video.deleted_at.is_(None),
            Video.sha256.is_not(None),
            or_(Assignment.end_at.is_(None), Assignment.end_at > now),
        )
        .order_by(Assignment.position, Assignment.start_at, Assignment.id)
    ).scalars()
    return tuple(
        pl.Want(
            assignment_id=a.id,
            video=planner_video(a.video),
            start_at=a.start_at,
            end_at=a.end_at,
            position=a.position,
            is_fallback=a.is_fallback,
        )
        for a in rows
    )


def legacy_delete_requests(device: Device) -> frozenset[str]:
    return frozenset(
        f.remote_name
        for f in device.files
        if f.location == "root" and f.state == "legacy" and f.delete_requested_at is not None
    )


def planner_input(
    device: Device,
    remote: pl.RemoteState,
    now: datetime,
    settings: Settings,
    *,
    lib: tuple[pl.Video, ...] | None = None,
) -> pl.PlannerInput:
    return pl.PlannerInput(
        now=now,
        remote=remote,
        wants=wants_for(device, now),
        library=library() if lib is None else lib,
        legacy_delete_requests=legacy_delete_requests(device),
        lookahead=timedelta(hours=settings.staging_lookahead_h),
        max_bytes=device.max_bytes,
    )


def remote_from_listing(
    root: list[ListEntry], staging_exists: bool, staging: list[ListEntry]
) -> pl.RemoteState:
    def convert(entries: list[ListEntry]) -> tuple[pl.Entry, ...]:
        return tuple(
            sorted(
                (pl.Entry(e.name, e.is_dir, e.size, e.mtime) for e in entries),
                key=lambda e: e.name,
            )
        )

    return pl.RemoteState(
        root=convert(root), staging_exists=staging_exists, staging=convert(staging)
    )


def remote_from_snapshot(device: Device) -> pl.RemoteState | None:
    """The last state read from the Pi, or None if it was never read."""
    if device.snapshot_at is None:
        return None
    root, staging = [], []
    for f in device.files:
        entry = pl.Entry(f.remote_name, f.is_dir, f.size_bytes, f.remote_mtime)
        (root if f.location == "root" else staging).append(entry)
    return pl.RemoteState(
        root=tuple(sorted(root, key=lambda e: e.name)),
        staging_exists=device.staging_exists,
        staging=tuple(sorted(staging, key=lambda e: e.name)),
    )


def plan_from_snapshot(device: Device, now: datetime, settings: Settings) -> pl.Plan | None:
    """What the next cycle would do, computed from the last snapshot (no FTP)."""
    if device.video_path is None:
        return None
    remote = remote_from_snapshot(device)
    if remote is None:
        return None
    return pl.plan(planner_input(device, remote, now, settings))


@dataclass
class SnapshotChanges:
    new_legacy: list[DeviceFile] = field(default_factory=list)
    dropped_requests: list[str] = field(default_factory=list)  # display names


def store_snapshot(
    device: Device,
    remote: pl.RemoteState,
    now: datetime,
    *,
    lib: tuple[pl.Video, ...] | None = None,
) -> SnapshotChanges:
    """Refresh DeviceFile rows from a listing.

    Bookkeeping (delete requests, adoption, upload dates) survives as long as the
    same file is present. A delete request is dropped as soon as the file looks
    different (size, date or type): it was made for the file the admin saw, not
    for whatever carries that name later. Rows of files that disappeared go.
    """
    classified = pl.classify(remote, library() if lib is None else lib)
    existing = {(f.location, f.remote_name): f for f in device.files}
    seen: set[tuple[str, str]] = set()
    changes = SnapshotChanges()

    for location, entries in (("root", remote.root), ("staging", remote.staging)):
        for entry in entries:
            key = (location, entry.name)
            seen.add(key)
            if location == "root":
                state, video_id, issues = classified.root_state(entry.name)
            else:
                state, video_id = classified.staging_state(entry.name)
                issues = ()
            row = existing.get(key)
            if row is None:
                row = DeviceFile(
                    device=device, location=location, remote_name=entry.name, first_seen_at=now
                )
                db.session.add(row)
                if state == "legacy":
                    changes.new_legacy.append(row)
            elif row.delete_requested_at is not None and (
                row.size_bytes != entry.size
                or row.remote_mtime != entry.mtime
                or row.is_dir != entry.is_dir
            ):
                changes.dropped_requests.append(row.display_name)
                row.delete_requested_at = None
                row.delete_requested_by = None
            row.is_dir = entry.is_dir
            row.size_bytes = entry.size
            row.remote_mtime = entry.mtime
            row.state = state
            row.video_id = video_id
            row.issues = list(issues)
            row.last_seen_at = now
            if state != "legacy":
                row.delete_requested_at = None
                row.delete_requested_by = None

    for key, row in existing.items():
        if key not in seen:
            device.files.remove(row)

    device.snapshot_at = now
    device.staging_exists = remote.staging_exists
    return changes
