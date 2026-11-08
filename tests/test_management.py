import asyncio
import errno
import re
import shutil
import stat
import tomllib
from datetime import datetime
from pathlib import Path

import pytest
from conftest import FakeCamPTZ, uid_for
from dotenv import dotenv_values

from ipcam_mcp import store
from ipcam_mcp.adapters.onvif import DeviceInfo
from ipcam_mcp.config import load_config
from ipcam_mcp.errors import ConfigError, DeviceError
from ipcam_mcp.management import (
    Credentials,
    Manager,
    prepare_config,
    validate_host,
    validate_id,
)
from ipcam_mcp.registry import Registry
from ipcam_mcp.scenes import latest_in_scene
from ipcam_mcp.store import UID_PATTERN, CameraMeta, read_meta, write_meta

USER = "camuser"
PASSWORD = "s3cr3t-pw"


class FakeProbe:
    def __init__(self, fail: bool = False, info: DeviceInfo | None = None):
        self.calls: list[tuple[str, str, str, int | None]] = []
        self.fail = fail
        self.info = info or DeviceInfo("TP-Link", "C210", True, 2020)

    async def __call__(
        self, host: str, user: str, password: str, port: int | None = None
    ) -> DeviceInfo:
        self.calls.append((host, user, password, port))
        await asyncio.sleep(0.01)  # let concurrent operations interleave
        if self.fail:
            raise DeviceError(f"{host}:2020: ONVIF login failed")
        return self.info


def prompt_with(creds: Credentials | None):
    messages: list[str] = []

    async def prompt(message: str) -> Credentials | None:
        messages.append(message)
        return creds

    prompt.messages = messages
    return prompt


