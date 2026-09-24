"""Discovery of the video folder (SPEC §7): read-only, against the simulated server."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lj_signage.discovery import discover, expand_videopath, extract_videopaths
from lj_signage.ftp import FtpClient

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
SCRIPT = """#!/bin/sh
setterm -cursor off
# VIDEOPATH="/home/tmagalhaes/antigo"
VIDEOPATH="{path}"
SERVICE="omxplayer"
"""


def snapshot(root: Path) -> dict:
    return {
        str(p.relative_to(root)): (p.is_dir(), p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
    }


def run_discovery(server, **kwargs):
    with FtpClient(server.host, server.port, server.user, server.password, timeout=5) as client:
        return discover(client, login_home=server.login_home, now=NOW, **kwargs)


def add_videos(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"v" * 10)


def test_videopath_from_the_player_script(ftp_server):
    (ftp_server.home / "player.sh").write_text(SCRIPT.format(path="/home/tmagalhaes/Videos"))
    add_videos(ftp_server.home / "Videos", "a.mp4", "b.mp4")
    before = snapshot(ftp_server.root)
    result = run_discovery(ftp_server)
    assert snapshot(ftp_server.root) == before  # nothing written on the Pi
    assert result.status == "found"
    [candidate] = result.candidates
    assert candidate.ftp_path == "/home/tmagalhaes/Videos"
    assert candidate.source == "script"
    assert candidate.video_count == 2
    assert any("player.sh (linha 4)" in e for e in candidate.evidence)
    assert result.chroot is False


def test_chrooted_ftp_maps_the_absolute_path(ftp_server_factory):
    server = ftp_server_factory(chroot=True)
    (server.home / "player.sh").write_text(SCRIPT.format(path="/home/tmagalhaes/Videos"))
    add_videos(server.home / "Videos", "a.mp4")
    result = run_discovery(server)
    assert result.status == "found"
    assert result.candidates[0].ftp_path == "/Videos"
    assert result.chroot is True


def test_script_one_level_below_and_home_variable(ftp_server):
    (ftp_server.home / "scripts").mkdir()
    (ftp_server.home / "scripts" / "tv.sh").write_text('VIDEOPATH="$HOME/Loja"\n')
    add_videos(ftp_server.home / "Loja", "x.mp4")
    result = run_discovery(ftp_server)
    assert result.status == "found"
    assert result.candidates[0].ftp_path == "/home/tmagalhaes/Loja"


def test_two_scripts_pointing_to_different_folders_is_ambiguous(ftp_server):
    (ftp_server.home / "a.sh").write_text('VIDEOPATH="/home/tmagalhaes/Videos"\n')
    (ftp_server.home / "b.sh").write_text("VIDEOPATH=/home/tmagalhaes/Outros\n")
    add_videos(ftp_server.home / "Videos", "1.mp4", "2.mp4")
    add_videos(ftp_server.home / "Outros", "3.mp4")
    result = run_discovery(ftp_server)
    assert result.status == "ambiguous"
    assert [c.ftp_path for c in result.candidates] == [
        "/home/tmagalhaes/Videos",
        "/home/tmagalhaes/Outros",
    ]


def test_path_unreachable_over_ftp_is_reported(ftp_server_factory):
    server = ftp_server_factory(chroot=True)
    (server.home / "player.sh").write_text('VIDEOPATH="/media/usb/videos"\n')
    result = run_discovery(server)
    assert result.status == "not_found"
    assert any("não é acessível por FTP" in note for note in result.notes)


def test_without_scripts_folders_with_videos_are_scanned(ftp_server):
    add_videos(ftp_server.home / "Media" / "TV", "promo.mov")
    add_videos(ftp_server.home / "a" / "b" / "c", "fundo.mp4")  # 3 levels: too deep
    result = run_discovery(ftp_server)
    assert result.status == "found"
    assert result.candidates[0].ftp_path == "/home/tmagalhaes/Media/TV"
    assert result.candidates[0].source == "scan"


def test_nothing_found(ftp_server):
    (ftp_server.home / "documentos").mkdir()
    assert run_discovery(ftp_server).status == "not_found"


def test_configured_path_is_validated(ftp_server):
    add_videos(ftp_server.home / "Videos", "a.mp4")
    ok = run_discovery(ftp_server, config_video_path="/home/tmagalhaes/Videos")
    assert ok.status == "found" and ok.candidates[0].source == "config"
    missing = run_discovery(ftp_server, config_video_path="/home/tmagalhaes/NaoExiste")
    assert missing.status == "error"
    assert "não existe" in missing.error


def test_scripts_over_64_kb_are_ignored(ftp_server):
    (ftp_server.home / "grande.sh").write_text('VIDEOPATH="/x"\n' + "#" * 70_000)
    result = run_discovery(ftp_server)
    assert any("64 KB" in note for note in result.notes)


def test_result_round_trips_through_json(ftp_server):
    (ftp_server.home / "player.sh").write_text(SCRIPT.format(path="/home/tmagalhaes/Videos"))
    add_videos(ftp_server.home / "Videos", "a.mp4")
    result = run_discovery(ftp_server)
    from lj_signage.discovery import DiscoveryResult

    assert DiscoveryResult.from_dict(result.to_dict()) == result


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("$HOME/Videos", "/home/tmagalhaes/Videos"),
        ("${HOME}/Videos/", "/home/tmagalhaes/Videos"),
        ("~/Videos", "/home/tmagalhaes/Videos"),
        ("$BASE/Videos", None),
    ],
)
def test_expand_videopath(value, expected):
    assert expand_videopath(value, "/home/tmagalhaes") == expected


def test_extract_videopaths_ignores_comments():
    text = "# VIDEOPATH=\"/a\"\nexport VIDEOPATH='/b'\n  VIDEOPATH=/c # nota\n"
    assert extract_videopaths(text) == [(2, "/b"), (3, "/c")]


def test_player_script_in_docs_is_parsed():
    script = Path(__file__).resolve().parent.parent / "docs" / "player-script.sh"
    assert extract_videopaths(script.read_text()) == [(13, "/home/tmagalhaes/Videos")]
    assert os.access(script, os.R_OK)
