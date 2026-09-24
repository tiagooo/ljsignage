"""Reads a Pi's state and applies planner actions over FTP.

Writes only happen through a client created with ``read_only=False``, which
the cycle does only when DRY_RUN=false and the device is enabled. The executor
stops at the first error; the next cycle resumes (the planner is idempotent).

Safety checks on top of the planner:
- local file size and sha256 are verified before an upload;
- the uploaded ``.part`` must have exactly the local size (SIZE) before it is
  renamed to its staging name;
- a rename never replaces an existing file (vsftpd would do it silently).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import posixpath
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..ftp import FtpClient, FtpConnectError, FtpError, FtpOperationError
from ..naming import PART_RE, STAGING_DIR
from . import planner as pl
from .state import remote_from_listing

log = logging.getLogger(__name__)


class ExecutionError(Exception):
    """A safety check failed (message in Portuguese, shown in the panel)."""


class LeaseLost(ExecutionError):
    """Another process now owns this device: stop at once, write nothing more."""


@dataclass
class Outcome:
    action: pl.Action
    ok: bool
    error: str | None = None
    seconds: float = 0.0
    fatal: bool = False  # the FTP session must not be used again (lease lost, link down)


def read_remote_state(client: FtpClient, video_path: str) -> pl.RemoteState:
    """List the video folder and the staging folder (SPEC §6.2). Read-only.

    The staging folder is hidden: its existence is checked with CWD, which works
    even on servers whose listings never show dotfiles. The age of ``.part``
    files comes from MDTM when available (LIST dates are imprecise).
    """
    root = client.list_dir(video_path)
    staging_path = posixpath.join(video_path, STAGING_DIR)
    listed = any(e.name == STAGING_DIR and e.is_dir for e in root)
    staging_exists = listed or client.is_dir(staging_path)
    staging = client.list_dir(staging_path) if staging_exists else []
    staging = [
        _with_mtime(client, staging_path, e) if PART_RE.match(e.name) else e for e in staging
    ]
    return remote_from_listing(root, staging_exists, staging)


def _with_mtime(client: FtpClient, folder: str, entry):
    precise = client.mdtm(posixpath.join(folder, entry.name))
    if precise is None:
        return entry
    return type(entry)(entry.name, entry.is_dir, entry.size, precise, entry.is_link)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Executor:
    def __init__(
        self,
        client: FtpClient,
        video_path: str,
        *,
        media_path: Callable[[int], Path | None],
        on_outcome: Callable[[Outcome, pl.Plan], None] | None = None,
        before_action: Callable[[pl.Action], None] | None = None,
        progress: Callable[[pl.Action, int], None] | None = None,
    ) -> None:
        """``before_action`` runs before every action (it may raise ExecutionError to
        stop, e.g. when the device lease was lost); ``progress`` is called while a
        file is being sent (it may raise LeaseLost to abort the transfer)."""
        if client.read_only:
            raise ExecutionError("O executor precisa de um cliente FTP com escrita autorizada.")
        self.client = client
        self.video_path = video_path
        self.media_path = media_path
        self.on_outcome = on_outcome
        self.before_action = before_action
        self.progress = progress

    def run(self, plan: pl.Plan) -> list[Outcome]:
        outcomes: list[Outcome] = []
        for action in plan.actions:
            started = time.monotonic()
            try:
                if self.before_action:
                    self.before_action(action)
                self._apply(action, plan)
            except (FtpError, ExecutionError, OSError) as exc:
                fatal = isinstance(exc, LeaseLost | FtpConnectError)
                outcome = Outcome(action, False, str(exc), time.monotonic() - started, fatal)
            else:
                outcome = Outcome(action, True, None, time.monotonic() - started)
            outcomes.append(outcome)
            if self.on_outcome:
                self.on_outcome(outcome, plan)
            if not outcome.ok:
                log.warning("stopping after failed action %s: %s", action.kind, outcome.error)
                break
        return outcomes

    # --- actions -----------------------------------------------------------------
    def _path(self, relative: str) -> str:
        return posixpath.join(self.video_path, relative)

    def _apply(self, action: pl.Action, plan: pl.Plan) -> None:
        kind = action.kind
        if kind is pl.ActionKind.MKDIR_STAGING:
            self.client.mkdir(self._path(action.dst))
        elif kind is pl.ActionKind.UPLOAD:
            self._upload(action, plan)
        elif kind in (pl.ActionKind.ACTIVATE, pl.ActionKind.REORDER, pl.ActionKind.DEACTIVATE):
            self._ensure_absent(action.dst)
            self.client.rename(self._path(action.src), self._path(action.dst))
        elif kind in (
            pl.ActionKind.DELETE_ACTIVE,
            pl.ActionKind.DELETE_LEGACY,
            pl.ActionKind.DELETE_STAGED,
            pl.ActionKind.DELETE_PART,
        ):
            self.client.delete(self._path(action.src))
        else:  # pragma: no cover
            raise ExecutionError(f"Ação desconhecida: {kind}")

    def _ensure_absent(self, relative: str) -> None:
        if self.client.size(self._path(relative)) is not None:
            raise ExecutionError(f"Já existe {relative} no Pi: a app nunca substitui ficheiros.")

    def _upload(self, action: pl.Action, plan: pl.Plan) -> None:
        video = plan.classified.videos_by_id.get(action.video_id)
        local = self.media_path(action.video_id)
        if video is None or local is None or not local.is_file():
            raise ExecutionError("O ficheiro convertido deste vídeo não está no servidor.")
        if local.stat().st_size != video.size_bytes:
            raise ExecutionError("O ficheiro convertido no servidor não tem o tamanho esperado.")
        if sha256_of(local) != video.sha256:
            raise ExecutionError("O ficheiro convertido no servidor está corrompido (sha256).")

        part = self._path(action.dst + ".part")
        with local.open("rb") as fh:
            self.client.store(
                part,
                fh,
                callback=(lambda sent: self.progress(action, sent)) if self.progress else None,
            )
        remote_size = self.client.size(part)
        if remote_size != video.size_bytes:
            with contextlib.suppress(FtpError):
                self.client.delete(part)
            raise ExecutionError(
                f"Verificação do envio falhou: o Pi tem {remote_size} bytes, "
                f"eram esperados {video.size_bytes}."
            )
        self._ensure_absent(action.dst)
        try:
            self.client.rename(part, self._path(action.dst))
        except FtpOperationError as exc:
            raise ExecutionError(f"Não foi possível concluir o envio ({exc}).") from None
