"""Thin wrapper around ftplib for the store Pis.

- Passive mode only, short timeouts, one retry when connecting.
- Read-only by default: every write method raises FtpReadOnlyError unless the
  client was created with ``read_only=False``. Only the executor does that, and
  only when DRY_RUN=false and the device is enabled.
- Listings include hidden entries when the server allows it (MLSD or LIST -a).
  Directory existence is always checked with CWD, because vsftpd hides dotfiles
  and answers LIST on a missing path with an empty listing.
- Names travel as latin-1 so any byte sequence round-trips unchanged; the UI
  decodes them for display (models.wire_to_display).
- Error messages never contain credentials.
"""

from __future__ import annotations

import contextlib
import ftplib
import logging
import posixpath
import re
import socket
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO

log = logging.getLogger(__name__)


class FtpError(Exception):
    """Base FTP error. The message is safe to show and to log."""


class FtpConnectError(FtpError):
    """Host unreachable, refused or timed out: the device is offline."""


class FtpAuthError(FtpError):
    """Login refused by the server."""


class FtpOperationError(FtpError):
    """A command was refused (e.g. 550) or failed mid-way."""


class FtpReadOnlyError(FtpError):
    """A write was attempted on a read-only client (a bug, never expected)."""


@dataclass(frozen=True)
class ListEntry:
    name: str
    is_dir: bool
    size: int | None = None
    mtime: datetime | None = None
    is_link: bool = False


_LIST_RE = re.compile(
    r"^(?P<type>[-dlbcps])[rwxsStTl-]{9}[.+@]?\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s+"
    r"(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<ty>\d{1,2}:\d{2}|\d{4})\s(?P<name>.+)$"
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}


def parse_list_line(line: str, now: datetime | None = None) -> ListEntry | None:
    """Parse one Unix-style LIST line (vsftpd, proftpd, pure-ftpd, pyftpdlib)."""
    match = _LIST_RE.match(line.rstrip("\r\n"))
    if not match:
        return None
    kind = match["type"]
    name = match["name"]
    is_link = kind == "l"
    if is_link and " -> " in name:
        name = name.split(" -> ", 1)[0]
    if name in (".", ".."):
        return None
    return ListEntry(
        name=name,
        is_dir=kind == "d",
        size=int(match["size"]),
        mtime=_parse_list_date(match["month"], match["day"], match["ty"], now),
        is_link=is_link,
    )


def _parse_list_date(month: str, day: str, time_or_year: str, now: datetime | None):
    """LIST dates are imprecise (no year for recent files); good enough for display."""
    now = now or datetime.now(UTC)
    try:
        month_n = _MONTHS[month.lower()]
        if ":" in time_or_year:
            hour, minute = (int(x) for x in time_or_year.split(":"))
            value = datetime(now.year, month_n, int(day), hour, minute, tzinfo=UTC)
            if (value - now).days > 1:  # "Dec 31 10:00" seen in January
                value = value.replace(year=now.year - 1)
            return value
        return datetime(int(time_or_year), month_n, int(day), tzinfo=UTC)
    except (KeyError, ValueError):
        return None


def _parse_mdtm(value: str) -> datetime | None:
    value = value.strip().split(".", 1)[0]
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _describe(exc: BaseException | None) -> str:
    if isinstance(exc, socket.gaierror):
        return "nome não encontrado no DNS"
    if isinstance(exc, ConnectionRefusedError):
        return "ligação recusada"
    if isinstance(exc, TimeoutError):
        return "sem resposta (tempo esgotado)"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    if exc is None:
        return "erro desconhecido"
    return str(exc) or exc.__class__.__name__


