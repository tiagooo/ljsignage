"""File naming conventions on the Pi (SPEC §5) and legacy name checks.

The player script runs ``for entry in $VIDEOPATH/*`` and ``omxplayer -z $entry``
without quotes, so an active name must never contain whitespace or glob
characters. Everything the app writes follows ACTIVE_NAME_RE.
"""

from __future__ import annotations

import re
import unicodedata

STAGING_DIR = ".lj-staging"
SLUG_MAX = 40
VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".h264", ".mkv", ".avi")

# Invariant from CLAUDE.md: every visible file the app creates matches this.
ACTIVE_NAME_RE = re.compile(r"^[0-9]{3}_[a-z0-9-]+\.mp4$")
# Names the app creates: <NNN>_<slug>-<sha256[:6]>.mp4
MANAGED_ACTIVE_RE = re.compile(
    r"^(?P<nnn>[0-9]{3})_(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<sha6>[0-9a-f]{6})\.mp4$"
)
STAGED_RE = re.compile(r"^(?P<sha12>[0-9a-f]{12})\.mp4$")
PART_RE = re.compile(r"^(?P<sha12>[0-9a-f]{12})\.mp4\.part$")

# Characters that break the unquoted $entry in the player script: word
# splitting (IFS whitespace) and pathname expansion (glob characters).
_UNSAFE_RE = re.compile(r"[\s*?\[\]]")


def slugify(text: str, *, max_length: int = SLUG_MAX) -> str:
    """Lowercase ASCII slug: accents removed, [a-z0-9-] only, at most 40 characters."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if len(slug) > max_length:
        cut = slug[:max_length]
        # Prefer cutting at a word boundary when it does not lose too much.
        boundary = cut.rfind("-")
        slug = cut[:boundary] if boundary >= max_length // 2 else cut
        slug = slug.strip("-")
    return slug or "video"


def active_name(nnn: int, slug: str, sha256: str) -> str:
    if not 1 <= nnn <= 999:
        raise ValueError(f"position prefix out of range: {nnn}")
    name = f"{nnn:03d}_{slug}-{sha256[:6]}.mp4"
    if not ACTIVE_NAME_RE.match(name) or not MANAGED_ACTIVE_RE.match(name):
        raise ValueError(f"invalid active name: {name!r}")
    return name


def staged_name(sha256: str) -> str:
    return f"{sha256[:12]}.mp4"


def part_name(sha256: str) -> str:
    return f"{sha256[:12]}.mp4.part"


def staging_path(name: str) -> str:
    """Path relative to video_path of a file inside the staging directory."""
    return f"{STAGING_DIR}/{name}"


def is_hidden(name: str) -> bool:
    return name.startswith(".")


def is_video_name(name: str) -> bool:
    return name.lower().endswith(VIDEO_EXTENSIONS)


def legacy_issues(name: str, *, is_dir: bool, size: int | None) -> list[str]:
    """Problems of a visible file the app did not create (codes; labels in the UI)."""
    if is_dir:
        return ["dir"]
    issues = []
    if not is_video_name(name):
        issues.append("not_video")
    if _UNSAFE_RE.search(name) or any(ord(ch) < 32 for ch in name):
        issues.append("unsafe_name")
    if size == 0:
        issues.append("empty")
    if not ACTIVE_NAME_RE.match(name):
        issues.append("non_standard_name")
    return issues


# Issues that stop the player script from playing the entry.
BLOCKING_ISSUES = frozenset({"dir", "not_video", "unsafe_name", "empty"})


def is_playable_legacy(issues: list[str]) -> bool:
    return not BLOCKING_ISSUES.intersection(issues)


ISSUE_LABELS = {
    "dir": "Pasta visível: o leitor tenta reproduzi-la e falha.",
    "not_video": "Não é um ficheiro de vídeo.",
    "unsafe_name": "Nome com espaços ou caracteres especiais: o leitor não o consegue abrir.",
    "empty": "Ficheiro vazio.",
    "non_standard_name": "Nome fora do padrão NNN_nome.mp4: a ordem pode não ser a esperada.",
    "size_mismatch": "Tem o nome de um vídeo da biblioteca mas o tamanho não corresponde.",
}
