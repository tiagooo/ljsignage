"""Shared fixtures.

Safety net (CLAUDE.md, rule 2): every outgoing connection made during the tests
must go to the loopback interface, so no test can ever reach a real Pi (or any
other real host), even by mistake.
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
_real_create_connection = socket.create_connection


def _guarded_create_connection(address, *args, **kwargs):
    host = address[0]
    if host not in _ALLOWED_HOSTS:
        raise AssertionError(f"test tried to connect to {host!r}: only loopback is allowed")
    return _real_create_connection(address, *args, **kwargs)


socket.create_connection = _guarded_create_connection


# --- simulated FTP server (pyftpdlib) -------------------------------------------------


@dataclass
class FtpServer:
    host: str
    port: int
    user: str
    password: str
    root: Path  # filesystem path of the FTP root
    home: Path  # filesystem path of /home/tmagalhaes
    login_home: str = "/home/tmagalhaes"

    def path(self, ftp_path: str) -> Path:
        """Filesystem path for an FTP path of this server."""
        return self.root / ftp_path.lstrip("/")


class _ServerThread(threading.Thread):
    """Runs the server loop and closes it from the same thread."""

    def __init__(self, server) -> None:
        super().__init__(daemon=True)
        self.server = server
        self.stopping = threading.Event()

    def run(self) -> None:
        while not self.stopping.is_set():
            self.server.serve_forever(timeout=0.05, blocking=False, handle_exit=False)
        self.server.close_all()

    def stop(self) -> None:
        self.stopping.set()
        self.join(timeout=5)


def _start_ftp_server(tmp_path: Path, *, chroot: bool, hide_dotfiles: bool, mlsd: bool):
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.filesystems import AbstractedFS
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import ThreadedFTPServer

    fs_root = tmp_path / "pi"
    home = fs_root / "home" / "tmagalhaes"
    home.mkdir(parents=True)
    ftp_root = home if chroot else fs_root

    authorizer = DummyAuthorizer()
    authorizer.add_user("tester", "s3cret", str(ftp_root), perm="elradfmwMT")

    class HidingFS(AbstractedFS):
        """vsftpd-like: LIST never shows dotfiles."""

        def listdir(self, path):
            return [n for n in super().listdir(path) if not n.startswith(".")]

    class Handler(FTPHandler):
        banner = "pyftpdlib (servidor de testes)"

        def on_login(self, username):
            if not chroot:
                self.fs.cwd = "/home/tmagalhaes"

    Handler.authorizer = authorizer
    Handler.passive_ports = None
    if hide_dotfiles:
        Handler.abstracted_fs = HidingFS
    if not mlsd:
        Handler.proto_cmds = {
            k: v for k, v in FTPHandler.proto_cmds.items() if k not in ("MLSD", "MLST")
        }

    server = ThreadedFTPServer(("127.0.0.1", 0), Handler)
    server.max_cons = 32
    thread = _ServerThread(server)
    thread.start()
    info = FtpServer(
        host="127.0.0.1",
        port=server.address[1],
        user="tester",
        password="s3cret",
        root=ftp_root,
        home=home,
    )
    return server, thread, info


@pytest.fixture
def ftp_server_factory(tmp_path):
    started = []

    def factory(*, chroot=False, hide_dotfiles=False, mlsd=True, name="srv"):
        server, thread, info = _start_ftp_server(
            tmp_path / name, chroot=chroot, hide_dotfiles=hide_dotfiles, mlsd=mlsd
        )
        started.append((server, thread))
        return info

    yield factory
    for _server, thread in started:
        thread.stop()


@pytest.fixture
def ftp_server(ftp_server_factory):
    """Non-chroot server: login lands in /home/tmagalhaes, like a stock vsftpd."""
    return ftp_server_factory()


# --- Flask app ---------------------------------------------------------------------------

ADMIN = "admin@lugardajoia.com"
EDITOR = "editor@lugardajoia.com"

DEVICES_YAML = """
defaults:
  domain: lugardajoia.internal
  login_home: /home/tmagalhaes
  enabled: false
groups:
  norte: ["04", "05"]
devices:
  - {store: "02", name: Lugar da Jóia UBBO, code: ubbo, hostname: raspservidorubbo}
  - {store: "04", name: Galeria da Jóia Porto, code: gjpt, hostname: raspservidorgjpt}
  - {store: "05", name: Lugar da Jóia Alameda, code: al, hostname: raspservidoral}
"""


def make_settings(tmp_path: Path, **overrides):
    from lj_signage.config import Settings

    devices_file = tmp_path / "devices.yaml"
    if not devices_file.exists():
        devices_file.write_text(DEVICES_YAML, encoding="utf-8")
    values = {
        "secret_key": "test-secret",
        "data_dir": tmp_path / "data",
        "devices_file": devices_file,
        "admin_emails": frozenset({ADMIN}),
        "ftp_user": "tester",
        "ftp_password": "s3cret",
        "ftp_timeout_s": 5.0,
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def app(settings):
    from lj_signage import create_app
    from lj_signage.extensions import db

    app = create_app(settings)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def synced(app, settings):
    """Devices from DEVICES_YAML loaded into the database."""
    from lj_signage.config import load_devices_file
    from lj_signage.extensions import db
    from lj_signage.inventory import sync_devices

    sync_devices(load_devices_file(settings.devices_file))
    db.session.commit()


def login(client, email: str, *, authorized: bool = True):
    from lj_signage.extensions import db
    from lj_signage.models import User

    user = db.session.execute(db.select(User).filter_by(email=email)).scalar_one_or_none()
    if user is None:
        user = User(email=email, name=email.split("@")[0].title(), authorized=authorized)
        db.session.add(user)
        db.session.commit()
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True
    return user


@pytest.fixture
def admin_client(client, synced):
    login(client, ADMIN)
    return client


@pytest.fixture
def editor_client(client, synced):
    login(client, EDITOR)
    return client
