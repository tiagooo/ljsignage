"""Synchronise config/devices.yaml (the single source of devices) into the database."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import audit
from .config import DevicesConfig
from .extensions import db
from .models import Device, Group


@dataclass
class SyncReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.removed)

    def summary(self) -> str:
        parts = []
        if self.created:
            parts.append(f"novas: {', '.join(self.created)}")
        if self.updated:
            parts.append(f"alteradas: {', '.join(self.updated)}")
        if self.removed:
            parts.append(f"retiradas: {', '.join(self.removed)}")
        return "; ".join(parts) or "sem alterações"


def forget_folder(device: Device) -> None:
    """Drop everything known about the previous video folder, including pending
    legacy delete requests: they refer to files in that folder only."""
    device.files.clear()
    device.snapshot_at = None
    device.staging_exists = False
    device.plan_digest = None


def sync_devices(config: DevicesConfig, *, actor: audit.Actor | None = None) -> SyncReport:
    """Upsert devices and groups. Devices missing from the file are kept but marked
    as out of config (history and audit entries keep pointing at them)."""
    report = SyncReport()
    devices = {d.code: d for d in db.session.execute(db.select(Device)).scalars()}
    groups = {g.name: g for g in db.session.execute(db.select(Group)).scalars()}

    for name in config.groups:
        if name not in groups:
            groups[name] = Group(name=name)
            db.session.add(groups[name])
        groups[name].in_config = True
        report.groups.append(name)
    for name, group in groups.items():
        if name not in config.groups:
            group.in_config = False

    for cfg in config.devices:
        device = devices.get(cfg.code)
        created = device is None
        if created:
            device = Device(code=cfg.code)
            db.session.add(device)
        changed = created
        values = {
            "store_number": cfg.store,
            "name": cfg.name,
            "hostname": cfg.hostname,
            "host": cfg.host,
            "ftp_port": cfg.ftp_port,
            "login_home": cfg.login_home,
            "enabled": cfg.enabled,
            "max_bytes": cfg.max_bytes,
            "in_config": True,
        }
        for key, value in values.items():
            if getattr(device, key) != value:
                setattr(device, key, value)
                changed = True

        # video_path from the file wins; it is validated on the next cycle.
        if cfg.video_path != device.config_video_path:
            changed = True
            device.config_video_path = cfg.video_path
            if cfg.video_path or device.video_path_source == "config":
                device.video_path = None
                device.video_path_source = "config" if cfg.video_path else None
                device.video_path_confirmed_by = None
                device.video_path_confirmed_at = None
                forget_folder(device)

        wanted = sorted(cfg.groups)
        if sorted(g.name for g in device.groups) != wanted:
            device.groups = [groups[name] for name in wanted]
            changed = True

        if created:
            report.created.append(cfg.code)
        elif changed:
            report.updated.append(cfg.code)

    codes = {cfg.code for cfg in config.devices}
    for code, device in devices.items():
        if code not in codes and device.in_config:
            device.in_config = False
            device.enabled = False  # a device out of the file is never written to
            device.groups = []
            report.removed.append(code)

    if report.changed:
        audit.record("devices_sync", report.summary(), actor=actor or audit.SYSTEM)
    db.session.flush()
    return report