@pytest.fixture
def env(tmp_path):
    """A cameras.toml with one camera on a shared secrets file, plus a secrets_dir."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    shared = tmp_path / "secrets.env"
    shared.write_text('OFFICE_HOST="10.0.0.2"\nOFFICE_USER="u"\nOFFICE_PASS="p"\n')
    cfg = tmp_path / "cameras.toml"
    cfg.write_text(
        f'media_dir = "{tmp_path / "media"}"\n'
        f'secrets_dir = "{secrets_dir}"\n'
        "\n"
        "# the office camera\n"
        "[cameras.office]\n"
        f'uid = "{uid_for("office")}"\n'
        'type = "tapo_onvif"\n'
        f'secrets_file = "{shared}"\n'
        'host_var = "OFFICE_HOST"\n'
        'user_var = "OFFICE_USER"\n'
        'password_var = "OFFICE_PASS"\n'
    )
    return cfg, secrets_dir, shared


def make_manager(cfg: Path, probe=None, load=load_config) -> tuple[Manager, Registry, FakeProbe]:
    probe = probe or FakeProbe()
    registry = Registry(load_config(cfg), adapters={"tapo_onvif": FakeCamPTZ, "onvif": FakeCamPTZ})
    return Manager(cfg, registry, probe=probe, load=load), registry, probe


def ids(registry: Registry) -> list[str]:
    return [c["id"] for c in registry.list()]


def write_env(path: Path, host: str, user: str, password: str) -> None:
    path.write_text(f'HOST="{host}"\nUSER="{user}"\nPASSWORD="{password}"\n')
    path.chmod(0o600)


async def test_add_form_success(env):
    cfg, secrets_dir, _ = env
    mgr, registry, probe = make_manager(cfg)
    prompt = prompt_with(Credentials(USER, PASSWORD))

    result = await mgr.add("garage", "10.0.0.5", prompt)

    env_file = secrets_dir / "garage.env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert dotenv_values(env_file, interpolate=False) == {
        "HOST": "10.0.0.5",
        "USER": USER,
        "PASSWORD": PASSWORD,
    }
    text = cfg.read_text()
    assert "[cameras.garage]" in text
    assert "# the office camera" in text
    assert ids(registry) == ["office", "garage"]
    assert probe.calls == [("10.0.0.5", USER, PASSWORD, None)]
    assert "C210" in result
    assert "camera, ptz" in result
    assert PASSWORD not in result and USER not in result
    assert "not sent to the AI model" in prompt.messages[0]
    assert "10.0.0.5" in prompt.messages[0]


async def test_add_writes_onvif_section_and_passes_port(env):
    cfg, _, _ = env
    mgr, _, probe = make_manager(cfg)
    result = await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)), 8000)
    assert probe.calls == [("10.0.0.5", USER, PASSWORD, 8000)]
    section = tomllib.loads(cfg.read_text())["cameras"]["garage"]
    assert section["type"] == "onvif"
    assert section["onvif_port"] == 2020
    assert section["ptz"] is True
    assert section["invert_pan"] is True
    assert section["host_var"] == "HOST"
    assert result.endswith("; onvif_port 2020")


async def test_add_non_tplink_has_no_invert_pan_and_loads_back(env):
    cfg, _, _ = env
    info = DeviceInfo("Hikvision", "DS-2", False, 80)
    mgr = Manager(cfg, Registry(load_config(cfg)), probe=FakeProbe(info=info), load=load_config)
    result = await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    section = tomllib.loads(cfg.read_text())["cameras"]["garage"]
    assert "invert_pan" not in section
    assert section["ptz"] is False and section["onvif_port"] == 80
    assert "capabilities: camera;" in result


def test_credentials_repr_hides_password():
    assert PASSWORD not in repr(Credentials(USER, PASSWORD))


async def test_add_form_cancelled(env):
    cfg, secrets_dir, _ = env
    before = cfg.read_bytes()
    mgr, registry, probe = make_manager(cfg)

    result = await mgr.add("garage", "10.0.0.5", prompt_with(None))

    assert "nothing was changed" in result
    assert not (secrets_dir / "garage.env").exists()
    assert cfg.read_bytes() == before
    assert probe.calls == []
    assert ids(registry) == ["office"]


async def test_add_file_mode_two_step(env):
    cfg, secrets_dir, _ = env
    before = cfg.read_bytes()
    mgr, registry, probe = make_manager(cfg)
    env_file = secrets_dir / "garage.env"

    first = await mgr.add("garage", "10.0.0.5", None)
    assert first == (
        f"Fill in USER and PASSWORD in {env_file}, then call add_camera again "
        "with name 'garage', the same host and credentials_via=\"file\"."
    )
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert dotenv_values(env_file, interpolate=False) == {
        "HOST": "10.0.0.5",
        "USER": "",
        "PASSWORD": "",
    }
    assert cfg.read_bytes() == before

    second = await mgr.add("garage", "10.0.0.5", None)
    assert second == first
    assert cfg.read_bytes() == before
    assert probe.calls == []

    write_env(env_file, "10.0.0.5", USER, PASSWORD)
    third = await mgr.add("garage", "10.0.0.5", None)
    assert "C210" in third
    assert PASSWORD not in third and USER not in third
    assert probe.calls == [("10.0.0.5", USER, PASSWORD, None)]
    assert ids(registry) == ["office", "garage"]
    assert env_file.exists()


async def test_add_form_refuses_when_env_file_exists(env):
    cfg, secrets_dir, _ = env
    before = cfg.read_bytes()
    mgr, registry, probe = make_manager(cfg)
    env_file = secrets_dir / "garage.env"
    write_env(env_file, "10.0.0.5", "", "")
    prompt = prompt_with(Credentials(USER, PASSWORD))

    with pytest.raises(ValueError, match='credentials_via="file"') as e:
        await mgr.add("garage", "10.0.0.5", prompt)

    assert str(env_file) in str(e.value)
    assert prompt.messages == []
    assert probe.calls == []
    assert cfg.read_bytes() == before
    assert ids(registry) == ["office"]


async def test_add_file_mode_refuses_file_used_by_other_camera(env):
    cfg, secrets_dir, shared = env
    # The hand-made shared file lives in secrets_dir, so id "secrets" would map onto it.
    in_dir = secrets_dir / "secrets.env"
    content = shared.read_text() + 'HOST="10.0.0.5"\nUSER="u"\nPASSWORD="p"\n'
    in_dir.write_text(content)
    cfg.write_text(cfg.read_text().replace(str(shared), str(in_dir)))
    before = cfg.read_bytes()
    mgr, registry, probe = make_manager(cfg)

    with pytest.raises(ValueError, match="office") as e:
        await mgr.add("secrets", "10.0.0.5", None)

    assert str(in_dir) in str(e.value)
    assert probe.calls == []
    assert cfg.read_bytes() == before
    assert in_dir.read_text() == content
    assert ids(registry) == ["office"]


async def test_add_file_mode_host_mismatch(env):
    cfg, secrets_dir, _ = env
    before = cfg.read_bytes()
    write_env(secrets_dir / "garage.env", "10.0.0.9", USER, PASSWORD)
    mgr, _, probe = make_manager(cfg)

    with pytest.raises(ValueError) as e:
        await mgr.add("garage", "10.0.0.5", None)
    assert "HOST" in str(e.value)
    assert PASSWORD not in str(e.value) and USER not in str(e.value)
    assert probe.calls == []
    assert cfg.read_bytes() == before


async def test_add_probe_failure_writes_nothing(env):
    cfg, secrets_dir, _ = env
    before = cfg.read_bytes()
    mgr, registry, _ = make_manager(cfg, probe=FakeProbe(fail=True))

    with pytest.raises(DeviceError):
        await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    assert not (secrets_dir / "garage.env").exists()
    assert cfg.read_bytes() == before

    write_env(secrets_dir / "garage.env", "10.0.0.5", USER, PASSWORD)
    with pytest.raises(DeviceError):
        await mgr.add("garage", "10.0.0.5", None)
    assert (secrets_dir / "garage.env").exists()
    assert cfg.read_bytes() == before
    assert ids(registry) == ["office"]


async def test_add_reload_failure_rolls_back(env):
    cfg, secrets_dir, _ = env
    before = cfg.read_bytes()
    calls = 0

    def flaky_load(path: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConfigError("boom")
        return load_config(path)

    mgr, registry, _ = make_manager(cfg, load=flaky_load)

    with pytest.raises(ConfigError, match="boom"):
        await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    assert cfg.read_bytes() == before
    assert not (secrets_dir / "garage.env").exists()
    assert ids(registry) == ["office"]


@pytest.mark.parametrize("bad", ["Office", "my camera", "-x", "a" * 33])
async def test_invalid_ids_rejected(env, bad):
    cfg, secrets_dir, _ = env
    before = cfg.read_bytes()
    mgr, _, probe = make_manager(cfg)

    with pytest.raises(ValueError, match="a-z0-9"):
        validate_id(bad)
    with pytest.raises(ValueError, match="a-z0-9"):
        await mgr.add(bad, "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    with pytest.raises(ValueError, match="a-z0-9"):
        await mgr.rename("office", bad)
    assert cfg.read_bytes() == before
    assert list(secrets_dir.iterdir()) == []
    assert probe.calls == []


@pytest.mark.parametrize("good", ["a", "garage", "cam_2", "0-front", "a" * 32])
def test_valid_ids_accepted(good):
    validate_id(good)


@pytest.mark.parametrize(
    "bad",
    ["http://1.2.3.4", "1.2.3.4:2020", "a b", "user@1.2.3.4", "", "fe80::1%eth0", "[fe80::1]"],
)
async def test_invalid_hosts_rejected(env, bad):
    cfg, secrets_dir, _ = env
    mgr, _, _ = make_manager(cfg)

    with pytest.raises(ValueError) as e:
        validate_host(bad)
    assert "1.2.3.4" not in str(e.value)  # the host (which may hold a user name) is not echoed
    with pytest.raises(ValueError):
        await mgr.add("garage", bad, None)
    assert list(secrets_dir.iterdir()) == []


@pytest.mark.parametrize("good", ["192.168.1.42", "fe80::1", "cam.local", "my-cam", "a.b-c.d"])
def test_valid_hosts_accepted(good):
    validate_host(good)


async def test_concurrent_add_same_id(env):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    prompt = prompt_with(Credentials(USER, PASSWORD))

    results = await asyncio.gather(
        mgr.add("garage", "10.0.0.5", prompt),
        mgr.add("garage", "10.0.0.5", prompt),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, str)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1 and len(errors) == 1
    # The id check runs under the lock, so the loser sees the camera, not its .env.
    assert isinstance(errors[0], ValueError)
    assert str(errors[0]) == "camera 'garage' already exists"
    assert cfg.read_text().count("[cameras.garage]") == 1
    assert ids(registry) == ["office", "garage"]


async def test_add_existing_id_rejected(env):
    cfg, _, _ = env
    mgr, _, probe = make_manager(cfg)
    with pytest.raises(ValueError, match="camera 'office' already exists"):
        await mgr.add("office", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    assert probe.calls == []


async def test_refuses_when_config_invalid(env):
    cfg, secrets_dir, _ = env
    mgr, _, probe = make_manager(cfg)
    cfg.write_text("[cameras\n")
    before = cfg.read_bytes()

    with pytest.raises(ConfigError):
        await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    with pytest.raises(ConfigError):
        await mgr.rename("office", "front")
    with pytest.raises(ConfigError):
        await mgr.remove("office")
    assert cfg.read_bytes() == before
    assert list(secrets_dir.iterdir()) == []
    assert probe.calls == []


async def test_rename_managed_secrets(env):
    cfg, secrets_dir, _ = env
    mgr, registry, _ = make_manager(cfg)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))

    result = await mgr.rename("garage", "carport")

    assert "carport" in result
    assert not (secrets_dir / "garage.env").exists()
    assert (secrets_dir / "carport.env").exists()
    text = cfg.read_text()
    assert "[cameras.garage]" not in text
    assert "[cameras.carport]" in text
    assert str(secrets_dir / "carport.env") in text
    assert ids(registry) == ["office", "carport"]


async def test_rename_managed_secrets_target_exists(env):
    cfg, secrets_dir, _ = env
    mgr, registry, _ = make_manager(cfg)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    (secrets_dir / "carport.env").write_text("X=1\n")
    before = cfg.read_bytes()

    with pytest.raises(ConfigError, match="already exists"):
        await mgr.rename("garage", "carport")
    assert (secrets_dir / "garage.env").exists()
    assert (secrets_dir / "carport.env").read_text() == "X=1\n"
    assert cfg.read_bytes() == before
    assert ids(registry) == ["office", "garage"]


async def test_rename_reload_failure_rolls_back(env):
    cfg, secrets_dir, _ = env
    calls = 0

    def load(path: Path):
        nonlocal calls
        calls += 1
        if calls == 4:  # add: 1, 2; rename: 3, then the reload (4)
            raise ConfigError("boom")
        return load_config(path)

    mgr, registry, _ = make_manager(cfg, load=load)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    before = cfg.read_bytes()

    with pytest.raises(ConfigError, match="boom"):
        await mgr.rename("garage", "carport")
    assert cfg.read_bytes() == before
    assert (secrets_dir / "garage.env").exists()
    assert not (secrets_dir / "carport.env").exists()
    assert ids(registry) == ["office", "garage"]


async def test_rename_shared_secrets_untouched(env):
    cfg, _, shared = env
    mgr, registry, _ = make_manager(cfg)
    shared_before = shared.read_bytes()

    await mgr.rename("office", "front")

    assert shared.read_bytes() == shared_before
    text = cfg.read_text()
    assert "[cameras.front]" in text
    assert f'secrets_file = "{shared}"' in text
    assert ids(registry) == ["front"]


async def test_rename_errors(env):
    cfg, _, _ = env
    mgr, _, _ = make_manager(cfg)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    with pytest.raises(ValueError, match="camera 'nope' not found"):
        await mgr.rename("nope", "x")
    with pytest.raises(ValueError, match="camera 'garage' already exists"):
        await mgr.rename("office", "garage")


async def test_remove_managed_and_shared(env):
    cfg, secrets_dir, shared = env
    mgr, registry, _ = make_manager(cfg)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    managed = secrets_dir / "garage.env"

    # Hand-edited: a second camera referencing garage's managed file.
    with cfg.open("a") as f:
        f.write(
            "\n[cameras.twin]\n"
            f'uid = "{uid_for("twin")}"\n'
            'type = "tapo_onvif"\n'
            f'secrets_file = "{managed}"\n'
            'host_var = "HOST"\nuser_var = "USER"\npassword_var = "PASSWORD"\n'
        )
    await mgr.reload()
    assert ids(registry) == ["office", "garage", "twin"]

    await mgr.remove("garage")
    assert managed.exists()  # still referenced by twin
    assert ids(registry) == ["office", "twin"]

    await mgr.remove("office")
    assert shared.exists()  # shared file is never deleted
    assert ids(registry) == ["twin"]

    await mgr.rename("twin", "garage")  # twin's file is not managed for 'twin'
    assert managed.exists()
    await mgr.remove("garage")  # now garage.env is managed and unreferenced
    assert not managed.exists()
    assert ids(registry) == []
    assert "[cameras" not in cfg.read_text().replace("[cameras]", "")


async def test_remove_unknown(env):
    cfg, _, _ = env
    mgr, _, _ = make_manager(cfg)
    with pytest.raises(ValueError, match="camera 'nope' not found"):
        await mgr.remove("nope")


async def test_remove_reload_failure_keeps_secrets(env):
    cfg, secrets_dir, _ = env
    calls = 0

    def load(path: Path):
        nonlocal calls
        calls += 1
        if calls == 4:  # add: 1, 2; remove: 3, then the reload (4)
            raise ConfigError("boom")
        return load_config(path)

    mgr, registry, _ = make_manager(cfg, load=load)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    before = cfg.read_bytes()

    with pytest.raises(ConfigError, match="boom"):
        await mgr.remove("garage")
    assert cfg.read_bytes() == before
    assert (secrets_dir / "garage.env").exists()
    assert ids(registry) == ["office", "garage"]


async def test_reload_returns_list(env):
    cfg, _, _ = env
    mgr, _, _ = make_manager(cfg, load=prepare_config)
    text = cfg.read_text().replace("[cameras.office]", "[cameras.desk]")
    cfg.write_text(text)
    assert await mgr.reload() == [
        {
            "id": "desk",
            "uid": uid_for("office"),
            "scene": "desk",  # no media folder yet: no history to keep
            "type": "tapo_onvif",
            "capabilities": ["camera", "ptz"],
        }
    ]


# --- first run: no cameras.toml yet ---


@pytest.fixture
def fresh(tmp_path, monkeypatch) -> tuple[Path, Manager, Registry, Path]:
    """No config file yet; HOME points at tmp so the default secrets_dir lands there."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / ".config" / "ipcam-mcp" / "cameras.toml"
    registry = Registry(
        load_config(cfg, missing_ok=True),
        adapters={"tapo_onvif": FakeCamPTZ, "onvif": FakeCamPTZ},
    )
    mgr = Manager(cfg, registry, probe=FakeProbe())
    return cfg, mgr, registry, tmp_path / ".config" / "ipcam-mcp" / "secrets"