class FtpClient:
    """One FTP session to one Pi. Use as a context manager."""

    def __init__(
        self,
        host: str,
        port: int = 21,
        user: str = "",
        password: str = "",
        *,
        timeout: float = 10.0,
        read_only: bool = True,
        connect_retries: int = 1,
        retry_delay: float = 2.0,
        prefer_mlsd: bool = True,
        ftp_factory: Callable[..., ftplib.FTP] = ftplib.FTP,
    ) -> None:
        self.host = host
        self.port = port
        self._user = user
        self._password = password
        self.timeout = timeout
        self.read_only = read_only
        self.connect_retries = connect_retries
        self.retry_delay = retry_delay
        self.prefer_mlsd = prefer_mlsd
        self._factory = ftp_factory
        self._ftp: ftplib.FTP | None = None
        self._features: set[str] | None = None
        self.welcome: str = ""

    def __repr__(self) -> str:  # never show credentials
        mode = "ro" if self.read_only else "rw"
        return f"<FtpClient {self.host}:{self.port} {mode}>"

    # --- session -----------------------------------------------------------
    def __enter__(self) -> FtpClient:
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def connect(self) -> FtpClient:
        last: BaseException | None = None
        for attempt in range(self.connect_retries + 1):
            ftp = self._factory(timeout=self.timeout, encoding="latin-1")
            try:
                ftp.connect(self.host, self.port)
                try:
                    ftp.login(self._user, self._password)
                except ftplib.error_perm as exc:
                    raise FtpAuthError(
                        f"Login FTP recusado por {self.host} ({str(exc)[:120]})"
                    ) from None
                ftp.set_pasv(True)
            except FtpAuthError:
                _close_quietly(ftp)
                raise
            except (OSError, EOFError, ftplib.Error) as exc:
                _close_quietly(ftp)
                last = exc
                if attempt < self.connect_retries:
                    time.sleep(self.retry_delay)
                continue
            self._ftp = ftp
            self.welcome = ftp.getwelcome() or ""
            return self
        raise FtpConnectError(f"Sem ligação a {self.host}:{self.port} ({_describe(last)})")

    def close(self) -> None:
        if self._ftp is None:
            return
        try:
            self._ftp.quit()
        except (OSError, EOFError, ftplib.Error):
            _close_quietly(self._ftp)
        self._ftp = None

    @property
    def ftp(self) -> ftplib.FTP:
        if self._ftp is None:
            raise FtpConnectError(f"Sessão FTP com {self.host} não está aberta")
        return self._ftp

    @contextmanager
    def _op(self, what: str) -> Iterator[None]:
        try:
            yield
        except FtpError:
            raise
        except ftplib.error_perm as exc:
            raise FtpOperationError(f"{what}: {str(exc)[:200]}") from None
        except TimeoutError:
            raise FtpConnectError(f"{what}: sem resposta de {self.host} (tempo esgotado)") from None
        except (OSError, EOFError) as exc:
            raise FtpConnectError(
                f"{what}: ligação a {self.host} perdida ({_describe(exc)})"
            ) from None
        except ftplib.Error as exc:
            raise FtpOperationError(f"{what}: {str(exc)[:200]}") from None

    # --- read operations -----------------------------------------------------
    def features(self) -> set[str]:
        if self._features is None:
            try:
                response = self.ftp.sendcmd("FEAT")
            except (OSError, EOFError, ftplib.Error):
                self._features = set()
            else:
                lines = response.splitlines()[1:-1]
                self._features = {line.strip().split(" ", 1)[0].upper() for line in lines}
        return self._features

    def pwd(self) -> str:
        with self._op("PWD"):
            return self.ftp.pwd()

    def canonical_dir(self, path: str) -> str | None:
        """Absolute FTP path of a directory (CWD + PWD), or None if not accessible."""
        try:
            self.ftp.cwd(path)
        except ftplib.error_perm:
            return None
        except (TimeoutError, OSError, EOFError, ftplib.Error) as exc:
            raise FtpConnectError(f"CWD {path}: {_describe(exc)}") from None
        return self.pwd()

    def is_dir(self, path: str) -> bool:
        return self.canonical_dir(path) is not None

    def list_dir(self, path: str) -> list[ListEntry]:
        """Entries of a directory, hidden ones included when the server allows it.

        Raises FtpOperationError if the directory does not exist.
        """
        with self._op(f"CWD {path}"):
            self.ftp.cwd(path)
        if self.prefer_mlsd and "MLST" in self.features():
            try:
                return self._list_mlsd()
            except FtpOperationError:
                log.info("MLSD refused by %s, falling back to LIST", self.host)
        return self._list_unix()

    def _list_mlsd(self) -> list[ListEntry]:
        entries = []
        with self._op("MLSD"):
            for name, facts in self.ftp.mlsd("", facts=["type", "size", "modify"]):
                kind = facts.get("type", "").lower()
                if kind in ("cdir", "pdir") or name in (".", ".."):
                    continue
                size = facts.get("size")
                entries.append(
                    ListEntry(
                        name=name,
                        is_dir=kind == "dir",
                        size=int(size) if size and size.isdigit() else None,
                        mtime=_parse_mdtm(facts["modify"]) if "modify" in facts else None,
                        is_link=kind.startswith("os.unix=slink"),
                    )
                )
        return entries

    def _list_unix(self) -> list[ListEntry]:
        def run(command: str) -> list[ListEntry] | None:
            lines: list[str] = []
            try:
                with self._op(command):
                    self.ftp.retrlines(command, lines.append)
            except FtpOperationError:
                return None
            now = datetime.now(UTC)
            parsed = [parse_list_line(line, now) for line in lines]
            unparsed = [line for line, entry in zip(lines, parsed, strict=True) if entry is None]
            for line in unparsed:
                if not line.lower().startswith("total") and line.strip():
                    log.warning("unparsed LIST line from %s: %r", self.host, line[:200])
            return [entry for entry in parsed if entry is not None]

        with_hidden = run("LIST -a")
        plain = run("LIST") if not with_hidden else None
        if with_hidden is None and plain is None:
            raise FtpOperationError(f"LIST recusado por {self.host}")
        candidates = [x for x in (with_hidden, plain) if x is not None]
        return max(candidates, key=len)

    def size(self, path: str) -> int | None:
        try:
            with self._op("TYPE I"):
                self.ftp.voidcmd("TYPE I")
            with self._op(f"SIZE {path}"):
                value = self.ftp.size(path)
        except FtpOperationError:
            return None
        return int(value) if value is not None else None

    def mdtm(self, path: str) -> datetime | None:
        try:
            with self._op(f"MDTM {path}"):
                response = self.ftp.sendcmd(f"MDTM {path}")
        except FtpOperationError:
            return None
        parts = response.split(" ", 1)
        return _parse_mdtm(parts[1]) if len(parts) == 2 and parts[0] == "213" else None

    def retrieve(
        self,
        path: str,
        fp: BinaryIO,
        *,
        callback: Callable[[int], None] | None = None,
    ) -> int:
        """Download a file (RETR). Read-only for the Pi. Returns bytes received."""
        received = 0

        def write(chunk: bytes) -> None:
            nonlocal received
            fp.write(chunk)
            received += len(chunk)
            if callback:
                callback(received)

        with self._op(f"RETR {path}"):
            self.ftp.retrbinary(f"RETR {path}", write, blocksize=65536)
        return received

    # --- write operations (executor only) --------------------------------------
    def _require_writable(self, what: str) -> None:
        if self.read_only:
            raise FtpReadOnlyError(f"Escrita bloqueada ({what}): cliente FTP em modo só de leitura")

    def mkdir(self, path: str) -> None:
        self._require_writable(f"MKD {path}")
        with self._op(f"MKD {path}"):
            self.ftp.mkd(path)

    def store(
        self,
        path: str,
        fp: BinaryIO,
        *,
        callback: Callable[[int], None] | None = None,
    ) -> int:
        self._require_writable(f"STOR {path}")
        sent = 0

        def on_block(block: bytes) -> None:
            nonlocal sent
            sent += len(block)
            if callback:
                callback(sent)

        with self._op(f"STOR {path}"):
            self.ftp.storbinary(f"STOR {path}", fp, blocksize=65536, callback=on_block)
        return sent

    def rename(self, src: str, dst: str) -> None:
        self._require_writable(f"RNFR {src}")
        with self._op(f"RNFR/RNTO {src} -> {dst}"):
            self.ftp.rename(src, dst)

    def delete(self, path: str) -> None:
        self._require_writable(f"DELE {path}")
        with self._op(f"DELE {path}"):
            self.ftp.delete(path)

    @staticmethod
    def join(*parts: str) -> str:
        return posixpath.join(*parts)


def _close_quietly(ftp: ftplib.FTP) -> None:
    with contextlib.suppress(OSError):
        ftp.close()
