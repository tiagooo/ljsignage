"""Executor against the simulated FTP server, cross-checked with the player simulator.

The same random plans are applied (a) over FTP by the real executor and (b) to
SimFS; the resulting folders must be identical and a new plan must be empty.
"""

from __future__ import annotations

import hashlib
import io
import os
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lj_signage.ftp import FtpClient
from lj_signage.naming import STAGING_DIR
from lj_signage.reconciler import planner as pl
from lj_signage.reconciler.executor import ExecutionError, Executor, read_remote_state
from tests.player_sim import SimFile, SimFS, apply_action

VIDEOS = "/home/tmagalhaes/Videos"
NOW = datetime.now(UTC).replace(microsecond=0)


def client(server, **kwargs) -> FtpClient:
    return FtpClient(server.host, server.port, server.user, server.password, timeout=5, **kwargs)


def scenario(rng: random.Random, media_dir: Path):
    library, media = [], {}
    for vid in range(1, rng.randint(2, 6)):
        content = rng.randbytes(rng.randint(10, 300) * 10)
        sha = hashlib.sha256(content).hexdigest()
        video = pl.Video(vid, f"video-{vid}", sha, len(content), f"Vídeo {vid}", 10.0)
        library.append(video)
        path = media_dir / f"{vid}.mp4"
        path.write_bytes(content)
        media[vid] = path

    fs, disk = SimFS(), {}
    for name in rng.sample(["Promo Natal.mp4", "clip.mp4", "leia-me.txt", "010_x.mp4"], 2):
        size = rng.randint(1, 50)
        fs.files[name] = SimFile(size, None, "legacy")
        disk[name] = b"L" * size
    prefixes = rng.sample(range(1, 999), 6)
    for video in rng.sample(library, rng.randint(0, len(library))):
        name = video.active_name(prefixes.pop())
        fs.files[name] = SimFile(video.size_bytes, video.sha256, "app")
        disk[name] = media[video.id].read_bytes()
    if rng.random() < 0.6:
        fs.dirs.add(STAGING_DIR)
        for video in rng.sample(library, rng.randint(0, min(2, len(library)))):
            fs.files[video.staged_path] = SimFile(video.size_bytes, video.sha256, "app")
            disk[video.staged_path] = media[video.id].read_bytes()
        for video in rng.sample(library, rng.randint(0, 1)):
            age = timedelta(hours=rng.choice([2, 30]))
            fs.files[video.part_path] = SimFile(7, None, "app", NOW - age)
            disk[video.part_path] = b"P" * 7
    wants = []
    for aid in range(1, rng.randint(1, 6)):
        start = NOW + timedelta(hours=rng.randint(-50, 30))
        end = None if rng.random() < 0.5 else start + timedelta(hours=rng.randint(1, 80))
        wants.append(
            pl.Want(
                aid, rng.choice(library), start, end, rng.choice([10, 20, 30]), rng.random() < 0.3
            )
        )
    requests = frozenset(
        n for n in ("clip.mp4", "leia-me.txt") if n in fs.files and rng.random() < 0.5
    )
    return library, media, fs, disk, wants, requests


def materialize(server, disk: dict[str, bytes], fs: SimFS) -> Path:
    videos = server.home / "Videos"
    videos.mkdir(exist_ok=True)
    for name in fs.dirs:
        (videos / name).mkdir(exist_ok=True)
    for rel, content in disk.items():
        path = videos / rel
        path.write_bytes(content)
        mtime = fs.files[rel].mtime
        if mtime is not None:
            os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return videos


def on_disk(folder: Path) -> dict[str, int]:
    return {str(p.relative_to(folder)): p.stat().st_size for p in folder.rglob("*") if p.is_file()}


