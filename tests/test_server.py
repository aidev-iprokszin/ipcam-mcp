import asyncio
import json
import time
from pathlib import Path

import pytest
from conftest import FakeCamPTZ, uid_for
from mcp import Client
from mcp.types import INTERNAL_ERROR, ElicitResult, ErrorData
from PIL import Image

from ipcam_mcp import server as server_mod
from ipcam_mcp.adapters.onvif import DeviceInfo
from ipcam_mcp.config import AppConfig, DeviceConfig, load_config
from ipcam_mcp.management import Manager
from ipcam_mcp.registry import Registry
from ipcam_mcp.server import build_server, main


def dev(dev_id="cam", type_="fake", host="10.0.0.9") -> DeviceConfig:
    return DeviceConfig(
        id=dev_id, type=type_, host=host, user="u", password="pw", uid=uid_for(dev_id)
    )


@pytest.fixture
def registry(tmp_path) -> Registry:
    return Registry(
        AppConfig(media_dir=tmp_path, devices={"cam": dev()}), adapters={"fake": FakeCamPTZ}
    )


@pytest.fixture(autouse=True)
def fake_preview(monkeypatch):
    async def preview(src):
        return b"\xff\xd8preview"

    monkeypatch.setattr(server_mod, "make_preview", preview)


async def call(registry: Registry, tool: str, args: dict | None = None):
    async with Client(build_server(registry)) as client:
        return await client.call_tool(tool, args or {})


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if c.type == "text")


async def test_list_cameras(registry):
    r = await call(registry, "list_cameras")
    assert not r.is_error
    data = json.loads(text_of(r))
    assert data == [
        {
            "id": "cam",
            "uid": uid_for("cam"),
            "scene": "cam",
            "type": "fake",
            "capabilities": ["camera", "ptz"],
        }
    ]


async def test_snapshot_saved_in_uid_folder(registry, tmp_path):
    r = await call(registry, "camera_snapshot", {"camera": "cam"})
    folder = tmp_path / uid_for("cam")
    assert str(folder / "cam_") in text_of(r)
    assert json.loads((folder / "camera.json").read_text())["name"] == "cam"


async def test_from_path_accepts_uid_folder_snapshot(registry, tmp_path):
    r = await call(registry, "camera_snapshot", {"camera": "cam"})
    path = text_of(r).split("Saved full-size snapshot: ")[1].strip()
    r = await call(
        registry, "camera_snapshot", {"camera": "cam", "from_path": path, "region": "center"}
    )
    assert not r.is_error, text_of(r)


async def test_compare_finds_snapshot_taken_under_earlier_name(registry, tmp_path, monkeypatch):
    folder = tmp_path / uid_for("cam")
    folder.mkdir()
    Image.new("RGB", (64, 48), (10, 10, 10)).save(folder / "oldname_20200101-000000.jpg", "JPEG")
    fake_compare(monkeypatch)
    t = text_of(await call(registry, "camera_compare", {"camera": "cam"}))
    assert t.startswith("Compared with previous snapshot oldname_20200101-000000.jpg (")


async def test_list_cameras_empty_hints_add_camera(managed, tmp_path):
    _, _, mgr = managed
    empty = Registry(AppConfig(media_dir=tmp_path, devices={}))
    async with Client(build_server(empty, mgr)) as client:
        r = await client.call_tool("list_cameras", {})
    assert not r.is_error
    assert "No cameras configured" in text_of(r) and "add_camera" in text_of(r)


async def test_list_cameras_empty_without_management_hints_config(managed, tmp_path):
    _, _, mgr = managed
    empty = Registry(AppConfig(media_dir=tmp_path, devices={}))
    async with Client(build_server(empty, mgr, allow_management=False)) as client:
        r = await client.call_tool("list_cameras", {})
    assert "No cameras configured" in text_of(r)
    assert "add_camera" not in text_of(r) and "cameras.toml" in text_of(r)


async def test_snapshot_returns_image_and_path(registry, tmp_path):
    r = await call(registry, "camera_snapshot", {"camera": "cam"})
    assert not r.is_error
    images = [c for c in r.content if c.type == "image"]
    assert len(images) == 1
    assert images[0].mime_type == "image/jpeg"
    assert str(tmp_path / uid_for("cam") / "cam_") in text_of(r)
    assert ".jpg" in text_of(r)


async def test_snapshot_low_quality(registry, tmp_path):
    r = await call(registry, "camera_snapshot", {"camera": "cam", "quality": "low"})
    assert str(tmp_path / uid_for("cam") / "cam_") in text_of(r)
    assert registry._devices["cam"][1].snapshots == 1


def saved_path(r) -> str:
    return text_of(r).split("snapshot: ")[1].splitlines()[0]


async def test_snapshot_crop_region(registry, tmp_path):
    r = await call(registry, "camera_snapshot", {"camera": "cam", "region": "top-right"})
    assert not r.is_error
    assert len([c for c in r.content if c.type == "image"]) == 1
    t = text_of(r)
    assert "cropped to [0.667, 0.000, 0.333, 0.333]" in t
    assert "(21\u00d716 px)" in t
    assert str(tmp_path / uid_for("cam") / "cam_") in t


async def test_snapshot_crop_and_region_refused(registry):
    args = {"camera": "cam", "crop": [0, 0, 0.5, 0.5], "region": "top"}
    r = await call(registry, "camera_snapshot", args)
    assert r.is_error
    assert "not both" in text_of(r)


