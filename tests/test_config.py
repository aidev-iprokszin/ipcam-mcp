import os
from pathlib import Path

import pytest

from ipcam_mcp.config import config_path, load_config
from ipcam_mcp.errors import ConfigError, scrub

SECRETS = "CAMERA_IP=10.0.0.5\nCAMERA_USER=u\nCAMERA_PASSWORD=p@ss\n"


def write_device_config(
    tmp_path: Path,
    secrets_file: str,
    extra_top: str = "",
    extra_dev: str = "",
    drop: str | None = None,
) -> Path:
    keys = {
        "type": '"tapo_onvif"',
        "secrets_file": f'"{secrets_file}"',
        "host_var": '"CAMERA_IP"',
        "user_var": '"CAMERA_USER"',
        "password_var": '"CAMERA_PASSWORD"',
    }
    if drop:
        keys.pop(drop)
    body = "\n".join(f"{k} = {v}" for k, v in keys.items())
    cfg = tmp_path / "cameras.toml"
    cfg.write_text(f"{extra_top}\n[cameras.livingroom]\n{body}\n{extra_dev}\n")
    return cfg


@pytest.fixture
def secrets(tmp_path: Path) -> Path:
    f = tmp_path / "secrets.env"
    f.write_text(SECRETS)
    return f


def test_loads_device_from_secrets_file(tmp_path, secrets):
    cfg = load_config(write_device_config(tmp_path, str(secrets), extra_dev="onvif_port = 2020"))
    dev = cfg.devices["livingroom"]
    assert dev.id == "livingroom"
    assert dev.type == "tapo_onvif"
    assert dev.host == "10.0.0.5"
    assert dev.user == "u"
    assert dev.password == "p@ss"
    assert dev.settings == {"onvif_port": 2020}
    assert "CAMERA_PASSWORD" not in os.environ


def test_secret_with_dollar_brace_is_literal(tmp_path):
    f = tmp_path / "s.env"
    f.write_text('CAMERA_IP=10.0.0.5\nCAMERA_USER=u\nCAMERA_PASSWORD="p${HOME}x"\n')
    cfg = load_config(write_device_config(tmp_path, str(f)))
    assert cfg.devices["livingroom"].password == "p${HOME}x"


def test_password_not_in_repr(tmp_path, secrets):
    cfg = load_config(write_device_config(tmp_path, str(secrets)))
    assert "p@ss" not in repr(cfg)


def test_tilde_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "x.env").write_text(SECRETS)
    cfg = load_config(write_device_config(tmp_path, "~/x.env"))
    assert cfg.devices["livingroom"].host == "10.0.0.5"


def test_default_media_dir(tmp_path, secrets):
    cfg = load_config(write_device_config(tmp_path, str(secrets)))
    assert cfg.media_dir == Path("~/.local/share/ipcam-mcp/media").expanduser()


def test_custom_media_dir(tmp_path, secrets):
    media = tmp_path / "media"
    cfg = load_config(
        write_device_config(tmp_path, str(secrets), extra_top=f'media_dir = "{media}"')
    )
    assert cfg.media_dir == media


DEFAULT = ".config/ipcam-mcp/cameras.toml"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("IPCAM_MCP_CONFIG", raising=False)
    return tmp_path


def test_config_path_env(home, monkeypatch):
    monkeypatch.setenv("IPCAM_MCP_CONFIG", str(home / "x.toml"))
    assert config_path() == home / "x.toml"


def test_config_path_empty_env_uses_default(home, monkeypatch):
    monkeypatch.setenv("IPCAM_MCP_CONFIG", "")
    assert config_path() == home / DEFAULT


def test_config_path_env_tilde_expanded(home, monkeypatch):
    monkeypatch.setenv("IPCAM_MCP_CONFIG", "~/x.toml")
    assert config_path() == home / "x.toml"


def test_config_path_default(home):
    assert config_path() == home / DEFAULT


def test_load_config_uses_config_path(tmp_path, secrets, monkeypatch):
    monkeypatch.setenv("IPCAM_MCP_CONFIG", str(write_device_config(tmp_path, str(secrets))))
    assert "livingroom" in load_config().devices


def test_missing_config_file(tmp_path):
    missing = tmp_path / "nope.toml"
    with pytest.raises(ConfigError, match=str(missing)):
        load_config(missing)


def test_missing_config_file_ok_gives_empty_config(tmp_path):
    cfg = load_config(tmp_path / "nope.toml", missing_ok=True)
    assert cfg.devices == {}
    assert cfg.allow_management is True