@pytest.mark.parametrize("seed", range(25))
@pytest.mark.parametrize("variant", ["mlsd", "vsftpd-like"])
def test_executor_matches_the_simulator(seed, variant, ftp_server_factory, tmp_path):
    server = ftp_server_factory(
        mlsd=variant == "mlsd", hide_dotfiles=variant == "vsftpd-like", name=f"s{seed}{variant}"
    )
    media_dir = tmp_path / f"media-{seed}-{variant}"
    media_dir.mkdir()
    rng = random.Random(seed)
    library, media, fs, disk, wants, requests = scenario(rng, media_dir)
    folder = materialize(server, disk, fs)

    def planner_input(remote):
        return pl.PlannerInput(
            now=NOW,
            remote=remote,
            wants=tuple(wants),
            library=tuple(library),
            legacy_delete_requests=requests,
        )

    with client(server, read_only=False) as ftp:
        remote = read_remote_state(ftp, VIDEOS)
        real_plan = pl.plan(planner_input(remote))
        sim_plan = pl.plan(planner_input(fs.remote_state()))
        assert real_plan.actions == sim_plan.actions

        outcomes = Executor(ftp, VIDEOS, media_path=media.get).run(real_plan)
        assert all(o.ok for o in outcomes), [o.error for o in outcomes if not o.ok]
        after = pl.plan(planner_input(read_remote_state(ftp, VIDEOS)))
        assert after.actions == ()

    videos = {v.id: v for v in library}
    for action in sim_plan.actions:
        apply_action(fs, action, videos, NOW)
    expected = {path: f.size for path, f in fs.files.items()}
    assert on_disk(folder) == expected


def test_executor_requires_a_writable_client(ftp_server):
    with client(ftp_server) as ftp, pytest.raises(ExecutionError):
        Executor(ftp, VIDEOS, media_path=lambda _id: None)


def test_rename_never_replaces_an_existing_file(ftp_server, tmp_path):
    videos = ftp_server.home / "Videos"
    (videos / STAGING_DIR).mkdir(parents=True)
    content = b"video"
    sha = hashlib.sha256(content).hexdigest()
    video = pl.Video(1, "natal", sha, len(content), "Natal")
    (videos / STAGING_DIR / f"{sha[:12]}.mp4").write_bytes(content)
    target = video.active_name(10)
    (videos / target).write_bytes(b"someone else's file")
    plan = pl.plan(
        pl.PlannerInput(
            now=NOW,
            remote=pl.RemoteState(
                root=(), staging_exists=True, staging=(pl.Entry(f"{sha[:12]}.mp4", size=5),)
            ),
            wants=(pl.Want(1, video, NOW - timedelta(hours=1)),),
            library=(video,),
        )
    )
    assert [a.kind for a in plan.actions] == [pl.ActionKind.ACTIVATE]
    with client(ftp_server, read_only=False) as ftp:
        [outcome] = Executor(ftp, VIDEOS, media_path=lambda _id: None).run(plan)
    assert not outcome.ok and "nunca substitui" in outcome.error
    assert (videos / target).read_bytes() == b"someone else's file"


def test_upload_progress_callback(ftp_server, tmp_path):
    (ftp_server.home / "Videos").mkdir()
    content = os.urandom(300_000)
    sha = hashlib.sha256(content).hexdigest()
    local = tmp_path / "v.mp4"
    local.write_bytes(content)
    video = pl.Video(1, "grande", sha, len(content), "Grande")
    plan = pl.plan(
        pl.PlannerInput(
            now=NOW,
            remote=pl.RemoteState(),
            wants=(pl.Want(1, video, NOW - timedelta(hours=1)),),
            library=(video,),
        )
    )
    seen = []
    with client(ftp_server, read_only=False) as ftp:
        outcomes = Executor(
            ftp, VIDEOS, media_path=lambda _id: local, progress=lambda a, sent: seen.append(sent)
        ).run(plan)
        assert all(o.ok for o in outcomes)
        buffer = io.BytesIO()
        ftp.retrieve(f"{VIDEOS}/{video.active_name(10)}", buffer)
    assert buffer.getvalue() == content
    assert seen[-1] == len(content)
