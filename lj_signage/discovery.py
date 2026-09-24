"""Read-only discovery of a Pi's video folder (SPEC §7).

Never writes to the Pi: it only uses PWD, CWD, LIST/MLSD, SIZE and RETR. The
result is shown in the panel and an admin must confirm it before it is used,
unless the path comes from config/devices.yaml (then it is validated and used).
"""

from __future__ import annotations

import io
import posixpath
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .ftp import FtpClient, FtpOperationError
from .naming import STAGING_DIR, is_hidden, is_video_name

MAX_SCRIPT_BYTES = 64 * 1024
MAX_DIRS_SCANNED = 200

_VIDEOPATH_RE = re.compile(
    r"""^\s*(?:export\s+)?VIDEOPATH\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s#;"']+))"""
)


@dataclass
class Candidate:
    ftp_path: str
    source: str  # config | script | scan
    evidence: list[str] = field(default_factory=list)
    video_count: int = 0
    other_count: int = 0
    sample: list[str] = field(default_factory=list)
    has_staging: bool = False


@dataclass
class DiscoveryResult:
    status: str  # found | ambiguous | not_found | error
    candidates: list[Candidate]
    at: str
    login_dir: str | None = None
    chroot: bool | None = None
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    server: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> DiscoveryResult:
        data = dict(data)
        data["candidates"] = [Candidate(**c) for c in data.get("candidates", [])]
        return cls(**data)

    @property
    def summary(self) -> str:
        if self.status == "found":
            return f"Pasta encontrada: {self.candidates[0].ftp_path}"
        if self.status == "ambiguous":
            return f"{len(self.candidates)} pastas possíveis: escolha a correta."
        if self.status == "not_found":
            return "Não foi encontrada nenhuma pasta de vídeos."
        return self.error or "Erro na descoberta."


class _Resolver:
    """Maps a path from the player script to an FTP path (SPEC §7.4)."""

    def __init__(self, client: FtpClient, login_home: str, login_dir: str) -> None:
        self.client = client
        self.login_home = login_home.rstrip("/") or "/"
        self.login_dir = login_dir

    def resolve(self, path: str) -> str | None:
        path = path.rstrip("/") or "/"
        if not path.startswith("/"):
            return self.client.canonical_dir(posixpath.join(self.login_dir, path))
        found = self.client.canonical_dir(path)
        if found is not None:
            return found
        # FTP in chroot: /home/tmagalhaes/Videos -> Videos (relative to the login dir)
        home = self.login_home
        if path == home or path.startswith(home + "/"):
            relative = path[len(home) :].lstrip("/")
            target = posixpath.join(self.login_dir, relative) if relative else self.login_dir
            return self.client.canonical_dir(target)
        return None


def expand_videopath(value: str, login_home: str) -> str | None:
    """Expand $HOME, ${HOME} and ~ like the shell would. None if other variables remain."""
    value = value.replace("${HOME}", login_home).replace("$HOME", login_home)
    if value == "~" or value.startswith("~/"):
        value = login_home + value[1:]
    if "$" in value or "`" in value:
        return None
    return value.rstrip("/") or "/"


def extract_videopaths(text: str) -> list[tuple[int, str]]:
    """(line number, raw value) of every active VIDEOPATH= assignment."""
    found = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        match = _VIDEOPATH_RE.match(line)
        if match:
            value = next(group for group in match.groups() if group is not None)
            found.append((lineno, value))
    return found


