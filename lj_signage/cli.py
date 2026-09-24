"""Flask CLI commands (CLAUDE.md, "Comandos").

flask devices sync            load config/devices.yaml into the database
flask devices list            devices and their state
flask discover <code|all>     read-only discovery of the video folder
flask plan <code|all>         show the action plan without executing it
flask reconcile <code> --execute
                              apply the plan (refused if DRY_RUN=true or enabled=false)
flask backup now|list|restore
"""

from __future__ import annotations

from pathlib import Path

import click
from flask import Flask, current_app
from flask.cli import AppGroup

from . import audit
from .backup import BackupError, list_backups, restore, run_backup
from .config import ConfigError, load_devices_file
from .discovery import DiscoveryResult, discover
from .extensions import db
from .ftp import FtpError
from .inventory import sync_devices
from .models import Device
from .reconciler import planner as pl
from .reconciler.cycle import CycleResult, client_for, run_cycle
from .reconciler.state import plan_from_snapshot
from .status import worker_status
from .timeutil import fmt_datetime, utcnow

CLI_ACTOR = audit.Actor(None, "Linha de comandos")

devices_cli = AppGroup("devices", help="Dispositivos (config/devices.yaml).")
backup_cli = AppGroup("backup", help="Backups da base de dados.")


def init_app(app: Flask) -> None:
    app.cli.add_command(devices_cli)
    app.cli.add_command(backup_cli)
    app.cli.add_command(discover_command)
    app.cli.add_command(plan_command)
    app.cli.add_command(reconcile_command)


def _settings():
    return current_app.config["LJ_SETTINGS"]


def _devices(code: str) -> list[Device]:
    query = db.select(Device).where(Device.in_config.is_(True)).order_by(Device.store_number)
    if code != "all":
        query = db.select(Device).where(Device.code == code)
    found = db.session.execute(query).scalars().all()
    if not found:
        raise click.ClickException(
            f"Loja '{code}' desconhecida. Corra 'flask devices sync' e veja 'flask devices list'."
        )
    return found


@devices_cli.command("sync")
def devices_sync() -> None:
    """Carregar config/devices.yaml para a base de dados."""
    settings = _settings()
    try:
        config = load_devices_file(settings.devices_file)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None
    report = sync_devices(config, actor=CLI_ACTOR)
    db.session.commit()
    click.echo(
        f"{len(config.devices)} dispositivos, {len(config.groups)} grupos: {report.summary()}"
    )


@devices_cli.command("list")
def devices_list() -> None:
    """Listar as lojas e o seu estado."""
    tz = _settings().tz
    rows = db.session.execute(db.select(Device).order_by(Device.store_number)).scalars()
    for d in rows:
        flags = []
        if not d.in_config:
            flags.append("fora do devices.yaml")
        flags.append("escrita ATIVA" if d.enabled else "só leitura")
        path = d.video_path_display or "(pasta por confirmar)"
        seen = fmt_datetime(d.last_seen_at, tz) or "nunca"
        click.echo(
            f"{d.store_number} {d.code:<6} {d.status:<8} {d.host:<42} {path}  "
            f"[{', '.join(flags)}] visto: {seen}"
        )


@click.command("discover")
@click.argument("code")
def discover_command(code: str) -> None:
    """Descoberta (só leitura) da pasta de vídeos de um Pi."""
    settings = _settings()
    for device in _devices(code):
        click.echo(f"\n[{device.store_number}] {device.name} ({device.host})")
        now = utcnow()
        try:
            with client_for(device, settings, read_only=True) as client:
                result = discover(
                    client,
                    login_home=device.login_home,
                    now=now,
                    config_video_path=device.config_video_path,
                )
        except FtpError as exc:
            result = DiscoveryResult(
                status="error", candidates=[], at=now.isoformat(), error=str(exc)
            )
        device.discovery = result.to_dict()
        device.discovery_at = now
        audit.record(
            "discovery",
            result.summary,
            actor=CLI_ACTOR,
            device=device,
            result="error" if result.status == "error" else "info",
        )
        db.session.commit()
        click.echo(f"  {result.summary}")
        if result.server:
            click.echo(f"  Servidor FTP: {result.server}")
        for candidate in result.candidates:
            click.echo(
                f"  - {candidate.ftp_path} ({candidate.source}, {candidate.video_count} vídeos)"
            )
            for evidence in candidate.evidence:
                click.echo(f"      {evidence}")
        for note in result.notes:
            click.echo(f"  nota: {note}")
    click.echo("\nNada foi gravado: confirme a pasta no painel (página da loja).")


