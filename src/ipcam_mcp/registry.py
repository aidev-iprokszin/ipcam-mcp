"""Map camera ids to adapter instances and look them up by capability."""

import asyncio
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ipcam_mcp import store
from ipcam_mcp.adapters.onvif import make_camera
from ipcam_mcp.capabilities import CAPABILITIES
from ipcam_mcp.config import AppConfig, DeviceConfig
from ipcam_mcp.errors import ConfigError, DeviceError

log = logging.getLogger(__name__)

AdapterFactory = Callable[[DeviceConfig, Path], object]

ADAPTERS: dict[str, AdapterFactory] = {
    "onvif": make_camera,
    "tapo_onvif": make_camera,
}


class Registry:
    def __init__(self, config: AppConfig, adapters: Mapping[str, AdapterFactory] = ADAPTERS):
        self._adapters = adapters
        self._devices: dict[str, tuple[DeviceConfig, object]] = {}
        self._media_dir = config.media_dir
        self._devices = self._build(config)
        self._locks: dict[str, asyncio.Lock] = {}
        self._retired: list[tuple[str, object]] = []

    def reload(self, config: AppConfig) -> None:
        """Swap in devices from config; on failure the current devices stay.

        Cameras whose config is unchanged keep their adapter (and its connection); replaced
        and removed adapters are queued for ``close_retired``.
        """
        devices = self._build(config)
        kept = {id(adapter) for _, adapter in devices.values()}
        self._retired += [
            (dev_id, adapter)
            for dev_id, (_, adapter) in self._devices.items()
            if id(adapter) not in kept
        ]
        self._devices = devices
        self._media_dir = config.media_dir

    async def close_retired(self) -> None:
        """Close adapters dropped by ``reload``, each once its in-flight call has finished."""
        retired, self._retired = self._retired, []
        for dev_id, adapter in retired:
            aclose = getattr(adapter, "aclose", None)
            if aclose is None:
                continue
            async with self.lock(dev_id):
                try:
                    await aclose()
                except Exception as e:  # noqa: BLE001 — best effort cleanup
                    log.warning("%s: closing old connection failed (%s)", dev_id, type(e).__name__)

    @property
    def media_dir(self) -> Path:
        return self._media_dir

    def _build(self, config: AppConfig) -> dict[str, tuple[DeviceConfig, object]]:
        devices: dict[str, tuple[DeviceConfig, object]] = {}
        for dev_id, dev in config.devices.items():
            factory = self._adapters.get(dev.type)
            if factory is None:
                known = ", ".join(sorted(self._adapters))
                raise ConfigError(f"camera '{dev_id}': unknown type '{dev.type}'; known: {known}")
            current = self._devices.get(dev_id)
            if current is not None and current[0] == dev and config.media_dir == self._media_dir:
                devices[dev_id] = current
            else:
                devices[dev_id] = (dev, factory(dev, config.media_dir))
        return devices

    def list(self) -> list[dict]:
        return [
            {"id": dev_id, "uid": dev.uid, "type": dev.type, "capabilities": _capabilities(adapter)}
            for dev_id, (dev, adapter) in self._devices.items()
        ]

    def _require(self, device_id: str) -> tuple[DeviceConfig, object]:
        if device_id not in self._devices:
            available = ", ".join(self._devices) or "(none)"
            raise DeviceError(f"camera '{device_id}' not found; available: {available}")
        return self._devices[device_id]

    def folder(self, device_id: str) -> Path:
        """The camera's media folder (``<media_dir>/<uid>/``), created if needed."""
        dev, _ = self._require(device_id)
        return store.ensure_folder(self._media_dir, dev.uid, device_id)

    def get(self, device_id: str, capability: str) -> Any:
        self._require(device_id)
        adapter = self._devices[device_id][1]
        if capability not in _capabilities(adapter):
            raise DeviceError(f"camera '{device_id}' does not support {capability}")
        return adapter

    def lock(self, device_id: str) -> asyncio.Lock:
        return self._locks.setdefault(device_id, asyncio.Lock())


def _capabilities(adapter: object) -> list[str]:
    return [name for name, proto in CAPABILITIES.items() if isinstance(adapter, proto)]
