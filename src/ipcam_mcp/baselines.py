"""Named reference snapshots for camera_compare, stored under <media_dir>/baselines/<id>/."""

import contextlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ipcam_mcp.management import ID_PATTERN

_NAME_RE = re.compile(ID_PATTERN)


@dataclass
class Baseline:
    name: str
    path: Path
    taken: datetime
    quality: str
    position: tuple[float, float] | None


def latest_snapshot(media_dir: Path, cam_id: str) -> Path | None:
    """Newest ``<cam_id>_YYYYmmdd-HHMMSS[-N].jpg`` (exact id match, so 'office' != 'office_test')."""
    pattern = re.compile(rf"^{re.escape(cam_id)}_(\d{{8}}-\d{{6}})(?:-(\d+))?\.jpg$")
    try:
        found = [
            (m, p)
            for p in media_dir.iterdir()
            if (m := pattern.match(p.name)) and p.stat().st_size > 0  # skip reservations
        ]
    except OSError:
        return None
    if not found:
        return None
    # Same-second captures get -2, -3, ...: sort by (time, counter), not by name.
    return max(found, key=lambda mp: (mp[0][1], int(mp[0][2] or 1)))[1]


def check_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid baseline name '{name}': use lowercase letters, digits, '-' or '_' "
            "(1-32 characters, starting with a letter or digit)."
        )


def _dir(media_dir: Path, cam_id: str) -> Path:
    return media_dir / "baselines" / cam_id


def _replace_atomic(dest: Path, write) -> None:
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-", suffix=dest.suffix)
    os.close(fd)
    try:
        write(Path(tmp))
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def save_baseline(
    media_dir: Path,
    cam_id: str,
    name: str,
    snapshot: Path,
    quality: str,
    position: tuple[float, float] | None,
) -> Baseline:
    check_name(name)
    d = _dir(media_dir, cam_id)
    d.mkdir(parents=True, exist_ok=True)
    taken = datetime.now().astimezone()
    meta = {
        "taken": taken.isoformat(timespec="seconds"),
        "quality": quality,
        "position": list(position) if position is not None else None,
    }
    jpg = d / f"{name}.jpg"
    _replace_atomic(jpg, lambda tmp: shutil.copyfile(snapshot, tmp))
    _replace_atomic(d / f"{name}.json", lambda tmp: tmp.write_text(json.dumps(meta)))
    return Baseline(name, jpg, datetime.fromisoformat(meta["taken"]), quality, position)


def list_baselines(media_dir: Path, cam_id: str) -> list[str]:
    d = _dir(media_dir, cam_id)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json") if (d / f"{p.stem}.jpg").is_file())


def load_baseline(media_dir: Path, cam_id: str, name: str) -> Baseline:
    check_name(name)
    d = _dir(media_dir, cam_id)
    jpg, meta_path = d / f"{name}.jpg", d / f"{name}.json"
    if not (jpg.is_file() and meta_path.is_file()):
        available = ", ".join(list_baselines(media_dir, cam_id)) or "none"
        raise ValueError(f"baseline '{name}' not found; available: {available}")
    meta = json.loads(meta_path.read_text())
    pos = meta.get("position")
    return Baseline(
        name,
        jpg,
        datetime.fromisoformat(meta["taken"]),
        meta.get("quality", "high"),
        (float(pos[0]), float(pos[1])) if pos else None,
    )
