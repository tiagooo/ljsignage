"""FtpClient against the simulated server (pyftpdlib) — never a real Pi."""

from __future__ import annotations

import io
import socket
from datetime import UTC, datetime

import pytest

from lj_signage.ftp import FtpAuthError, FtpClient, FtpConnectError, FtpReadOnlyError
from lj_signage.ftp.client import parse_list_line
from lj_signage.models import wire_to_display


def client_for(server, **kwargs) -> FtpClient:
    kwargs.setdefault("timeout", 5)
    kwargs.setdefault("retry_delay", 0.01)
    return FtpClient(server.host, server.port, server.user, server.password, **kwargs)


def test_safety_net_blocks_real_hosts():
    client = FtpClient("raspservidorubbo.lugardajoia.internal", 21, "x", "y", connect_retries=0)
    with pytest.raises(AssertionError, match="only loopback"):
        client.connect()


def test_login_lands_in_the_home_directory(ftp_server):
    with client_for(ftp_server) as client:
        assert client.pwd() == "/home/tmagalhaes"
        assert "pyftpdlib" in client.welcome


def test_chroot_server_starts_at_root(ftp_server_factory):
    server = ftp_server_factory(chroot=True)
    with client_for(server) as client:
        assert client.pwd() == "/"


def test_wrong_password_is_an_auth_error(ftp_server):
    client = FtpClient(ftp_server.host, ftp_server.port, "tester", "wrong", timeout=5)
    with pytest.raises(FtpAuthError) as info:
        client.connect()
    assert "wrong" not in str(info.value)


def test_nothing_listening_is_a_connect_error():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client = FtpClient("127.0.0.1", port, "a", "b", timeout=2, connect_retries=1, retry_delay=0.01)
    with pytest.raises(FtpConnectError, match="Sem ligação"):
        client.connect()


@pytest.mark.parametrize("mlsd", [True, False])
def test_listing_includes_hidden_entries_and_sizes(ftp_server_factory, mlsd):
    server = ftp_server_factory(mlsd=mlsd)
    videos = server.home / "Videos"
    (videos / ".lj-staging").mkdir(parents=True)
    (videos / "010_a-abcdef.mp4").write_bytes(b"x" * 1234)
    (videos / "sub").mkdir()
    with client_for(server) as client:
        entries = {e.name: e for e in client.list_dir("/home/tmagalhaes/Videos")}
    assert set(entries) == {".lj-staging", "010_a-abcdef.mp4", "sub"}
    assert entries["010_a-abcdef.mp4"].size == 1234
    assert not entries["010_a-abcdef.mp4"].is_dir
    assert entries[".lj-staging"].is_dir and entries["sub"].is_dir
    assert entries["010_a-abcdef.mp4"].mtime.tzinfo is not None


def test_server_hiding_dotfiles_still_finds_staging_with_cwd(ftp_server_factory):
    server = ftp_server_factory(hide_dotfiles=True, mlsd=False)
    videos = server.home / "Videos"
    (videos / ".lj-staging").mkdir(parents=True)
    with client_for(server) as client:
        names = {e.name for e in client.list_dir("/home/tmagalhaes/Videos")}
        assert ".lj-staging" not in names
        assert client.is_dir("/home/tmagalhaes/Videos/.lj-staging")
        assert not client.is_dir("/home/tmagalhaes/Videos/nao-existe")


def test_read_only_client_refuses_every_write(ftp_server):
    target = ftp_server.home / "Videos"
    target.mkdir()
    (target / "a.mp4").write_bytes(b"1")
    with client_for(ftp_server) as client:
        with pytest.raises(FtpReadOnlyError):
            client.mkdir("/home/tmagalhaes/Videos/.lj-staging")
        with pytest.raises(FtpReadOnlyError):
            client.store("/home/tmagalhaes/Videos/b.mp4", io.BytesIO(b"2"))
        with pytest.raises(FtpReadOnlyError):
            client.rename("/home/tmagalhaes/Videos/a.mp4", "/home/tmagalhaes/Videos/c.mp4")
        with pytest.raises(FtpReadOnlyError):
            client.delete("/home/tmagalhaes/Videos/a.mp4")
    assert sorted(p.name for p in target.iterdir()) == ["a.mp4"]


def test_writable_client_operations(ftp_server):
    base = "/home/tmagalhaes/Videos"
    (ftp_server.home / "Videos").mkdir()
    with client_for(ftp_server, read_only=False) as client:
        client.mkdir(f"{base}/.lj-staging")
        sent = client.store(f"{base}/.lj-staging/x.mp4.part", io.BytesIO(b"z" * 5000))
        assert sent == 5000
        assert client.size(f"{base}/.lj-staging/x.mp4.part") == 5000
        client.rename(f"{base}/.lj-staging/x.mp4.part", f"{base}/010_x-abcdef.mp4")
        assert client.size(f"{base}/010_x-abcdef.mp4") == 5000
        assert client.size(f"{base}/nao-existe.mp4") is None
        assert isinstance(client.mdtm(f"{base}/010_x-abcdef.mp4"), datetime)
        client.delete(f"{base}/010_x-abcdef.mp4")
    assert list((ftp_server.home / "Videos").iterdir()) == [
        ftp_server.home / "Videos" / ".lj-staging"
    ]


def test_non_ascii_names_round_trip(ftp_server):
    videos = ftp_server.home / "Videos"
    videos.mkdir()
    (videos / "Vídeo Loja.mp4").write_bytes(b"1")
    with client_for(ftp_server, read_only=False) as client:
        [entry] = client.list_dir("/home/tmagalhaes/Videos")
        assert wire_to_display(entry.name) == "Vídeo Loja.mp4"
        client.delete(f"/home/tmagalhaes/Videos/{entry.name}")
    assert not any(videos.iterdir())


def test_retrieve_downloads_without_changing_the_pi(ftp_server):
    (ftp_server.home / "player.sh").write_bytes(b"VIDEOPATH=/x\n")
    buffer = io.BytesIO()
    with client_for(ftp_server) as client:
        assert client.retrieve("/home/tmagalhaes/player.sh", buffer) == 13
    assert buffer.getvalue() == b"VIDEOPATH=/x\n"


def test_repr_hides_credentials(ftp_server):
    assert "s3cret" not in repr(client_for(ftp_server))


@pytest.mark.parametrize(
    ("line", "name", "is_dir", "size"),
    [
        (
            "-rw-r--r--    1 1000     1000     52428800 Sep 20 10:00 010_promo-abc123.mp4",
            "010_promo-abc123.mp4",
            False,
            52428800,
        ),
        (
            "drwxr-xr-x    2 1000     1000         4096 Sep 20  2025 .lj-staging",
            ".lj-staging",
            True,
            4096,
        ),
        (
            "-rw-r--r--    1 ftp      ftp           100 Jan 03 09:15 Promo Natal.mp4",
            "Promo Natal.mp4",
            False,
            100,
        ),
        (
            "lrwxrwxrwx    1 0        0              10 Jan 01  2020 atalho -> /media/x",
            "atalho",
            False,
            10,
        ),
    ],
)
def test_parse_list_line(line, name, is_dir, size):
    entry = parse_list_line(line, now=datetime(2026, 9, 24, tzinfo=UTC))
    assert (entry.name, entry.is_dir, entry.size) == (name, is_dir, size)
    assert entry.mtime is not None


def test_parse_list_line_skips_dots_and_totals():
    assert parse_list_line("total 12") is None
    assert parse_list_line("drwxr-xr-x    2 0 0 4096 Sep 20 10:00 .") is None