async def test_snapshot_from_path_crops_without_camera(registry):
    r1 = await call(registry, "camera_snapshot", {"camera": "cam", "region": "center"})
    path = saved_path(r1)
    r = await call(
        registry, "camera_snapshot", {"camera": "cam", "from_path": path, "crop": [0, 0, 0.5, 0.5]}
    )
    assert not r.is_error
    assert registry._devices["cam"][1].snapshots == 1
    assert "Source: " + path in text_of(r)
    assert "cropped to [0.000, 0.000, 0.500, 0.500] (32\u00d724 px)" in text_of(r)


async def test_snapshot_from_path_without_crop_returns_preview(registry):
    r1 = await call(registry, "camera_snapshot", {"camera": "cam", "region": "center"})
    r = await call(registry, "camera_snapshot", {"camera": "cam", "from_path": saved_path(r1)})
    assert not r.is_error
    assert len([c for c in r.content if c.type == "image"]) == 1
    assert registry._devices["cam"][1].snapshots == 1


async def test_snapshot_from_path_outside_media_dir_refused(registry, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    registry._media_dir = media  # media_dir below tmp_path
    img = Image.new("RGB", (8, 8))
    outside = tmp_path / "out.jpg"
    img.save(outside)
    (media / "link.jpg").symlink_to(outside)
    (media / "sub").mkdir()
    bad = [outside, media / "sub" / ".." / ".." / "out.jpg", media / "link.jpg"]
    for p in bad:
        r = await call(registry, "camera_snapshot", {"camera": "cam", "from_path": str(p)})
        assert r.is_error, p
        assert "Traceback" not in text_of(r)


async def test_snapshot_from_path_bad_target_refused(registry, tmp_path):
    (tmp_path / "x.png").write_bytes(b"x")
    for name in ("x.png", "missing.jpg"):
        r = await call(
            registry, "camera_snapshot", {"camera": "cam", "from_path": str(tmp_path / name)}
        )
        assert r.is_error


async def test_snapshot_from_path_unknown_camera(registry, tmp_path):
    r = await call(registry, "camera_snapshot", {"camera": "zz", "from_path": str(tmp_path)})
    assert r.is_error
    assert "not found" in text_of(r)


def test_region_literal_matches_imaging():
    import typing

    from ipcam_mcp.imaging import REGIONS

    assert typing.get_args(server_mod.Region) == REGIONS


async def test_record_returns_path_and_size(registry, tmp_path):
    r = await call(registry, "camera_record", {"camera": "cam"})
    assert not r.is_error
    t = text_of(r)
    assert str(tmp_path / uid_for("cam") / "clip.mp4") in t
    assert "10s" in t
    assert "1234 bytes" in t


async def test_record_rejects_out_of_range(registry):
    r = await call(registry, "camera_record", {"camera": "cam", "seconds": 61})
    assert r.is_error
    assert "60" in text_of(r)


async def test_ptz_move_reports_position(registry):
    r = await call(registry, "ptz_move", {"camera": "cam", "direction": "left"})
    assert not r.is_error
    assert "0.1" in text_of(r) and "-0.2" in text_of(r)


async def test_ptz_move_rejects_bad_direction(registry):
    r = await call(registry, "ptz_move", {"camera": "cam", "direction": "sideways"})
    assert r.is_error


async def test_ptz_list_presets(registry):
    r = await call(registry, "ptz_list_presets", {"camera": "cam"})
    assert json.loads(text_of(r)) == [{"name": "Door", "token": "1"}]


async def test_ptz_goto_preset(registry):
    ok = await call(registry, "ptz_goto_preset", {"camera": "cam", "preset": "Door"})
    assert not ok.is_error
    bad = await call(registry, "ptz_goto_preset", {"camera": "cam", "preset": "X"})
    assert bad.is_error
    assert "preset 'X' not found; available: Door" in text_of(bad)


async def test_ptz_save_preset(registry):
    r = await call(registry, "ptz_save_preset", {"camera": "cam", "name": "desk"})
    assert not r.is_error
    assert text_of(r) == "saved preset 'desk' (token 2)"


async def test_ptz_remove_preset(registry):
    r = await call(registry, "ptz_remove_preset", {"camera": "cam", "preset": "Door"})
    assert not r.is_error
    assert text_of(r) == "removed preset 'Door'"


async def test_preset_tools_need_ptz(tmp_path):
    class CamOnly:
        def __init__(self, cfg, media_dir):
            pass

    reg = Registry(AppConfig(tmp_path, {"cam": dev()}), adapters={"fake": CamOnly})
    a = await call(reg, "ptz_save_preset", {"camera": "cam", "name": "x"})
    b = await call(reg, "ptz_remove_preset", {"camera": "cam", "preset": "x"})
    assert a.is_error and b.is_error


async def test_unknown_camera_is_tool_error(registry):
    r = await call(registry, "camera_snapshot", {"camera": "x"})
    assert r.is_error
    assert "camera 'x' not found; available: cam" in text_of(r)


async def test_missing_capability_is_tool_error(tmp_path):
    class CamOnly:
        def __init__(self, cfg, media_dir):
            pass

        async def snapshot(self, quality):
            return Path("x")

        async def record(self, seconds, quality):
            return Path("x")

    reg = Registry(AppConfig(tmp_path, {"cam": dev()}), adapters={"fake": CamOnly})
    r = await call(reg, "ptz_move", {"camera": "cam", "direction": "up"})
    assert r.is_error
    assert "does not support ptz" in text_of(r)


async def test_unexpected_error_hides_details(registry):
    registry.get("cam", "ptz").fail_with = RuntimeError("secret123")
    r = await call(registry, "ptz_move", {"camera": "cam", "direction": "up"})
    assert r.is_error
    assert "secret123" not in text_of(r)
    assert "RuntimeError" in text_of(r)


async def test_same_device_calls_serialized(registry):
    async with Client(build_server(registry)) as client:
        await asyncio.gather(
            client.call_tool("ptz_move", {"camera": "cam", "direction": "up"}),
            client.call_tool("ptz_move", {"camera": "cam", "direction": "down"}),
        )
    assert registry.get("cam", "ptz").overlaps == 0


async def test_server_starts_without_contacting_devices(tmp_path):
    start = time.monotonic()
    reg = Registry(AppConfig(tmp_path, {"lr": dev("lr", "tapo_onvif", host="192.0.2.1")}))
    r = await call(reg, "list_cameras")
    assert time.monotonic() - start < 1
    assert json.loads(text_of(r))[0]["id"] == "lr"


def bad_config(tmp_path, monkeypatch) -> Path:
    bad = tmp_path / "bad.toml"
    bad.write_text("not = [valid")
    monkeypatch.setenv("IPCAM_MCP_CONFIG", str(bad))
    return bad


def test_main_bad_config_exits_2(tmp_path, monkeypatch, capsys):
    bad = bad_config(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as e:
        main([])
    assert e.value.code == 2
    assert str(bad) in capsys.readouterr().err


def test_main_missing_config_starts_empty(tmp_path, monkeypatch, caplog):
    missing = tmp_path / "new" / "cameras.toml"
    monkeypatch.setenv("IPCAM_MCP_CONFIG", str(missing))
    started = {}

    class StubServer:
        def run(self, transport, **kw):
            started["transport"] = transport

    def fake_build(registry, manager, allow_management):
        started.update(cameras=registry.list(), manager=manager, allow=allow_management)
        return StubServer()

    monkeypatch.setattr(server_mod, "build_server", fake_build)
    with caplog.at_level("INFO", logger="ipcam_mcp"):
        main([])
    assert started["transport"] == "stdio"
    assert started["cameras"] == [] and started["allow"] is True
    assert started["manager"] is not None
    assert str(missing) in caplog.text and "add_camera" in caplog.text
    assert not missing.exists()


def test_main_assigns_missing_uids(tmp_path, monkeypatch):
    secrets = tmp_path / "s.env"
    secrets.write_text('H="10.0.0.2"\nU="u"\nP="p"\n')
    secrets.chmod(0o600)
    cfg = tmp_path / "cameras.toml"
    cfg.write_text(
        f'media_dir = "{tmp_path / "media"}"\n[cameras.office]\ntype = "tapo_onvif"\n'
        f'secrets_file = "{secrets}"\nhost_var = "H"\nuser_var = "U"\npassword_var = "P"\n'
    )
    monkeypatch.setenv("IPCAM_MCP_CONFIG", str(cfg))
    started = {}

    class StubServer:
        def run(self, transport, **kw):
            pass

    def fake_build(registry, manager, allow_management):
        started["cameras"] = registry.list()
        return StubServer()

    monkeypatch.setattr(server_mod, "build_server", fake_build)
    main([])
    assert [c["id"] for c in started["cameras"]] == ["office"]
    assert 'uid = "' in cfg.read_text()


def test_main_runs_migration(tmp_path, monkeypatch):
    secrets = tmp_path / "s.env"
    secrets.write_text('H="10.0.0.2"\nU="u"\nP="p"\n')
    secrets.chmod(0o600)
    media = tmp_path / "media"
    media.mkdir()
    (media / "office_20261103-111200.jpg").write_bytes(b"x")
    cfg = tmp_path / "cameras.toml"
    cfg.write_text(
        f'media_dir = "{media}"\n[cameras.office]\nuid = "{uid_for("office")}"\n'
        f'type = "tapo_onvif"\nsecrets_file = "{secrets}"\n'
        'host_var = "H"\nuser_var = "U"\npassword_var = "P"\n'
    )
    monkeypatch.setenv("IPCAM_MCP_CONFIG", str(cfg))

    class StubServer:
        def run(self, transport, **kw):
            pass

    monkeypatch.setattr(server_mod, "build_server", lambda *a, **kw: StubServer())
    main([])
    assert (media / uid_for("office") / "office_20261103-111200.jpg").exists()
    assert not (media / "office_20261103-111200.jpg").exists()


# --- camera management tools ---


async def fake_probe(host: str, user: str, password: str, port: int | None = None) -> DeviceInfo:
    return DeviceInfo("TP-Link", "C210", True, 2020)


@pytest.fixture
def managed(tmp_path) -> tuple[Path, Registry, Manager]:
    """A tmp cameras.toml with one camera, its registry and a Manager with a fake probe."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    shared = tmp_path / "secrets.env"
    shared.write_text('OFFICE_HOST="10.0.0.2"\nOFFICE_USER="u"\nOFFICE_PASS="p"\n')
    cfg = tmp_path / "cameras.toml"
    cfg.write_text(
        f'media_dir = "{tmp_path / "media"}"\n'
        f'secrets_dir = "{secrets_dir}"\n'
        "\n"
        "[cameras.office]\n"
        f'uid = "{uid_for("office")}"\n'
        'type = "tapo_onvif"\n'
        f'secrets_file = "{shared}"\n'
        'host_var = "OFFICE_HOST"\n'
        'user_var = "OFFICE_USER"\n'
        'password_var = "OFFICE_PASS"\n'
    )
    reg = Registry(load_config(cfg), adapters={"tapo_onvif": FakeCamPTZ, "onvif": FakeCamPTZ})
    return cfg, reg, Manager(cfg, reg, probe=fake_probe)


async def tool_names(server) -> set[str]:
    async with Client(server) as client:
        return {t.name for t in (await client.list_tools()).tools}


def answering(action: str, content: dict | None = None):
    calls: list = []

    async def cb(context, params):
        calls.append(params)
        return ElicitResult(action=action, content=content)

    cb.calls = calls
    return cb


async def test_management_tools_on_by_default(managed):
    _, reg, manager = managed
    names = await tool_names(build_server(reg, manager))
    assert {
        "add_camera",
        "rename_camera",
        "remove_camera",
        "delete_camera",
        "reload_cameras",
    } <= names


async def test_management_tools_hidden_when_disabled(managed):
    _, reg, manager = managed
    names = await tool_names(build_server(reg, manager, allow_management=False))
    assert "reload_cameras" in names
    assert not {"add_camera", "rename_camera", "remove_camera", "delete_camera"} & names
    names = await tool_names(build_server(reg))
    assert "reload_cameras" not in names
    assert "add_camera" not in names


async def test_management_tools_present_when_allowed(managed):
    _, reg, manager = managed
    server = build_server(reg, manager, allow_management=True)
    names = await tool_names(server)
    assert {
        "add_camera",
        "rename_camera",
        "remove_camera",
        "delete_camera",
        "reload_cameras",
    } <= names
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    props = tools["add_camera"].input_schema["properties"]
    assert set(props) == {"name", "host", "credentials_via", "onvif_port", "scene"}


async def test_add_camera_via_form(managed):
    _, reg, manager = managed
    cb = answering("accept", {"username": "u", "password": "s3cr3t"})
    server = build_server(reg, manager, allow_management=True)
    async with Client(server, mode="legacy", elicitation_callback=cb) as client:
        r = await client.call_tool("add_camera", {"name": "garage", "host": "10.0.0.7"})
        listed = await client.call_tool("list_cameras", {})
    assert not r.is_error
    assert "added 'garage'" in text_of(r)
    assert "s3cr3t" not in text_of(r)
    assert "garage" in [c["id"] for c in json.loads(text_of(listed))]
    params = cb.calls[0]
    assert "not a cloud login" in params.message
    assert "tapo" not in params.message.lower()
    assert "not a cloud login" in params.requested_schema["properties"]["username"]["description"]


async def test_no_tapo_in_tool_descriptions(managed):
    _, reg, manager = managed
    server = build_server(reg, manager, allow_management=True)
    async with Client(server, mode="legacy") as client:
        tools = (await client.list_tools()).tools
    assert tools
    for t in tools:
        blob = f"{t.description} {json.dumps(t.input_schema)}"
        assert "tapo" not in blob.lower(), t.name
    from ipcam_mcp.server import _LoginForm

    assert "tapo" not in json.dumps(_LoginForm.model_json_schema()).lower()


async def test_add_camera_form_declined(managed):
    _, reg, manager = managed
    server = build_server(reg, manager, allow_management=True)
    async with Client(server, mode="legacy", elicitation_callback=answering("decline")) as client:
        r = await client.call_tool("add_camera", {"name": "garage", "host": "10.0.0.7"})
    assert not r.is_error
    assert "nothing was changed" in text_of(r)
    assert "garage" not in [c["id"] for c in reg.list()]


async def test_add_camera_form_error_gives_next_step(managed, tmp_path):
    cfg, reg, manager = managed
    before = cfg.read_text()

    async def cb(context, params):
        return ErrorData(code=INTERNAL_ERROR, message="client-side detail xyz")

    server = build_server(reg, manager, allow_management=True)
    async with Client(server, mode="legacy", elicitation_callback=cb) as client:
        r = await client.call_tool("add_camera", {"name": "garage", "host": "10.0.0.7"})
    assert r.is_error
    assert 'retry with credentials_via="file"' in text_of(r)
    assert "nothing was changed" in text_of(r)
    assert "client-side detail xyz" not in text_of(r)
    assert not (tmp_path / "secrets" / "garage.env").exists()
    assert cfg.read_text() == before


async def test_add_camera_invalid_form_content_does_not_leak(managed, tmp_path):
    cfg, reg, manager = managed
    before = cfg.read_text()
    form = {"username": ["admin_xyz"], "password": "pw_9876_secret"}  # gitleaks:allow
    cb = answering("accept", form)
    server = build_server(reg, manager, allow_management=True)
    async with Client(server, mode="legacy", elicitation_callback=cb) as client:
        r = await client.call_tool("add_camera", {"name": "garage", "host": "10.0.0.7"})
    assert r.is_error
    assert "does not match the requested schema" in text_of(r)  # the SDK's sanitized message
    assert "admin_xyz" not in text_of(r)
    assert "pw_9876_secret" not in text_of(r)
    assert not (tmp_path / "secrets" / "garage.env").exists()
    assert cfg.read_text() == before


async def test_add_camera_without_elicitation_falls_back(managed, tmp_path):
    _, reg, manager = managed
    server = build_server(reg, manager, allow_management=True)
    async with Client(server) as client:
        r = await client.call_tool("add_camera", {"name": "garage", "host": "10.0.0.7"})
    assert not r.is_error
    assert text_of(r).startswith("This client can't show login forms")
    assert str(tmp_path / "secrets" / "garage.env") in text_of(r)


async def test_add_camera_modern_protocol_falls_back(managed, tmp_path):
    _, reg, manager = managed
    cb = answering("accept", {"username": "u", "password": "s3cr3t"})
    server = build_server(reg, manager, allow_management=True)
    async with Client(server, elicitation_callback=cb) as client:  # negotiates 2026-07-28
        r = await client.call_tool("add_camera", {"name": "garage", "host": "10.0.0.7"})
    assert not r.is_error
    assert text_of(r).startswith("This client can't show login forms")
    assert str(tmp_path / "secrets" / "garage.env") in text_of(r)
    assert cb.calls == []


async def test_add_camera_explicit_file_mode_two_step(managed, tmp_path):
    _, reg, manager = managed
    env_file = tmp_path / "secrets" / "garage.env"
    cb = answering("accept", {"username": "u", "password": "s3cr3t"})
    server = build_server(reg, manager, allow_management=True)
    args = {"name": "garage", "host": "10.0.0.7", "credentials_via": "file"}
    async with Client(server, mode="legacy", elicitation_callback=cb) as client:
        first = await client.call_tool("add_camera", args)
        assert not first.is_error
        assert str(env_file) in text_of(first)
        assert 'credentials_via="file"' in text_of(first)

        # A default (form) retry must not prompt again; it points back to file mode.
        retry = await client.call_tool("add_camera", {"name": "garage", "host": "10.0.0.7"})
        assert retry.is_error
        assert 'credentials_via="file"' in text_of(retry)
        assert cb.calls == []

        env_file.write_text('HOST="10.0.0.7"\nUSER="u"\nPASSWORD="s3cr3t"\n')
        second = await client.call_tool("add_camera", args)
    assert not second.is_error
    assert "added 'garage'" in text_of(second)
    assert "s3cr3t" not in text_of(second)
    assert "garage" in [c["id"] for c in reg.list()]


async def test_rename_and_remove_camera(managed):
    _, reg, manager = managed
    server = build_server(reg, manager, allow_management=True)
    async with Client(server) as client:
        renamed = await client.call_tool("rename_camera", {"old": "office", "new": "study"})
        removed = await client.call_tool("remove_camera", {"name": "study"})
        missing = await client.call_tool("remove_camera", {"name": "nope"})
    assert "renamed 'office' to 'study'" in text_of(renamed)
    assert "removed 'study'" in text_of(removed)
    assert missing.is_error
    assert "camera 'nope' not found" in text_of(missing)


async def test_reload_cameras_picks_up_manual_edit(managed):
    cfg, reg, manager = managed
    cfg.write_text(cfg.read_text().replace("[cameras.office]", "[cameras.study]"))
    async with Client(build_server(reg, manager)) as client:
        r = await client.call_tool("reload_cameras", {})
    assert not r.is_error
    assert [c["id"] for c in json.loads(text_of(r))] == ["study"]


# --- camera_compare / baselines ---------------------------------------------------------------

from ipcam_mcp.imaging import CompareResult


def fake_compare(monkeypatch, **kw):
    seen = {}
    values = {
        "changed_pct": 0.0,
        "cells": [],
        "box": None,
        "aspect_mismatch": False,
        "annotated": b"\xff\xd8annot",
    }
    values.update(kw)

    def cmp(ref, cur):
        seen["ref"], seen["cur"] = ref, cur
        return CompareResult(**values)

    monkeypatch.setattr(server_mod, "compare", cmp)
    return seen


async def test_compare_without_previous_snapshot_errors(registry):
    r = await call(registry, "camera_compare", {"camera": "cam"})
    assert r.is_error
    assert "take a snapshot first" in text_of(r)


async def test_compare_reads_reference_before_capture(registry, tmp_path, monkeypatch):
    from ipcam_mcp.media import media_path

    cam = registry.get("cam", "camera")

    async def snap(quality):
        p = media_path(registry.folder("cam"), "cam", "jpg")
        p.write_bytes(b"NEW")
        return p

    monkeypatch.setattr(cam, "snapshot", snap)
    media_path(registry.folder("cam"), "cam", "jpg").write_bytes(b"OLD")
    seen = fake_compare(monkeypatch)
    r = await call(registry, "camera_compare", {"camera": "cam"})
    assert not r.is_error, text_of(r)
    assert seen["ref"] == b"OLD"
    assert seen["cur"] == b"NEW"


def prime(tmp_path):
    folder = tmp_path / uid_for("cam")
    folder.mkdir(exist_ok=True)
    Image.new("RGB", (64, 48), (10, 10, 10)).save(folder / "cam_20200101-000000.jpg", "JPEG")


async def test_compare_text_change(registry, tmp_path, monkeypatch):
    prime(tmp_path)
    fake_compare(
        monkeypatch, changed_pct=4.2, cells=["top-right", "right"], box=(0.62, 0.1, 0.3, 0.45)
    )
    r = await call(registry, "camera_compare", {"camera": "cam"})
    t = text_of(r)
    assert t.startswith("Compared with previous snapshot cam_20200101-000000.jpg (")
    assert (
        "Changed: 4.2% of the frame, in cells: top-right, right. Box: [0.62, 0.10, 0.30, 0.45]."
        in t
    )
    assert "Saved full-size snapshot:" in t
    assert [c for c in r.content if c.type == "image"]


async def test_compare_text_no_change(registry, tmp_path, monkeypatch):
    prime(tmp_path)
    fake_compare(monkeypatch, changed_pct=0.3)
    assert "No significant change." in text_of(
        await call(registry, "camera_compare", {"camera": "cam"})
    )


async def test_compare_text_lighting(registry, tmp_path, monkeypatch):
    prime(tmp_path)
    fake_compare(monkeypatch, changed_pct=60.0, cells=["top"])
    assert "probably lighting" in text_of(await call(registry, "camera_compare", {"camera": "cam"}))


async def test_compare_aspect_warning(registry, tmp_path, monkeypatch):
    prime(tmp_path)
    fake_compare(monkeypatch, aspect_mismatch=True)
    assert "different size or aspect" in text_of(
        await call(registry, "camera_compare", {"camera": "cam"})
    )


async def test_compare_baseline_moved_warning(registry, tmp_path, monkeypatch):
    fake_compare(monkeypatch)
    r = await call(registry, "camera_save_baseline", {"camera": "cam", "name": "door"})
    assert not r.is_error, text_of(r)
    assert "saved baseline 'door'" in text_of(r)
    cam = registry.get("cam", "camera")

    async def pos():
        return (0.5, -0.2)

    monkeypatch.setattr(cam, "position", pos)
    t = text_of(await call(registry, "camera_compare", {"camera": "cam", "baseline": "door"}))
    assert "Compared with baseline 'door' (" in t
    assert "camera has moved since the baseline; consider ptz_goto_preset" in t

    async def near():
        return (0.105, -0.205)

    monkeypatch.setattr(cam, "position", near)
    t = text_of(await call(registry, "camera_compare", {"camera": "cam", "baseline": "door"}))
    assert "has moved" not in t


async def test_baseline_roundtrip_real_compare(registry):
    await call(registry, "camera_save_baseline", {"camera": "cam", "name": "ok"})
    r = await call(registry, "camera_compare", {"camera": "cam", "baseline": "ok"})
    assert not r.is_error, text_of(r)
    assert "No significant change." in text_of(r)


async def test_compare_unknown_baseline_errors(registry):
    r = await call(registry, "camera_compare", {"camera": "cam", "baseline": "nope"})
    assert r.is_error


async def test_baseline_and_compare_tolerate_unreadable_position(registry, monkeypatch):
    from ipcam_mcp.errors import DeviceError

    fake_compare(monkeypatch)
    cam = registry.get("cam", "camera")

    async def broken():
        raise DeviceError("cam: timed out")

    monkeypatch.setattr(cam, "position", broken, raising=False)
    r = await call(registry, "camera_save_baseline", {"camera": "cam", "name": "door"})
    assert not r.is_error, text_of(r)
    assert "saved baseline 'door'" in text_of(r)
    b = server_mod.baselines.load_baseline(registry.folder("cam"), "door")
    assert b.position is None

    async def good():
        return (0.5, -0.2)

    monkeypatch.setattr(cam, "position", good)
    await call(registry, "camera_save_baseline", {"camera": "cam", "name": "pos"})
    monkeypatch.setattr(cam, "position", broken)
    r = await call(registry, "camera_compare", {"camera": "cam", "baseline": "pos"})
    assert not r.is_error, text_of(r)
    assert "has moved" not in text_of(r)


def test_main_refuses_remote_http_host(monkeypatch, tmp_path, capsys):
    bad_config(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as e:
        main(["--transport", "http", "--host", "0.0.0.0"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "--allow-remote" in err and "invalid TOML" not in err


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_main_allows_loopback_http_host(monkeypatch, tmp_path, capsys, host):
    bad_config(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        main(["--transport", "http", "--host", host])
    assert "invalid TOML" in capsys.readouterr().err  # got past the host check


def test_main_allow_remote_flag(monkeypatch, tmp_path, capsys):
    bad_config(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        main(["--transport", "http", "--host", "0.0.0.0", "--allow-remote"])
    assert "invalid TOML" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["127.0.0.2", "0:0:0:0:0:0:0:1"])
def test_main_refuses_loopback_without_sdk_protection(monkeypatch, tmp_path, capsys, host):
    """The MCP SDK only enables DNS-rebinding protection for these exact strings:
    127.0.0.1, localhost, ::1. Other loopback spellings would run unprotected."""
    bad_config(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as e:
        main(["--transport", "http", "--host", host])
    assert e.value.code == 2 and "--allow-remote" in capsys.readouterr().err


# --- delete_camera / removed cameras ---


def leftover(media: Path, uid: str = "zzzzzzzz", name: str = "garage") -> Path:
    from datetime import datetime as dt

    from ipcam_mcp.store import CameraMeta, write_meta

    folder = media / uid
    write_meta(folder, CameraMeta(uid, name, dt(2025, 1, 1).astimezone()))
    (folder / f"{name}_20250101-000000.jpg").write_bytes(b"x")
    return folder


async def test_delete_camera_form_flow(managed, tmp_path):
    _, reg, manager = managed
    folder = leftover(tmp_path / "media")
    cb = answering("accept", {"confirm": True})
    server = build_server(reg, manager, allow_management=True)
    async with Client(server, mode="legacy", elicitation_callback=cb) as client:
        r = await client.call_tool("delete_camera", {"name": "garage"})
    assert not r.is_error, text_of(r)
    assert text_of(r).startswith("deleted 'garage'")
    assert not folder.exists()
    params = cb.calls[0]
    assert "cannot be undone" in params.message
    assert (
        "garage (removed, added 2025-01-01)"
        in params.requested_schema["properties"]["confirm"]["description"]
    )


async def test_delete_camera_form_unchecked_cancels(managed, tmp_path):
    _, reg, manager = managed
    folder = leftover(tmp_path / "media")
    server = build_server(reg, manager, allow_management=True)
    cb = answering("accept", {"confirm": False})
    async with Client(server, mode="legacy", elicitation_callback=cb) as client:
        r = await client.call_tool("delete_camera", {"name": "garage"})
    assert text_of(r) == "cancelled; nothing was deleted"
    assert folder.exists()


async def test_delete_camera_form_choice(managed, tmp_path):
    _, reg, manager = managed
    first = leftover(tmp_path / "media", "yyyyyyyy")
    second = leftover(tmp_path / "media", "zzzzzzzz")
    server = build_server(reg, manager, allow_management=True)
    async with Client(
        server, mode="legacy", elicitation_callback=answering("accept", {"target": "all of them"})
    ) as client:
        r = await client.call_tool("delete_camera", {"name": "garage"})
    assert not r.is_error, text_of(r)
    assert not first.exists() and not second.exists()


async def test_delete_camera_no_form_client(managed, tmp_path):
    _, reg, manager = managed
    folder = leftover(tmp_path / "media")
    server = build_server(reg, manager, allow_management=True)
    async with Client(server) as client:
        r = await client.call_tool("delete_camera", {"name": "garage"})
    assert not r.is_error
    assert "nothing was deleted" in text_of(r) and str(folder) in text_of(r)
    assert folder.exists()


async def test_list_cameras_include_removed(managed, tmp_path):
    _, reg, manager = managed
    leftover(tmp_path / "media")
    server = build_server(reg, manager, allow_management=True)
    async with Client(server) as client:
        default = json.loads(text_of(await client.call_tool("list_cameras", {})))
        full = json.loads(
            text_of(await client.call_tool("list_cameras", {"include_removed": True}))
        )
    assert [c["id"] for c in default] == ["office"]
    assert full[1] == {
        "id": "garage",
        "uid": "zzzzzzzz",
        "scene": "garage",
        "removed": True,
        "created": "2025-01-01",
        "snapshots": 1,
        "recordings": 0,
        "baselines": 0,
    }


async def test_call_queued_behind_camera_removal_fails_cleanly(registry, tmp_path):
    """A camera call waiting for the camera lock while the camera is removed (delete_camera,
    remove_camera, rename_camera) must not run with the retired adapter afterwards."""
    async with Client(build_server(registry)) as client:
        async with registry.lock("cam"):
            call = asyncio.create_task(client.call_tool("camera_snapshot", {"camera": "cam"}))
            await asyncio.sleep(0.05)  # the call is now waiting for the lock
            registry.reload(AppConfig(media_dir=tmp_path, devices={}))
        r = await call
    assert r.is_error and "camera 'cam' not found" in text_of(r)
    assert not (tmp_path / uid_for("cam")).exists()


async def test_set_scene_tool(managed):
    _, reg, manager = managed
    async with Client(build_server(reg, manager, allow_management=True)) as client:
        r = await client.call_tool("set_scene", {"camera": "office", "scene": "desk"})
        listed = json.loads(text_of(await client.call_tool("list_cameras", {})))
    assert text_of(r) == "moved 'office' to scene 'desk'"
    assert listed[0]["scene"] == "desk"


async def test_set_scene_hidden_without_management(managed):
    _, reg, manager = managed
    assert "set_scene" not in await tool_names(build_server(reg, manager, allow_management=False))


async def test_add_camera_schema_has_scene(managed):
    _, reg, manager = managed
    async with Client(build_server(reg, manager, allow_management=True)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert "scene" in tools["add_camera"].input_schema["properties"]


async def test_list_cameras_include_removed_has_scene(managed, tmp_path):
    _, reg, manager = managed
    leftover(tmp_path / "media")
    async with Client(build_server(reg, manager, allow_management=True)) as client:
        full = json.loads(
            text_of(await client.call_tool("list_cameras", {"include_removed": True}))
        )
    assert full[1]["scene"] == "garage"


# --- compare stays within the scene ---


def old_snapshot(reg, camera="office", stamp="20200101-000000"):
    folder = reg.folder(camera)
    Image.new("RGB", (64, 48), (10, 10, 10)).save(folder / f"{camera}_{stamp}.jpg", "JPEG")


async def test_compare_ignores_snapshots_from_previous_scene(managed, monkeypatch):
    _, reg, manager = managed
    old_snapshot(reg)
    fake_compare(monkeypatch)
    async with Client(build_server(reg, manager, allow_management=True)) as client:
        await client.call_tool("set_scene", {"camera": "office", "scene": "desk"})
        r = await client.call_tool("camera_compare", {"camera": "office"})
    assert r.is_error
    assert "no previous snapshot of 'office'" in text_of(r)


async def test_compare_baseline_from_other_scene_not_found(managed, monkeypatch):
    _, reg, manager = managed
    fake_compare(monkeypatch)
    async with Client(build_server(reg, manager, allow_management=True)) as client:
        await client.call_tool("camera_save_baseline", {"camera": "office", "name": "door"})
        await client.call_tool("set_scene", {"camera": "office", "scene": "desk"})
        await client.call_tool("camera_save_baseline", {"camera": "office", "name": "lamp"})
        r = await client.call_tool("camera_compare", {"camera": "office", "baseline": "door"})
    assert r.is_error
    assert "baseline 'door' not found; available: lamp" in text_of(r)


async def test_compare_keeps_working_after_rename(managed, monkeypatch):
    _, reg, manager = managed
    old_snapshot(reg)
    fake_compare(monkeypatch)
    async with Client(build_server(reg, manager, allow_management=True)) as client:
        await client.call_tool("rename_camera", {"old": "office", "new": "front"})
        r = await client.call_tool("camera_compare", {"camera": "front"})
    assert not r.is_error, text_of(r)
    assert text_of(r).startswith("Compared with previous snapshot office_20200101-000000.jpg (")


async def test_from_path_without_camera(registry, tmp_path):
    src = tmp_path / "zzzzzzzz" / "gone_20250101-000000.jpg"  # a removed camera's snapshot
    src.parent.mkdir()
    Image.new("RGB", (64, 48), (10, 10, 10)).save(src, "JPEG")
    r = await call(registry, "camera_snapshot", {"from_path": str(src), "region": "center"})
    assert not r.is_error, text_of(r)
    assert text_of(r).startswith(f"Source: {src}")


async def test_snapshot_without_camera_or_from_path_errors(registry):
    r = await call(registry, "camera_snapshot", {})
    assert r.is_error
    assert "camera is required unless from_path is given" in text_of(r)


# --- successor forms ---


def answering_each(*answers):
    """An elicitation callback giving one (action, content) answer per call."""
    calls: list = []
    queue = list(answers)

    async def cb(context, params):
        calls.append(params)
        action, content = queue.pop(0)
        return ElicitResult(action=action, content=content)

    cb.calls = calls
    return cb


async def test_add_camera_successor_form(managed, tmp_path):
    _, reg, manager = managed
    leftover(tmp_path / "media")
    label = (
        "Replaces 'garage' (added 2025-01-01, 1 snapshot, 0 baselines): "
        "continue its compare and baselines"
    )
    cb = answering_each(
        ("accept", {"choice": label}), ("accept", {"username": "u", "password": "p"})
    )
    server = build_server(reg, manager, allow_management=True)
    async with Client(server, mode="legacy", elicitation_callback=cb) as client:
        r = await client.call_tool("add_camera", {"name": "garage", "host": "10.0.0.7"})
    assert not r.is_error, text_of(r)
    assert text_of(r).endswith("; continues 'garage' in scene 'garage'")
    choices = cb.calls[0].requested_schema["properties"]["choice"]["enum"]
    assert choices == [label, "An additional camera in 'garage'", "Cancel"]


async def test_set_scene_successor_form(managed, tmp_path):
    _, reg, manager = managed
    leftover(tmp_path / "media")
    cb = answering_each(("accept", {"choice": "An additional camera in 'garage'"}))
    server = build_server(reg, manager, allow_management=True)
    async with Client(server, mode="legacy", elicitation_callback=cb) as client:
        r = await client.call_tool("set_scene", {"camera": "office", "scene": "garage"})
        cancel = answering_each(("decline", None))
    assert text_of(r) == "moved 'office' to scene 'garage'"
    assert len(cb.calls) == 1 and "office" in cb.calls[0].message
    async with Client(server, mode="legacy", elicitation_callback=cancel) as client:
        r = await client.call_tool("set_scene", {"camera": "office", "scene": "office"})
    assert text_of(r) == "moved 'office' to scene 'office'"  # no removed camera in 'office'
    assert cancel.calls == []


# --- predecessor fallback ---

NOTE = "(camera changed: angle, lens or resolution may differ)"


async def choose(index):
    async def successor(message, labels):
        return index

    return successor


async def test_compare_falls_back_to_predecessor_with_note(managed, tmp_path, monkeypatch):
    _, reg, manager = managed
    leftover(tmp_path / "media")
    reg.folder("office")
    await manager.set_scene("office", "garage", successor=await choose(0))
    fake_compare(monkeypatch)
    async with Client(build_server(reg, manager)) as client:
        r = await client.call_tool("camera_compare", {"camera": "office"})
    assert not r.is_error, text_of(r)
    assert text_of(r).startswith("Compared with previous snapshot garage_20250101-000000.jpg")
    assert f"reference taken by 'garage' {NOTE}" in text_of(r)


async def test_compare_prefers_own_snapshot(managed, tmp_path, monkeypatch):
    _, reg, manager = managed
    leftover(tmp_path / "media")
    reg.folder("office")
    await manager.set_scene("office", "garage", successor=await choose(0))
    Image.new("RGB", (64, 48)).save(reg.folder("office") / "office_20300101-000000.jpg", "JPEG")
    fake_compare(monkeypatch)
    async with Client(build_server(reg, manager)) as client:
        r = await client.call_tool("camera_compare", {"camera": "office"})
    assert text_of(r).startswith("Compared with previous snapshot office_20300101-000000.jpg")
    assert "reference taken by" not in text_of(r)


async def test_compare_baseline_from_predecessor_two_steps(managed, tmp_path, monkeypatch):
    from datetime import datetime as dt

    from ipcam_mcp.baselines import save_baseline
    from ipcam_mcp.store import CameraMeta, Period, write_meta

    _, reg, manager = managed
    media = tmp_path / "media"
    old = dt(2025, 1, 1).astimezone()
    oldest = leftover(media, "zzzzzzzz", "garage")
    save_baseline(oldest, "door", oldest / "garage_20250101-000000.jpg", "high", None, "garage")
    write_meta(
        media / "yyyyyyyy",
        CameraMeta("yyyyyyyy", "garage-2", old, (Period("garage", old, "zzzzzzzz"),)),
    )
    reg.folder("office")

    async def pick_second(message, labels):
        return next(i for i, label in enumerate(labels) if "'garage-2'" in label)

    await manager.set_scene("office", "garage", successor=pick_second)
    fake_compare(monkeypatch)
    async with Client(build_server(reg, manager)) as client:
        r = await client.call_tool("camera_compare", {"camera": "office", "baseline": "door"})
        missing = await client.call_tool("camera_compare", {"camera": "office", "baseline": "x"})
    assert not r.is_error, text_of(r)
    assert text_of(r).startswith("Compared with baseline 'door'")
    assert f"reference taken by 'garage' {NOTE}" in text_of(r)
    assert "baseline 'x' not found; available: door" in text_of(missing)


async def test_predecessor_baseline_skips_position_check(managed, tmp_path, monkeypatch):
    """Another camera's PTZ position says nothing about this camera's."""
    from ipcam_mcp.baselines import save_baseline

    _, reg, manager = managed
    old = leftover(tmp_path / "media")
    save_baseline(old, "door", old / "garage_20250101-000000.jpg", "high", (5.0, 5.0), "garage")
    reg.folder("office")
    await manager.set_scene("office", "garage", successor=await choose(0))
    fake_compare(monkeypatch)
    async with Client(build_server(reg, manager)) as client:
        r = await client.call_tool("camera_compare", {"camera": "office", "baseline": "door"})
    assert not r.is_error, text_of(r)
    assert "moved" not in text_of(r)