async def test_add_creates_missing_config(fresh):
    cfg, mgr, registry, secrets_dir = fresh

    result = await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))

    assert "added 'garage'" in result
    assert tomllib.loads(cfg.read_text())["cameras"]["garage"]["type"] == "onvif"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert (secrets_dir / "garage.env").exists()
    assert ids(registry) == ["garage"]


async def test_add_file_mode_with_missing_config(fresh):
    cfg, mgr, _, secrets_dir = fresh
    result = await mgr.add("garage", "10.0.0.5", None)
    assert "Fill in USER and PASSWORD" in result
    assert (secrets_dir / "garage.env").exists()
    assert not cfg.exists()


async def test_add_failure_with_missing_config_leaves_no_file(fresh, monkeypatch):
    cfg, mgr, registry, secrets_dir = fresh

    def broken_reload(config):
        raise ConfigError("boom")

    monkeypatch.setattr(registry, "reload", broken_reload)
    with pytest.raises(ConfigError, match="boom"):
        await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    assert not cfg.exists()
    assert not (secrets_dir / "garage.env").exists()
    assert ids(registry) == []


async def test_reload_with_missing_config_is_empty(fresh):
    _, mgr, _, _ = fresh
    assert await mgr.reload() == []


@pytest.mark.parametrize("op", ["rename", "remove"])
async def test_rename_remove_with_missing_config_not_found(fresh, op):
    cfg, mgr, _, _ = fresh
    with pytest.raises(ValueError, match="'garage' not found"):
        if op == "rename":
            await mgr.rename("garage", "carport")
        else:
            await mgr.remove("garage")
    assert not cfg.exists()


