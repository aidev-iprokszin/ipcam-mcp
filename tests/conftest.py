"""Shared test doubles."""

import asyncio
from pathlib import Path

from PIL import Image

from ipcam_mcp.capabilities import Preset
from ipcam_mcp.config import DeviceConfig
from ipcam_mcp.errors import DeviceError
from ipcam_mcp.media import media_path
from ipcam_mcp.store import ensure_folder


class FakeCamPTZ:
    def __init__(self, cfg: DeviceConfig, media_dir: Path):
        self.cfg = cfg
        self.media_dir = media_dir
        self.snapshots = 0
        self.active = 0
        self.overlaps = 0
        self.fail_with: Exception | None = None
        self.presets = [Preset("Door", "1")]

    async def snapshot(self, quality):
        self.snapshots += 1
        p = media_path(ensure_folder(self.media_dir, self.cfg.uid, self.cfg.id), self.cfg.id, "jpg")
        Image.new("RGB", (64, 48), (200, 30, 30)).save(p, "JPEG")
        return p

    async def record(self, seconds, quality):
        if not 1 <= seconds <= 60:
            raise ValueError(f"seconds must be between 1 and 60, got {seconds}")
        p = ensure_folder(self.media_dir, self.cfg.uid, self.cfg.id) / "clip.mp4"
        p.write_bytes(b"x" * 1234)
        return p

    async def move(self, direction, amount):
        if self.fail_with:
            raise self.fail_with
        self.active += 1
        if self.active > 1:
            self.overlaps += 1
        await asyncio.sleep(0.05)
        self.active -= 1
        return (0.1, -0.2)

    async def list_presets(self):
        return list(self.presets)

    async def goto_preset(self, preset):
        if preset != "Door":
            raise DeviceError(f"preset '{preset}' not found; available: Door")

    async def save_preset(self, name, overwrite=False):
        existing = next((p for p in self.presets if p.name == name), None)
        if existing and not overwrite:
            raise DeviceError(f"preset '{name}' exists; pass overwrite=true to replace it")
        if existing:
            return existing
        new = Preset(name, str(len(self.presets) + 1))
        self.presets.append(new)
        return new

    async def remove_preset(self, preset):
        match = next((p for p in self.presets if preset in (p.name, p.token)), None)
        if match is None:
            raise DeviceError(f"preset '{preset}' not found; available: Door")
        self.presets.remove(match)
        return match

    async def position(self):
        return (0.1, -0.2)


def uid_for(name: str) -> str:
    """A deterministic, valid camera uid for test configs."""
    import hashlib

    return hashlib.sha1(name.encode()).hexdigest()[:8]
