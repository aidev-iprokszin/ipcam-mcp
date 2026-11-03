"""Generic ONVIF/RTSP IP camera: RTSP media via ffmpeg, optional PTZ via ONVIF."""

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp
import zeep.exceptions

from ipcam_mcp.capabilities import Amount, Direction, Preset, Quality
from ipcam_mcp.config import DeviceConfig
from ipcam_mcp.errors import CameraMcpError, ConfigError, DeviceError, scrub
from ipcam_mcp.media import mask_url, media_path, record_cmd, run_ffmpeg, snapshot_cmd

log = logging.getLogger(__name__)

PROBE_PORTS = (80, 2020)
DEFAULT_RTSP_PORT = 554
SNAPSHOT_TIMEOUT = 20
RECORD_TIMEOUT_EXTRA = 15
MAX_RECORD_SECONDS = 60
ONVIF_TIMEOUT = 15.0

# Standard ONVIF axes; cameras with an inverted pan axis (e.g. Tapo TC70) set invert_pan.
VECTORS: dict[str, tuple[int, int]] = {
    "left": (-1, 0),
    "right": (1, 0),
    "up": (0, 1),
    "down": (0, -1),
}
RELATIVE_STEPS: dict[str, float] = {"small": 0.05, "medium": 0.15, "large": 0.30}
CONTINUOUS_SPEED = 0.5
CONTINUOUS_DURATIONS: dict[str, float] = {"small": 0.3, "medium": 0.8, "large": 1.5}
# The TC70 reports MoveStatus UNKNOWN and drops new moves while still moving, so after a
# move we poll the position until two consecutive reads match.
SETTLE_POLL = 0.3
STOP_TIMEOUT = 5.0
SETTLE_MAX_POLLS = 20
# Settling has its own budget: a move that worked must not be reported as a timeout.
SETTLE_TIMEOUT = 10.0


def _default_onvif_factory(host: str, port: int, user: str, password: str) -> Any:
    from onvif import ONVIFCamera

    return ONVIFCamera(host, port, user, password)


@dataclass(frozen=True)
class DeviceInfo:
    manufacturer: str
    model: str
    ptz: bool = False
    onvif_port: int = 80


# (manufacturer substring, model substring or None, settings), matched case-insensitively.
KNOWN_MODELS: list[tuple[str, str | None, dict[str, Any]]] = [
    ("tp-link", None, {"invert_pan": True}),
]


def known_settings(manufacturer: str, model: str) -> dict[str, Any]:
    """Settings known for a device (e.g. an inverted pan axis); empty if unknown."""
    result: dict[str, Any] = {}
    for maker, mdl, settings in KNOWN_MODELS:
        if maker.lower() in manufacturer.lower() and (mdl is None or mdl.lower() in model.lower()):
            result.update(settings)
    return result


async def _probe_port(
    host: str, port: int, user: str, password: str, factory: Callable[..., Any]
) -> DeviceInfo:
    cam = factory(host, port, user, password)
    try:
        await cam.update_xaddrs()
        mgmt = await cam.create_devicemgmt_service()
        info = await mgmt.GetDeviceInformation()
        try:
            caps = await mgmt.GetCapabilities({"Category": "All"})
            ptz = getattr(caps, "PTZ", None) is not None
        except Exception:  # noqa: BLE001 — PTZ detection is best effort
            ptz = False
        return DeviceInfo(str(info.Manufacturer), str(info.Model), ptz, port)
    finally:
        if hasattr(cam, "close"):
            with contextlib.suppress(Exception):
                await cam.close()


async def probe(
    host: str,
    user: str,
    password: str,
    port: int | None = None,
    onvif_factory: Callable[..., Any] | None = None,
) -> DeviceInfo:
    """Verify ONVIF credentials and detect the port (80, then 2020) and PTZ support."""
    factory = onvif_factory or _default_onvif_factory
    ports = (port,) if port is not None else PROBE_PORTS
    reasons: list[str] = []
    for p in ports:
        try:
            return await asyncio.wait_for(
                _probe_port(host, p, user, password, factory), ONVIF_TIMEOUT
            )
        except TimeoutError:
            reasons.append(f"port {p}: timed out after {ONVIF_TIMEOUT:.0f}s (unreachable?)")
        except (OSError, aiohttp.ClientError, zeep.exceptions.TransportError) as e:
            msg = scrub(f"{type(e).__name__}: {e}", [password, user])
            reasons.append(f"port {p}: {msg}")
        except (zeep.exceptions.XMLSyntaxError, zeep.exceptions.XMLParseError):
            # Something answered (often the camera's web UI), but not with SOAP: not ONVIF.
            reasons.append(f"port {p}: answered, but not as an ONVIF service")
        except Exception as e:  # noqa: BLE001 — a SOAP fault or other error: stop searching
            msg = scrub(f"{type(e).__name__}: {e}", [password, user])
            raise DeviceError(f"{host}:{p}: ONVIF login failed: {msg}") from None
    if len(ports) == 1:
        raise DeviceError(f"{host}:{ports[0]}: ONVIF login failed ({reasons[0]})")
    tried = ", ".join(str(p) for p in ports)
    raise DeviceError(
        f"{host}: no ONVIF service found on ports {tried} ({'; '.join(reasons)}); "
        "pass onvif_port if the camera uses another port"
    )


