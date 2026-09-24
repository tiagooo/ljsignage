from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lj_signage.config import ConfigError, Settings, load_devices_file, parse_devices

EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "devices.example.yaml"


def minimal(**device):
    base = {"store": "02", "name": "Loja", "code": "ubbo", "hostname": "raspservidorubbo"}
    base.update(device)
    return {"defaults": {"domain": "lugardajoia.internal"}, "devices": [base]}


def test_example_file_is_valid():
    config = load_devices_file(EXAMPLE)
    assert len(config.devices) == 12
    codes = [d.code for d in config.devices]
    assert "no" in codes  # quoted in the file, so not read as a boolean
    ubbo = config.by_code("ubbo")
    assert ubbo.host == "raspservidorubbo.lugardajoia.internal"
    assert ubbo.login_home == "/home/tmagalhaes"
    assert ubbo.enabled is False
    assert ubbo.video_path is None
    assert ubbo.groups == ("sul",)
    assert config.by_code("ub").groups == ("galeria-da-joia", "sul")
    assert config.groups["centro"] == ("fc", "fv")


def test_unquoted_no_is_reported_as_a_yaml_boolean():
    data = yaml.safe_load("devices:\n  - {store: '06', name: X, code: no, hostname: raspno}\n")
    with pytest.raises(ConfigError, match="booleano"):
        parse_devices(data)


def test_unquoted_store_number_is_rejected():
    with pytest.raises(ConfigError, match="store"):
        parse_devices(minimal(store=2))


def test_duplicates_are_reported():
    data = minimal()
    data["devices"].append(dict(data["devices"][0], store="03"))
    with pytest.raises(ConfigError, match="repetido"):
        parse_devices(data)


def test_unknown_keys_and_groups_are_reported_together():
    data = minimal(videopath="/x")
    data["groups"] = {"norte": ["99"], "todas": ["02"]}
    with pytest.raises(ConfigError) as info:
        parse_devices(data)
    message = str(info.value)
    assert "videopath" in message and "'99'" in message and "reservado" in message


def test_active_ftp_mode_is_not_allowed():
    with pytest.raises(ConfigError, match="passive"):
        parse_devices(minimal(ftp_passive=False))


def test_video_path_is_normalised():
    config = parse_devices(minimal(video_path="/home/tmagalhaes/Videos/"))
    assert config.devices[0].video_path == "/home/tmagalhaes/Videos"


def test_settings_from_env():
    settings = Settings.from_env(
        {
            "SECRET_KEY": "x" * 32,
            "DATA_DIR": "/tmp/lj",
            "DRY_RUN": "false",
            "MAX_PARALLEL": "4",
            "ADMIN_EMAILS": " Tiago@LugarDaJoia.com , outra@lugardajoia.com",
            "FTP_USER": "shared",
            "FTP_PASSWORD": "shared-pw",
            "FTP_PASSWORD__UBBO": "ubbo-pw",
            "FTP_USER__GJPT": "gjpt-user",
        }
    )
    assert settings.dry_run is False
    assert settings.max_parallel == 4
    assert settings.admin_emails == {"tiago@lugardajoia.com", "outra@lugardajoia.com"}
    assert settings.ftp_credentials("ubbo") == ("shared", "ubbo-pw")
    assert settings.ftp_credentials("gjpt") == ("gjpt-user", "shared-pw")
    assert settings.ftp_credentials("fa") == ("shared", "shared-pw")


def test_dry_run_is_on_by_default():
    assert Settings.from_env({"SECRET_KEY": "k"}).dry_run is True


def test_secrets_never_appear_in_repr():
    settings = Settings.from_env(
        {"SECRET_KEY": "super-secret", "FTP_PASSWORD": "ftp-pw", "GOOGLE_CLIENT_SECRET": "g-pw"}
    )
    text = repr(settings)
    assert "super-secret" not in text and "ftp-pw" not in text and "g-pw" not in text


def test_secret_key_is_required_outside_debug():
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        Settings.from_env({})
    assert Settings.from_env({"FLASK_DEBUG": "1"}).secret_key


def test_dev_login_requires_debug():
    with pytest.raises(ConfigError, match="AUTH_DEV_LOGIN"):
        Settings.from_env({"SECRET_KEY": "k", "AUTH_DEV_LOGIN": "true"})


@pytest.mark.parametrize(
    ("key", "value"), [("DRY_RUN", "talvez"), ("MAX_PARALLEL", "0"), ("TZ", "Europe/Porto2")]
)
def test_invalid_values_are_rejected(key, value):
    with pytest.raises(ConfigError, match=key):
        Settings.from_env({"SECRET_KEY": "k", key: value})
