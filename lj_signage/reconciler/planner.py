"""Reconciliation planner — a pure function (CLAUDE.md, rule 4).

``plan(PlannerInput) -> Plan`` turns *what should be playing* (the schedule)
and *what is on the Pi* (a listing) into an ordered list of FTP actions. It
does no I/O and reads no clock, so its properties can be tested exhaustively
(tests/test_planner*.py, together with tests/player_sim.py).

The action order (SPEC §6.5) keeps the invariants after *every* action, not
only at the end, because the executor stops at the first error:

0. staging cleanup (incomplete or unneeded staged files, ``.part`` older than
   24 h) comes first: it is hidden, frees space, and guarantees that no upload
   or rename lands on an existing name;
1. uploads of what must play now go to the hidden staging folder
   (``.part`` → size check → rename);
2. activations rename staged files into the video folder;
3. reorders rename only the files whose prefix changed — prefixes are chosen
   to keep as many current names as possible (each rename makes the player
   skip that video once);
4. removals of app files, and of legacy files a user asked to delete, come
   after the activations and are all skipped if they would leave the folder
   without a playable video;
5. with a tight ``max_bytes``, what must play now but only fits once old
   content is gone is uploaded and activated after the removals, and only if
   something else keeps playing meanwhile;
6. staging ahead of time (the fallback, what starts within the lookahead)
   comes last: nothing plays from it yet.

Paths in actions are relative to the device's video folder.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from ..naming import (
    MANAGED_ACTIVE_RE,
    PART_RE,
    SLUG_MAX,
    STAGED_RE,
    STAGING_DIR,
    active_name,
    is_hidden,
    is_playable_legacy,
    legacy_issues,
    part_name,
    staged_name,
    staging_path,
)

# A slug that cannot produce a player-safe active name is never planned.
_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class ActionKind(StrEnum):
    MKDIR_STAGING = "mkdir_staging"
    UPLOAD = "upload"
    ACTIVATE = "activate"
    REORDER = "reorder"
    DEACTIVATE = "deactivate"  # active -> staging: out of the loop, kept for later
    DELETE_ACTIVE = "delete_active"
    DELETE_LEGACY = "delete_legacy"
    DELETE_STAGED = "delete_staged"
    DELETE_PART = "delete_part"


# --- inputs -------------------------------------------------------------------


@dataclass(frozen=True)
class Video:
    id: int
    slug: str
    sha256: str
    size_bytes: int
    title: str = ""
    duration_s: float | None = None

    @property
    def sha12(self) -> str:
        return self.sha256[:12]

    @property
    def staged_path(self) -> str:
        return staging_path(staged_name(self.sha256))

    @property
    def part_path(self) -> str:
        return staging_path(part_name(self.sha256))

    def active_name(self, nnn: int) -> str:
        return active_name(nnn, self.slug, self.sha256)


@dataclass(frozen=True)
class Want:
    """An assignment that applies to this device."""

    assignment_id: int
    video: Video
    start_at: datetime
    end_at: datetime | None = None
    position: int = 100
    is_fallback: bool = False

    def is_active(self, now: datetime) -> bool:
        return self.start_at <= now and (self.end_at is None or now < self.end_at)


@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool = False
    size: int | None = None
    mtime: datetime | None = None


@dataclass(frozen=True)
class RemoteState:
    root: tuple[Entry, ...] = ()
    staging_exists: bool = False
    staging: tuple[Entry, ...] = ()


@dataclass(frozen=True)
class PlannerInput:
    now: datetime
    remote: RemoteState
    wants: tuple[Want, ...] = ()
    library: tuple[Video, ...] = ()  # every video the app produced, to recognise its files
    legacy_delete_requests: frozenset[str] = frozenset()
    lookahead: timedelta = timedelta(hours=48)
    part_max_age: timedelta = timedelta(hours=24)
    # A .part written to less than this long ago may still be receiving data
    # (another process whose lease expired): its video is not uploaded now.
    part_busy_window: timedelta = timedelta(minutes=10)
    max_bytes: int | None = None


# --- outputs ------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    src: str | None = None
    dst: str | None = None
    video_id: int | None = None
    size: int | None = None


@dataclass(frozen=True)
class Notice:
    code: str
    message: str
    level: str = "warning"  # info | warning | error
    video_id: int | None = None


@dataclass(frozen=True)
class DesiredItem:
    video: Video
    nnn: int
    name: str
    want: Want


@dataclass(frozen=True)
class LegacyFile:
    name: str
    is_dir: bool
    size: int | None
    issues: tuple[str, ...]
    playable: bool
    delete_requested: bool


@dataclass(frozen=True)
class Classified:
    """What each remote entry is, from the app's point of view."""

    managed: Mapping[int, tuple[tuple[int, Entry], ...]]  # video id -> (prefix, entry), by name
    legacy: tuple[LegacyFile, ...]
    staged: Mapping[str, Entry]  # sha12 -> complete staged file of a library video
    corrupt_staged: tuple[Entry, ...]  # staged name of a library video, wrong size
    orphan_staged: tuple[Entry, ...]  # staged name, unknown video
    parts: tuple[Entry, ...]
    staging_other: tuple[Entry, ...]  # not created by the app: left alone
    hidden_root: tuple[Entry, ...]
    videos_by_id: Mapping[int, Video]

    def root_state(self, name: str) -> tuple[str, int | None, tuple[str, ...]]:
        """(state, video id, issues) of a root entry, for DeviceFile rows."""
        for video_id, copies in self.managed.items():
            if any(entry.name == name for _, entry in copies):
                return "active", video_id, ()
        for legacy in self.legacy:
            if legacy.name == name:
                return "legacy", None, legacy.issues
        return "other", None, ()

    def staging_state(self, name: str) -> tuple[str, int | None]:
        for sha12, entry in self.staged.items():
            if entry.name == name:
                video = next((v for v in self.videos_by_id.values() if v.sha12 == sha12), None)
                return "staged", video.id if video else None
        if any(entry.name == name for entry in self.parts):
            return "partial", None
        return "other", None


