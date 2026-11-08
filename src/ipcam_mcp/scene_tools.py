"""Tools that work on a scene (the place cameras watch) instead of a single camera."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ipcam_mcp import scenes
from ipcam_mcp.annotations import changes, read_only
from ipcam_mcp.capabilities import Quality
from ipcam_mcp.registry import Registry
from ipcam_mcp.tooling import client_message, guarded

CameraOp = Callable[[str], Awaitable[list]]
MAX_LIMIT = 500


def add_scene_tools(
    mcp: MCPServer,
    registry: Registry,
    snapshot: Callable[[str, Quality], Awaitable[list]],
    compare: Callable[[str, str | None, Quality], Awaitable[list]],
    record: Callable[[str, int, Quality], Awaitable[list]],
) -> None:
    """Register the scene tools. ``snapshot``/``compare``/``record`` do one camera's work
    (and raise)."""

    def index() -> dict[str, scenes.SceneInfo]:
        return scenes.scene_index(registry.media_dir, registry.configs())

    def find(scene: str) -> scenes.SceneInfo:
        found = index()
        if scene not in found:
            raise scenes.unknown_scene(scene, found)
        return found[scene]

    async def per_camera(scene: str, op: CameraOp) -> list:
        # The index scans every camera folder: keep it off the event loop.
        cameras = (await asyncio.to_thread(find, scene)).cameras
        if not cameras:
            raise ValueError(
                f"scene '{scene}' has no configured camera; scene_timeline shows its history"
            )

        async def one(camera: str) -> list | str:
            """The camera's labelled result, or its error message."""
            try:
                return [f"[{camera}]", *await op(camera)]
            except Exception as e:  # noqa: BLE001 — one camera failing must not fail the rest
                return client_message(e)

        results = dict(zip(cameras, await asyncio.gather(*(one(c) for c in cameras))))
        failed = {c: r for c, r in results.items() if isinstance(r, str)}
        if len(failed) == len(results):
            details = "; ".join(f"[{c}] {msg}" for c, msg in failed.items())
            raise ValueError(f"every camera in '{scene}' failed: {details}")
        return [
            item
            for camera, r in results.items()
            for item in ([f"[{camera}] error: {r}"] if isinstance(r, str) else r)
        ]

    @mcp.tool(**read_only("List scenes"))
    async def list_scenes() -> str:
        """The scenes (places watched): their configured cameras, removed cameras that were
        there, the time of the oldest and newest capture, and how many there are."""

        def run():
            return json.dumps(
                [
                    {
                        "scene": s.scene,
                        "cameras": s.cameras,
                        "removed_cameras": [{"name": n, "uid": u} for n, u in s.removed],
                        "first": s.first.isoformat() if s.first else None,
                        "last": s.last.isoformat() if s.last else None,
                        "snapshots": s.snapshots,
                        "recordings": s.recordings,
                        "baselines": s.baselines,
                    }
                    for s in index().values()
                ],
                indent=2,
            )

        return await guarded(lambda: asyncio.to_thread(run))

    @mcp.tool(**changes("Snapshot every camera in a scene", destructive=False, idempotent=False))
    async def scene_snapshot(scene: str, quality: Quality = "high") -> list:
        """Take a snapshot with every camera in the scene, labelled with the camera's name."""
        return await guarded(lambda: per_camera(scene, lambda c: snapshot(c, quality)))

    @mcp.tool(**changes("Compare every camera in a scene", destructive=False, idempotent=False))
    async def scene_compare(
        scene: str, baseline: str | None = None, quality: Quality = "high"
    ) -> list:
        """camera_compare for every camera in the scene, labelled with the camera's name."""
        return await guarded(lambda: per_camera(scene, lambda c: compare(c, baseline, quality)))

    @mcp.tool(**changes("Record every camera in a scene", destructive=False, idempotent=False))
    async def scene_record(
        scene: str,
        seconds: Annotated[int, Field(ge=1, le=60)] = 10,
        quality: Quality = "high",
    ) -> list:
        """Record a video clip (1-60 s) with every camera in the scene at the same time,
        labelled with the camera's name. Returns each file's path and size, not the video.

        quality: "high" = main stream (full resolution set on the camera); "low" = sub stream (smaller file).
        """
        return await guarded(lambda: per_camera(scene, lambda c: record(c, seconds, quality)))

    @mcp.tool(**read_only("Scene timeline"))
    async def scene_timeline(
        scene: str,
        start: str | None = None,
        end: str | None = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 50,
    ) -> str:
        """The scene's snapshots, recordings and baselines, newest first, from every camera
        that watched it (removed ones too). start/end: YYYY-MM-DD (whole day) or an ISO
        datetime. limit: 1-500 entries. Open a snapshot with camera_snapshot(from_path=...)."""

        def run():
            lo = scenes.parse_bound(start, end=False) if start else None
            hi = scenes.parse_bound(end, end=True) if end else None
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(f"start ({start}) is after end ({end})")
            entries, more = scenes.timeline(
                registry.media_dir, registry.configs(), scene, lo, hi, limit
            )
            if not entries:
                ranged = start is not None or end is not None
                return f"no captures of '{scene}' {'in that range' if ranged else 'yet'}"
            lines = [
                f"{e.time.isoformat()}  {e.camera}{' (removed)' if e.removed else ''}  "
                f"{e.kind}  {e.path}"
                for e in entries
            ]
            if more:
                lines.append(f"… and {more} more")
            return "\n".join(lines)

        return await guarded(lambda: asyncio.to_thread(run))
