"""MCP server exposing local camera tools (snapshot, record, pan/tilt)."""

import argparse
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp import types
from mcp.server.elicitation import AcceptedElicitation
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field, create_model

from ipcam_mcp import baselines, scenes, store
from ipcam_mcp.capabilities import PTZ, Amount, Direction, Quality
from ipcam_mcp.config import AppConfig, config_path
from ipcam_mcp.errors import CameraMcpError, ConfigError, DeviceError
from ipcam_mcp.imaging import (
    LIGHTING_PCT,
    NO_CHANGE_PCT,
    compare,
    crop_box,
    crop_jpeg,
)
from ipcam_mcp.management import Credentials, Manager, prepare_config
from ipcam_mcp.media import make_preview
from ipcam_mcp.migrate import migrate_flat_layout
from ipcam_mcp.registry import Registry

log = logging.getLogger("ipcam_mcp")


async def _position(adapter) -> tuple[float, float] | None:
    """PTZ position, or None when the camera cannot report it right now."""
    try:
        return await adapter.position()
    except DeviceError as e:
        log.warning("could not read PTZ position (%s)", type(e).__name__)
        return None


INSTRUCTIONS = (
    "Control local network cameras. Call list_cameras first to learn camera ids and what each "
    "supports (ptz = pan/tilt). Snapshots and recordings are saved on the user's machine; paths "
    "are returned."
)


async def _guarded[T](fn: Callable[[], Awaitable[T]]) -> T:
    """Turn our errors into tool errors; hide details of unexpected ones."""
    try:
        return await fn()
    except (CameraMcpError, ValueError) as e:
        raise ToolError(str(e)) from None
    except Exception as e:  # noqa: BLE001 — never leak unexpected messages to the client
        log.error("unexpected %s in tool call", type(e).__name__)
        raise ToolError(f"internal error ({type(e).__name__}); see server log") from None


Region = Literal[
    "top-left",
    "top",
    "top-right",
    "left",
    "center",
    "right",
    "bottom-left",
    "bottom",
    "bottom-right",
]

FORM_FAILED = 'the login form failed; nothing was changed; retry with credentials_via="file"'
NO_FORM_PREFIX = "This client can't show login forms; using the secrets file instead. "


DELETE_ALL = "all of them"


def _delete_form(targets: list[str]) -> type[BaseModel]:
    """Approval form for delete_camera: a checkbox for one target, a choice for several."""
    if len(targets) == 1:
        return create_model(
            "DeleteForm", confirm=(bool, Field(default=False, description=f"Delete {targets[0]}"))
        )
    choices = Literal[(*targets, DELETE_ALL)]  # type: ignore[valid-type]
    return create_model(
        "DeleteForm", target=(choices, Field(description="What to delete (cannot be undone)"))
    )


class _LoginForm(BaseModel):
    username: str = Field(
        description=(
            "The camera's local ONVIF/RTSP account user "
            "(not a cloud login; see docs/cameras for your brand)"
        )
    )
    password: str = Field(description="Camera account password")


MOVED_TOLERANCE = 0.01


