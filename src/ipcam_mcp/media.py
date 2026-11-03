"""Media files: ffmpeg commands (run safely, credentials masked) and the on-disk layout."""

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from ipcam_mcp.errors import MediaError, scrub

RTSP_SOCKET_TIMEOUT_US = "10000000"  # 10 s
PREVIEW_TIMEOUT = 15.0
_RTSP_CREDS = re.compile(r"rtsp://[^/\s@]+@")


def mask_url(text: str) -> str:
    """Hide the credentials part of any RTSP URL in ``text``."""
    return _RTSP_CREDS.sub("rtsp://***@", text)


def media_path(media_dir: Path, device_id: str, ext: str, now: datetime | None = None) -> Path:
    """Reserve a new ``<id>_YYYYmmdd-HHMMSS[-N].<ext>`` (created empty) that no other capture
    uses, so a failed capture can never overwrite or delete an earlier one."""
    media_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    n = 1
    while True:
        path = media_dir / f"{device_id}_{stamp}{'' if n == 1 else f'-{n}'}.{ext}"
        try:
            path.touch(exist_ok=False)
            return path
        except FileExistsError:
            n += 1


def baselines_dir(media_dir: Path, cam_id: str) -> Path:
    return media_dir / "baselines" / cam_id


def capture_files(media_dir: Path, cam_id: str) -> list[Path]:
    """The camera's snapshots and recordings, as named by ``media_path`` (exact id match)."""
    pattern = re.compile(rf"^{re.escape(cam_id)}_\d{{8}}-\d{{6}}(?:-\d+)?\.(?:jpg|mp4)$")
    try:
        return sorted(p for p in media_dir.iterdir() if pattern.match(p.name) and p.is_file())
    except OSError:
        return []


def rename_camera_media(media_dir: Path, old: str, new: str) -> Callable[[], None]:
    """Move ``old``'s baselines folder and captures to ``new``; return a function that
    moves them back. Refuses if ``new`` already has media (left by an earlier camera with
    that id), which would otherwise be mixed in as comparison references."""
    old_dir, new_dir = baselines_dir(media_dir, old), baselines_dir(media_dir, new)
    if new_dir.exists() or capture_files(media_dir, new):
        raise ValueError(
            f"media for '{new}' already exist in {media_dir} (baselines/{new}/ or {new}_* "
            "files from an earlier camera); move or delete them first"
        )
    moves = [(p, p.with_name(new + p.name[len(old) :])) for p in capture_files(media_dir, old)]
    if old_dir.is_dir():
        moves.append((old_dir, new_dir))

    done: list[tuple[Path, Path]] = []

    def undo() -> None:
        for src, dst in reversed(done):
            os.rename(dst, src)
        done.clear()

    try:
        for src, dst in moves:
            os.rename(src, dst)
            done.append((src, dst))
    except OSError as exc:
        undo()
        raise MediaError(
            f"could not move media of '{old}' to '{new}': {exc.strerror or exc}"
        ) from exc
    return undo


def _rtsp_input(url: str) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-timeout", RTSP_SOCKET_TIMEOUT_US,
        "-i", url,
    ]  # fmt: skip


def snapshot_cmd(url: str, out: Path) -> list[str]:
    return [*_rtsp_input(url), "-frames:v", "1", "-q:v", "2", "-y", str(out)]


def record_cmd(url: str, out: Path, seconds: int) -> list[str]:
    # Some cameras send G.711 audio (pcm_alaw), which mp4 can't hold: copy video, transcode audio.
    return [
        *_rtsp_input(url),
        "-t", str(seconds),
        "-c:v", "copy", "-c:a", "aac",
        "-movflags", "+faststart",
        "-y", str(out),
    ]  # fmt: skip


async def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):  # it may have exited in the meantime
        proc.kill()
    await proc.wait()


async def run_ffmpeg(
    args: list[str],
    out: Path,
    timeout: float,
    context: str,
    secrets: Sequence[str] = (),
) -> Path:
    """Run ``args`` and return ``out`` once it exists and is non-empty.

    All error messages are prefixed with ``context`` and have credentials masked. On any
    failure (including cancellation) ``out`` is removed, so no empty reservation remains.
    """
    try:
        return await _run_ffmpeg(args, out, timeout, context, secrets)
    except BaseException:
        with contextlib.suppress(OSError):
            out.unlink(missing_ok=True)
        raise


async def _run_ffmpeg(
    args: list[str], out: Path, timeout: float, context: str, secrets: Sequence[str]
) -> Path:

    def fail(msg: str) -> MediaError:
        with contextlib.suppress(FileNotFoundError):
            out.unlink()
        return MediaError(scrub(mask_url(f"{context}: {msg}"), secrets))

    if shutil.which(args[0]) is None:
        if args[0] == "ffmpeg":
            raise MediaError("ffmpeg not installed (sudo apt install ffmpeg)")
        raise MediaError(f"{args[0]} not found")

    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await _kill(proc)
        raise fail(f"timed out after {timeout:.0f}s (camera unreachable?)") from None
    except asyncio.CancelledError:
        await _kill(proc)
        with contextlib.suppress(FileNotFoundError):
            out.unlink()
        raise

    if proc.returncode != 0:
        # Mask before truncating: a cut inside the URL would defeat the masking.
        safe = scrub(mask_url(stderr.decode(errors="replace").strip()), secrets)
        raise fail(f"ffmpeg failed: {safe[-300:]}")
    if not out.exists() or out.stat().st_size == 0:
        raise fail("no data received")
    return out


async def make_preview(src: Path, max_width: int = 1280) -> bytes:
    """Return ``src`` downscaled to at most ``max_width`` px wide, as JPEG bytes."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "preview.jpg"
        args = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-vf", f"scale='min({max_width},iw)':-2",
            "-q:v", "4", "-y", str(out),
        ]  # fmt: skip
        await run_ffmpeg(args, out, PREVIEW_TIMEOUT, "preview")
        return out.read_bytes()
