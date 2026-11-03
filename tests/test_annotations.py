"""Tool annotations (MCP hints) as a client sees them in list_tools."""

import pytest
from mcp import Client

from ipcam_mcp.config import AppConfig
from ipcam_mcp.management import Manager
from ipcam_mcp.registry import Registry
from ipcam_mcp.server import build_server

READ = "read-only"
ADD = "additive"  # changes things without losing data
DESTROY = "destructive"  # may replace or delete data

# tool: (kind, idempotent) — idempotent only matters for tools that are not read-only
EXPECTED = {
    "list_cameras": (READ, None),
    "list_scenes": (READ, None),
    "scene_timeline": (READ, None),
    "ptz_list_presets": (READ, None),
    "camera_snapshot": (ADD, False),
    "scene_snapshot": (ADD, False),
    "camera_compare": (ADD, False),
    "scene_compare": (ADD, False),
    "camera_record": (ADD, False),
    "camera_save_baseline": (DESTROY, False),
    "ptz_move": (ADD, False),
    "ptz_goto_preset": (ADD, True),
    "ptz_save_preset": (DESTROY, False),
    "ptz_remove_preset": (DESTROY, False),
    "reload_cameras": (ADD, True),
    "add_camera": (ADD, False),
    "rename_camera": (ADD, False),
    "set_scene": (ADD, False),
    "remove_camera": (DESTROY, False),
    "delete_camera": (DESTROY, False),
}


@pytest.fixture
async def tools(tmp_path):
    registry = Registry(AppConfig(tmp_path, {}))
    manager = Manager(tmp_path / "cameras.toml", registry)
    async with Client(build_server(registry, manager)) as client:
        return {t.name: t for t in (await client.list_tools()).tools}


async def test_every_tool_is_covered(tools):
    # A new tool must be added to EXPECTED, so it cannot ship without annotations.
    assert sorted(tools) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_tool_annotations(tools, name):
    kind, idempotent = EXPECTED[name]
    a = tools[name].annotations
    assert a is not None, f"{name} has no annotations"
    assert a.open_world_hint is False  # only local cameras and the local disk
    assert a.read_only_hint is (kind == READ)
    if kind != READ:
        assert a.destructive_hint is (kind == DESTROY)
        assert a.idempotent_hint is idempotent


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_tool_has_a_title(tools, name):
    title = tools[name].title
    assert title and title != name
