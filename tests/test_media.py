import asyncio
import shutil
import subprocess
import time
from datetime import datetime

import pytest

from ipcam_mcp import media
from ipcam_mcp.errors import MediaError
from ipcam_mcp.media import (
    make_preview,
    mask_url,
    media_path,
    record_cmd,
    run_ffmpeg,
    snapshot_cmd,
)

URL = "rtsp://u:p%40s@1.2.3.4:554/stream1"
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _in_order(seq: list[str], *items: str) -> bool:
    idx = [seq.index(i) for i in items]
    return idx == sorted(idx)


def test_mask_url():
    assert mask_url(f"x {URL} y") == "x rtsp://***@1.2.3.4:554/stream1 y"
    assert mask_url("no url here") == "no url here"


def test_media_path(tmp_path):
    p = media_path(tmp_path / "m", "livingroom", "jpg", datetime(2026, 11, 2, 17, 5, 9))
    assert p == tmp_path / "m" / "livingroom_20261102-170509.jpg"
    assert (tmp_path / "m").is_dir()


def test_snapshot_cmd(tmp_path):
    out = tmp_path / "a.jpg"
    cmd = snapshot_cmd(URL, out)
    assert cmd[0] == "ffmpeg"
    assert _in_order(cmd, "-rtsp_transport", "-timeout", "-i", "-frames:v")
    assert cmd[cmd.index("-rtsp_transport") + 1] == "tcp"
    assert cmd[cmd.index("-timeout") + 1] == "10000000"
    assert cmd[cmd.index("-i") + 1] == URL
    assert cmd[cmd.index("-frames:v") + 1] == "1"
    assert cmd[-2:] == ["-y", str(out)]


def test_record_cmd(tmp_path):
    out = tmp_path / "a.mp4"
    cmd = record_cmd(URL, out, 7)
    assert cmd[cmd.index("-t") + 1] == "7"
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert "-c" not in cmd
    assert cmd[-2:] == ["-y", str(out)]


async def test_run_ffmpeg_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda _: None)
    with pytest.raises(MediaError, match="ffmpeg not installed"):
        await run_ffmpeg(["ffmpeg", "-version"], tmp_path / "o", 5, "cam")


async def test_run_ffmpeg_failure_is_masked(tmp_path):
    args = ["sh", "-c", "echo 'bad rtsp://u:secret@h/s secret' >&2; exit 1"]
    with pytest.raises(MediaError) as e:
        await run_ffmpeg(args, tmp_path / "o", 5, "cam (h:554)", secrets=["secret"])
    msg = str(e.value)
    assert "cam (h:554)" in msg
    assert "secret" not in msg


async def test_run_ffmpeg_empty_output_raises(tmp_path):
    with pytest.raises(MediaError, match="no data"):
        await run_ffmpeg(["true"], tmp_path / "o", 5, "cam")
    out = tmp_path / "empty"
    with pytest.raises(MediaError, match="no data"):
        await run_ffmpeg(["sh", "-c", f": > {out}"], out, 5, "cam")
    assert not out.exists()


async def test_run_ffmpeg_timeout_kills_and_cleans(tmp_path):
    out = tmp_path / "o"
    start = time.monotonic()
    with pytest.raises(MediaError, match="timed out"):
        await run_ffmpeg(["sh", "-c", f"echo x > {out}; exec sleep 5"], out, 0.5, "cam")
    assert time.monotonic() - start < 2
    assert not out.exists()


async def test_run_ffmpeg_success(tmp_path):
    out = tmp_path / "o"
    assert await run_ffmpeg(["sh", "-c", f"echo data > {out}"], out, 5, "cam") == out


@needs_ffmpeg
async def test_make_preview(tmp_path):
    src = tmp_path / "big.jpg"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=1920x1080",
            "-frames:v",
            "1",
            "-y",
            str(src),
        ],
        check=True,
    )
    data = await make_preview(src)
    assert data.startswith(b"\xff\xd8")
    preview = tmp_path / "p.jpg"
    preview.write_bytes(data)
    width = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width", "-of", "csv=p=0", str(preview)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert width == "1280"


async def test_run_ffmpeg_long_stderr_never_leaks_partial_password(tmp_path):
    # Truncating stderr before masking could cut the URL mid-credential (review I-1).
    pw = "Sup3rSecretPw"
    url_line = f"Error opening input file rtsp://admin:{pw}@10.0.0.5:554/stream1."
    for n in range(240, 300):
        args = ["sh", "-c", f"printf '%s' '{url_line}{'x' * n}' >&2; exit 1"]
        with pytest.raises(MediaError) as e:
            await run_ffmpeg(args, tmp_path / "o", 5, "cam", secrets=[pw])
        msg = str(e.value)
        assert not any(pw[i : i + 4] in msg for i in range(len(pw) - 3)), (n, msg)
        assert "admin" not in msg, (n, msg)


def test_media_path_same_second_gets_a_new_name(tmp_path):
    """Two captures in one second must not share a file: a failed second capture
    would otherwise delete the first, successful one."""
    now = datetime(2026, 11, 2, 17, 5, 9)
    first = media_path(tmp_path, "cam", "jpg", now)
    first.write_bytes(b"ok")
    second = media_path(tmp_path, "cam", "jpg", now)
    third = media_path(tmp_path, "cam", "jpg", now)
    assert len({first, second, third}) == 3
    assert second.name == "cam_20261102-170509-2.jpg"
    assert third.name == "cam_20261102-170509-3.jpg"
    assert first.read_bytes() == b"ok"


async def test_failed_capture_keeps_earlier_same_second_snapshot(tmp_path, monkeypatch):
    now = datetime(2026, 11, 2, 17, 5, 9)
    first = media_path(tmp_path, "cam", "jpg", now)
    first.write_bytes(b"ok")
    out = media_path(tmp_path, "cam", "jpg", now)
    with pytest.raises(MediaError):
        await media.run_ffmpeg(["false"], out, 5, "snapshot")
    assert first.read_bytes() == b"ok" and not out.exists()


class _ExitedProcess:
    """ffmpeg that exits just as we time out / get cancelled: kill() finds no process."""

    returncode = 0

    async def communicate(self):
        await asyncio.sleep(5)

    def kill(self):
        raise ProcessLookupError

    async def wait(self):
        return 0


async def _fake_exec(*args, **kwargs):
    return _ExitedProcess()


async def test_run_ffmpeg_timeout_when_process_already_exited(tmp_path, monkeypatch):
    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", _fake_exec)
    with pytest.raises(MediaError, match="timed out"):
        await run_ffmpeg(["sh"], tmp_path / "o", 0.05, "cam")


async def test_run_ffmpeg_cancel_when_process_already_exited(tmp_path, monkeypatch):
    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", _fake_exec)
    task = asyncio.create_task(run_ffmpeg(["sh"], tmp_path / "o", 5, "cam"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
