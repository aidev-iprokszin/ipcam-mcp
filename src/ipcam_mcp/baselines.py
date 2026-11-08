"""Named reference snapshots for camera_compare, stored in <camera folder>/baselines/."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ipcam_mcp.config import NAME_PATTERN
from ipcam_mcp.fileio import write_atomic
from ipcam_mcp.store import parse_time

_NAME_RE = re.compile(NAME_PATTERN)


@dataclass
class Baseline:
    name: str
    path: Path
    taken: datetime
    quality: str
    position: tuple[float, float] | None
    scene: str | None = None  # None: saved before scenes existed


def check_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid baseline name '{name}': use lowercase letters, digits, '-' or '_' "
            "(1-32 characters, starting with a letter or digit)."
        )


def _dir(folder: Path) -> Path:
    return folder / "baselines"


def save_baseline(
    folder: Path,
    name: str,
    snapshot: Path,
    quality: str,
    position: tuple[float, float] | None,
    scene: str | None = None,
) -> Baseline:
    check_name(name)
    d = _dir(folder)
    d.mkdir(parents=True, exist_ok=True)
    taken = datetime.now().astimezone()
    meta = {
        "taken": taken.isoformat(timespec="seconds"),
        "quality": quality,
        "position": list(position) if position is not None else None,
        "scene": scene,
    }
    jpg = d / f"{name}.jpg"
    write_atomic(jpg, snapshot.read_bytes())
    write_atomic(d / f"{name}.json", json.dumps(meta))
    return Baseline(name, jpg, parse_time(meta["taken"]), quality, position, scene)


def list_baselines(folder: Path) -> list[str]:
    d = _dir(folder)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json") if (d / f"{p.stem}.jpg").is_file())


def load_baseline(folder: Path, name: str) -> Baseline:
    check_name(name)
    d = _dir(folder)
    jpg, meta_path = d / f"{name}.jpg", d / f"{name}.json"
    if not (jpg.is_file() and meta_path.is_file()):
        available = ", ".join(list_baselines(folder)) or "none"
        raise ValueError(f"baseline '{name}' not found; available: {available}")
    meta = json.loads(meta_path.read_text())
    pos = meta.get("position")
    return Baseline(
        name,
        jpg,
        parse_time(meta["taken"]),
        meta.get("quality", "high"),
        (float(pos[0]), float(pos[1])) if pos else None,
        meta.get("scene"),
    )
