"""Scenario tests of the pure planner (SPEC §6)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from lj_signage.naming import ACTIVE_NAME_RE, STAGING_DIR
from lj_signage.reconciler import planner as pl
from tests.planner_helpers import NOW, fs_with, kinds, make_input, make_video, run, want
from tests.player_sim import PlayerSim, SimFile

A, B, C, D, F = (make_video(i) for i in (1, 2, 3, 4, 9))
LIB = (A, B, C, D, F)
BY_SHA = {v.sha256: v for v in LIB}


def playable(fs):
    return PlayerSim().playable(fs, BY_SHA)


# --- first synchronisation ---------------------------------------------------------------


def test_empty_folder_uploads_then_activates_with_step_10_prefixes():
    fs = fs_with()
    inp = make_input(fs, [want(1, A, position=10), want(2, B, position=20)], LIB)
    plan = run(fs, inp)
    assert kinds(plan) == ["mkdir_staging", "upload", "upload", "activate", "activate"]
    assert playable(fs) == [A.active_name(10), B.active_name(20)]
    assert fs.visible() == ["010_video-1-" + A.sha256[:6] + ".mp4", B.active_name(20)]


def test_first_sync_keeps_existing_files_as_legacy():
    fs = fs_with(legacy=[("Promo Natal.mp4", 500), ("clip.mp4", 800)], dirs=["fotos"])
    plan = run(fs, make_input(fs, [want(1, A)], LIB))
    assert "delete_legacy" not in kinds(plan)
    assert {"Promo Natal.mp4", "clip.mp4", "fotos"} <= set(fs.visible())
    legacy = {lf.name: lf for lf in plan.classified.legacy}
    assert legacy["Promo Natal.mp4"].issues == ("unsafe_name", "non_standard_name")
    assert not legacy["Promo Natal.mp4"].playable
    assert legacy["clip.mp4"].playable
    assert legacy["fotos"].issues == ("dir",)


def test_nothing_to_do_is_an_empty_plan():
    fs = fs_with(active=[(A, 10)], staged=[F])
    inp = make_input(fs, [want(1, A), want(2, F, fallback=True)], LIB)
    assert pl.plan(inp).actions == ()


# --- campaigns and fallback ---------------------------------------------------------------


def test_fallback_is_kept_staged_while_campaigns_run():
    fs = fs_with(active=[(A, 10)], staging=True)
    plan = run(fs, make_input(fs, [want(1, A), want(2, F, fallback=True)], LIB))
    assert kinds(plan) == ["upload"]
    assert F.staged_path in fs.files
    assert fs.visible() == [A.active_name(10)]


def test_end_of_campaign_activates_fallback_before_removing():
    fs = fs_with(active=[(A, 10)], staged=[F])
    campaign = want(1, A, start=NOW - timedelta(days=2), end=NOW - timedelta(minutes=1))
    plan = run(fs, make_input(fs, [campaign, want(2, F, fallback=True)], LIB))
    assert kinds(plan) == ["activate", "delete_active"]
    assert plan.using_fallback
    assert fs.visible() == [F.active_name(10)]


def test_campaign_back_moves_fallback_to_staging_instead_of_deleting():
    fs = fs_with(active=[(F, 10)], staged=[A])
    plan = run(fs, make_input(fs, [want(1, A), want(2, F, fallback=True)], LIB))
    assert kinds(plan) == ["activate", "deactivate"]
    assert fs.visible() == [A.active_name(10)]
    assert F.staged_path in fs.files


def test_nothing_scheduled_and_no_fallback_keeps_current_videos():
    fs = fs_with(active=[(A, 10), (B, 20)])
    plan = pl.plan(make_input(fs, [], LIB))
    assert plan.actions == ()
    codes = {n.code for n in plan.notices}
    assert {"keep_last_videos", "no_fallback", "nothing_scheduled"} <= codes


def test_expired_campaign_is_removed_when_a_legacy_video_remains():
    fs = fs_with(active=[(A, 10)], legacy=[("antigo.mp4", 100)])
    plan = run(fs, make_input(fs, [], LIB))
    assert kinds(plan) == ["delete_active"]
    assert fs.visible() == ["antigo.mp4"]


def test_upcoming_assignment_is_staged_within_lookahead_only():
    fs = fs_with(active=[(A, 10)], staging=True)
    soon = want(2, B, start=NOW + timedelta(hours=20))
    later = want(3, C, start=NOW + timedelta(hours=60))
    plan = run(fs, make_input(fs, [want(1, A), soon, later], LIB))
    assert [(a.kind.value, a.video_id) for a in plan.actions] == [("upload", B.id)]
    assert fs.visible() == [A.active_name(10)]


def test_scheduled_start_activates_the_staged_file_without_upload():
    fs = fs_with(active=[(A, 10)], staged=[B])
    start = NOW + timedelta(hours=2)
    inp = make_input(fs, [want(1, A, position=10), want(2, B, start=start, position=20)], LIB)
    assert kinds(pl.plan(inp)) == []
    plan = run(fs, replace(inp, now=start))
    assert kinds(plan) == ["activate"]
    assert fs.visible() == [A.active_name(10), B.active_name(20)]


def test_end_at_is_exclusive():
    end = NOW + timedelta(hours=1)
    w = want(1, A, end=end)
    assert w.is_active(end - timedelta(seconds=1))
    assert not w.is_active(end)


def test_same_video_through_two_targets_plays_once():
    fs = fs_with()
    plan = pl.plan(make_input(fs, [want(1, A, position=10), want(2, A, position=50)], LIB))
    assert [item.video.id for item in plan.desired] == [A.id]


def test_duplicate_content_is_played_once():
    twin = replace(B, id=7, slug="gemeo", sha256=A.sha256, size_bytes=A.size_bytes)
    plan = pl.plan(make_input(fs_with(), [want(1, A), want(2, twin)], (*LIB, twin)))
    assert [item.video.id for item in plan.desired] == [A.id]
    assert "duplicate_content" in {n.code for n in plan.notices}


def test_order_is_position_then_start():
    fs = fs_with()
    early = want(1, A, position=20, start=NOW - timedelta(hours=5))
    late = want(2, B, position=20, start=NOW - timedelta(hours=1))
    first = want(3, C, position=10)
    plan = pl.plan(make_input(fs, [late, early, first], LIB))
    assert [item.video.id for item in plan.desired] == [C.id, A.id, B.id]


# --- reordering with few renames --------------------------------------------------------------


def test_swap_renames_a_single_file():
    fs = fs_with(active=[(A, 10), (B, 20)])
    plan = run(fs, make_input(fs, [want(1, A, position=20), want(2, B, position=10)], LIB))
    assert kinds(plan) == ["reorder"]
    order = [name.split("_", 1)[1][:7] for name in fs.visible()]
    assert order == ["video-2", "video-1"]


def test_insert_at_top_does_not_rename_existing_files():
    fs = fs_with(active=[(A, 10), (B, 20)], staged=[C])
    inp = make_input(
        fs, [want(1, A, position=20), want(2, B, position=30), want(3, C, position=10)], LIB
    )
    plan = run(fs, inp)
    assert kinds(plan) == ["activate"]
    assert plan.actions[0].dst == C.active_name(5)
    assert fs.visible() == [C.active_name(5), A.active_name(10), B.active_name(20)]


def test_removing_the_middle_video_renames_nothing():
    fs = fs_with(active=[(A, 10), (B, 20), (C, 30)])
    plan = run(fs, make_input(fs, [want(1, A, position=10), want(3, C, position=30)], LIB))
    assert kinds(plan) == ["delete_active"]
    assert fs.visible() == [A.active_name(10), C.active_name(30)]


def test_extra_copies_of_a_playing_video_are_removed_before_reordering():
    fs = fs_with(active=[(A, 10), (A, 40), (B, 20)])
    plan = run(fs, make_input(fs, [want(1, B, position=10), want(2, A, position=20)], LIB))
    assert kinds(plan)[0] == "delete_active"
    assert len([n for n in fs.visible() if "video-1" in n]) == 1
    assert [n.split("_", 1)[1][:7] for n in fs.visible()] == ["video-2", "video-1"]


@pytest.mark.parametrize(
    ("order", "current", "expected"),
    [
        ([1, 2, 3], {}, {1: 10, 2: 20, 3: 30}),
        ([4, 1, 2], {1: 10, 2: 20}, {4: 5, 1: 10, 2: 20}),
        ([1, 4, 2], {1: 10, 2: 20}, {1: 10, 4: 15, 2: 20}),
        ([1, 2, 4], {1: 10, 2: 20}, {1: 10, 2: 20, 4: 30}),
        ([1, 3], {1: 10, 2: 20, 3: 30}, {1: 10, 3: 30}),
    ],
)
def test_assign_prefixes_examples(order, current, expected):
    assert pl.assign_prefixes(order, current) == expected


def test_assign_prefixes_tight_gap_moves_as_few_as_possible():
    result = pl.assign_prefixes([1, 5, 2, 3], {1: 10, 2: 11, 3: 12})
    values = [result[k] for k in [1, 5, 2, 3]]
    assert values == sorted(values) and len(set(values)) == 4
    assert sum(result[k] != v for k, v in {1: 10, 2: 11, 3: 12}.items()) == 1


def test_assign_prefixes_scales_past_99_videos():
    order = list(range(150))
    result = pl.assign_prefixes(order, {})
    values = [result[k] for k in order]
    assert values == sorted(values) and len(set(values)) == 150
    assert min(values) >= 1 and max(values) <= 999


def test_active_names_follow_the_player_safe_pattern():
    video = make_video(5, slug="colecao-filigrana-natal-2026")
    name = video.active_name(10)
    assert ACTIVE_NAME_RE.match(name)
    assert name == f"010_colecao-filigrana-natal-2026-{video.sha256[:6]}.mp4"


# --- legacy files ------------------------------------------------------------------------


def test_legacy_delete_request_runs_after_activation():
    fs = fs_with(legacy=[("antigo.mp4", 100)], staged=[A])
    inp = make_input(fs, [want(1, A)], LIB, legacy_delete_requests=frozenset({"antigo.mp4"}))
    plan = run(fs, inp)
    assert kinds(plan) == ["activate", "delete_legacy"]
    assert fs.visible() == [A.active_name(10)]


def test_deleting_the_last_video_is_refused():
    fs = fs_with(legacy=[("antigo.mp4", 100)])
    inp = make_input(fs, [], LIB, legacy_delete_requests=frozenset({"antigo.mp4"}))
    plan = pl.plan(inp)
    assert plan.actions == ()
    assert "keep_last_videos" in {n.code for n in plan.notices}


def test_legacy_directories_are_never_deleted():
    fs = fs_with(active=[(A, 10)], dirs=["fotos"])
    inp = make_input(fs, [want(1, A)], LIB, legacy_delete_requests=frozenset({"fotos"}))
    plan = pl.plan(inp)
    assert plan.actions == ()
    assert "legacy_dir" in {n.code for n in plan.notices}


def test_unrequested_legacy_files_are_never_deleted():
    fs = fs_with(legacy=[("a.mp4", 10), ("b.txt", 10)], active=[(A, 10)])
    plan = pl.plan(make_input(fs, [want(1, B)], LIB))
    assert all(a.kind is not pl.ActionKind.DELETE_LEGACY for a in plan.actions)


def test_name_clash_with_a_legacy_file_uses_another_prefix():
    lookalike = A.active_name(10)
    fs = fs_with(legacy=[(lookalike, 123)])  # the app's name, but not the app's file
    plan = run(fs, make_input(fs, [want(1, A)], LIB))
    [activation] = [a for a in plan.actions if a.kind is pl.ActionKind.ACTIVATE]
    assert activation.dst == A.active_name(11)
    assert fs.files[lookalike].size == 123  # left alone
    legacy = plan.classified.legacy[0]
    assert "size_mismatch" in legacy.issues and not legacy.playable
    assert pl.plan(make_input(fs, [want(1, A)], LIB)).actions == ()


def test_hidden_entries_in_the_video_folder_are_ignored():
    fs = fs_with(active=[(A, 10)], legacy=[(".config", 5)])
    plan = pl.plan(make_input(fs, [want(1, A)], LIB))
    assert plan.actions == ()
    assert [e.name for e in plan.classified.hidden_root] == [".config"]


# --- staging hygiene -----------------------------------------------------------------------


def test_old_part_is_deleted_young_part_is_kept():
    fs = fs_with(active=[(A, 10)], staging=True)
    old = f"{STAGING_DIR}/{B.sha256[:12]}.mp4.part"
    young = f"{STAGING_DIR}/{C.sha256[:12]}.mp4.part"
    fs.files[old] = SimFile(5, None, "app", NOW - timedelta(hours=25))
    fs.files[young] = SimFile(5, None, "app", NOW - timedelta(hours=2))
    plan = run(fs, make_input(fs, [want(1, A)], LIB))
    assert [(a.kind.value, a.src) for a in plan.actions] == [("delete_part", old)]
    assert young in fs.files


def test_part_of_a_video_being_uploaded_is_overwritten():
    fs = fs_with(staging=True)
    fs.files[A.part_path] = SimFile(5, None, "app", NOW - timedelta(hours=30))
    plan = run(fs, make_input(fs, [want(1, A)], LIB))
    assert kinds(plan) == ["upload", "activate"]
    assert A.part_path not in fs.files


def test_corrupt_staged_file_is_replaced():
    fs = fs_with(staging=True)
    fs.files[A.staged_path] = SimFile(A.size_bytes - 1, None, "app")
    plan = run(fs, make_input(fs, [want(1, A)], LIB))
    assert kinds(plan) == ["delete_staged", "upload", "activate"]


def test_corrupt_staged_copy_does_not_block_moving_back_to_staging():
    fs = fs_with(active=[(F, 10)], staged=[A])
    fs.files[F.staged_path] = SimFile(F.size_bytes - 1, None, "app")
    plan = run(fs, make_input(fs, [want(1, A), want(2, F, fallback=True)], LIB))
    assert kinds(plan) == ["delete_staged", "activate", "deactivate"]
    assert fs.files[F.staged_path].size == F.size_bytes


def test_unneeded_staged_files_are_cleaned_unknown_files_are_left():
    fs = fs_with(active=[(A, 10)], staged=[B])
    fs.files[f"{STAGING_DIR}/notas.txt"] = SimFile(3, None, "legacy")
    plan = run(fs, make_input(fs, [want(1, A)], LIB))
    assert [(a.kind.value, a.src) for a in plan.actions] == [("delete_staged", B.staged_path)]
    assert f"{STAGING_DIR}/notas.txt" in fs.files
    assert "staging_other" in {n.code for n in plan.notices}


# --- capacity ------------------------------------------------------------------------------


def test_capacity_skips_optional_staging_first():
    fs = fs_with(active=[(A, 10)], staging=True)
    inp = make_input(
        fs,
        [want(1, A), want(2, B, start=NOW + timedelta(hours=1))],
        LIB,
        max_bytes=A.size_bytes + B.size_bytes - 1,
    )
    plan = pl.plan(inp)
    assert plan.actions == ()
    assert "capacity_skip_staging" in {n.code for n in plan.notices}


def test_staging_ahead_uses_space_freed_in_the_same_plan():
    fs = fs_with(active=[(A, 10), (C, 20)], staging=True)
    upcoming = want(2, B, start=NOW + timedelta(hours=1))
    inp = make_input(fs, [want(1, A), upcoming], LIB, max_bytes=A.size_bytes + C.size_bytes)
    plan = run(fs, inp)
    assert kinds(plan) == ["delete_active", "upload"]
    assert pl.plan(replace(inp, remote=fs.remote_state())).actions == ()


def test_capacity_blocks_uploads_that_do_not_fit():
    fs = fs_with(legacy=[("antigo.mp4", 100)])
    plan = pl.plan(make_input(fs, [want(1, D)], LIB, max_bytes=D.size_bytes))
    assert plan.actions == ()
    assert "capacity_exceeded" in {n.code for n in plan.notices}


# --- loop duration ---------------------------------------------------------------------------


def test_loop_duration_counts_what_will_play():
    fs = fs_with(active=[(A, 10)], legacy=[("antigo.mp4", 100)])
    plan = pl.plan(make_input(fs, [want(1, A), want(2, B)], LIB))
    assert plan.loop_known_s == 60.0
    assert plan.loop_unknown == 1  # legacy duration unknown
    assert plan.loop_duration_s is None


def test_every_action_has_a_portuguese_description():
    fs = fs_with(active=[(A, 10)], legacy=[("x.mp4", 1)], staged=[C])
    fs.files[f"{STAGING_DIR}/{D.sha256[:12]}.mp4.part"] = SimFile(
        1, None, "app", NOW - timedelta(days=2)
    )
    inp = make_input(
        fs,
        [want(2, B), want(3, F, fallback=True)],
        LIB,
        legacy_delete_requests=frozenset({"x.mp4"}),
    )
    plan = pl.plan(inp)
    assert plan.actions
    for action in plan.actions:
        assert pl.describe(action, plan)


def test_a_video_with_an_invalid_slug_is_skipped_not_fatal():
    broken = replace(B, slug="alianças first")
    fs = fs_with(active=[(A, 10)])
    plan = pl.plan(make_input(fs, [want(1, A), want(2, broken)], (A, broken)))
    assert [item.video.id for item in plan.desired] == [A.id]
    assert "invalid_assignment" in {n.code for n in plan.notices}


def test_videos_with_identical_content_share_one_staged_copy():
    twin = replace(B, id=7, slug="gemeo", sha256=F.sha256, size_bytes=F.size_bytes)
    fs = fs_with(active=[(F, 10), (twin, 20)], staging=True)
    library = (*LIB, twin)
    upcoming = NOW + timedelta(hours=5)
    wants = [
        want(1, A),
        want(2, F, fallback=True),
        want(3, twin, start=upcoming, fallback=True),
    ]
    plan = run(fs, make_input(fs, wants, library))
    staged = [a for a in plan.actions if a.dst == F.staged_path]
    assert len(staged) == 1  # one move/upload onto the shared staging path
    assert pl.plan(make_input(fs, wants, library)).actions == ()


def test_a_fresh_part_is_left_alone():
    fs = fs_with(active=[(A, 10)], staging=True)
    fs.files[B.part_path] = SimFile(5, None, "app", NOW - timedelta(minutes=3))
    plan = pl.plan(make_input(fs, [want(1, A), want(2, B)], LIB))
    assert plan.actions == ()
    assert "upload_in_progress" in {n.code for n in plan.notices}
    later = pl.plan(make_input(fs, [want(1, A), want(2, B)], LIB, now=NOW + timedelta(minutes=8)))
    assert kinds(later) == ["upload", "activate"]


def test_a_part_dated_in_the_future_does_not_block():
    """A Pi whose clock runs ahead must not stop uploads forever."""
    fs = fs_with(active=[(A, 10)], staging=True)
    fs.files[B.part_path] = SimFile(5, None, "app", NOW + timedelta(days=3))
    plan = pl.plan(make_input(fs, [want(1, A), want(2, B)], LIB))
    assert kinds(plan) == ["upload", "activate"]