def test_prepare_config_assigns_and_loads(env):
    cfg, _, _ = env
    cfg.write_text(cfg.read_text().replace(f'uid = "{uid_for("office")}"\n', ""))
    loaded = prepare_config(cfg)
    uid = loaded.devices["office"].uid
    assert re.fullmatch(UID_PATTERN, uid)
    assert tomllib.loads(cfg.read_text())["cameras"]["office"]["uid"] == uid


def test_prepare_config_missing_file_is_empty(tmp_path):
    assert prepare_config(tmp_path / "nope.toml").devices == {}


# --- uids and per-camera folders ---


async def test_add_writes_uid_and_camera_json(env):
    cfg, _, _ = env
    mgr, _, _ = make_manager(cfg)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    uid = tomllib.loads(cfg.read_text())["cameras"]["garage"]["uid"]
    assert re.fullmatch(UID_PATTERN, uid)
    assert read_meta(cfg.parent / "media" / uid).name == "garage"


async def test_add_uid_avoids_existing_folders(env, monkeypatch):
    cfg, _, _ = env
    (cfg.parent / "media" / "aaaaaaaa").mkdir(parents=True)
    chars = iter("aaaaaaaabbbbbbbb")
    monkeypatch.setattr(store.secrets, "choice", lambda _alphabet: next(chars))
    mgr, _, _ = make_manager(cfg)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    assert tomllib.loads(cfg.read_text())["cameras"]["garage"]["uid"] == "bbbbbbbb"


async def test_rename_moves_no_files_and_updates_meta(env):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    folder = registry.folder("office")
    (folder / "office_20261103-111200.jpg").write_bytes(b"jpg")

    result = await mgr.rename("office", "front")

    assert result == "renamed 'office' to 'front'"
    assert read_meta(folder).name == "front"  # before registry.folder() could rewrite it
    assert registry.folder("front") == folder
    meta = read_meta(folder)
    assert (
        latest_in_scene(folder, meta, meta.current_scene) == folder / "office_20261103-111200.jpg"
    )
    assert sorted(p.name for p in folder.parent.iterdir()) == [uid_for("office")]


async def test_rename_to_name_of_removed_camera_notes_it(env):
    cfg, _, _ = env
    media = cfg.parent / "media"
    old = media / "zzzzzzzz"
    write_meta(old, CameraMeta("zzzzzzzz", "front", datetime(2025, 1, 1).astimezone()))
    (old / "front_20250101-000000.jpg").write_bytes(b"old")
    mgr, registry, _ = make_manager(cfg)

    result = await mgr.rename("office", "front")

    assert "removed camera named 'front'" in result
    assert read_meta(old).name == "front" and (old / "front_20250101-000000.jpg").exists()
    assert registry.folder("front") == media / uid_for("office")


def test_prepare_config_warns_about_detached_history(env, caplog):
    """A section that lost its uid (editor save, old backup) gets a new one; the earlier
    camera's folder with the same name is pointed out instead of silently orphaned."""
    cfg, _, _ = env
    media = cfg.parent / "media"
    write_meta(
        media / "zzzzzzzz", CameraMeta("zzzzzzzz", "office", datetime(2025, 1, 1).astimezone())
    )
    cfg.write_text(cfg.read_text().replace(f'uid = "{uid_for("office")}"\n', ""))
    with caplog.at_level("WARNING", logger="ipcam_mcp"):
        prepare_config(cfg)
    assert "zzzzzzzz" in caplog.text and 'uid = "zzzzzzzz"' in caplog.text


# --- delete_camera ---


def choosing(answer):
    calls: list[tuple[str, list[str]]] = []

    async def confirm(message: str, targets: list[str]):
        calls.append((message, targets))
        return answer

    confirm.calls = calls
    return confirm


def removed_folder(cfg: Path, uid: str, name: str, created=datetime(2025, 1, 1)) -> Path:
    folder = cfg.parent / "media" / uid
    write_meta(folder, CameraMeta(uid, name, created.astimezone()))
    (folder / f"{name}_20250101-000000.jpg").write_bytes(b"x" * 2_000_000)
    return folder


async def test_delete_configured_camera(env):
    cfg, secrets_dir, _ = env
    mgr, registry, _ = make_manager(cfg)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    folder = registry.folder("garage")
    (folder / "garage_20261103-111200.jpg").write_bytes(b"x")
    confirm = choosing(0)

    result = await mgr.delete("garage", confirm)

    assert result.startswith("deleted 'garage'")
    assert not folder.exists()
    assert not (secrets_dir / "garage.env").exists()
    assert "[cameras.garage]" not in cfg.read_text()
    assert ids(registry) == ["office"]
    assert len(confirm.calls[0][1]) == 1


async def test_delete_removed_camera(env):
    cfg, _, _ = env
    folder = removed_folder(cfg, "zzzzzzzz", "garage")
    before = cfg.read_bytes()
    mgr, registry, _ = make_manager(cfg)

    result = await mgr.delete("garage", choosing(0))

    assert result.startswith("deleted 'garage'")
    assert not folder.exists()
    assert cfg.read_bytes() == before
    assert ids(registry) == ["office"]


async def test_delete_description_text(env):
    cfg, _, _ = env
    removed_folder(cfg, "zzzzzzzz", "office")
    mgr, registry, _ = make_manager(cfg)
    folder = registry.folder("office")
    (folder / "office_20261103-111200.jpg").write_bytes(b"x" * 1_500_000)
    (folder / "office_20261103-111300.mp4").write_bytes(b"x")
    confirm = choosing(None)

    await mgr.delete("office", confirm)

    message, targets = confirm.calls[0]
    assert "cannot be undone" in message
    assert targets == [
        "office (configured): 1 snapshot, 1 recording, 0 baselines, 1.5 MB; config entry",
        "office (removed, added 2025-01-01): 1 snapshot, 0 recordings, 0 baselines, 2.0 MB",
    ]


async def test_delete_several_choose_one(env):
    cfg, _, _ = env
    first = removed_folder(cfg, "yyyyyyyy", "garage", datetime(2025, 1, 1))
    second = removed_folder(cfg, "zzzzzzzz", "garage", datetime(2025, 2, 1))
    mgr, _, _ = make_manager(cfg)
    await mgr.delete("garage", choosing(1))
    assert first.exists() and not second.exists()


async def test_delete_several_all(env):
    cfg, _, _ = env
    first = removed_folder(cfg, "yyyyyyyy", "office")
    mgr, registry, _ = make_manager(cfg)
    folder = registry.folder("office")
    await mgr.delete("office", choosing("all"))
    assert not first.exists() and not folder.exists()
    assert ids(registry) == []


async def test_delete_cancelled_changes_nothing(env):
    cfg, _, _ = env
    folder = removed_folder(cfg, "zzzzzzzz", "garage")
    before = cfg.read_bytes()
    mgr, registry, _ = make_manager(cfg)
    assert await mgr.delete("office", choosing(None)) == "cancelled; nothing was deleted"
    assert await mgr.delete("garage", choosing(None)) == "cancelled; nothing was deleted"
    assert folder.exists() and cfg.read_bytes() == before and ids(registry) == ["office"]