@dataclass(frozen=True)
class Plan:
    actions: tuple[Action, ...]
    notices: tuple[Notice, ...]
    desired: tuple[DesiredItem, ...]  # what plays after the plan (app files), in order
    keep_staged: tuple[Video, ...]
    classified: Classified
    using_fallback: bool
    loop_known_s: float  # summed known durations of what will play
    loop_unknown: int  # playable files of unknown duration (legacy)
    current_bytes: int
    projected_bytes: int  # peak usage while the plan runs

    @property
    def is_empty(self) -> bool:
        return not self.actions

    @property
    def loop_duration_s(self) -> float | None:
        return self.loop_known_s if self.loop_unknown == 0 else None

    @property
    def digest(self) -> str:
        payload = [(a.kind.value, a.src, a.dst, a.video_id) for a in self.actions]
        return hashlib.sha256(json.dumps(payload).encode()).hexdigest()

    def title(self, video_id: int | None) -> str:
        video = self.classified.videos_by_id.get(video_id) if video_id is not None else None
        return (video.title or video.slug) if video else "?"


# --- classification -------------------------------------------------------------


def classify(
    remote: RemoteState,
    library: Iterable[Video],
    legacy_delete_requests: frozenset[str] = frozenset(),
) -> Classified:
    videos_by_id: dict[int, Video] = {}
    by_key: dict[tuple[str, str], list[Video]] = {}
    by_sha12: dict[str, list[Video]] = {}
    for video in library:
        videos_by_id[video.id] = video
        by_key.setdefault((video.slug, video.sha256[:6]), []).append(video)
        by_sha12.setdefault(video.sha12, []).append(video)

    managed: dict[int, list[tuple[int, Entry]]] = {}
    legacy: list[LegacyFile] = []
    hidden: list[Entry] = []
    for entry in sorted(remote.root, key=lambda e: e.name):
        if is_hidden(entry.name):
            hidden.append(entry)
            continue
        issues = legacy_issues(entry.name, is_dir=entry.is_dir, size=entry.size)
        match = None if entry.is_dir else MANAGED_ACTIVE_RE.match(entry.name)
        if match:
            owners = by_key.get((match["slug"], match["sha6"]), [])
            video = next((v for v in owners if v.size_bytes == entry.size), None)
            if video is not None:
                managed.setdefault(video.id, []).append((int(match["nnn"]), entry))
                continue
            if owners:
                issues.append("size_mismatch")
        legacy.append(
            LegacyFile(
                name=entry.name,
                is_dir=entry.is_dir,
                size=entry.size,
                issues=tuple(issues),
                playable=is_playable_legacy(issues) and "size_mismatch" not in issues,
                delete_requested=entry.name in legacy_delete_requests,
            )
        )

    staged: dict[str, Entry] = {}
    corrupt: list[Entry] = []
    orphan: list[Entry] = []
    parts: list[Entry] = []
    other: list[Entry] = []
    for entry in sorted(remote.staging, key=lambda e: e.name):
        if entry.is_dir or is_hidden(entry.name):
            other.append(entry)
            continue
        if match := STAGED_RE.match(entry.name):
            owners = by_sha12.get(match["sha12"], [])
            if any(v.size_bytes == entry.size for v in owners):
                staged[match["sha12"]] = entry
            elif owners:
                corrupt.append(entry)
            else:
                orphan.append(entry)
        elif PART_RE.match(entry.name):
            parts.append(entry)
        else:
            other.append(entry)

    return Classified(
        managed={vid: tuple(copies) for vid, copies in managed.items()},
        legacy=tuple(legacy),
        staged=staged,
        corrupt_staged=tuple(corrupt),
        orphan_staged=tuple(orphan),
        parts=tuple(parts),
        staging_other=tuple(other),
        hidden_root=tuple(hidden),
        videos_by_id=videos_by_id,
    )