def _print_plan(device: Device, plan: pl.Plan | None, header: str) -> None:
    click.echo(f"\n[{device.store_number}] {device.name} — {header}")
    if plan is None:
        return
    if not plan.actions:
        click.echo("  Em dia: nada a fazer.")
    for index, action in enumerate(plan.actions, start=1):
        click.echo(f"  {index:>2}. {pl.describe(action, plan)}")
    for notice in plan.notices:
        click.echo(f"  [{notice.level}] {notice.message}")


def _print_result(device: Device, result: CycleResult) -> None:
    header = f"{result.status}: {result.message}"
    if result.read_only_reason and result.plan and result.plan.actions and not result.wrote:
        header += f" ({result.read_only_reason})"
    _print_plan(device, result.plan, header)
    for outcome in result.outcomes:
        mark = "ok" if outcome.ok else f"ERRO: {outcome.error}"
        click.echo(f"      -> {outcome.action.kind.value}: {mark}")


@click.command("plan")
@click.argument("code")
@click.option("--offline", is_flag=True, help="Usar a última leitura guardada (sem FTP).")
def plan_command(code: str, offline: bool) -> None:
    """Mostrar o plano de ações sem executar."""
    settings = _settings()
    for device in _devices(code):
        if offline:
            plan = plan_from_snapshot(device, utcnow(), settings)
            when = fmt_datetime(device.snapshot_at, settings.tz)
            header = f"leitura de {when}" if plan else "sem leitura guardada / pasta por confirmar"
            _print_plan(device, plan, header)
            continue
        result = run_cycle(device.id, settings, execute=False, actor=CLI_ACTOR)
        _print_result(device, result)


@click.command("reconcile")
@click.argument("code")
@click.option("--execute", is_flag=True, help="Aplicar o plano (escreve no Pi).")
@click.option("--yes", is_flag=True, help="Não pedir confirmação.")
def reconcile_command(code: str, execute: bool, yes: bool) -> None:
    """Sincronizar uma loja. Sem --execute só lê e mostra o plano."""
    settings = _settings()
    if code == "all":
        raise click.ClickException("Indique uma loja: a escrita é feita uma loja de cada vez.")
    [device] = _devices(code)
    if execute:
        if settings.dry_run:
            raise click.ClickException(
                "Recusado: DRY_RUN=true. Nada é escrito nos Pis enquanto o modo simulação "
                "estiver ativo (.env)."
            )
        if not device.in_config:
            raise click.ClickException(
                f"Recusado: a loja '{device.code}' já não está no devices.yaml."
            )
        if not device.enabled:
            raise click.ClickException(
                f"Recusado: a loja '{device.code}' tem enabled: false no devices.yaml."
            )
        if not yes:
            click.confirm(
                f"Vai ESCREVER no Pi {device.host} ({device.name}). Confirmar?", abort=True
            )
    result = run_cycle(device.id, settings, execute=execute, actor=CLI_ACTOR)
    _print_result(device, result)
    if result.status not in ("online",):
        raise SystemExit(1)


@backup_cli.command("now")
def backup_now() -> None:
    """Criar um backup agora."""
    path = run_backup(_settings())
    audit.record("backup", f"Backup criado (CLI): {path.name}", actor=CLI_ACTOR)
    db.session.commit()
    click.echo(f"Backup criado: {path}")


@backup_cli.command("list")
def backup_list() -> None:
    """Listar os backups existentes."""
    for path in list_backups(_settings().backups_dir):
        click.echo(f"{path.name}  {path.stat().st_size / 1e6:.1f} MB")


@backup_cli.command("restore")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--yes", is_flag=True, help="Não pedir confirmação.")
def backup_restore(file: Path, yes: bool) -> None:
    """Repor a base de dados a partir de um backup (com os serviços parados)."""
    settings = _settings()
    status = worker_status()
    if status.alive:
        raise click.ClickException(
            "O worker está a correr. Pare os serviços antes de repor: docker compose stop"
        )
    if not yes:
        click.confirm(f"Substituir {settings.db_path} por {file.name}?", abort=True)
    db.session.remove()
    db.engine.dispose()
    try:
        restore(file, settings)
    except BackupError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo("Base de dados reposta. Arranque os serviços: docker compose up -d")
