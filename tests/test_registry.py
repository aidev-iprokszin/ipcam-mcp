import asyncio
import dataclasses
import time
from pathlib import Path
from typing import ClassVar

import pytest
from conftest import FakeCamPTZ, uid_for

from ipcam_mcp.adapters.onvif import OnvifPtzCamera
from ipcam_mcp.config import AppConfig, DeviceConfig
from ipcam_mcp.errors import ConfigError, DeviceError
from ipcam_mcp.registry import Registry
from ipcam_mcp.store import read_meta


class FakeCamera:
    def __init__(self, cfg: DeviceConfig, media_dir: Path):
        self.cfg = cfg

    async def snapshot(self, quality):
        return Path("x.jpg")

    async def record(self, seconds, quality):
        return Path("x.mp4")


def dev(dev_id: str = "cam", type_: str = "fake", host: str = "10.0.0.9") -> DeviceConfig:
    return DeviceConfig(
        id=dev_id, type=type_, host=host, user="u", password="pw", uid=uid_for(dev_id)
    )


def app(*devices: DeviceConfig, media_dir: Path = Path("/tmp/m")) -> AppConfig:
    return AppConfig(media_dir=media_dir, devices={d.id: d for d in devices})


@pytest.fixture
def registry() -> Registry:
    return Registry(app(dev("cam"), dev("other")), adapters={"fake": FakeCamera})


def test_unknown_type_raises():
    with pytest.raises(ConfigError) as e:
        Registry(app(dev(type_="nope")))
    assert "nope" in str(e.value)
    assert "tapo_onvif" in str(e.value)


def test_list():
    reg = Registry(app(dev("cam")), adapters={"fake": FakeCamera})
    listed = reg.list()
    assert listed == [
        {
            "id": "cam",
            "uid": uid_for("cam"),
            "scene": "cam",
            "type": "fake",
            "capabilities": ["camera"],
        }
    ]
    assert "10.0.0.9" not in repr(listed)
    assert "pw" not in repr(listed)


def test_list_tapo_capabilities_in_order(tmp_path):
    reg = Registry(app(dev("lr", "tapo_onvif"), media_dir=tmp_path))
    assert reg.list()[0]["capabilities"] == ["camera", "ptz"]


def test_get_ok(registry):
    assert isinstance(registry.get("cam", "camera"), FakeCamera)


def test_get_unknown_device(registry):
    with pytest.raises(DeviceError) as e:
        registry.get("x", "camera")
    assert str(e.value) == "camera 'x' not found; available: cam, other"


def test_get_missing_capability(registry):
    with pytest.raises(DeviceError) as e:
        registry.get("cam", "ptz")
    assert str(e.value) == "camera 'cam' does not support ptz"


def test_lock_per_device(registry):
    assert registry.lock("cam") is registry.lock("cam")
    assert registry.lock("cam") is not registry.lock("other")


def test_constructs_without_io(tmp_path):
    start = time.monotonic()
    reg = Registry(app(dev("lr", "tapo_onvif", host="192.0.2.1"), media_dir=tmp_path))
    assert time.monotonic() - start < 0.5
    assert isinstance(reg.get("lr", "camera"), OnvifPtzCamera)


def test_reload_swaps_devices():
    r = Registry(app(dev("a")), adapters={"fake": FakeCamera})
    r.reload(app(dev("b")))
    assert [d["id"] for d in r.list()] == ["b"]
    with pytest.raises(DeviceError, match="not found"):
        r.get("a", "camera")


def test_reload_failure_keeps_old():
    r = Registry(app(dev("a")), adapters={"fake": FakeCamera})
    before = r.list()
    with pytest.raises(ConfigError):
        r.reload(app(dev("b", type_="nope")))
    assert r.list() == before


async def test_inflight_op_survives_reload():
    gate = asyncio.Event()

    class SlowCamera(FakeCamera):
        async def snapshot(self, quality):
            await gate.wait()
            return Path("slow.jpg")

    r = Registry(app(dev("a")), adapters={"fake": SlowCamera})
    task = asyncio.create_task(r.get("a", "camera").snapshot("high"))
    await asyncio.sleep(0)
    r.reload(app())
    gate.set()
    assert await task == Path("slow.jpg")
    with pytest.raises(DeviceError, match="not found"):
        r.get("a", "camera")