async def test_delete_without_form_returns_instructions(env):
    cfg, _, _ = env
    folder = removed_folder(cfg, "zzzzzzzz", "office")
    before = cfg.read_bytes()
    mgr, registry, _ = make_manager(cfg)

    result = await mgr.delete("office", None)

    assert "nothing was deleted" in result
    assert str(folder) in result and str(cfg.parent / "media" / uid_for("office")) in result
    assert "[cameras.office]" in result
    assert folder.exists() and cfg.read_bytes() == before and ids(registry) == ["office"]


async def test_delete_unknown_name(env):
    cfg, _, _ = env
    mgr, _, _ = make_manager(cfg)
    with pytest.raises(ValueError, match="no camera or leftover files named 'nope'"):
        await mgr.delete("nope", choosing(0))


async def test_delete_partial_failure_lists_leftovers(env, monkeypatch):
    cfg, _, _ = env
    folder = removed_folder(cfg, "zzzzzzzz", "garage")
    stuck = folder / "garage_20250101-000000.jpg"
    monkeypatch.setattr(store, "delete_folder", lambda media, uid: [stuck])
    mgr, _, _ = make_manager(cfg)
    result = await mgr.delete("garage", choosing(0))
    assert "could not delete" in result and str(stuck) in result


async def test_delete_waits_for_running_capture(env):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    folder = registry.folder("office")
    order: list[str] = []

    async def capture():
        async with registry.lock("office"):
            await asyncio.sleep(0.05)
            (folder / "office_20261103-111300.jpg").write_bytes(b"jpg")
            order.append("capture done")

    task = asyncio.create_task(capture())
    await asyncio.sleep(0)
    await mgr.delete("office", choosing(0))
    order.append("deleted")
    await task
    assert order == ["capture done", "deleted"]
    assert not folder.exists()


async def test_remove_result_points_to_delete_camera(env):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    folder = registry.folder("office")
    result = await mgr.remove("office")
    assert f"files kept in {folder}" in result
    assert "delete_camera('office') deletes them" in result
    assert folder.exists()


async def test_removed_lists_leftovers(env):
    cfg, _, _ = env
    removed_folder(cfg, "zzzzzzzz", "garage")
    mgr, registry, _ = make_manager(cfg)
    registry.folder("office")
    assert [(c.uid, c.name) for c in await mgr.removed()] == [("zzzzzzzz", "garage")]


async def test_delete_identical_targets_get_distinct_labels(env):
    cfg, _, _ = env
    removed_folder(cfg, "yyyyyyyy", "garage")
    removed_folder(cfg, "zzzzzzzz", "garage")
    confirm = choosing(None)
    mgr, _, _ = make_manager(cfg)
    await mgr.delete("garage", confirm)
    targets = confirm.calls[0][1]
    assert len(set(targets)) == 2
    assert targets[0].endswith("[yyyyyyyy]") and targets[1].endswith("[zzzzzzzz]")


async def test_delete_refuses_symlinked_folder_before_changing_anything(env, tmp_path):
    cfg, _, _ = env
    elsewhere = tmp_path / "disk2"
    elsewhere.mkdir()
    media = cfg.parent / "media"
    media.mkdir(exist_ok=True)
    (media / uid_for("office")).symlink_to(elsewhere)
    before = cfg.read_bytes()
    confirm = choosing(0)
    mgr, registry, _ = make_manager(cfg)
    with pytest.raises(ValueError, match="symlink"):
        await mgr.delete("office", confirm)
    assert confirm.calls == []  # refused before asking
    assert cfg.read_bytes() == before and ids(registry) == ["office"]


# --- scenes ---


async def test_rename_pins_implicit_scene(env):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    await mgr.rename("office", "front")
    assert tomllib.loads(cfg.read_text())["cameras"]["front"]["scene"] == "office"
    assert registry.config("front").scene == "office"
    assert read_meta(registry.folder("front")).current_scene == "office"


async def test_rename_keeps_explicit_scene(env):
    cfg, _, _ = env
    cfg.write_text(
        cfg.read_text().replace("[cameras.office]\n", '[cameras.office]\nscene = "desk"\n')
    )
    mgr, _, _ = make_manager(cfg)
    await mgr.rename("office", "front")
    assert tomllib.loads(cfg.read_text())["cameras"]["front"]["scene"] == "desk"


async def test_set_scene_appends_period(env):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    folder = registry.folder("office")
    result = await mgr.set_scene("office", "front-door")
    assert result == "moved 'office' to scene 'front-door'"
    assert tomllib.loads(cfg.read_text())["cameras"]["office"]["scene"] == "front-door"
    assert [p.scene for p in read_meta(folder).scenes] == ["office", "front-door"]
    assert registry.config("office").scene == "front-door"


async def test_set_scene_same_scene_noop(env):
    cfg, _, _ = env
    before = cfg.read_bytes()
    mgr, _, _ = make_manager(cfg)
    assert await mgr.set_scene("office", "office") == "'office' is already in scene 'office'"
    assert cfg.read_bytes() == before


async def test_set_scene_errors(env):
    cfg, _, _ = env
    mgr, _, _ = make_manager(cfg)
    with pytest.raises(ValueError, match="camera 'nope' not found"):
        await mgr.set_scene("nope", "x")
    with pytest.raises(ValueError, match="invalid scene"):
        await mgr.set_scene("office", "Front Door")


async def test_set_scene_waits_for_running_capture(env):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    folder = registry.folder("office")
    order: list[str] = []

    async def capture():
        async with registry.lock("office"):
            await asyncio.sleep(0.05)
            order.append("capture done")

    task = asyncio.create_task(capture())
    await asyncio.sleep(0)
    await mgr.set_scene("office", "front-door")
    order.append("moved")
    await task
    assert order == ["capture done", "moved"]
    assert read_meta(folder).current_scene == "front-door"


async def test_hand_edited_scene_appends_period_on_reload(env, caplog):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg, load=prepare_config)
    folder = registry.folder("office")
    cfg.write_text(
        cfg.read_text().replace("[cameras.office]\n", '[cameras.office]\nscene = "desk"\n')
    )
    with caplog.at_level("INFO", logger="ipcam_mcp"):
        await mgr.reload()
    assert read_meta(folder).current_scene == "desk"
    assert "set_scene" in caplog.text


async def test_add_with_scene(env):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    await mgr.add(
        "garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)), scene="front-door"
    )
    assert tomllib.loads(cfg.read_text())["cameras"]["garage"]["scene"] == "front-door"
    assert read_meta(registry.folder("garage")).scenes[0].scene == "front-door"
    await mgr.add("carport", "10.0.0.6", prompt_with(Credentials(USER, PASSWORD)), scene="carport")
    assert "scene" not in tomllib.loads(cfg.read_text())["cameras"]["carport"]