TYPE_DEFAULTS: dict[str, dict[str, Any]] = {
    "onvif": {"onvif_port": 80, "invert_pan": False, "ptz": False},
    "tapo_onvif": {
        "onvif_port": 2020,
        "invert_pan": True,
        "ptz": True,
        "stream_high": "/stream1",
        "stream_low": "/stream2",
    },
}


def _check_settings(cfg: DeviceConfig, s: dict[str, Any]) -> None:
    def bad(key: str, why: str) -> ConfigError:
        return ConfigError(f"camera '{cfg.id}': setting '{key}' {why}")

    for key in ("onvif_port", "rtsp_port"):
        v = s.get(key)
        if v is None:
            continue
        if type(v) is not int or not 1 <= v <= 65535:
            raise bad(key, "must be an integer between 1 and 65535")
    for key in ("invert_pan", "ptz"):
        if key in s and type(s[key]) is not bool:
            raise bad(key, "must be true or false")
    for key in ("stream_high", "stream_low"):
        v = s.get(key)
        if v is not None and not (isinstance(v, str) and v.startswith("/")):
            raise bad(key, "must be a string starting with '/' (an RTSP path)")


def make_camera(
    cfg: DeviceConfig,
    media_dir: Path,
    onvif_factory: Callable[..., Any] | None = None,
) -> "OnvifCamera":
    """Apply per-type defaults, validate settings, build the adapter (PTZ only if ptz is on)."""
    settings = {**TYPE_DEFAULTS.get(cfg.type, TYPE_DEFAULTS["onvif"]), **cfg.settings}
    _check_settings(cfg, settings)
    resolved = dataclasses.replace(cfg, settings=settings)
    cls = OnvifPtzCamera if settings.get("ptz") else OnvifCamera
    return cls(resolved, media_dir, onvif_factory)


