"""Simulator of docs/player-script.sh, for tests.

The script on each Pi runs, forever::

    for entry in $VIDEOPATH/*
    do
        omxplayer -z $entry
    done

``SimFS`` models the video folder and changes it exactly like the executor
does over FTP (STOR, SIZE check, RNFR/RNTO, DELE, MKD). ``PlayerSim`` expands
the glob like /bin/sh (visible entries only, sorted; the literal pattern when
nothing matches) and reports what omxplayer would be asked to play.

The invariant checks in ``check_invariants`` are the ones listed in CLAUDE.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime

from lj_signage.naming import ACTIVE_NAME_RE, STAGING_DIR, is_video_name
from lj_signage.reconciler.planner import Action, ActionKind, Entry, RemoteState, Video

_WORD_SPLIT = re.compile(r"[\s]")
_GLOB_CHARS = re.compile(r"[*?\[]")


class SimError(Exception):
    """An operation the real FTP server would refuse, or a clobbering rename."""


@dataclass
class SimFile:
    size: int
    content: str | None  # sha256 of the complete content, None for garbage/incomplete
    origin: str = "legacy"  # legacy | app
    mtime: datetime | None = None


@dataclass
class SimFS:
    """Files keyed by path relative to video_path ("a.mp4", ".lj-staging/x.mp4")."""

    files: dict[str, SimFile] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)  # relative dir paths ("sub", ".lj-staging")
    log: list[str] = field(default_factory=list)

    def copy(self) -> SimFS:
        return SimFS(
            files={k: replace(v) for k, v in self.files.items()},
            dirs=set(self.dirs),
            log=list(self.log),
        )

    # --- FTP-like operations ---------------------------------------------------
    def mkdir(self, path: str) -> None:
        if path in self.dirs or path in self.files:
            raise SimError(f"MKD {path}: already exists")
        self.dirs.add(path)
        self.log.append(f"MKD {path}")

    def store(self, path: str, file: SimFile) -> None:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent and parent not in self.dirs:
            raise SimError(f"STOR {path}: no such directory")
        if path in self.dirs:
            raise SimError(f"STOR {path}: is a directory")
        self.files[path] = file  # STOR truncates/overwrites
        self.log.append(f"STOR {path}")

    def rename(self, src: str, dst: str) -> None:
        if src not in self.files:
            raise SimError(f"RNFR {src}: no such file")
        if dst in self.files or dst in self.dirs:
            # vsftpd would silently replace it: the planner must never do this.
            raise SimError(f"RNTO {dst}: destination exists (would clobber)")
        parent = dst.rsplit("/", 1)[0] if "/" in dst else ""
        if parent and parent not in self.dirs:
            raise SimError(f"RNTO {dst}: no such directory")
        self.files[dst] = self.files.pop(src)
        self.log.append(f"RNFR {src} RNTO {dst}")

    def delete(self, path: str) -> None:
        if path not in self.files:
            raise SimError(f"DELE {path}: no such file")
        del self.files[path]
        self.log.append(f"DELE {path}")

    # --- views -----------------------------------------------------------------------
    def remote_state(self) -> RemoteState:
        root = [
            Entry(name, size=f.size, mtime=f.mtime)
            for name, f in self.files.items()
            if "/" not in name
        ]
        root += [Entry(d, is_dir=True) for d in self.dirs if "/" not in d]
        prefix = STAGING_DIR + "/"
        staging = [
            Entry(name[len(prefix) :], size=f.size, mtime=f.mtime)
            for name, f in self.files.items()
            if name.startswith(prefix) and "/" not in name[len(prefix) :]
        ]
        staging += [
            Entry(d[len(prefix) :], is_dir=True)
            for d in self.dirs
            if d.startswith(prefix) and "/" not in d[len(prefix) :]
        ]
        return RemoteState(
            root=tuple(sorted(root, key=lambda e: e.name)),
            staging_exists=STAGING_DIR in self.dirs,
            staging=tuple(sorted(staging, key=lambda e: e.name)),
        )

    def visible(self) -> list[str]:
        names = [n for n in self.files if "/" not in n] + [d for d in self.dirs if "/" not in d]
        # Byte order. Prefixes are unique per device, so locale collation gives
        # the same order for the app's files.
        return sorted(n for n in names if not n.startswith("."))


def apply_action(fs: SimFS, action: Action, videos: dict[int, Video], now: datetime) -> None:
    """Apply one planner action the way the executor does over FTP."""
    kind = action.kind
    if kind is ActionKind.MKDIR_STAGING:
        fs.mkdir(action.dst)
    elif kind is ActionKind.UPLOAD:
        video = videos[action.video_id]
        fs.store(video.part_path, SimFile(video.size_bytes, video.sha256, "app", now))
        if fs.files[video.part_path].size != video.size_bytes:  # SIZE check
            raise SimError("size mismatch")
        fs.rename(video.part_path, action.dst)
    elif kind in (ActionKind.ACTIVATE, ActionKind.REORDER, ActionKind.DEACTIVATE):
        fs.rename(action.src, action.dst)
    elif kind in (
        ActionKind.DELETE_ACTIVE,
        ActionKind.DELETE_LEGACY,
        ActionKind.DELETE_STAGED,
        ActionKind.DELETE_PART,
    ):
        fs.delete(action.src)
    else:  # pragma: no cover
        raise AssertionError(kind)


@dataclass(frozen=True)
class PlayAttempt:
    argv: tuple[str, ...]  # what omxplayer receives after word splitting
    entry: str
    ok: bool
    problem: str | None = None


class PlayerSim:
    """What one pass of the carousel would try to play, given the folder state."""

    def __init__(self, videopath: str = "/home/tmagalhaes/Videos") -> None:
        self.videopath = videopath

    def carousel(self, fs: SimFS, library: dict[str, Video]) -> list[PlayAttempt]:
        entries = fs.visible()
        if not entries:
            literal = f"{self.videopath}/*"
            return [PlayAttempt((literal,), "*", False, "pasta vazia: omxplayer recebe '*'")]
        attempts = []
        for name in entries:
            path = f"{self.videopath}/{name}"
            argv = tuple(part for part in _WORD_SPLIT.split(path) if part)
            if name in fs.dirs:
                attempts.append(PlayAttempt(argv, name, False, "é uma pasta"))
            elif len(argv) != 1 or _GLOB_CHARS.search(name):
                attempts.append(PlayAttempt(argv, name, False, "nome partido pela shell"))
            elif not is_video_name(name):
                attempts.append(PlayAttempt(argv, name, False, "não é vídeo"))
            else:
                file = fs.files[name]
                if file.origin == "app":
                    video = library.get(file.content or "")
                    if video is None or file.size != video.size_bytes:
                        attempts.append(PlayAttempt(argv, name, False, "ficheiro incompleto"))
                        continue
                if file.size == 0:
                    attempts.append(PlayAttempt(argv, name, False, "ficheiro vazio"))
                    continue
                attempts.append(PlayAttempt(argv, name, True))
        return attempts

    def playable(self, fs: SimFS, library: dict[str, Video]) -> list[str]:
        return [a.entry for a in self.carousel(fs, library) if a.ok]


def check_invariants(
    fs: SimFS,
    library_by_sha: dict[str, Video],
    *,
    initial_legacy: set[str],
    require_playable: bool,
) -> list[str]:
    """Violations of the CLAUDE.md invariants in the current folder state."""
    problems = []
    player = PlayerSim()
    if require_playable and not player.playable(fs, library_by_sha):
        problems.append("a pasta ficou sem nenhum vídeo que o leitor consiga reproduzir")
    for name in fs.visible():
        if name in fs.dirs:
            if name not in initial_legacy:
                problems.append(f"pasta visível criada pela app: {name}")
            continue
        file = fs.files[name]
        if file.origin != "app":
            continue
        if not ACTIVE_NAME_RE.match(name):
            problems.append(f"nome ativo inválido: {name}")
        video = library_by_sha.get(file.content or "")
        if video is None or file.size != video.size_bytes:
            problems.append(f"ficheiro visível incompleto ou não verificado: {name}")
    for path in fs.files:
        if path.endswith(".part") and not path.startswith(STAGING_DIR + "/"):
            problems.append(f"ficheiro temporário visível: {path}")
    return problems
