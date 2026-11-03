"""Live tests against the real camera. Opt-in: ``uv run pytest -m live``.

Uses the user's real config (``IPCAM_MCP_CONFIG`` or ``~/.config/ipcam-mcp/cameras.toml``) and
the camera named by ``IPCAM_MCP_CAMERA`` (default ``livingroom``, e.g.
``IPCAM_MCP_CAMERA=office``).
"""

import contextlib
import os
import shutil
import subprocess

import pytest
import tomlkit
from mcp import Client

from ipcam_mcp.config import config_path, load_config
from ipcam_mcp.config_writer import write_secrets_file
from ipcam_mcp.errors import ConfigError
from ipcam_mcp.management import Manager
from ipcam_mcp.registry import Registry
from ipcam_mcp.server import build_server

pytestmark = pytest.mark.live

DEVICE = os.environ.get("IPCAM_MCP_CAMERA", "livingroom")


@pytest.fixture
def registry() -> Registry:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    try:
        reg = Registry(load_config())
    except ConfigError as e:
        pytest.skip(f"no usable config: {e}")
    if DEVICE not in {d["id"] for d in reg.list()}:
        pytest.skip(f"camera {DEVICE} not configured")
    return reg


async def test_snapshot_is_jpeg(registry):
    path = await registry.get(DEVICE, "camera").snapshot("high")
    data = path.read_bytes()
    assert data.startswith(b"\xff\xd8")
    assert len(data) > 10_000


async def test_record_3s_is_valid_mp4(registry):
    path = await registry.get(DEVICE, "camera").record(3, "low")
    duration = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert 2 <= float(duration) <= 5


async def test_list_presets_runs(registry):
    presets = await registry.get(DEVICE, "ptz").list_presets()
    assert isinstance(presets, list)


async def test_move_and_back(registry):
    ptz = registry.get(DEVICE, "ptz")
    await ptz.move("left", "small")
    await ptz.move("right", "small")


async def test_live_preset_save_goto_remove(registry):
    ptz = registry.get(DEVICE, "ptz")
    try:
        saved = await ptz.save_preset("cmcp_test", overwrite=True)
        assert saved.name == "cmcp_test"
        await ptz.goto_preset("cmcp_test")
    finally:
        with contextlib.suppress(Exception):
            await ptz.remove_preset("cmcp_test")
    assert "cmcp_test" not in [p.name for p in await ptz.list_presets()]


async def test_live_baseline_compare_no_change(registry):
    folder = registry.media_dir / "baselines" / DEVICE
    try:
        async with Client(build_server(registry)) as client:
            saved = await client.call_tool(
                "camera_save_baseline", {"camera": DEVICE, "name": "cmcp_test"}
            )
            assert not saved.is_error
            result = await client.call_tool(
                "camera_compare", {"camera": DEVICE, "baseline": "cmcp_test"}
            )
        assert not result.is_error
        text = "\n".join(c.text for c in result.content if c.type == "text")
        assert "No significant change." in text
    finally:
        for suffix in (".jpg", ".json"):
            (folder / f"cmcp_test{suffix}").unlink(missing_ok=True)


async def test_live_add_snapshot_remove(tmp_path):
    """File-mode add of a second entry for the real camera, snapshot, remove. Works on a copy of
    the config with secrets_dir and media_dir in tmp_path; the real files are only read."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    try:
        real = load_config()
    except ConfigError as e:
        pytest.skip(f"no usable config: {e}")
    if DEVICE not in real.devices:
        pytest.skip(f"camera {DEVICE} not configured")
    source = real.devices[DEVICE]

    doc = tomlkit.parse(config_path().read_text())
    doc["secrets_dir"] = str(tmp_path / "secrets")
    doc["media_dir"] = str(tmp_path)
    copy = tmp_path / "cameras.toml"
    copy.write_text(tomlkit.dumps(doc))
    original = copy.read_bytes()

    cfg = load_config(copy)
    assert cfg.secrets_dir == tmp_path / "secrets"
    registry = Registry(cfg)
    manager = Manager(copy, registry)
    write_secrets_file(
        cfg.secrets_dir / "office_test.env",
        {"HOST": source.host, "USER": source.user, "PASSWORD": source.password},
    )

    added = await manager.add("office_test", source.host, None)
    assert "added" in added
    try:
        path = await registry.get("office_test", "camera").snapshot("high")
        assert path.is_file() and path.stat().st_size > 0
        assert tmp_path in path.parents
    finally:
        await manager.remove("office_test")
    assert copy.read_bytes() == original