class OnvifCamera:
    """RTSP media via ffmpeg; stream paths from settings or looked up over ONVIF."""

    def __init__(
        self,
        cfg: DeviceConfig,
        media_dir: Path,
        onvif_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._cfg = cfg
        self._media_dir = media_dir
        self._rtsp_port = int(cfg.settings.get("rtsp_port", DEFAULT_RTSP_PORT))
        self._onvif_port = int(cfg.settings.get("onvif_port", 80))
        self._invert_pan = bool(cfg.settings.get("invert_pan", False))
        self._onvif_factory = onvif_factory or _default_onvif_factory
        self._connect_lock = asyncio.Lock()
        self._cam: Any = None
        self._profiles: list[str] = []
        self._ptz: Any = None
        self._use_continuous = False
        self._streams: dict[str, tuple[int, str]] = {}

    # --- media -----------------------------------------------------------------

    async def rtsp_url(self, quality: Quality) -> str:
        c = self._cfg
        port, path = await self._stream(quality)
        creds = f"{quote(c.user, safe='')}:{quote(c.password, safe='')}"
        host = f"[{c.host}]" if ":" in c.host else c.host  # IPv6 literal
        return f"rtsp://{creds}@{host}:{port}{path}"

    async def _stream(self, quality: Quality) -> tuple[int, str]:
        """Return (port, path+query) for a quality: configured path, else ONVIF lookup."""
        key = f"stream_{'high' if quality == 'high' else 'low'}"
        configured = self._cfg.settings.get(key)
        if configured:
            return self._rtsp_port, configured
        if quality not in self._streams:
            try:
                self._streams[quality] = await self._onvif(lambda: self._lookup_stream(quality))
            except DeviceError as e:
                raise DeviceError(
                    f"{e}; set '{key}' in the camera settings to give the RTSP path"
                ) from None
        return self._streams[quality]

    async def _lookup_stream(self, quality: Quality) -> tuple[int, str]:
        c = self._cfg
        try:
            _, profiles = await self._client()
            token = profiles[0] if quality == "high" or len(profiles) < 2 else profiles[1]
            media = await self._cam.create_media_service()
            reply = await media.GetStreamUri(
                {
                    "ProfileToken": token,
                    "StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
                }
            )
            parts = urlsplit(str(reply.Uri))
            if parts.scheme != "rtsp" or not parts.hostname:
                raise ValueError("camera returned a non-RTSP stream URI")
            port = self._rtsp_port if "rtsp_port" in c.settings else parts.port or self._rtsp_port
        except CameraMcpError:
            raise
        except Exception as e:  # noqa: BLE001 — zeep/aiohttp raise many types
            msg = scrub(mask_url(f"{type(e).__name__}: {e}"), [c.password, c.user])
            raise DeviceError(
                f"{c.id}: could not get the {quality} stream URI over ONVIF ({msg})"
            ) from None
        path = parts.path or "/"
        return port, f"{path}?{parts.query}" if parts.query else path

    @property
    def _context(self) -> str:
        return f"{self._cfg.id} ({self._cfg.host}:{self._rtsp_port})"

    async def snapshot(self, quality: Quality) -> Path:
        url = await self.rtsp_url(quality)  # before reserving: a failed lookup leaves no file
        out = media_path(self._media_dir, self._cfg.id, "jpg")
        cmd = snapshot_cmd(url, out)
        return await run_ffmpeg(cmd, out, SNAPSHOT_TIMEOUT, self._context, [self._cfg.password])

    async def record(self, seconds: int, quality: Quality) -> Path:
        if not 1 <= seconds <= MAX_RECORD_SECONDS:
            raise ValueError(f"seconds must be between 1 and {MAX_RECORD_SECONDS}, got {seconds}")
        url = await self.rtsp_url(quality)
        out = media_path(self._media_dir, self._cfg.id, "mp4")
        cmd = record_cmd(url, out, seconds)
        timeout = seconds + RECORD_TIMEOUT_EXTRA
        return await run_ffmpeg(cmd, out, timeout, self._context, [self._cfg.password])

    async def aclose(self) -> None:
        """Close the cached ONVIF client (its HTTP session); the next call reconnects."""
        async with self._connect_lock:
            cam, self._cam = self._cam, None
            self._reset_services()
        if cam is not None and hasattr(cam, "close"):
            with contextlib.suppress(Exception):
                await cam.close()

    def _reset_services(self) -> None:
        pass

    # --- ONVIF plumbing --------------------------------------------------------

    async def _client(self) -> tuple[Any, list[str]]:
        async with self._connect_lock:
            if self._cam is None:
                c = self._cfg
                cam = self._onvif_factory(c.host, self._onvif_port, c.user, c.password)
                await cam.update_xaddrs()
                media = await cam.create_media_service()
                profiles = await media.GetProfiles()
                if not profiles:
                    raise DeviceError(f"{c.id}: camera reports no media profiles")
                self._profiles = [str(p.token) for p in profiles]
                self._cam = cam
            return self._cam, self._profiles

    async def _onvif[T](self, op: Callable[[], Awaitable[T]]) -> T:
        c = self._cfg
        try:
            return await asyncio.wait_for(op(), ONVIF_TIMEOUT)
        except CameraMcpError:
            raise
        except TimeoutError:
            raise DeviceError(
                f"{c.id}: ONVIF timed out after {ONVIF_TIMEOUT:.0f}s "
                f"({c.host}:{self._onvif_port} unreachable?)"
            ) from None
        except Exception as e:  # noqa: BLE001 — zeep/aiohttp raise many types
            msg = scrub(f"{type(e).__name__}: {e}", [c.password])
            raise DeviceError(f"{c.id}: ONVIF error: {msg}") from None


class OnvifPtzCamera(OnvifCamera):
    """An ONVIF camera with pan/tilt (and presets)."""

    # --- PTZ -------------------------------------------------------------------

    async def move(self, direction: Direction, amount: Amount) -> tuple[float, float] | None:
        vx, vy = VECTORS[direction]
        if self._invert_pan:
            vx = -vx

        async def op() -> tuple[Any, str]:
            ptz, profile = await self._connect()
            if not self._use_continuous:
                step = RELATIVE_STEPS[amount]
                try:
                    await ptz.RelativeMove(
                        {
                            "ProfileToken": profile,
                            "Translation": {"PanTilt": {"x": vx * step, "y": vy * step}},
                        }
                    )
                except zeep.exceptions.Fault as e:
                    # A SOAP fault means the camera rejects RelativeMove; transport errors
                    # propagate instead so a network blip doesn't switch modes for good.
                    log.info("%s: RelativeMove rejected (%s); using ContinuousMove",
                             self._cfg.id, type(e).__name__)  # fmt: skip
                    self._use_continuous = True
            if self._use_continuous:
                speed = CONTINUOUS_SPEED
                await ptz.ContinuousMove(
                    {
                        "ProfileToken": profile,
                        "Velocity": {"PanTilt": {"x": vx * speed, "y": vy * speed}},
                    }
                )
                try:
                    await asyncio.sleep(CONTINUOUS_DURATIONS[amount])
                finally:
                    # Always stop, even if cancelled, or the motor runs to its end stop.
                    await self._stop(ptz, profile)
            return ptz, profile

        ptz, profile = await self._onvif(op)
        # The move is done; waiting for it to settle has its own budget and never fails it.
        try:
            return await asyncio.wait_for(self._position(ptz, profile), SETTLE_TIMEOUT)
        except TimeoutError:
            log.info("%s: position did not settle within %.0fs", self._cfg.id, SETTLE_TIMEOUT)
            return None

    async def list_presets(self) -> list[Preset]:
        async def op() -> list[Preset]:
            ptz, profile = await self._connect()
            presets = await ptz.GetPresets({"ProfileToken": profile}) or []
            return [Preset(name=str(p.Name or p.token), token=str(p.token)) for p in presets]

        return await self._onvif(op)

    async def _find_preset(self, preset: str) -> Preset:
        presets = await self.list_presets()
        match = next((p for p in presets if preset in (p.name, p.token)), None)
        if match is None:
            available = ", ".join(p.name for p in presets) or "(none)"
            raise DeviceError(f"preset '{preset}' not found; available: {available}")
        return match

    async def goto_preset(self, preset: str) -> None:
        match = await self._find_preset(preset)

        async def op() -> None:
            ptz, profile = await self._connect()
            await ptz.GotoPreset({"ProfileToken": profile, "PresetToken": match.token})

        await self._onvif(op)

    async def save_preset(self, name: str, overwrite: bool = False) -> Preset:
        if not 1 <= len(name) <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError("preset name must be 1-32 characters with no control characters")
        existing = next((p for p in await self.list_presets() if p.name == name), None)
        if existing and not overwrite:
            raise DeviceError(f"preset '{name}' already exists; pass overwrite=true to replace it")

        async def op() -> Preset:
            ptz, profile = await self._connect()
            req = {"ProfileToken": profile, "PresetName": name}
            if existing:
                req["PresetToken"] = existing.token
            token = await ptz.SetPreset(req)
            return Preset(name, str(token))

        return await self._onvif(op)

    async def remove_preset(self, preset: str) -> Preset:
        match = await self._find_preset(preset)

        async def op() -> Preset:
            ptz, profile = await self._connect()
            await ptz.RemovePreset({"ProfileToken": profile, "PresetToken": match.token})
            return match

        return await self._onvif(op)

    async def position(self) -> tuple[float, float] | None:
        async def op() -> tuple[float, float] | None:
            ptz, profile = await self._connect()
            try:
                pan_tilt = (await ptz.GetStatus({"ProfileToken": profile})).Position.PanTilt
                return (float(pan_tilt.x), float(pan_tilt.y))
            except Exception:  # noqa: BLE001 — position is optional
                return None

        return await self._onvif(op)

    def _reset_services(self) -> None:
        self._ptz = None

    async def _connect(self) -> tuple[Any, str]:
        cam, profiles = await self._client()
        async with self._connect_lock:
            if self._ptz is None:
                self._ptz = await cam.create_ptz_service()
        return self._ptz, profiles[0]

    async def _stop(self, ptz: Any, profile: str) -> None:
        stop = ptz.Stop({"ProfileToken": profile, "PanTilt": True, "Zoom": False})
        try:
            await asyncio.shield(asyncio.wait_for(stop, STOP_TIMEOUT))
        except Exception as e:  # noqa: BLE001 — best effort; report but don't mask the cause
            log.warning("%s: Stop failed (%s)", self._cfg.id, type(e).__name__)

    @staticmethod
    async def _position(ptz: Any, profile: str) -> tuple[float, float] | None:
        """Wait until the camera stops moving; return its final position if it reports one."""
        last: tuple[float, float] | None = None
        for _ in range(SETTLE_MAX_POLLS):
            try:
                status = await ptz.GetStatus({"ProfileToken": profile})
                pan_tilt = status.Position.PanTilt
                pos = (float(pan_tilt.x), float(pan_tilt.y))
            except Exception:  # noqa: BLE001 — position is optional
                return None
            if pos == last:
                return pos
            last = pos
            await asyncio.sleep(SETTLE_POLL)
        return last