async def test_hand_rename_keeps_scene(env):
    """Renaming the section by hand is a rename, not a move: the scene is pinned."""
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg, load=prepare_config)
    folder = registry.folder("office")
    cfg.write_text(cfg.read_text().replace("[cameras.office]", "[cameras.desk]"))
    listed = await mgr.reload()
    assert listed[0]["scene"] == "office"
    assert tomllib.loads(cfg.read_text())["cameras"]["desk"]["scene"] == "office"
    assert [p.scene for p in read_meta(folder).scenes] == ["office"]


async def test_set_scene_returns_once_the_new_period_has_started(env):
    """A capture right after set_scene must be stamped in the new scene."""
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    folder = registry.folder("office")
    await mgr.set_scene("office", "desk")
    assert datetime.now().astimezone() >= read_meta(folder).scenes[-1].since


def test_scene_sync_io_error_is_logged_not_fatal(env, monkeypatch, caplog):
    cfg, _, _ = env
    _, registry, _ = make_manager(cfg)
    registry.folder("office")
    cfg.write_text(
        cfg.read_text().replace("[cameras.office]\n", '[cameras.office]\nscene = "desk"\n')
    )

    def fail(*a, **kw):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(store, "enter_scene", fail)
    with caplog.at_level("WARNING", logger="ipcam_mcp"):
        config = prepare_config(cfg)
    assert config.devices["office"].scene == "desk"
    assert "could not record" in caplog.text


# --- successors ---

OLD = datetime(2025, 1, 1).astimezone()


def removed_camera(media: Path, uid="zzzzzzzz", name="garage", scene=None) -> Path:
    folder = media / uid
    write_meta(folder, CameraMeta(uid, name, OLD, (store.Period(scene or name, OLD),)))
    (folder / f"{name}_20250101-000000.jpg").write_bytes(b"x")
    return folder


def asking(answer):
    calls: list = []

    async def successor(message, labels):
        calls.append((message, labels))
        return answer

    successor.calls = calls
    return successor


def last_period(registry, camera):
    return read_meta(registry.folder(camera)).scenes[-1]


GARAGE_LABEL = (
    "Replaces 'garage' (added 2025-01-01, 1 snapshot, 0 baselines): "
    "continue its compare and baselines"
)


async def test_add_into_scene_with_removed_camera_asks_first(env):
    cfg, _, _ = env
    removed_camera(cfg.parent / "media")
    mgr, _, _ = make_manager(cfg)
    prompt = prompt_with(Credentials(USER, PASSWORD))
    successor = asking(None)
    result = await mgr.add("door", "10.0.0.5", prompt, scene="garage", successor=successor)
    assert result == "cancelled; nothing was changed"
    assert prompt.messages == []  # asked before the login form
    assert successor.calls[0][1] == [GARAGE_LABEL]
    assert "door" not in tomllib.loads(cfg.read_text())["cameras"]


async def test_add_successor_sets_continues(env):
    cfg, _, _ = env
    removed_camera(cfg.parent / "media")
    mgr, registry, _ = make_manager(cfg)
    result = await mgr.add(
        "garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)), successor=asking(0)
    )
    assert result.endswith("; continues 'garage' in scene 'garage'")
    period = last_period(registry, "garage")
    assert (period.scene, period.continues) == ("garage", "zzzzzzzz")


async def test_add_additional_no_continues(env):
    cfg, _, _ = env
    removed_camera(cfg.parent / "media")
    mgr, registry, _ = make_manager(cfg)
    result = await mgr.add(
        "door", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)), scene="garage",
        successor=asking("additional"),
    )  # fmt: skip
    assert result.startswith("added 'door'") and "continues" not in result
    assert last_period(registry, "door").continues is None


async def test_add_successor_no_form_note(env):
    cfg, _, _ = env
    removed_camera(cfg.parent / "media")
    mgr, registry, _ = make_manager(cfg)
    result = await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    assert result.endswith(
        "; 'garage' has earlier cameras (garage); declare a successor with set_scene from a "
        "client that shows forms"
    )
    assert last_period(registry, "garage").continues is None


async def test_set_scene_successor(env):
    cfg, _, _ = env
    removed_camera(cfg.parent / "media")
    mgr, registry, _ = make_manager(cfg)
    registry.folder("office")
    result = await mgr.set_scene("office", "garage", successor=asking(0))
    assert result == "moved 'office' to scene 'garage'; continues 'garage'"
    period = last_period(registry, "office")
    assert (period.scene, period.continues) == ("garage", "zzzzzzzz")


async def test_set_scene_successor_cancel_changes_nothing(env):
    cfg, _, _ = env
    removed_camera(cfg.parent / "media")
    mgr, registry, _ = make_manager(cfg)
    registry.folder("office")
    assert await mgr.set_scene("office", "garage", successor=asking(None)) == (
        "cancelled; nothing was changed"
    )
    assert last_period(registry, "office").scene == "office"
    assert "scene" not in tomllib.loads(cfg.read_text())["cameras"]["office"]


async def test_set_scene_same_scene_reasks_without_continues(env):
    cfg, _, _ = env
    removed_camera(cfg.parent / "media", "yyyyyyyy", "office-old", scene="office")
    mgr, registry, _ = make_manager(cfg)
    registry.folder("office")
    result = await mgr.set_scene("office", "office", successor=asking(0))
    assert result == "'office' continues 'office-old' in scene 'office'"
    assert last_period(registry, "office").continues == "yyyyyyyy"
    again = asking(0)
    assert await mgr.set_scene("office", "office", successor=again) == (
        "'office' is already in scene 'office'"
    )
    assert again.calls == []


async def test_delete_description_mentions_successor(env):
    cfg, _, _ = env
    removed_camera(cfg.parent / "media")
    mgr, registry, _ = make_manager(cfg)
    registry.folder("office")
    await mgr.set_scene("office", "garage", successor=asking(0))
    seen = []

    async def confirm(message, targets):
        seen.extend(targets)  # and cancel

    assert await mgr.delete("garage", confirm) == "cancelled; nothing was deleted"
    assert seen[0].endswith("; predecessor of 'office' in 'garage'")


async def test_delete_description_mentions_successor_deeper_in_the_chain(env):
    """office continues garage (removed), which continued garage-old: deleting garage-old
    still takes a reference away from office."""
    cfg, _, _ = env
    media = cfg.parent / "media"
    removed_camera(media, "yyyyyyyy", "garage-old", scene="garage")
    write_meta(
        media / "zzzzzzzz",
        CameraMeta("zzzzzzzz", "garage", OLD, (store.Period("garage", OLD, "yyyyyyyy"),)),
    )
    (media / "zzzzzzzz" / "garage_20250102-000000.jpg").write_bytes(b"x")
    mgr, registry, _ = make_manager(cfg)
    registry.folder("office")
    await mgr.set_scene("office", "garage", successor=asking(1))  # labels oldest first
    assert last_period(registry, "office").continues == "zzzzzzzz"
    seen = []

    async def confirm(message, targets):
        seen.extend(targets)

    await mgr.delete("garage-old", confirm)
    assert seen[0].endswith("; predecessor of 'office' in 'garage'")