def test_missing_ok_still_rejects_invalid_toml(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("not = [valid")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(bad, missing_ok=True)


def test_missing_secrets_file(tmp_path):
    missing = tmp_path / "missing.env"
    with pytest.raises(ConfigError) as e:
        load_config(write_device_config(tmp_path, str(missing)))
    assert "livingroom" in str(e.value)
    assert str(missing) in str(e.value)


def test_missing_secret_var(tmp_path):
    f = tmp_path / "s.env"
    f.write_text("CAMERA_IP=10.0.0.5\nCAMERA_USER=u\n")
    with pytest.raises(ConfigError) as e:
        load_config(write_device_config(tmp_path, str(f)))
    assert "livingroom" in str(e.value)
    assert "CAMERA_PASSWORD" in str(e.value)


def test_empty_secret_var(tmp_path):
    f = tmp_path / "s.env"
    f.write_text("CAMERA_IP=10.0.0.5\nCAMERA_USER=u\nCAMERA_PASSWORD=\n")
    with pytest.raises(ConfigError, match="CAMERA_PASSWORD"):
        load_config(write_device_config(tmp_path, str(f)))


def test_missing_required_key(tmp_path, secrets):
    with pytest.raises(ConfigError) as e:
        load_config(write_device_config(tmp_path, str(secrets), drop="type"))
    assert "type" in str(e.value)
    assert "livingroom" in str(e.value)


def test_invalid_toml(tmp_path):
    cfg = tmp_path / "bad.toml"
    cfg.write_text("[cameras\n")
    with pytest.raises(ConfigError, match=str(cfg)):
        load_config(cfg)


def test_scrub():
    out = scrub("rtsp://u:p%40ss@h and p@ss", ["p@ss"])
    assert "p@ss" not in out
    assert "p%40ss" not in out
    assert scrub("abc", [""]) == "abc"


def test_management_defaults(tmp_path, secrets):
    cfg = load_config(write_device_config(tmp_path, str(secrets)))
    assert cfg.allow_management is True
    assert cfg.secrets_dir == Path("~/.config/ipcam-mcp/secrets").expanduser()


def test_management_keys(tmp_path, secrets):
    cfg = load_config(
        write_device_config(
            tmp_path,
            str(secrets),
            extra_top='allow_management = false\nsecrets_dir = "~/sec"',
        )
    )
    assert cfg.allow_management is False
    assert cfg.secrets_dir == Path("~/sec").expanduser()


def test_allow_management_must_be_bool(tmp_path, secrets):
    with pytest.raises(ConfigError, match="allow_management"):
        load_config(
            write_device_config(tmp_path, str(secrets), extra_top='allow_management = "yes"')
        )


def _chmod_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "chmod 600" in r.getMessage()]


def test_world_readable_secrets_warns(tmp_path, secrets, caplog):
    caplog.set_level("WARNING", logger="ipcam_mcp")
    secrets.chmod(0o644)
    load_config(write_device_config(tmp_path, str(secrets)))
    msgs = _chmod_warnings(caplog)
    assert len(msgs) == 1 and str(secrets) in msgs[0]
    assert "p@ss" not in caplog.text and "10.0.0.5" not in caplog.text


def test_private_secrets_silent(tmp_path, secrets, caplog):
    caplog.set_level("WARNING", logger="ipcam_mcp")
    secrets.chmod(0o600)
    load_config(write_device_config(tmp_path, str(secrets)))
    assert _chmod_warnings(caplog) == []


def test_shared_secrets_file_warns_once(tmp_path, secrets, caplog):
    caplog.set_level("WARNING", logger="ipcam_mcp")
    secrets.chmod(0o644)
    second = (
        f'[cameras.office]\ntype = "onvif"\nsecrets_file = "{secrets}"\n'
        'host_var = "CAMERA_IP"\nuser_var = "CAMERA_USER"\npassword_var = "CAMERA_PASSWORD"\n'
    )
    cfg = load_config(write_device_config(tmp_path, str(secrets), extra_dev=second))
    assert set(cfg.devices) == {"livingroom", "office"}
    assert len(_chmod_warnings(caplog)) == 1


def test_example_config_secrets_outside_managed_dir():
    """Hand-made secrets files in the example must not sit where add/rename/remove manage
    files (<secrets_dir>/<id>.env), or remove_camera would delete them."""
    import re

    from ipcam_mcp.config import DEFAULT_SECRETS_DIR

    text = (Path(__file__).parent.parent / "cameras.example.toml").read_text()
    dirs = {Path(DEFAULT_SECRETS_DIR).expanduser()} | {
        Path(d).expanduser() for d in re.findall(r'secrets_dir = "([^"]+)"', text)
    }
    files = re.findall(r'secrets_file = "([^"]+)"', text)
    assert len(files) == 2
    for f in files:
        assert Path(f).expanduser().parent not in dirs, f


def test_permission_check_race_is_not_a_crash(tmp_path, secrets, monkeypatch):
    """If the file vanishes between the existence check and the permission check, loading
    reports a normal ConfigError instead of crashing with an OSError."""
    secrets.unlink()
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    with pytest.raises(ConfigError):
        load_config(write_device_config(tmp_path, str(secrets)))


def test_group_writable_secrets_warning_wording(tmp_path, secrets, caplog):
    caplog.set_level("WARNING", logger="ipcam_mcp")
    secrets.chmod(0o620)  # group-writable, not readable: still exposed
    load_config(write_device_config(tmp_path, str(secrets)))
    assert "accessible by other users" in caplog.text
