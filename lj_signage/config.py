"""Settings (.env) and the devices file (config/devices.yaml).

Settings come from environment variables (see .env.example). The devices file is
the single source of truth for devices and groups (CLAUDE.md, rule 6): no other
module hardcodes hostnames, codes or paths.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(ValueError):
    """Invalid configuration. Messages are in Portuguese: they are shown to admins."""


_TRUE = {"1", "true", "yes", "on", "sim"}
_FALSE = {"0", "false", "no", "off", "nao", "não", ""}


def _get_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{key}: valor inválido ({raw!r}); use true ou false.")


def _get_int(env: Mapping[str, str], key: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key}: tem de ser um número inteiro ({raw!r}).") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key}: tem de estar entre {minimum} e {maximum} ({value}).")
    return value


def _get_float(
    env: Mapping[str, str], key: str, default: float, *, minimum: float, maximum: float
) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key}: tem de ser um número ({raw!r}).") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key}: tem de estar entre {minimum} e {maximum} ({value}).")
    return value


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Secrets are excluded from repr() so they never reach logs."""

    secret_key: str = field(repr=False)
    data_dir: Path
    devices_file: Path
    timezone: str = "Europe/Lisbon"
    log_level: str = "INFO"
    dry_run: bool = True
    reconcile_interval_min: int = 5
    staging_lookahead_h: int = 48
    max_parallel: int = 3
    audio_enabled: bool = False
    max_upload_mb: int = 8192
    ftp_user: str = ""
    ftp_password: str = field(default="", repr=False)
    ftp_timeout_s: float = 10.0
    ftp_overrides: Mapping[str, tuple[str | None, str | None]] = field(
        default_factory=dict, repr=False
    )
    google_client_id: str = ""
    google_client_secret: str = field(default="", repr=False)
    allowed_domain: str = "lugardajoia.com"
    admin_emails: frozenset[str] = frozenset()
    dev_login: bool = False
    session_cookie_secure: bool = False
    trust_proxy: bool = False
    database_url: str | None = None

    # --- paths under DATA_DIR ---------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "lj-signage.db"

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url or f"sqlite:///{self.db_path}"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.media_dir / "originals",
            self.media_dir / "videos",
            self.media_dir / "thumbnails",
            self.backups_dir,
            self.tmp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def ftp_credentials(self, code: str) -> tuple[str, str]:
        """FTP user/password for a device: per-device override or the shared default."""
        user, password = self.ftp_overrides.get(_env_code(code), (None, None))
        return (user or self.ftp_user, password or self.ftp_password)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        debug = _get_bool(env, "FLASK_DEBUG", False)

        secret_key = env.get("SECRET_KEY", "").strip()
        if not secret_key:
            if not debug:
                raise ConfigError(
                    "SECRET_KEY em falta no .env. Gere uma com: "
                    'python3 -c "import secrets; print(secrets.token_hex(32))"'
                )
            secret_key = secrets.token_hex(32)

        timezone = env.get("TZ", "").strip() or "Europe/Lisbon"
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"TZ: fuso horário desconhecido ({timezone!r}).") from exc

        overrides: dict[str, tuple[str | None, str | None]] = {}
        for key, value in env.items():
            for prefix, index in (("FTP_USER__", 0), ("FTP_PASSWORD__", 1)):
                if key.startswith(prefix) and value:
                    code = key[len(prefix) :]
                    pair = list(overrides.get(code, (None, None)))
                    pair[index] = value
                    overrides[code] = (pair[0], pair[1])

        admin_emails = frozenset(
            email.strip().lower()
            for email in env.get("ADMIN_EMAILS", "").split(",")
            if email.strip()
        )

        dev_login = _get_bool(env, "AUTH_DEV_LOGIN", False)
        if dev_login and not debug:
            raise ConfigError(
                "AUTH_DEV_LOGIN=true só é permitido em desenvolvimento (FLASK_DEBUG=1)."
            )

        return cls(
            secret_key=secret_key,
            data_dir=Path(env.get("DATA_DIR", "").strip() or "./data").resolve(),
            devices_file=Path(env.get("DEVICES_FILE", "").strip() or "config/devices.yaml"),
            timezone=timezone,
            log_level=(env.get("LOG_LEVEL", "").strip() or "INFO").upper(),
            dry_run=_get_bool(env, "DRY_RUN", True),
            reconcile_interval_min=_get_int(
                env, "RECONCILE_INTERVAL_MIN", 5, minimum=1, maximum=1440
            ),
            staging_lookahead_h=_get_int(env, "STAGING_LOOKAHEAD_H", 48, minimum=0, maximum=720),
            max_parallel=_get_int(env, "MAX_PARALLEL", 3, minimum=1, maximum=12),
            audio_enabled=_get_bool(env, "AUDIO_ENABLED", False),
            max_upload_mb=_get_int(env, "MAX_UPLOAD_MB", 8192, minimum=1, maximum=65536),
            ftp_user=env.get("FTP_USER", "").strip(),
            ftp_password=env.get("FTP_PASSWORD", ""),
            ftp_timeout_s=_get_float(env, "FTP_TIMEOUT_S", 10.0, minimum=1.0, maximum=120.0),
            ftp_overrides=overrides,
            google_client_id=env.get("GOOGLE_CLIENT_ID", "").strip(),
            google_client_secret=env.get("GOOGLE_CLIENT_SECRET", "").strip(),
            allowed_domain=(
                env.get("AUTH_ALLOWED_DOMAIN", "").strip() or "lugardajoia.com"
            ).lower(),
            admin_emails=admin_emails,
            dev_login=dev_login,
            session_cookie_secure=_get_bool(env, "SESSION_COOKIE_SECURE", False),
            trust_proxy=_get_bool(env, "TRUST_PROXY", False),
            database_url=env.get("DATABASE_URL", "").strip() or None,
        )


