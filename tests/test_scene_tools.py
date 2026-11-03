import json
from datetime import datetime

import pytest
from conftest import FakeCamPTZ
from mcp import Client
from PIL import Image

from ipcam_mcp import server as server_mod
from ipcam_mcp.config import AppConfig, DeviceConfig
from ipcam_mcp.errors import DeviceError
from ipcam_mcp.imaging import CompareResult
from ipcam_mcp.registry import Registry
from ipcam_mcp.server import build_server
from ipcam_mcp.store import CameraMeta, Period, write_meta

OLD = datetime(2025, 1, 1).astimezone()


def dev(name, uid, scene) -> DeviceConfig:
    return DeviceConfig(
        id=name, type="fake", host="10.0.0.9", user="u", password="p", uid=uid, scene=scene
    )


def jpg(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), (10, 10, 10)).save(path, "JPEG")
    return path


@pytest.fixture
def registry(tmp_path) -> Registry:
    """door-l and door-r watch front-door; old-door (removed) watched it before; shed is a
    scene of a removed camera only."""
    reg = Registry(
        AppConfig(
            media_dir=tmp_path,
            devices={
                "door-l": dev("door-l", "aaaaaaaa", "front-door"),
                "door-r": dev("door-r", "bbbbbbbb", "front-door"),
            },
        ),
        adapters={"fake": FakeCamPTZ},
    )
    reg.folder("door-l")
    reg.folder("door-r")
    write_meta(
        tmp_path / "cccccccc", CameraMeta("cccccccc", "old-door", OLD, (Period("front-door", OLD),))
    )
    jpg(tmp_path / "cccccccc" / "old-door_20250102-080000.jpg")
    write_meta(tmp_path / "dddddddd", CameraMeta("dddddddd", "shed", OLD, (Period("shed", OLD),)))
    jpg(tmp_path / "dddddddd" / "shed_20250103-080000.jpg")
    return reg


async def call(registry, tool, args=None):
    async with Client(build_server(registry)) as client:
        return await client.call_tool(tool, args or {})


def texts(result) -> list[str]:
    return [c.text for c in result.content if c.type == "text"]


def kinds(result) -> list[str]:
    return [c.type for c in result.content]


async def test_list_scenes(registry):
    r = await call(registry, "list_scenes")
    assert not r.is_error
    data = {s["scene"]: s for s in json.loads(texts(r)[0])}
    assert sorted(data) == ["front-door", "shed"]
    front = data["front-door"]
    assert front["cameras"] == ["door-l", "door-r"]
    assert front["removed_cameras"] == [{"name": "old-door", "uid": "cccccccc"}]
    assert front["snapshots"] == 1 and front["recordings"] == 0 and front["baselines"] == 0
    assert front["first"] == front["last"] == datetime(2025, 1, 2, 8).astimezone().isoformat()
    assert data["shed"]["cameras"] == []


async def test_scene_snapshot_two_cameras(registry):
    r = await call(registry, "scene_snapshot", {"scene": "front-door"})
    assert not r.is_error
    assert kinds(r) == ["text", "image", "text", "text", "image", "text"]
    t = texts(r)
    assert t[0] == "[door-l]" and t[2] == "[door-r]"
    assert t[1].startswith("Saved full-size snapshot: ") and "aaaaaaaa" in t[1]


async def test_scene_snapshot_one_camera_fails(registry, monkeypatch):
    cam = registry.get("door-l", "camera")

    async def broken(quality):
        raise DeviceError("door-l: timed out")

    monkeypatch.setattr(cam, "snapshot", broken)
    r = await call(registry, "scene_snapshot", {"scene": "front-door"})
    assert not r.is_error
    t = texts(r)
    assert t[0] == "[door-l] error: door-l: timed out"
    assert t[1] == "[door-r]"
    assert kinds(r) == ["text", "text", "image", "text"]


async def test_scene_compare_blocks(registry, tmp_path, monkeypatch):
    jpg(tmp_path / "aaaaaaaa" / "door-l_20200101-000000.jpg")  # before its first period
    monkeypatch.setattr(
        server_mod,
        "compare",
        lambda ref, cur: CompareResult(0.0, [], None, False, b"\xff\xd8annot"),
    )
    r = await call(registry, "scene_compare", {"scene": "front-door"})
    assert not r.is_error
    t = texts(r)
    assert t[0] == "[door-l]"
    assert t[1].startswith("Compared with previous snapshot door-l_20200101-000000.jpg")
    assert t[2].startswith("[door-r] error: no previous snapshot of 'door-r'")


async def test_scene_tools_unknown_scene(registry):
    for tool in ("scene_snapshot", "scene_compare", "scene_timeline"):
        r = await call(registry, tool, {"scene": "attic"})
        assert r.is_error
        assert "scene 'attic' not found; available: front-door, shed" in texts(r)[0]


async def test_scene_snapshot_removed_only_scene_errors(registry):
    for tool in ("scene_snapshot", "scene_compare"):
        r = await call(registry, tool, {"scene": "shed"})
        assert r.is_error
        assert (
            "scene 'shed' has no configured camera; scene_timeline shows its history"
            in (texts(r)[0])
        )


async def test_scene_timeline_output_and_limits(registry, tmp_path):
    jpg(tmp_path / "aaaaaaaa" / "door-l_20261104-100000.jpg")
    (tmp_path / "bbbbbbbb" / "door-r_20261104-110000.mp4").write_bytes(b"x")
    r = await call(registry, "scene_timeline", {"scene": "front-door"})
    lines = texts(r)[0].splitlines()

    def line(when, camera, kind, path):
        return f"{when.astimezone().isoformat()}  {camera}  {kind}  {path}"

    assert lines == [
        line(
            datetime(2026, 11, 4, 11),
            "door-r",
            "recording",
            tmp_path / "bbbbbbbb" / "door-r_20261104-110000.mp4",
        ),
        line(
            datetime(2026, 11, 4, 10),
            "door-l",
            "snapshot",
            tmp_path / "aaaaaaaa" / "door-l_20261104-100000.jpg",
        ),
        line(
            datetime(2025, 1, 2, 8),
            "old-door (removed)",
            "snapshot",
            tmp_path / "cccccccc" / "old-door_20250102-080000.jpg",
        ),
    ]
    r = await call(registry, "scene_timeline", {"scene": "front-door", "limit": 1})
    assert texts(r)[0].splitlines()[-1] == "… and 2 more"
    r = await call(
        registry,
        "scene_timeline",
        {"scene": "front-door", "start": "2026-11-04", "end": "2026-11-04"},
    )
    assert len(texts(r)[0].splitlines()) == 2
    r = await call(registry, "scene_timeline", {"scene": "front-door", "end": "2024-12-31"})
    assert texts(r)[0] == "no captures of 'front-door' in that range"
    for bad in (0, 501):
        r = await call(registry, "scene_timeline", {"scene": "front-door", "limit": bad})
        assert r.is_error and "limit" in texts(r)[0]


async def test_scene_timeline_bad_date(registry):
    r = await call(registry, "scene_timeline", {"scene": "front-door", "start": "last week"})
    assert r.is_error
    assert "invalid date 'last week': use YYYY-MM-DD or an ISO datetime" in texts(r)[0]


async def test_scene_tools_without_management(registry):
    async with Client(build_server(registry, None, allow_management=False)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"list_scenes", "scene_snapshot", "scene_compare", "scene_timeline"} <= names
