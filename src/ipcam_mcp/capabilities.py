"""Capability protocols that camera adapters implement.

Tools are defined per capability, so any adapter implementing ``Camera`` works with
the ``camera_*`` tools, and so on. Add a new Protocol here for a new camera feature.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

Quality = Literal["high", "low"]
Direction = Literal["left", "right", "up", "down"]
Amount = Literal["small", "medium", "large"]


@dataclass(frozen=True)
class Preset:
    name: str
    token: str


@runtime_checkable
class Camera(Protocol):
    async def snapshot(self, quality: Quality) -> Path: ...

    async def record(self, seconds: int, quality: Quality) -> Path: ...


@runtime_checkable
class PTZ(Protocol):
    async def move(self, direction: Direction, amount: Amount) -> tuple[float, float] | None: ...

    async def list_presets(self) -> list[Preset]: ...

    async def goto_preset(self, preset: str) -> None: ...

    async def save_preset(self, name: str, overwrite: bool = False) -> Preset: ...

    async def remove_preset(self, preset: str) -> Preset: ...

    async def position(self) -> tuple[float, float] | None: ...


CAPABILITIES: dict[str, type] = {"camera": Camera, "ptz": PTZ}