def _env_code(code: str) -> str:
    """Device code as used in env var names: FTP_PASSWORD__<CODE>."""
    return code.upper().replace("-", "_")


# ---------------------------------------------------------------------------
# config/devices.yaml
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")
_STORE_RE = re.compile(r"^[0-9]{2,3}$")
_GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
RESERVED_GROUPS = frozenset({"todas", "all"})

_DEFAULT_KEYS = {
    "domain",
    "ftp_port",
    "ftp_passive",
    "login_home",
    "enabled",
    "max_bytes",
}
_DEVICE_KEYS = _DEFAULT_KEYS | {"store", "name", "code", "hostname", "video_path"}


@dataclass(frozen=True)
class DeviceConfig:
    store: str
    name: str
    code: str
    hostname: str
    host: str
    ftp_port: int
    ftp_passive: bool
    login_home: str
    video_path: str | None
    enabled: bool
    max_bytes: int | None
    groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class DevicesConfig:
    devices: tuple[DeviceConfig, ...]
    groups: Mapping[str, tuple[str, ...]]  # group name -> device codes

    def by_code(self, code: str) -> DeviceConfig | None:
        return next((d for d in self.devices if d.code == code), None)


def load_devices_file(path: Path) -> DevicesConfig:
    if not path.exists():
        raise ConfigError(
            f"Ficheiro de dispositivos não encontrado: {path}. "
            "Copie config/devices.example.yaml para config/devices.yaml."
        )
    with path.open(encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: YAML inválido ({exc}).") from exc
    return parse_devices(data, source=str(path))


def parse_devices(data: object, *, source: str = "devices.yaml") -> DevicesConfig:
    """Validate the parsed YAML and return an immutable DevicesConfig.

    All problems are collected and reported together, so an admin can fix the
    file in one go.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: o ficheiro tem de ser um mapa com 'devices'.")

    unknown_top = set(data) - {"defaults", "groups", "devices"}
    if unknown_top:
        errors.append(f"chaves desconhecidas no topo: {', '.join(sorted(map(str, unknown_top)))}")

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        errors.append("'defaults' tem de ser um mapa.")
        defaults = {}
    unknown = set(defaults) - _DEFAULT_KEYS
    if unknown:
        errors.append(f"defaults: chaves desconhecidas: {', '.join(sorted(map(str, unknown)))}")

    raw_devices = data.get("devices")
    if not isinstance(raw_devices, list) or not raw_devices:
        errors.append("'devices' tem de ser uma lista não vazia.")
        raw_devices = []

    devices: list[dict] = []
    seen: dict[str, dict[str, int]] = {"store": {}, "code": {}, "host": {}}
    for index, raw in enumerate(raw_devices, start=1):
        where = f"devices[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{where}: cada dispositivo tem de ser um mapa.")
            continue
        unknown = set(raw) - _DEVICE_KEYS
        if unknown:
            errors.append(f"{where}: chaves desconhecidas: {', '.join(sorted(map(str, unknown)))}")
        merged = {**defaults, **raw}
        device = _parse_device(merged, where, errors)
        if device is None:
            continue
        where = f"{where} ({device['code']})"
        for key, value in (
            ("store", device["store"]),
            ("code", device["code"]),
            ("host", device["host"]),
        ):
            if value in seen[key]:
                errors.append(
                    f"{where}: {key} '{value}' repetido (já usado em devices[{seen[key][value]}])."
                )
            seen[key][value] = index
        devices.append(device)

    store_to_code = {d["store"]: d["code"] for d in devices}
    groups: dict[str, tuple[str, ...]] = {}
    raw_groups = data.get("groups") or {}
    if not isinstance(raw_groups, dict):
        errors.append("'groups' tem de ser um mapa nome -> lista de lojas.")
        raw_groups = {}
    for name, members in raw_groups.items():
        if not isinstance(name, str) or not _GROUP_RE.match(name):
            errors.append(f"grupo {name!r}: nome inválido (minúsculas, números e '-').")
            continue
        if name in RESERVED_GROUPS:
            errors.append(f"grupo '{name}': nome reservado ('todas' é implícito).")
            continue
        if not isinstance(members, list):
            errors.append(f"grupo '{name}': tem de ser uma lista de números de loja.")
            continue
        codes = []
        for member in members:
            if not isinstance(member, str):
                errors.append(
                    f"grupo '{name}': a loja {member!r} tem de estar entre aspas (ex.: \"04\")."
                )
                continue
            if member not in store_to_code:
                errors.append(f"grupo '{name}': a loja '{member}' não existe em devices.")
                continue
            if store_to_code[member] not in codes:
                codes.append(store_to_code[member])
        groups[name] = tuple(codes)

    if errors:
        raise ConfigError(f"{source} inválido:\n- " + "\n- ".join(errors))

    device_groups: dict[str, list[str]] = {d["code"]: [] for d in devices}
    for name, codes in sorted(groups.items()):
        for code in codes:
            device_groups[code].append(name)

    return DevicesConfig(
        devices=tuple(
            DeviceConfig(**device, groups=tuple(device_groups[device["code"]]))
            for device in devices
        ),
        groups=groups,
    )


def _parse_device(merged: dict, where: str, errors: list[str]) -> dict | None:
    start = len(errors)

    code = merged.get("code")
    if isinstance(code, bool):
        errors.append(
            f"{where}: code {code!r} foi lido como booleano pelo YAML; "
            'escreva-o entre aspas (ex.: code: "no").'
        )
    elif not isinstance(code, str) or not _CODE_RE.match(code):
        errors.append(f"{where}: code {code!r} inválido (minúsculas, números e '-').")

    store = merged.get("store")
    if not isinstance(store, str) or not _STORE_RE.match(store):
        errors.append(f'{where}: store {store!r} inválido; use texto entre aspas (ex.: "02").')

    name = merged.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{where}: name em falta.")

    hostname = merged.get("hostname")
    if not isinstance(hostname, str) or not _HOST_RE.match(hostname):
        errors.append(f"{where}: hostname {hostname!r} inválido.")

    domain = merged.get("domain")
    if domain is not None and (not isinstance(domain, str) or not _HOST_RE.match(domain)):
        errors.append(f"{where}: domain {domain!r} inválido.")

    port = merged.get("ftp_port", 21)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        errors.append(f"{where}: ftp_port {port!r} inválido.")

    passive = merged.get("ftp_passive", True)
    if passive is not True:
        errors.append(f"{where}: ftp_passive tem de ser true (o modo ativo não é suportado).")

    login_home = merged.get("login_home", "/")
    if not isinstance(login_home, str) or not login_home.startswith("/"):
        errors.append(f"{where}: login_home {login_home!r} tem de ser um caminho absoluto.")

    video_path = merged.get("video_path")
    if video_path is not None and (
        not isinstance(video_path, str)
        or not video_path.strip()
        or any(ch in video_path for ch in "\r\n\0")
    ):
        errors.append(f"{where}: video_path {video_path!r} inválido.")

    enabled = merged.get("enabled", False)
    if not isinstance(enabled, bool):
        errors.append(f"{where}: enabled tem de ser true ou false.")

    max_bytes = merged.get("max_bytes")
    if max_bytes is not None and (
        isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0
    ):
        errors.append(f"{where}: max_bytes tem de ser um número de bytes positivo ou null.")

    if len(errors) > start:
        return None

    host = hostname if "." in hostname or not domain else f"{hostname}.{domain}"
    return {
        "store": store,
        "name": name.strip(),
        "code": code,
        "hostname": hostname,
        "host": host,
        "ftp_port": port,
        "ftp_passive": True,
        "login_home": login_home.rstrip("/") or "/",
        "video_path": (video_path.strip().rstrip("/") or "/") if video_path else None,
        "enabled": enabled,
        "max_bytes": max_bytes,
    }
