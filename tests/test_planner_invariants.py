"""Property tests: the CLAUDE.md invariants hold after *every* action of any plan.

Random device states and schedules are generated from fixed seeds (reproducible
without extra dependencies). Each plan is applied action by action to SimFS and
checked with the player simulator, including plans interrupted by a failure
(the executor stops at the first error) and multi-cycle runs over time.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import replace
from datetime import timedelta

import pytest

from lj_signage.naming import STAGING_DIR
from lj_signage.reconciler import planner as pl
from tests.planner_helpers import NOW
from tests.player_sim import PlayerSim, SimFile, SimFS, apply_action, check_invariants

LEGACY_NAMES = [
    ("Promo Natal.mp4", 5_000),  # spaces: the unquoted $entry breaks it
    ("video1.mp4", 7_000),
    ("010_antigo.mp4", 9_000),  # valid active pattern, not an app file
    ("leia-me.txt", 10),
    ("clip.MOV", 3_000),
    ("vazio.mp4", 0),
    ("Vídeo Loja.mp4", 4_000),
]
LEGACY_DIRS = ["fotos", "Backup"]
SLUGS = ["natal", "filigrana", "alianças".replace("ç", "c"), "first-day", "relogios", "promo"]


def random_video(rng: random.Random, vid: int) -> pl.Video:
    sha = hashlib.sha256(f"{vid}-{rng.random()}".encode()).hexdigest()
    return pl.Video(
        id=vid,
        slug=f"{rng.choice(SLUGS)}-{vid}",
        sha256=sha,
        size_bytes=rng.randint(1, 60) * 1_000_000,
        title=f"Vídeo {vid}",
        duration_s=rng.choice([None, 15.0, 30.0, 60.0]),
    )


def random_scenario(rng: random.Random):
    library = [random_video(rng, vid) for vid in range(1, rng.randint(2, 9))]
    fs = SimFS()
    legacy_names = set()
    for name, size in rng.sample(LEGACY_NAMES, rng.randint(0, 3)):
        fs.files[name] = SimFile(size, None, "legacy")
        legacy_names.add(name)
    for name in rng.sample(LEGACY_DIRS, rng.randint(0, 1)):
        fs.dirs.add(name)
        legacy_names.add(name)
    fs.files[".bash_history"] = SimFile(10, None, "legacy") if rng.random() < 0.3 else None
    fs.files = {k: v for k, v in fs.files.items() if v is not None}

    used_prefixes = set()
    for video in rng.sample(library, rng.randint(0, len(library))):
        for _ in range(2 if rng.random() < 0.15 else 1):  # sometimes a duplicate copy
            nnn = rng.randint(1, 999)
            if nnn in used_prefixes:
                continue
            used_prefixes.add(nnn)
            fs.files[video.active_name(nnn)] = SimFile(video.size_bytes, video.sha256, "app")

    if rng.random() < 0.6:
        fs.dirs.add(STAGING_DIR)
        for video in rng.sample(library, rng.randint(0, min(3, len(library)))):
            corrupt = rng.random() < 0.2
            size = video.size_bytes - 1 if corrupt else video.size_bytes
            fs.files[video.staged_path] = SimFile(size, None if corrupt else video.sha256, "app")
        for video in rng.sample(library, rng.randint(0, min(2, len(library)))):
            age = timedelta(hours=rng.choice([1, 5, 23, 25, 72]))
            fs.files[video.part_path] = SimFile(12, None, "app", NOW - age)
        if rng.random() < 0.2:
            orphan = hashlib.sha256(str(rng.random()).encode()).hexdigest()[:12]
            fs.files[f"{STAGING_DIR}/{orphan}.mp4"] = SimFile(99, None, "app")
        if rng.random() < 0.2:
            fs.files[f"{STAGING_DIR}/notas.txt"] = SimFile(5, None, "legacy")

    wants = []
    for aid in range(1, rng.randint(1, 8)):
        start = NOW + timedelta(hours=rng.randint(-200, 100))
        end = None if rng.random() < 0.4 else start + timedelta(hours=rng.randint(1, 150))
        wants.append(
            pl.Want(
                assignment_id=aid,
                video=rng.choice(library),
                start_at=start,
                end_at=end,
                position=rng.choice([10, 20, 20, 30, 100]),
                is_fallback=rng.random() < 0.25,
            )
        )

    requests = {n for n in legacy_names if rng.random() < 0.4}
    if rng.random() < 0.1:
        requests.add("ja-nao-existe.mp4")
    total = sum(f.size for f in fs.files.values())
    max_bytes = None if rng.random() < 0.7 else total + rng.randint(0, 150) * 1_000_000
    return library, fs, wants, frozenset(requests), max_bytes, legacy_names


def plan_for(fs, library, wants, requests, max_bytes, now):
    return pl.plan(
        pl.PlannerInput(
            now=now,
            remote=fs.remote_state(),
            wants=tuple(wants),
            library=tuple(library),
            legacy_delete_requests=requests,
            max_bytes=max_bytes,
        )
    )


def apply_checked(fs, plan, library, requests, legacy_names, now, *, stop_after=None):
    """Apply the plan action by action, asserting the invariants after each one."""
    by_sha = {v.sha256: v for v in library}
    videos = {v.id: v for v in library}
    player = PlayerSim()
    had_playable = bool(player.playable(fs, by_sha))
    for index, action in enumerate(plan.actions):
        if stop_after is not None and index >= stop_after:
            return
        if action.src is not None and action.kind.value.startswith("delete"):
            target = fs.files.get(action.src)
            assert target is not None, f"deleting a missing file: {action}"
            if target.origin != "app":
                assert action.kind is pl.ActionKind.DELETE_LEGACY, action
                assert action.src in requests, f"unrequested delete: {action.src}"
        apply_action(fs, action, videos, now)
        problems = check_invariants(
            fs, by_sha, initial_legacy=legacy_names, require_playable=had_playable
        )
        assert not problems, (index, action, problems, fs.log)
        had_playable = had_playable or bool(player.playable(fs, by_sha))


@pytest.mark.parametrize("seed", range(400))
def test_random_plans_keep_invariants_and_are_idempotent(seed):
    rng = random.Random(seed)
    library, fs, wants, requests, max_bytes, legacy = random_scenario(rng)
    plan = plan_for(fs, library, wants, requests, max_bytes, NOW)
    apply_checked(fs, plan, library, requests, legacy, NOW)

    again = plan_for(fs, library, wants, requests, max_bytes, NOW)
    assert again.actions == (), [a for a in again.actions]

    # What plays is exactly the desired list, in order (unless the planner kept
    # the current videos because nothing else would play).
    app_visible = [n for n in fs.visible() if fs.files.get(n) and fs.files[n].origin == "app"]
    if "keep_last_videos" in {n.code for n in plan.notices}:
        assert not plan.desired
    else:
        assert app_visible == [item.name for item in plan.desired]


@pytest.mark.parametrize("seed", range(200))
def test_interrupted_plans_recover_on_the_next_cycle(seed):
    rng = random.Random(10_000 + seed)
    library, fs, wants, requests, max_bytes, legacy = random_scenario(rng)
    plan = plan_for(fs, library, wants, requests, max_bytes, NOW)
    if not plan.actions:
        return
    stop = rng.randrange(len(plan.actions))
    apply_checked(fs, plan, library, requests, legacy, NOW, stop_after=stop)

    resumed = plan_for(fs, library, wants, requests, max_bytes, NOW)
    apply_checked(fs, resumed, library, requests, legacy, NOW)
    assert plan_for(fs, library, wants, requests, max_bytes, NOW).actions == ()


@pytest.mark.parametrize("seed", range(100))
def test_cycles_over_a_week_keep_invariants(seed):
    rng = random.Random(20_000 + seed)
    library, fs, wants, requests, max_bytes, legacy = random_scenario(rng)
    now = NOW
    for _ in range(40):
        plan = plan_for(fs, library, wants, requests, max_bytes, now)
        apply_checked(fs, plan, library, requests, legacy, now)
        assert plan_for(fs, library, wants, requests, max_bytes, now).actions == ()
        requests = frozenset()  # a request is consumed once executed
        now += timedelta(hours=rng.choice([1, 3, 6, 12]))


# --- adversarial scenarios -------------------------------------------------------------------
# Identical content under two videos (they share a staging path), legacy files carrying the
# very name the app would use, prefixes near 999, sub-folders in staging, tight max_bytes.


def adversarial_scenario(rng: random.Random):
    library: list[pl.Video] = []
    for vid in range(1, rng.randint(2, 8)):
        if library and rng.random() < 0.3:
            twin = rng.choice(library)
            library.append(
                pl.Video(vid, f"{rng.choice(SLUGS)}-{vid}", twin.sha256, twin.size_bytes, f"V{vid}")
            )
        else:
            library.append(random_video(rng, vid))
    fs = SimFS()
    legacy = set()
    for _ in range(rng.randint(0, 3)):
        roll = rng.random()
        if roll < 0.4:
            video = rng.choice(library)
            name, size = video.active_name(rng.choice([5, 10, 11, 20, 30])), video.size_bytes + 7
        elif roll < 0.7:
            name, size = rng.choice(LEGACY_NAMES)
        else:
            name, size = "fotos", 0
        if name in fs.files or name in fs.dirs:
            continue
        if name == "fotos":
            fs.dirs.add(name)
        else:
            fs.files[name] = SimFile(size, None, "legacy")
        legacy.add(name)
    used: set[int] = set()
    for video in rng.sample(library, rng.randint(0, len(library))):
        for _ in range(rng.choice([1, 1, 2])):
            nnn = rng.choice([*range(1, 30), 990, 995, 999])
            name = video.active_name(nnn)
            if nnn in used or name in fs.files:
                continue
            used.add(nnn)
            fs.files[name] = SimFile(video.size_bytes, video.sha256, "app")
    if rng.random() < 0.7:
        fs.dirs.add(STAGING_DIR)
        for video in rng.sample(library, rng.randint(0, min(3, len(library)))):
            corrupt = rng.random() < 0.2
            size = video.size_bytes - (1 if corrupt else 0)
            fs.files[video.staged_path] = SimFile(size, None if corrupt else video.sha256, "app")
        for video in rng.sample(library, rng.randint(0, min(2, len(library)))):
            age = timedelta(hours=rng.choice([1, 23, 25, 72]))
            fs.files[video.part_path] = SimFile(12, None, "app", NOW - age)
        if rng.random() < 0.2:
            fs.dirs.add(f"{STAGING_DIR}/sub")
    wants = [
        pl.Want(
            aid,
            rng.choice(library),
            NOW + timedelta(hours=rng.randint(-100, 60)),
            None,
            rng.choice([10, 20, 100]),
            rng.random() < 0.3,
        )
        for aid in range(1, rng.randint(1, 9))
    ]
    wants = [
        replace(
            w,
            end_at=None
            if rng.random() < 0.4
            else w.start_at + timedelta(hours=rng.randint(1, 120)),
        )
        for w in wants
    ]
    requests = frozenset(n for n in sorted(legacy) if rng.random() < 0.5)
    total = sum(f.size for f in fs.files.values())
    max_bytes = None if rng.random() < 0.6 else max(1, total + rng.randint(-20, 120) * 1_000_000)
    return library, fs, wants, requests, max_bytes, legacy


@pytest.mark.parametrize("seed", range(300))
def test_adversarial_scenarios_over_eight_cycles(seed):
    rng = random.Random(50_000 + seed)
    library, fs, wants, requests, max_bytes, legacy = adversarial_scenario(rng)
    now = NOW
    for _ in range(8):
        plan = plan_for(fs, library, wants, requests, max_bytes, now)
        interrupted = bool(plan.actions) and rng.random() < 0.3
        stop = rng.randrange(len(plan.actions) + 1) if interrupted else None
        apply_checked(fs, plan, library, requests, legacy, now, stop_after=stop)
        if stop is None:
            assert plan_for(fs, library, wants, requests, max_bytes, now).actions == ()
            requests = frozenset()
            now += timedelta(hours=rng.choice([1, 6, 12]))