async def test_successor_labels_unique_for_twins(env):
    """Two removed cameras with the same name, date and counts must stay distinguishable."""
    cfg, _, _ = env
    removed_camera(cfg.parent / "media", "yyyyyyyy")
    removed_camera(cfg.parent / "media", "zzzzzzzz")
    mgr, _, _ = make_manager(cfg)
    successor = asking(None)
    await mgr.add(
        "garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)), successor=successor
    )
    labels = successor.calls[0][1]
    assert labels == [f"{GARAGE_LABEL} [yyyyyyyy]", f"{GARAGE_LABEL} [zzzzzzzz]"]


async def test_rename_with_corrupt_meta_keeps_scene(env):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    folder = registry.folder("office")
    (folder / "camera.json").write_text("{not json")
    await mgr.rename("office", "front")
    meta = read_meta(folder)
    assert meta.name == "front" and meta.current_scene == "office"


def production_manager(cfg: Path) -> tuple[Manager, Registry]:
    registry = Registry(load_config(cfg), adapters={"tapo_onvif": FakeCamPTZ, "onvif": FakeCamPTZ})
    return Manager(cfg, registry, probe=FakeProbe()), registry


async def test_failed_set_scene_leaves_no_period(env, monkeypatch):
    cfg, _, _ = env
    mgr, registry = production_manager(cfg)
    folder = registry.folder("office")

    def failing(config):
        raise ConfigError("boom")

    monkeypatch.setattr(registry, "reload", failing)
    with pytest.raises(ConfigError, match="boom"):
        await mgr.set_scene("office", "desk")
    monkeypatch.undo()
    await mgr.reload()
    assert [p.scene for p in read_meta(folder).scenes] == ["office"]


async def test_failed_reload_records_no_scene_period(env):
    cfg, _, _ = env
    mgr, registry = production_manager(cfg)
    folder = registry.folder("office")
    cfg.write_text(
        cfg.read_text().replace('type = "tapo_onvif"\n', 'type = "nope"\nscene = "desk"\n')
    )
    with pytest.raises(ConfigError, match="unknown type"):
        await mgr.reload()
    assert [p.scene for p in read_meta(folder).scenes] == ["office"]


async def test_set_scene_logs_no_hand_edit_hint(env, caplog):
    cfg, _, _ = env
    mgr, registry = production_manager(cfg)
    registry.folder("office")
    with caplog.at_level("INFO", logger="ipcam_mcp"):
        await mgr.set_scene("office", "desk")
    assert "set_scene can declare" not in caplog.text


def _unwritable(monkeypatch, name):
    def fail(media_dir, uid, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", str(media_dir / uid))

    monkeypatch.setattr(store, name, fail)


async def test_add_reports_unwritable_folder_after_commit(env, monkeypatch):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    _unwritable(monkeypatch, "ensure_folder")
    result = await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    assert result.startswith("added 'garage'")
    assert "; could not update its media folder (" in result and "Permission denied" in result
    assert "garage" in ids(registry)


async def test_add_successor_reports_unrecorded_predecessor(env, monkeypatch):
    cfg, _, _ = env
    removed_camera(cfg.parent / "media")
    mgr, _, _ = make_manager(cfg)
    _unwritable(monkeypatch, "enter_scene")
    result = await mgr.add(
        "door", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)), scene="garage",
        successor=asking(0),
    )  # fmt: skip
    assert result.startswith("added 'door'")
    assert "could not record that it continues 'garage'" in result
    assert "declare it again with set_scene" in result


async def test_rename_reports_unwritable_folder_after_commit(env, monkeypatch):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    registry.folder("office")
    _unwritable(monkeypatch, "ensure_folder")
    result = await mgr.rename("office", "front")
    assert result.startswith("renamed 'office' to 'front'")
    assert "; could not update its media folder (" in result


async def test_set_scene_reports_unwritable_folder_after_commit(env, monkeypatch):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    registry.folder("office")
    _unwritable(monkeypatch, "enter_scene")
    result = await mgr.set_scene("office", "desk")
    assert result.startswith("moved 'office' to scene 'desk'")
    assert "; could not record the move in its media folder (" in result


async def test_rename_waits_for_running_capture(env):
    cfg, _, _ = env
    mgr, registry, _ = make_manager(cfg)
    registry.folder("office")
    order: list[str] = []

    async def capture():
        async with registry.lock("office"):
            await asyncio.sleep(0.05)
            order.append("capture done")

    task = asyncio.create_task(capture())
    await asyncio.sleep(0)
    await mgr.rename("office", "front")
    order.append("renamed")
    await task
    assert order == ["capture done", "renamed"]


async def test_set_scene_back_keeps_predecessor_without_asking(env):
    cfg, _, _ = env
    removed_camera(cfg.parent / "media")
    mgr, registry, _ = make_manager(cfg)
    registry.folder("office")
    await mgr.set_scene("office", "garage", successor=asking(0))
    await mgr.set_scene("office", "office")
    again = asking(None)
    result = await mgr.set_scene("office", "garage", successor=again)
    assert result == "moved 'office' to scene 'garage'; continues 'garage' again"
    assert again.calls == []
    assert last_period(registry, "office").continues == "zzzzzzzz"


async def test_set_scene_back_after_predecessor_deleted_records_none(env):
    cfg, _, _ = env
    folder = removed_camera(cfg.parent / "media")
    mgr, registry, _ = make_manager(cfg)
    registry.folder("office")
    await mgr.set_scene("office", "garage", successor=asking(0))
    await mgr.set_scene("office", "office")
    shutil.rmtree(folder)
    assert await mgr.set_scene("office", "garage") == "moved 'office' to scene 'garage'"
    assert last_period(registry, "office").continues is None


async def test_set_scene_back_does_not_carry_a_configured_camera(env):
    cfg, _, shared = env
    removed_camera(cfg.parent / "media")
    mgr, registry, _ = make_manager(cfg)
    registry.folder("office")
    await mgr.set_scene("office", "garage", successor=asking(0))
    await mgr.set_scene("office", "office")
    cfg.write_text(
        cfg.read_text() + '\n[cameras.garage]\nuid = "zzzzzzzz"\ntype = "tapo_onvif"\n'
        f'secrets_file = "{shared}"\nhost_var = "OFFICE_HOST"\nuser_var = "OFFICE_USER"\n'
        'password_var = "OFFICE_PASS"\n'
    )
    await mgr.reload()
    assert await mgr.set_scene("office", "garage") == "moved 'office' to scene 'garage'"
    assert last_period(registry, "office").continues is None