# --- prefixes -------------------------------------------------------------------


def assign_prefixes(
    order: Sequence[int], current: Mapping[int, int], *, step: int = 10
) -> dict[int, int]:
    """Pick a 3-digit prefix (1..999) per key, strictly increasing along ``order``.

    Keeps the largest possible number of current prefixes. A key at index i
    with prefix p can keep it only if there is room for the keys around it,
    i.e. ``p - i`` is non-decreasing among kept keys and within [1, 1000 - n];
    the largest such set is a longest non-decreasing subsequence. New or moved
    keys are then spread over the gaps (steps of 10 at the end, like 010, 020…).
    """
    n = len(order)
    if n == 0:
        return {}
    if n > 999:
        raise ValueError("at most 999 videos per device")
    candidates = [
        (index, current[key] - index)
        for index, key in enumerate(order)
        if key in current and 1 <= current[key] - index <= 1000 - n
    ]
    kept = _longest_non_decreasing(candidates)

    result: dict[int, int] = {order[i]: current[order[i]] for i in kept}
    pending: list[int] = []
    low = 0
    for index, key in enumerate(order):
        if index in kept:
            _fill(pending, low, result[key], result, step, open_end=False)
            pending = []
            low = result[key]
        else:
            pending.append(key)
    _fill(pending, low, 1000, result, step, open_end=True)
    return result


def _longest_non_decreasing(items: list[tuple[int, int]]) -> set[int]:
    tail_keys: list[int] = []
    tail_pos: list[int] = []
    previous = [-1] * len(items)
    for pos, (_, key) in enumerate(items):
        slot = bisect.bisect_right(tail_keys, key)
        if slot > 0:
            previous[pos] = tail_pos[slot - 1]
        if slot == len(tail_keys):
            tail_keys.append(key)
            tail_pos.append(pos)
        else:
            tail_keys[slot] = key
            tail_pos[slot] = pos
    kept: set[int] = set()
    pos = tail_pos[-1] if tail_pos else -1
    while pos != -1:
        kept.add(items[pos][0])
        pos = previous[pos]
    return kept