def _age(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h"
    return f"{s // 86400} d"


def _compare_text(res, label: str, age: str, path: Path, moved: bool) -> str:
    lines = [f"Compared with {label} ({age} ago)."]
    if res.changed_pct < NO_CHANGE_PCT:
        lines.append("No significant change.")
    else:
        cells = ", ".join(res.cells) or "none"
        line = f"Changed: {res.changed_pct}% of the frame, in cells: {cells}."
        if res.box is not None:
            x, y, w, h = res.box
            line += f" Box: [{x:.2f}, {y:.2f}, {w:.2f}, {h:.2f}]."
        lines.append(line)
    if res.changed_pct > LIGHTING_PCT:
        lines.append(
            "Most of the frame changed: probably lighting (e.g. night mode) or the camera moved."
        )
    if res.aspect_mismatch:
        lines.append(
            "The reference has a different size or aspect (another quality/stream?); "
            "the result is approximate."
        )
    if moved:
        lines.append("Warning: camera has moved since the baseline; consider ptz_goto_preset.")
    lines.append(f"Saved full-size snapshot: {path}")
    return "\n".join(lines)


def _can_show_form(ctx: Context) -> bool:
    """Whether ``ctx.elicit`` can show a form: the client must advertise elicitation and the
    connection must allow server-to-client requests mid-call (not so on 2026-07-28+)."""
    session = ctx.request_context.session
    form = types.ClientCapabilities(elicitation=types.ElicitationCapability())
    return session.check_client_capability(form) and session.can_send_request


def build_server(
    registry: Registry, manager: Manager | None = None, allow_management: bool = True
) -> MCPServer:
    mcp = MCPServer("ipcam-mcp", instructions=INSTRUCTIONS)

    def _scene_meta(camera: str) -> tuple[Path, store.CameraMeta]:
        """The camera's folder and camera.json (its scene periods)."""
        folder = registry.folder(camera)  # writes a missing or unreadable camera.json
        meta = store.read_meta(folder)
        if meta is None:
            raise DeviceError(f"{camera}: cannot read {folder / store.META}")
        return folder, meta

    async def with_camera(camera: str, capability: str, op: Callable[[Any], Awaitable[Any]]):
        async def run():
            registry.get(camera, capability)  # fail fast for unknown cameras
            async with registry.lock(camera):
                # Again under the lock: the camera may have been removed or replaced
                # while this call waited (delete/remove/rename_camera, reload).
                adapter = registry.get(camera, capability)
                return await op(adapter)

        return await _guarded(run)

    if manager is not None and allow_management:
        empty_hint = "No cameras configured yet. Use add_camera to add one."
    else:
        empty_hint = "No cameras configured. Add one to cameras.toml, then restart or reload."

    @mcp.tool()
    async def list_cameras(include_removed: bool = False) -> str:
        """List configured cameras with their ids (names), uids, types and capabilities
        (camera, ptz). include_removed=true also lists removed cameras whose files are still
        on disk ("removed": true), e.g. to clean them up with delete_camera."""
        cameras = registry.list()
        if include_removed:
            configured = {c["uid"] for c in cameras}
            cameras += [
                {
                    "id": c.name,
                    "uid": c.uid,
                    "scene": c.scene,
                    "removed": True,
                    "created": c.created.date().isoformat(),
                    "snapshots": c.snapshots,
                    "recordings": c.recordings,
                    "baselines": c.baselines,
                }
                for c in store.removed_cameras(registry.media_dir, configured)
            ]
        return json.dumps(cameras) if cameras else empty_hint

    def resolve_source(from_path: str) -> Path:
        media = registry.media_dir.resolve()
        try:
            path = Path(from_path).resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("from_path does not exist.") from None
        if not path.is_relative_to(media) or path.suffix.lower() != ".jpg" or not path.is_file():
            raise ValueError("from_path must be a .jpg snapshot inside the media directory.")
        return path

    @mcp.tool()
    async def camera_snapshot(
        camera: str,
        quality: Quality = "high",
        crop: list[float] | None = None,
        region: Region | None = None,
        from_path: str | None = None,
    ) -> list:
        """Take a still picture from a camera. Returns the image (downscaled to at most 1280 px
        wide) and the path of the full-size JPEG saved on the local machine.

        quality: "high" = main stream (1080p); "low" = sub stream (smaller, quicker).
        crop: [x, y, w, h] as fractions of the frame (0-1) to see a detail at full resolution.
        region: a named ninth of the frame instead of crop (not both).
        from_path: reuse an earlier full-size snapshot (e.g. the one just viewed) instead of
        taking a new one; combine with crop/region to zoom into it. The camera is not contacted.
        """

        async def run():
            box = crop_box(crop, region)
            registry.get(camera, "camera")  # fail fast for unknown cameras
            async with registry.lock(camera):
                # Again under the lock: the camera may have been removed or replaced
                # while this call waited (delete/remove/rename_camera, reload).
                adapter = registry.get(camera, "camera")
                if from_path is not None:
                    path = resolve_source(from_path)
                    label = "Source"
                else:
                    path = await adapter.snapshot(quality)
                    label = "Saved full-size snapshot"
                if box is None and from_path is None:
                    preview = await make_preview(path)
                    return [Image(data=preview, format="jpeg"), f"{label}: {path}"]
                data, (w, h) = crop_jpeg(path, box)
                text = f"{label}: {path}"
                if box is not None:
                    x, y, bw, bh = box
                    text += f"\ncropped to [{x:.3f}, {y:.3f}, {bw:.3f}, {bh:.3f}] ({w}\u00d7{h} px)"
                return [Image(data=data, format="jpeg"), text]

        return await _guarded(run)

    @mcp.tool()
    async def camera_save_baseline(camera: str, name: str, quality: Quality = "high") -> list:
        """Take a snapshot and keep it as a named reference for camera_compare (e.g.
        'door-closed')."""

        async def run():
            registry.get(camera, "camera")  # fail fast for unknown cameras
            async with registry.lock(camera):
                # Again under the lock: the camera may have been removed or replaced
                # while this call waited (delete/remove/rename_camera, reload).
                adapter = registry.get(camera, "camera")
                baselines.check_name(name)
                path = await adapter.snapshot(quality)
                pos = await _position(adapter) if isinstance(adapter, PTZ) else None
                folder, meta = _scene_meta(camera)
                baselines.save_baseline(folder, name, path, quality, pos, meta.current_scene)
                data, _ = crop_jpeg(path, None)
                return [
                    Image(data=data, format="jpeg"),
                    f"saved baseline '{name}'\nSaved full-size snapshot: {path}",
                ]

        return await _guarded(run)

    @mcp.tool()
    async def camera_compare(
        camera: str, baseline: str | None = None, quality: Quality = "high"
    ) -> list:
        """Take a new snapshot and report what changed compared with the previous snapshot of
        this camera, or with a named baseline. Use the reported cells as `region` in
        camera_snapshot to look closer."""

        async def run():
            registry.get(camera, "camera")  # fail fast for unknown cameras
            async with registry.lock(camera):
                # Again under the lock: the camera may have been removed or replaced
                # while this call waited (delete/remove/rename_camera, reload).
                adapter = registry.get(camera, "camera")
                folder, meta = _scene_meta(camera)
                base = None
                if baseline is not None:
                    base = scenes.load_scene_baseline(folder, meta, meta.current_scene, baseline)
                    ref_path = base.path
                    label = f"baseline '{baseline}'"
                    age = datetime.now().astimezone() - base.taken
                    age_s = age.total_seconds()
                else:
                    ref_path = scenes.latest_in_scene(folder, meta, meta.current_scene)
                    if ref_path is None:
                        raise ValueError(
                            f"no previous snapshot of '{camera}' to compare with; "
                            "take a snapshot first (camera_snapshot or camera_save_baseline)."
                        )
                    label = f"previous snapshot {ref_path.name}"
                    age_s = time.time() - ref_path.stat().st_mtime
                # Read before capturing, so the comparison is against the previous frame.
                ref = ref_path.read_bytes()
                path = await adapter.snapshot(quality)
                moved = False
                if base is not None and base.position is not None and isinstance(adapter, PTZ):
                    now = await _position(adapter)
                    if now is not None:
                        moved = (
                            abs(now[0] - base.position[0]) > MOVED_TOLERANCE
                            or abs(now[1] - base.position[1]) > MOVED_TOLERANCE
                        )
                res = compare(ref, path.read_bytes())
                text = _compare_text(res, label, _age(age_s), path, moved)
                return [Image(data=res.annotated, format="jpeg"), text]

        return await _guarded(run)

    @mcp.tool()
    async def camera_record(
        camera: str,
        seconds: Annotated[int, Field(ge=1, le=60)] = 10,
        quality: Quality = "high",
    ) -> str:
        """Record a video clip (1-60 s) from a camera to an mp4 file on the local machine.
        Returns the file path and size; the video itself is not returned, so take a snapshot
        if you need to see the scene.

        quality: "high" = main stream (1080p); "low" = sub stream (smaller file).
        """

        async def op(cam):
            path = await cam.record(seconds, quality)
            return f"Saved {seconds}s clip: {path} ({path.stat().st_size} bytes)"

        return await with_camera(camera, "camera", op)

    @mcp.tool()
    async def ptz_move(camera: str, direction: Direction, amount: Amount = "small") -> str:
        """Pan or tilt a camera. Directions are as seen in the camera image ("left" turns the
        view left). amount: "small" is a slight nudge, "medium" a noticeable turn, "large" a big
        sweep. Waits until the camera has stopped, then returns its pan/tilt position if the
        camera reports one. Take a snapshot afterwards to see the new view.
        """

        async def op(cam):
            pos = await cam.move(direction, amount)
            done = f"Moved {direction} ({amount})."
            return done if pos is None else f"{done} Position: pan={pos[0]}, tilt={pos[1]}"

        return await with_camera(camera, "ptz", op)

    @mcp.tool()
    async def ptz_list_presets(camera: str) -> str:
        """List the saved camera positions (presets) of a pan/tilt camera."""

        async def op(cam):
            presets = await cam.list_presets()
            return json.dumps([{"name": p.name, "token": p.token} for p in presets])

        return await with_camera(camera, "ptz", op)

    @mcp.tool()
    async def ptz_goto_preset(camera: str, preset: str) -> str:
        """Move a pan/tilt camera to a saved preset, given by name or token."""

        async def op(cam):
            await cam.goto_preset(preset)
            return f"Moved to preset '{preset}'."

        return await with_camera(camera, "ptz", op)

    @mcp.tool()
    async def ptz_save_preset(camera: str, name: str, overwrite: bool = False) -> str:
        """Save the camera's current position as a preset. Refuses an existing name unless
        overwrite=true."""

        async def op(cam):
            p = await cam.save_preset(name, overwrite)
            return f"saved preset '{p.name}' (token {p.token})"

        return await with_camera(camera, "ptz", op)

    @mcp.tool()
    async def ptz_remove_preset(camera: str, preset: str) -> str:
        """Delete a saved preset by name or token. This cannot be undone."""

        async def op(cam):
            p = await cam.remove_preset(preset)
            return f"removed preset '{p.name}'"

        return await with_camera(camera, "ptz", op)

    if manager is not None:
        _add_management_tools(mcp, manager, allow_management)
    return mcp


def _add_management_tools(mcp: MCPServer, manager: Manager, allow_management: bool) -> None:
    @mcp.tool()
    async def reload_cameras() -> str:
        """Re-read cameras.toml after it was edited by hand and list the cameras, in the same
        format as list_cameras."""
        return await _guarded(lambda: _json_list(manager))

    if not allow_management:
        return

    @mcp.tool()
    async def add_camera(
        name: str,
        host: str,
        ctx: Context,
        credentials_via: Literal["form", "file"] = "form",
        onvif_port: int | None = None,
        scene: str | None = None,
    ) -> str:
        """Add an ONVIF/RTSP IP camera. Ask the user for the camera's IP address (from its app
        or router) and pick a short name such as "garage".

        Credentials are never passed as arguments: by default the user is shown a login form;
        with credentials_via="file" a secrets file is created for the user to fill in, then
        call add_camera again with credentials_via="file". Use credentials_via="file" when the
        user is on Remote Control or a phone and the form does not appear.

        onvif_port: only if the camera uses a non-standard ONVIF port (80 and 2020 are tried).
        scene: the place it watches, if not its own name (several cameras may share a scene).
        """

        async def prompt(message: str) -> Credentials | None:
            try:
                result = await ctx.elicit(message, _LoginForm)
            except MCPError:
                # The client's error text is not passed on: it may echo what was typed.
                raise ValueError(FORM_FAILED) from None
            if isinstance(result, AcceptedElicitation):
                return Credentials(result.data.username, result.data.password)
            return None

        async def run() -> str:
            if credentials_via == "file":
                return await manager.add(name, host, None, onvif_port, scene)
            if not _can_show_form(ctx):
                return NO_FORM_PREFIX + await manager.add(name, host, None, onvif_port, scene)
            return await manager.add(name, host, prompt, onvif_port, scene)

        return await _guarded(run)

    @mcp.tool()
    async def rename_camera(old: str, new: str) -> str:
        """Change a camera's name. Its baselines, snapshots and recordings stay with it."""
        return await _guarded(lambda: manager.rename(old, new))

    @mcp.tool()
    async def set_scene(camera: str, scene: str) -> str:
        """Move a camera to another scene (the place it watches). Its earlier snapshots stay
        with the earlier scene; camera_compare only compares within the current scene."""
        return await _guarded(lambda: manager.set_scene(camera, scene))

    @mcp.tool()
    async def remove_camera(name: str) -> str:
        """Remove a camera from the configuration. Never deletes snapshots or recordings
        (delete_camera does, after the user approves)."""
        return await _guarded(lambda: manager.remove(name))

    @mcp.tool()
    async def delete_camera(name: str, ctx: Context) -> str:
        """Delete a camera and all its snapshots, recordings and baselines, or the leftover
        files of a removed camera with that name. The user approves in a form that lists what
        will be deleted; you cannot approve it. Without form support nothing is deleted and the
        result says how to do it by hand."""

        async def confirm(message: str, targets: list[str]) -> int | Literal["all"] | None:
            form = _delete_form(targets)
            try:
                result = await ctx.elicit(message, form)
            except MCPError:
                raise ValueError("the approval form failed; nothing was deleted") from None
            if not isinstance(result, AcceptedElicitation):
                return None
            if len(targets) == 1:
                return 0 if result.data.confirm else None
            choice = result.data.target
            return "all" if choice == DELETE_ALL else targets.index(choice)

        async def run() -> str:
            return await manager.delete(name, confirm if _can_show_form(ctx) else None)

        return await _guarded(run)


async def _json_list(manager: Manager) -> str:
    return json.dumps(await manager.reload())


# Exactly the hosts for which the MCP SDK enables DNS-rebinding protection.
_PROTECTED_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _is_loopback(host: str) -> bool:
    return host in _PROTECTED_LOCAL_HOSTS


def _migrate(cfg: AppConfig) -> None:
    """Move media from the flat pre-uid layout into per-camera folders (once)."""
    report = migrate_flat_layout(cfg.media_dir, {n: d.uid for n, d in cfg.devices.items()})
    if report.moved:
        log.info(
            "migrated media of %d camera(s) to per-camera folders; %d removed camera(s) kept",
            len(report.moved),
            len(report.removed_cameras),
        )
    for path in report.skipped:
        log.warning("not migrated, a file with that name already exists: %s", path)
    if report.error:
        log.warning("media migration stopped: %s; retried at next start", report.error)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ipcam-mcp", description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="http only (no auth: keep local)")
    parser.add_argument("--port", type=int, default=8765, help="http only")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="http only: allow a non-loopback --host (no auth, no DNS-rebinding protection)",
    )
    args = parser.parse_args(argv)
    if args.transport == "http" and not _is_loopback(args.host) and not args.allow_remote:
        parser.error(
            f"--host {args.host} is not a loopback address. The HTTP transport has no"
            " authentication; anyone who can reach it controls your cameras. Pass"
            " --allow-remote if you really want this."
        )

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
    path = config_path()
    if not path.exists():
        log.info("no config at %s yet; starting with no cameras (add_camera creates it)", path)
    try:
        cfg = prepare_config(path)
        registry = Registry(cfg)
    except ConfigError as e:
        print(f"ipcam-mcp: {e}", file=sys.stderr)
        sys.exit(2)
    _migrate(cfg)

    manager = Manager(path, registry)
    server = build_server(registry, manager, allow_management=cfg.allow_management)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        if not _is_loopback(args.host):
            log.warning("HTTP transport on %s: unauthenticated and reachable remotely", args.host)
        server.run("streamable-http", host=args.host, port=args.port)