def test_onvif_type_has_no_ptz_by_default(tmp_path):
    reg = Registry(
        app(
            dev("c", "onvif"),
            dataclasses.replace(dev("p", "onvif"), settings={"ptz": True}),
            media_dir=tmp_path,
        )
    )
    assert [(d["id"], d["capabilities"]) for d in reg.list()] == [
        ("c", ["camera"]),
        ("p", ["camera", "ptz"]),
    ]


def test_registry_media_dir(tmp_path):
    reg = Registry(app(dev(), media_dir=tmp_path / "a"), adapters={"fake": FakeCamera})
    assert reg.media_dir == tmp_path / "a"
    reg.reload(app(dev(), media_dir=tmp_path / "b"))
    assert reg.media_dir == tmp_path / "b"
    with pytest.raises(ConfigError):
        reg.reload(app(dev(type_="nope"), media_dir=tmp_path / "c"))
    assert reg.media_dir == tmp_path / "b"


class ClosableCamera(FakeCamera):
    closed: ClassVar[list[str]] = []

    async def aclose(self):
        ClosableCamera.closed.append(self.cfg.id)


async def test_reload_keeps_unchanged_adapters_and_closes_replaced_ones():
    ClosableCamera.closed = []
    reg = Registry(app(dev("keep"), dev("change"), dev("gone")), adapters={"fake": ClosableCamera})
    keep, change = reg.get("keep", "camera"), reg.get("change", "camera")
    reg.reload(app(dev("keep"), dev("change", host="10.0.0.10")))
    assert reg.get("keep", "camera") is keep  # learned state and connection survive
    assert reg.get("change", "camera") is not change
    assert ClosableCamera.closed == []  # closing happens in close_retired, not in reload
    await reg.close_retired()
    assert sorted(ClosableCamera.closed) == ["change", "gone"]
    await reg.close_retired()
    assert sorted(ClosableCamera.closed) == ["change", "gone"]  # only once


async def test_close_retired_waits_for_in_flight_call():
    ClosableCamera.closed = []
    reg = Registry(app(dev("cam")), adapters={"fake": ClosableCamera})
    async with reg.lock("cam"):
        reg.reload(app())
        task = asyncio.create_task(reg.close_retired())
        await asyncio.sleep(0.01)
        assert ClosableCamera.closed == []  # still in use
    await task
    assert ClosableCamera.closed == ["cam"]


async def test_close_retired_ignores_adapters_without_aclose_and_errors():
    class Broken(FakeCamera):
        async def aclose(self):
            raise RuntimeError("boom")

    reg = Registry(app(dev("a")), adapters={"fake": Broken})
    reg.reload(app())
    await reg.close_retired()  # must not raise
    reg2 = Registry(app(dev("b")), adapters={"fake": FakeCamera})
    reg2.reload(app())
    await reg2.close_retired()


def test_folder(tmp_path):
    reg = Registry(AppConfig(tmp_path, {"lr": dev("lr")}), adapters={"fake": FakeCamPTZ})
    assert reg.folder("lr") == tmp_path / uid_for("lr")
    assert (tmp_path / uid_for("lr") / "camera.json").exists()


def test_folder_unknown_camera(tmp_path):
    reg = Registry(AppConfig(tmp_path, {}))
    with pytest.raises(DeviceError, match="camera 'nope' not found"):
        reg.folder("nope")


def test_list_has_scene_and_config(tmp_path):
    d = dataclasses.replace(dev("cam"), scene="front-door")
    reg = Registry(AppConfig(tmp_path, {"cam": d}), adapters={"fake": FakeCamPTZ})
    assert reg.list()[0]["scene"] == "front-door"
    assert reg.config("cam").scene == "front-door"
    assert read_meta(reg.folder("cam")).current_scene == "front-door"
    with pytest.raises(DeviceError, match="camera 'nope' not found"):
        reg.config("nope")