def _fill(keys: list[int], low: int, high: int, result: dict, step: int, *, open_end: bool):
    count = len(keys)
    if not count:
        return
    if open_end and low + step * count <= 999:
        values = [low + step * (j + 1) for j in range(count)]
    else:
        span = high - low  # >= count + 1, guaranteed by assign_prefixes
        values = [low + span * (j + 1) // (count + 1) for j in range(count)]
    result.update(zip(keys, values, strict=True))


# --- the planner ------------------------------------------------------------------


def _nearby(wanted: int, low: int, high: int):
    """Prefixes strictly between low and high, closest to the wanted one first."""
    for delta in range(1, 1000):
        for candidate in (wanted + delta, wanted - delta):
            if low < candidate < high and 1 <= candidate <= 999:
                yield candidate
        if wanted - delta <= low and wanted + delta >= high:
            return


def _order_key(want: Want) -> tuple:
    return (want.position, want.start_at, want.assignment_id)


def plan(inp: PlannerInput) -> Plan:
    now = inp.now
    c = classify(inp.remote, inp.library, inp.legacy_delete_requests)
    notices: list[Notice] = []

    # 1. What should play now: regular assignments, or the fallback if none.
    wants = [w for w in inp.wants if _valid_want(w, notices)]
    active = [w for w in wants if w.is_active(now)]
    regular = sorted((w for w in active if not w.is_fallback), key=_order_key)
    fallbacks = sorted((w for w in active if w.is_fallback), key=_order_key)
    using_fallback = not regular and bool(fallbacks)
    desired_wants = _dedupe(regular or fallbacks, notices)
    desired_ids = {w.video.id for w in desired_wants}
    if not fallbacks:
        notices.append(
            Notice(
                "no_fallback",
                "Sem vídeo de reserva para esta loja: quando a programação acabar, "
                "fica a passar o que já lá estiver.",
            )
        )
    if not desired_wants:
        notices.append(Notice("nothing_scheduled", "Não há nada programado para agora."))
    elif using_fallback:
        notices.append(
            Notice("fallback_in_use", "Sem campanhas a decorrer: passa o vídeo de reserva.", "info")
        )

    # 2. What to keep ready (hidden) in staging: the fallback, and what starts soon.
    upcoming = sorted(
        (w for w in wants if now < w.start_at <= now + inp.lookahead),
        key=lambda w: (w.start_at, *_order_key(w)),
    )
    # The staging path comes from the content (sha256), so videos with identical
    # content share it: keep one staged copy per content, and none for content
    # that is about to play anyway.
    desired_shas = {w.video.sha12 for w in desired_wants}
    keep_staged: list[Video] = []
    for video in [w.video for w in fallbacks] + [w.video for w in upcoming]:
        if video.id in desired_ids or video.sha12 in desired_shas:
            continue
        if all(v.id != video.id and v.sha12 != video.sha12 for v in keep_staged):
            keep_staged.append(video)
    keep_ids = {v.id for v in keep_staged}
    busy = {
        e.name[:12]
        for e in c.parts
        if e.mtime is not None and timedelta(0) <= now - e.mtime < inp.part_busy_window
    }

    def missing(video: Video) -> bool:
        return video.id not in c.managed and video.sha12 not in c.staged

    # 3. Target names, keeping current prefixes where possible. Extra copies of a
    #    video that keeps playing are deleted before the reorders, so their names
    #    do not block a rename.
    primary = {vid: copies[0][1] for vid, copies in c.managed.items()}
    prefixes = assign_prefixes(
        [w.video.id for w in desired_wants],
        {w.video.id: c.managed[w.video.id][0][0] for w in desired_wants if w.video.id in c.managed},
    )
    duplicates = {entry.name: vid for vid in desired_ids for _, entry in c.managed.get(vid, ())[1:]}
    occupied = {e.name for e in inp.remote.root} - set(duplicates)
    desired: list[DesiredItem] = []
    taken: set[str] = set()

    def clashes(name: str, current: Entry | None) -> bool:
        if name in taken:
            return True
        return name in occupied and (current is None or current.name != name)

    for index, want in enumerate(desired_wants):
        video = want.video
        current = primary.get(video.id)
        if current is None and video.sha12 in busy and missing(video):
            notices.append(
                Notice(
                    "upload_in_progress",
                    f"Envio de «{video.title or video.slug}» em curso ou interrompido há "
                    "pouco: é retomado no próximo ciclo.",
                    "info",
                    video.id,
                )
            )
            continue
        wanted = prefixes[video.id]
        name = video.active_name(wanted)
        if clashes(name, current):
            # Same place in the order, another prefix: a legacy file with the very
            # name the app would use never blocks a video.
            low = desired[-1].nnn if desired else 0
            following = [prefixes[w.video.id] for w in desired_wants[index + 1 :]]
            high = following[0] if following else 1000
            name = next(
                (
                    video.active_name(p)
                    for p in _nearby(wanted, low, high)
                    if not clashes(video.active_name(p), current)
                ),
                None,
            )
            if name is None:
                if current is None:
                    notices.append(
                        Notice(
                            "name_conflict",
                            f"«{video.title or video.slug}» não é ativado: não há nome livre "
                            "nessa posição da ordem.",
                            "error",
                            video.id,
                        )
                    )
                    continue
                name = current.name  # keep the current name this time
        taken.add(name)
        desired.append(DesiredItem(video=video, nnn=int(name[:3]), name=name, want=want))
    placed_ids = {item.video.id for item in desired}
    to_activate = {item.video.sha12 for item in desired if item.video.id not in c.managed}
    # A staged copy of something that should play but could not be placed now is kept.
    waiting = {w.video.sha12 for w in desired_wants if w.video.id not in placed_ids}

    # 4. Actions, in the order that preserves the invariants.
    actions: list[Action] = []
    staging_ready = inp.remote.staging_exists
    staged_now = set(c.staged)
    activated: set[str] = set()
    uploaded: set[str] = set()

    def ensure_staging() -> None:
        nonlocal staging_ready
        if not staging_ready:
            actions.append(Action(ActionKind.MKDIR_STAGING, dst=STAGING_DIR))
            staging_ready = True

    def upload(video: Video) -> None:
        ensure_staging()
        actions.append(
            Action(
                ActionKind.UPLOAD, dst=video.staged_path, video_id=video.id, size=video.size_bytes
            )
        )
        staged_now.add(video.sha12)
        uploaded.add(video.sha12)

    def activate(item: DesiredItem) -> None:
        actions.append(
            Action(
                ActionKind.ACTIVATE,
                src=item.video.staged_path,
                dst=item.name,
                video_id=item.video.id,
                size=item.video.size_bytes,
            )
        )
        staged_now.discard(item.video.sha12)
        activated.add(item.video.sha12)

    # 4.0 staging cleanup first: it is hidden, so it is always safe, it frees
    #     space for the uploads, and no later upload or rename may land on the
    #     name of an incomplete copy.
    needed = {v.sha12 for v in keep_staged} | to_activate | waiting
    cleanup = [staging_path(e.name) for e in c.corrupt_staged]
    cleanup += [
        staging_path(e.name) for sha12, e in sorted(c.staged.items()) if sha12 not in needed
    ]
    cleanup += [staging_path(e.name) for e in c.orphan_staged]
    for path in cleanup:
        actions.append(Action(ActionKind.DELETE_STAGED, src=path))
    required = [item.video for item in desired if missing(item.video)]
    required_shas = {v.sha12 for v in required}
    for entry in c.parts:
        stale = entry.mtime is not None and now - entry.mtime > inp.part_max_age
        if stale and entry.name[:12] not in required_shas:
            actions.append(Action(ActionKind.DELETE_PART, src=staging_path(entry.name)))
    staged_now -= {path.rsplit("/", 1)[1][:12] for path in cleanup}
    if c.staging_other:
        notices.append(
            Notice(
                "staging_other",
                f"{len(c.staging_other)} elemento(s) desconhecido(s) na pasta de preparação "
                "(não são apagados).",
                "info",
            )
        )

    # Capacity (max_bytes). What must play now but only fits once old content
    # is removed goes after the removals ("late"), and only if something else
    # keeps playing meanwhile; staging ahead of time comes last.
    sizes = {e.name: e.size or 0 for e in inp.remote.root if not e.is_dir}
    sizes.update({staging_path(e.name): e.size or 0 for e in inp.remote.staging if not e.is_dir})
    current_bytes = sum(sizes.values())

    def usage() -> int:
        freed = sum(sizes.get(a.src or "", 0) for a in actions if a.kind in _DELETE_KINDS)
        added = sum(a.size or 0 for a in actions if a.kind is ActionKind.UPLOAD)
        return current_bytes - freed + added

    late: set[int] = set()
    if inp.max_bytes is not None:
        base = usage()
        while required and base + sum(v.size_bytes for v in required) > inp.max_bytes:
            late.add(required.pop().id)
    on_time = [item for item in desired if item.video.id not in late]
    later = [item for item in desired if item.video.id in late]

    # 4.1 uploads (hidden) of what plays now
    for video in required:
        if video.sha12 not in staged_now:
            upload(video)
    peak = usage()

    # 4.2 activations
    for item in on_time:
        if item.video.id not in c.managed:
            activate(item)

    # 4.3 extra copies of videos that keep playing (their main copy stays visible)
    for name, vid in sorted(duplicates.items()):
        actions.append(Action(ActionKind.DELETE_ACTIVE, src=name, video_id=vid))

    # 4.4 reorders
    for item in desired:
        current = primary.get(item.video.id)
        if current is not None and current.name != item.name:
            actions.append(
                Action(ActionKind.REORDER, src=current.name, dst=item.name, video_id=item.video.id)
            )

    # 4.5 removals, only if something playable remains afterwards
    removals: list[Action] = []
    deactivating: set[str] = set()
    for video_id, copies in sorted(c.managed.items()):
        if video_id in placed_ids:
            continue
        video = c.videos_by_id[video_id]
        for index, (_, entry) in enumerate(copies):
            if (
                index == 0
                and video_id in keep_ids
                and video.sha12 not in staged_now
                and video.sha12 not in deactivating
            ):
                removals.append(
                    Action(
                        ActionKind.DEACTIVATE,
                        src=entry.name,
                        dst=video.staged_path,
                        video_id=video_id,
                    )
                )
                deactivating.add(video.sha12)
            else:
                removals.append(Action(ActionKind.DELETE_ACTIVE, src=entry.name, video_id=video_id))
    for legacy in c.legacy:
        if not legacy.delete_requested:
            continue
        if legacy.is_dir:
            message = f"«{legacy.name}» é uma pasta: a app não apaga pastas."
            notices.append(Notice("legacy_dir", message, "error"))
            continue
        removals.append(Action(ActionKind.DELETE_LEGACY, src=legacy.name))

    removed = {a.src for a in removals}
    playable_after = len(on_time) + sum(
        1 for legacy in c.legacy if legacy.playable and legacy.name not in removed
    )
    if removals and playable_after == 0:
        notices.append(
            Notice(
                "keep_last_videos",
                "Nada ficaria a passar: não retiro nenhum vídeo até haver programação "
                "ou vídeo de reserva.",
            )
        )
        removals = []
        deactivating = set()
    for action in removals:
        if action.kind is ActionKind.DEACTIVATE:
            ensure_staging()
        actions.append(action)
    staged_now |= deactivating

    # 4.6 what must play now but only fits in the space freed above
    for item in later:
        video = item.video
        if playable_after == 0 or usage() + video.size_bytes > inp.max_bytes:
            desired.remove(item)
            notices.append(
                Notice(
                    "capacity_exceeded",
                    f"Sem espaço no Pi para «{video.title or video.slug}»: não é enviado.",
                    "error",
                    video.id,
                )
            )
            continue
        upload(video)
        activate(item)

    # 4.7 staging ahead of time (fallback first, then by start), last: nothing
    #     plays from it yet, and it can use the space freed above.
    for video in keep_staged:
        if not missing(video) or video.sha12 in staged_now or video.sha12 in busy:
            continue
        if inp.max_bytes is not None and usage() + video.size_bytes > inp.max_bytes:
            notices.append(
                Notice(
                    "capacity_skip_staging",
                    f"Sem espaço para preparar «{video.title or video.slug}» com antecedência.",
                    video_id=video.id,
                )
            )
            continue
        upload(video)

    # Loop duration after the plan (SPEC §6: "entra no ar até HH:MM").
    removed = {a.src for a in actions if a.kind is ActionKind.DELETE_LEGACY}
    kept_legacy = [lf for lf in c.legacy if lf.playable and lf.name not in removed]
    loop_known = sum(item.video.duration_s or 0.0 for item in desired)
    loop_unknown = sum(1 for item in desired if item.video.duration_s is None) + len(kept_legacy)

    return Plan(
        actions=tuple(actions),
        notices=tuple(notices),
        desired=tuple(desired),
        keep_staged=tuple(keep_staged),
        classified=c,
        using_fallback=using_fallback,
        loop_known_s=loop_known,
        loop_unknown=loop_unknown,
        current_bytes=current_bytes,
        projected_bytes=max(peak, usage()),
    )


_DELETE_KINDS = frozenset(
    {
        ActionKind.DELETE_ACTIVE,
        ActionKind.DELETE_LEGACY,
        ActionKind.DELETE_STAGED,
        ActionKind.DELETE_PART,
    }
)


def _valid_want(want: Want, notices: list[Notice]) -> bool:
    video = want.video
    ok = (
        len(video.sha256) == 64
        and all(ch in "0123456789abcdef" for ch in video.sha256)
        and video.size_bytes > 0
        and len(video.slug) <= SLUG_MAX
        and _SLUG_RE.fullmatch(video.slug) is not None
        and (want.end_at is None or want.end_at > want.start_at)
    )
    if not ok:
        notices.append(
            Notice(
                "invalid_assignment",
                f"Programação #{want.assignment_id} ignorada (vídeo ou período inválido).",
                "error",
                video.id,
            )
        )
    return ok


def _dedupe(wants: list[Want], notices: list[Notice]) -> list[Want]:
    seen_ids: set[int] = set()
    seen_sha: set[str] = set()
    result = []
    for want in wants:
        video = want.video
        if video.id in seen_ids:
            continue
        if video.sha256 in seen_sha:
            notices.append(
                Notice(
                    "duplicate_content",
                    f"«{video.title or video.slug}» é igual a outro vídeo programado: "
                    "passa uma vez.",
                    "info",
                    video.id,
                )
            )
            continue
        seen_ids.add(video.id)
        seen_sha.add(video.sha256)
        result.append(want)
    return result


# --- descriptions for the panel ---------------------------------------------------------


def describe(action: Action, plan: Plan) -> str:
    """One-line Portuguese description of an action, for the Plan page and the log."""
    title = plan.title(action.video_id)
    kind = action.kind
    if kind is ActionKind.MKDIR_STAGING:
        return "Criar a pasta de preparação (oculta, o leitor não a vê)."
    if kind is ActionKind.UPLOAD:
        return f"Enviar «{title}» para a preparação (oculto)."
    if kind is ActionKind.ACTIVATE:
        return f"Pôr «{title}» a passar ({action.dst})."
    if kind is ActionKind.REORDER:
        return f"Mudar a ordem de «{title}» ({action.src} → {action.dst})."
    if kind is ActionKind.DEACTIVATE:
        return f"Retirar «{title}» da montra e guardá-lo na preparação (volta a passar em breve)."
    if kind is ActionKind.DELETE_ACTIVE:
        return f"Retirar «{title}» (apagar {action.src})."
    if kind is ActionKind.DELETE_LEGACY:
        return f"Apagar o ficheiro legado {action.src} (pedido no painel)."
    if kind is ActionKind.DELETE_STAGED:
        return f"Limpar da preparação: {action.src}."
    if kind is ActionKind.DELETE_PART:
        return f"Apagar envio incompleto antigo: {action.src}."
    return f"{kind.value} {action.src or ''} {action.dst or ''}".strip()


__all__ = [
    "Action",
    "ActionKind",
    "Classified",
    "DesiredItem",
    "Entry",
    "LegacyFile",
    "Notice",
    "Plan",
    "PlannerInput",
    "RemoteState",
    "Video",
    "Want",
    "assign_prefixes",
    "classify",
    "describe",
    "plan",
]
