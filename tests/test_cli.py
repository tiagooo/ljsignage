"""Flask CLI commands."""

from __future__ import annotations

from dataclasses import replace

from lj_signage.extensions import db
from lj_signage.models import Device


def test_devices_sync_and_list(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["devices", "sync"])
    assert result.exit_code == 0, result.output
    assert "3 dispositivos, 1 grupos: novas: ubbo, gjpt, al" in result.output
    again = runner.invoke(args=["devices", "sync"])
    assert "sem alterações" in again.output
    listing = runner.invoke(args=["devices", "list"])
    assert "raspservidorubbo.lugardajoia.internal" in listing.output
    assert "só leitura" in listing.output


def test_reconcile_execute_is_refused_in_dry_run(app, synced):
    result = app.test_cli_runner().invoke(args=["reconcile", "ubbo", "--execute", "--yes"])
    assert result.exit_code != 0
    assert "DRY_RUN=true" in result.output


def test_reconcile_execute_is_refused_for_disabled_devices(app, synced, settings):
    app.config["LJ_SETTINGS"] = replace(settings, dry_run=False)
    try:
        result = app.test_cli_runner().invoke(args=["reconcile", "ubbo", "--execute", "--yes"])
    finally:
        app.config["LJ_SETTINGS"] = settings
    assert result.exit_code != 0
    assert "enabled: false" in result.output


def test_reconcile_all_is_refused(app, synced):
    result = app.test_cli_runner().invoke(args=["reconcile", "all", "--execute"])
    assert result.exit_code != 0 and "uma loja de cada vez" in result.output


def test_plan_offline_uses_the_stored_snapshot(app, synced):
    result = app.test_cli_runner().invoke(args=["plan", "all", "--offline"])
    assert result.exit_code == 0
    assert "pasta por confirmar" in result.output


def test_unknown_device(app, synced):
    result = app.test_cli_runner().invoke(args=["discover", "nao-existe"])
    assert result.exit_code != 0 and "desconhecida" in result.output


def test_discover_against_the_simulated_server(app, ftp_server):
    from lj_signage.config import parse_devices
    from lj_signage.inventory import sync_devices

    (ftp_server.home / "player.sh").write_text('VIDEOPATH="/home/tmagalhaes/Videos"\n')
    (ftp_server.home / "Videos").mkdir()
    sync_devices(
        parse_devices(
            {
                "devices": [
                    {
                        "store": "02",
                        "name": "Teste",
                        "code": "teste",
                        "hostname": "127.0.0.1",
                        "ftp_port": ftp_server.port,
                        "login_home": "/home/tmagalhaes",
                    }
                ]
            }
        )
    )
    db.session.commit()
    result = app.test_cli_runner().invoke(args=["discover", "teste"])
    assert result.exit_code == 0, result.output
    assert "Pasta encontrada: /home/tmagalhaes/Videos" in result.output
    assert "Nada foi gravado" in result.output
    device = db.session.execute(db.select(Device).filter_by(code="teste")).scalar_one()
    assert device.video_path is None


def test_backup_now_and_list(app):
    runner = app.test_cli_runner()
    assert "Backup criado" in runner.invoke(args=["backup", "now"]).output
    assert "lj-signage-" in runner.invoke(args=["backup", "list"]).output
