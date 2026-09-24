"""Builders shared by the planner, player simulator and executor tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from lj_signage.naming import STAGING_DIR
from lj_signage.reconciler import planner as pl
from tests.player_sim import SimFile, SimFS, apply_action

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


def make_video(vid: int, slug: str | None = None, *, size: int | None = None, duration=30.0):
    sha = hashlib.sha256(f"video-{vid}".encode()).hexdigest()
    return pl.Video(
        id=vid,
        slug=slug or f"video-{vid}",
        sha256=sha,
        size_bytes=size if size is not None else 1_000_000 * vid,
        title=f"Vídeo {vid}",
        duration_s=duration,
    )


def want(
    aid: int,
    video: pl.Video,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    hours: float | None = None,
    position: int = 100,
    fallback: bool = False,
) -> pl.Want:
    start = start or NOW - timedelta(hours=1)
    if hours is not None:
        end = start + timedelta(hours=hours)
    return pl.Want(aid, video, start, end, position, fallback)


def fs_with(*, active=(), staged=(), legacy=(), dirs=(), staging=False) -> SimFS:
    """active: (video, prefix); staged: video; legacy: (name, size)."""
    fs = SimFS()
    for video, nnn in active:
        fs.files[video.active_name(nnn)] = SimFile(video.size_bytes, video.sha256, "app")
    if staging or staged:
        fs.dirs.add(STAGING_DIR)
    for video in staged:
        fs.files[video.staged_path] = SimFile(video.size_bytes, video.sha256, "app")
    for name, size in legacy:
        fs.files[name] = SimFile(size, None, "legacy")
    for name in dirs:
        fs.dirs.add(name)
    return fs


def make_input(fs: SimFS, wants=(), library=(), *, now=NOW, **kwargs) -> pl.PlannerInput:
    return pl.PlannerInput(
        now=now, remote=fs.remote_state(), wants=tuple(wants), library=tuple(library), **kwargs
    )


def kinds(plan: pl.Plan) -> list[str]:
    return [a.kind.value for a in plan.actions]


def run(fs: SimFS, inp: pl.PlannerInput) -> pl.Plan:
    """Plan and apply every action to ``fs``."""
    result = pl.plan(inp)
    videos = {v.id: v for v in inp.library}
    for action in result.actions:
        apply_action(fs, action, videos, inp.now)
    return result