async def test_hand_edit_back_keeps_predecessor(env):
    cfg, _, _ = env
    removed_camera(cfg.parent / "media")
    mgr, registry, _ = make_manager(cfg)
    registry.folder("office")
    await mgr.set_scene("office", "garage", successor=asking(0))
    await mgr.set_scene("office", "office")
    cfg.write_text(cfg.read_text().replace('scene = "office"', 'scene = "garage"'))
    await mgr.reload()
    assert (last_period(registry, "office").scene, last_period(registry, "office").continues) == (
        "garage",
        "zzzzzzzz",
    )


async def test_failure_after_commit_still_succeeds(env, caplog):
    cfg, _, _ = env

    calls = 0

    def broken_sync(path, config):
        nonlocal calls
        calls += 1
        if calls > 1:  # the first call is the hand-edit sync before the change
            raise AssertionError("boom")
        return False

    registry = Registry(load_config(cfg), adapters={"tapo_onvif": FakeCamPTZ, "onvif": FakeCamPTZ})
    mgr = Manager(cfg, registry, probe=FakeProbe(), sync=broken_sync)
    with caplog.at_level("WARNING", logger="ipcam_mcp"):
        result = await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    assert result.startswith("added 'garage'")
    assert "could not record scene changes" in caplog.text


def test_hand_rename_with_read_only_config_still_loads(env, caplog):
    cfg, _, _ = env
    folder = Registry(prepare_config(cfg), adapters={"tapo_onvif": FakeCamPTZ}).folder("office")
    cfg.write_text(cfg.read_text().replace("[cameras.office]", "[cameras.desk]"))
    cfg.chmod(0o444)
    with caplog.at_level("WARNING", logger="ipcam_mcp"):
        config = prepare_config(cfg)
    assert "desk" in config.devices
    assert 'scene = "office"' in caplog.text
    assert [p.scene for p in read_meta(folder).scenes] == ["office"]


async def test_rename_keeps_managed_secrets_shared_with_another_camera(env):
    cfg, secrets_dir, _ = env
    mgr, registry, _ = make_manager(cfg)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    managed = secrets_dir / "garage.env"
    with cfg.open("a") as f:
        f.write(
            "\n[cameras.twin]\n"
            f'uid = "{uid_for("twin")}"\n'
            'type = "tapo_onvif"\n'
            f'secrets_file = "{managed}"\n'
            'host_var = "HOST"\nuser_var = "USER"\npassword_var = "PASSWORD"\n'
        )
    await mgr.reload()

    result = await mgr.rename("garage", "carport")

    assert result == (
        f"renamed 'garage' to 'carport'; its secrets file {managed} is shared with 'twin' "
        "and keeps its name"
    )
    assert managed.exists() and not (secrets_dir / "carport.env").exists()
    assert tomllib.loads(cfg.read_text())["cameras"]["carport"]["secrets_file"] == str(managed)
    assert sorted(ids(registry)) == ["carport", "office", "twin"]


def _add_twin(cfg: Path, secrets_file: Path) -> None:
    with cfg.open("a") as f:
        f.write(
            "\n[cameras.twin]\n"
            f'uid = "{uid_for("twin")}"\n'
            'type = "tapo_onvif"\n'
            f'secrets_file = "{secrets_file}"\n'
            'host_var = "HOST"\nuser_var = "USER"\npassword_var = "PASSWORD"\n'
        )


async def test_add_with_form_refuses_secrets_file_in_use(env):
    cfg, secrets_dir, _ = env
    mgr, _, _ = make_manager(cfg)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    _add_twin(cfg, secrets_dir / "garage.env")
    await mgr.reload()
    await mgr.rename("garage", "carport")
    with pytest.raises(ValueError, match="already used by camera 'carport'; pick another name"):
        await mgr.add("garage", "10.0.0.6", prompt_with(Credentials(USER, PASSWORD)))


async def test_rename_does_not_claim_a_file_it_does_not_use(env):
    """office uses the shared secrets.env; twin alone uses <secrets_dir>/office.env."""
    cfg, secrets_dir, _ = env
    mgr, _, _ = make_manager(cfg)
    other = secrets_dir / "office.env"
    write_env(other, "10.0.0.7", USER, PASSWORD)
    _add_twin(cfg, other)
    await mgr.reload()
    assert await mgr.rename("office", "front") == "renamed 'office' to 'front'"
    assert other.exists()


def _add_sharer(cfg: Path, name: str, secrets_file: Path) -> None:
    with cfg.open("a") as f:
        f.write(
            f"\n[cameras.{name}]\n"
            f'uid = "{uid_for(name)}"\n'
            'type = "tapo_onvif"\n'
            f'secrets_file = "{secrets_file}"\n'
            'host_var = "HOST"\nuser_var = "USER"\npassword_var = "PASSWORD"\n'
        )


async def test_rename_note_names_every_sharer(env):
    cfg, secrets_dir, _ = env
    mgr, _, _ = make_manager(cfg)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    managed = secrets_dir / "garage.env"
    _add_sharer(cfg, "twin", managed)
    _add_sharer(cfg, "porch", managed)
    await mgr.reload()
    result = await mgr.rename("garage", "carport")
    assert result.endswith(
        f"its secrets file {managed} is shared with 'twin', 'porch' and keeps its name"
    )


async def test_rename_sees_sharing_through_a_symlinked_path(env, tmp_path):
    cfg, secrets_dir, _ = env
    mgr, _, _ = make_manager(cfg)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    link = tmp_path / "secrets-link"
    link.symlink_to(secrets_dir)
    _add_sharer(cfg, "twin", link / "garage.env")
    await mgr.reload()
    result = await mgr.rename("garage", "carport")
    assert "is shared with 'twin'" in result
    assert (secrets_dir / "garage.env").exists()


async def _kept_file_setup(env):
    cfg, secrets_dir, _ = env
    mgr, registry, _ = make_manager(cfg)
    await mgr.add("garage", "10.0.0.5", prompt_with(Credentials(USER, PASSWORD)))
    kept = secrets_dir / "garage.env"
    _add_sharer(cfg, "twin", kept)
    await mgr.reload()
    await mgr.rename("garage", "carport")  # keeps garage.env: twin shares it
    return mgr, registry, kept


async def test_remove_reports_a_kept_secrets_file_nobody_uses(env):
    mgr, _, kept = await _kept_file_setup(env)
    assert "no longer used" not in await mgr.remove("twin")  # carport still uses it
    result = await mgr.remove("carport")
    assert (
        f"; {kept} is no longer used by any camera; delete it by hand if you don't need it"
        in result
    )
    assert kept.exists()


async def test_delete_reports_a_kept_secrets_file_nobody_uses(env):
    mgr, _, kept = await _kept_file_setup(env)
    await mgr.remove("twin")
    seen = []

    async def confirm(message, targets):
        seen.extend(targets)
        return 0

    result = await mgr.delete("carport", confirm)
    assert f"keeps its secrets file {kept}, which no other camera uses" in seen[0]
    assert f"{kept} is no longer used by any camera" in result
    assert kept.exists()