def discover(
    client: FtpClient,
    *,
    login_home: str,
    now: datetime,
    config_video_path: str | None = None,
) -> DiscoveryResult:
    """Run the discovery steps of SPEC §7 on an open (read-only) FTP session."""
    notes: list[str] = []
    login_dir = client.pwd()
    home = login_home.rstrip("/") or "/"
    chroot = login_dir == "/" and home != "/"
    if chroot:
        notes.append(f"O FTP está em chroot: a raiz do FTP corresponde a {home}.")
    resolver = _Resolver(client, home, login_dir)

    def result(status: str, candidates: list[Candidate], error: str | None = None):
        return DiscoveryResult(
            status=status,
            candidates=candidates,
            at=now.isoformat(),
            login_dir=login_dir,
            chroot=chroot,
            notes=notes,
            error=error,
            server=client.welcome or None,
        )

    # 1. Path set in devices.yaml: validate and use.
    if config_video_path:
        resolved = resolver.resolve(config_video_path)
        if resolved is None:
            return result(
                "error",
                [],
                f"A pasta definida em devices.yaml ({config_video_path}) não existe "
                "ou não é acessível por FTP.",
            )
        candidate = Candidate(
            resolved, "config", [f"Definida em config/devices.yaml: {config_video_path}"]
        )
        _inspect(client, candidate)
        return result("found", [candidate])

    # 2. Player scripts (*.sh) in the login dir and one level below.
    candidates: dict[str, Candidate] = {}
    for script_path, size in _find_scripts(client, login_dir, notes):
        if size is not None and size > MAX_SCRIPT_BYTES:
            notes.append(f"{script_path} ignorado: maior do que 64 KB.")
            continue
        buffer = io.BytesIO()
        try:
            client.retrieve(script_path, buffer)
        except FtpOperationError as exc:
            notes.append(f"Não foi possível ler {script_path} ({exc}).")
            continue
        if buffer.tell() > MAX_SCRIPT_BYTES:
            notes.append(f"{script_path} ignorado: maior do que 64 KB.")
            continue
        text = buffer.getvalue().decode("latin-1")
        for lineno, raw in extract_videopaths(text):
            expanded = expand_videopath(raw, home)
            if expanded is None:
                notes.append(
                    f"{script_path} (linha {lineno}): VIDEOPATH usa variáveis que não é "
                    f"possível resolver ({raw})."
                )
                continue
            resolved = resolver.resolve(expanded)
            if resolved is None:
                notes.append(
                    f"{script_path} (linha {lineno}): a pasta {expanded} não é acessível "
                    "por FTP (fora de âmbito: só é possível usar pastas visíveis por FTP)."
                )
                continue
            candidate = candidates.setdefault(resolved, Candidate(resolved, "script"))
            candidate.evidence.append(f'VIDEOPATH="{raw}" em {script_path} (linha {lineno})')

    if candidates:
        found = list(candidates.values())
        for candidate in found:
            _inspect(client, candidate)
        found.sort(key=lambda c: (-c.video_count, c.ftp_path))
        return result("found" if len(found) == 1 else "ambiguous", found)

    # 3. Folders with video files, up to 2 levels below the login dir.
    notes.append("Nenhum script com VIDEOPATH encontrado: procurei pastas com vídeos.")
    scanned = _scan_for_videos(client, login_dir, max_depth=2)
    if not scanned:
        return result("not_found", [])
    scanned.sort(key=lambda c: (-c.video_count, c.ftp_path))
    return result("found" if len(scanned) == 1 else "ambiguous", scanned)


def _find_scripts(client: FtpClient, login_dir: str, notes: list[str]):
    try:
        entries = client.list_dir(login_dir)
    except FtpOperationError as exc:
        notes.append(f"Não foi possível listar {login_dir} ({exc}).")
        return []
    scripts = [
        (posixpath.join(login_dir, e.name), e.size)
        for e in entries
        if not e.is_dir and e.name.endswith(".sh")
    ]
    for entry in entries:
        if not entry.is_dir or entry.is_link or is_hidden(entry.name):
            continue
        sub = posixpath.join(login_dir, entry.name)
        try:
            children = client.list_dir(sub)
        except FtpOperationError:
            continue
        scripts.extend(
            (posixpath.join(sub, c.name), c.size)
            for c in children
            if not c.is_dir and c.name.endswith(".sh")
        )
    return scripts


def _inspect(client: FtpClient, candidate: Candidate) -> None:
    try:
        entries = client.list_dir(candidate.ftp_path)
    except FtpOperationError:
        return
    visible = [e for e in entries if not is_hidden(e.name)]
    videos = sorted(e.name for e in visible if not e.is_dir and is_video_name(e.name))
    candidate.video_count = len(videos)
    candidate.other_count = len(visible) - len(videos)
    candidate.sample = videos[:5]
    candidate.has_staging = any(e.name == STAGING_DIR and e.is_dir for e in entries)
    candidate.evidence.append(
        f"Contém {len(videos)} vídeo(s)"
        + (f" e {candidate.other_count} outro(s) elemento(s)" if candidate.other_count else "")
    )


def _scan_for_videos(client: FtpClient, root: str, *, max_depth: int) -> list[Candidate]:
    found: list[Candidate] = []
    queue: list[tuple[str, int]] = [(root, 0)]
    visited = 0
    while queue and visited < MAX_DIRS_SCANNED:
        path, depth = queue.pop(0)
        visited += 1
        try:
            entries = client.list_dir(path)
        except FtpOperationError:
            continue
        visible = [e for e in entries if not is_hidden(e.name)]
        videos = sorted(e.name for e in visible if not e.is_dir and is_video_name(e.name))
        if videos:
            candidate = Candidate(
                path,
                "scan",
                [f"Contém {len(videos)} vídeo(s) (encontrada por pesquisa)"],
                video_count=len(videos),
                other_count=len(visible) - len(videos),
                sample=videos[:5],
                has_staging=any(e.name == STAGING_DIR and e.is_dir for e in entries),
            )
            found.append(candidate)
        if depth < max_depth:
            queue.extend(
                (posixpath.join(path, e.name), depth + 1)
                for e in visible
                if e.is_dir and not e.is_link
            )
    return found
