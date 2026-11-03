import asyncio
import re
import stat
import tomllib
from datetime import datetime
from pathlib import Path

import pytest
from conftest import FakeCamPTZ, uid_for
from dotenv import dotenv_values

from ipcam_mcp import store
from ipcam_mcp.adapters.onvif import DeviceInfo
from ipcam_mcp.baselines import latest_snapshot
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
    mgr, _, _ = make_manager(cfg)
    text = cfg.read_text().replace("[cameras.office]", "[cameras.desk]")
    cfg.write_text(text)
    assert await mgr.reload() == [
        {
            "id": "desk",
            "uid": uid_for("office"),
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
    assert latest_snapshot(folder) == folder / "office_20261103-111200.jpg"
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
