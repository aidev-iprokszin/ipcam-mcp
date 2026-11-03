"""Scenes: the place a camera watches. Which scene a capture belongs to, and scene-scoped
references for compare and baselines.

A capture's scene is the scene its camera was in at the capture's time (from the timestamp in
its filename, local time). Captures older than the first recorded period belong to the first
period, since they predate the record (e.g. migrated files).
"""

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

from ipcam_mcp import baselines, store
from ipcam_mcp.baselines import Baseline
from ipcam_mcp.config import DeviceConfig
from ipcam_mcp.store import CameraMeta, Period

_CAPTURE_RE = re.compile(r"^.+_(\d{8}-\d{6})(?:-(\d+))?\.(jpg|mp4)$")


def _match(path: Path) -> tuple[datetime, int, str] | None:
    m = _CAPTURE_RE.match(path.name)
    if not m:
        return None
    try:
        when = datetime.strptime(m[1], "%Y%m%d-%H%M%S").astimezone()
    except ValueError:
        return None
    return when, int(m[2] or 1), m[3]


def capture_time(path: Path) -> datetime | None:
    found = _match(path)
    return found[0] if found else None


def period_at(meta: CameraMeta, when: datetime) -> Period:
    current = meta.scenes[0]
    for period in meta.scenes[1:]:
        if period.since <= when:
            current = period
    return current


def _captures(folder: Path, meta: CameraMeta, scene: str, kinds: tuple[str, ...]):
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    found = []
    for p in entries:
        m = _match(p)
        if m is None or m[2] not in kinds or period_at(meta, m[0]).scene != scene:
            continue
        try:
            if not p.is_file() or p.stat().st_size == 0:  # skip reservations
                continue
        except OSError:
            continue
        found.append((m[0], m[1], p))
    # Same-second captures get -2, -3, ...: order by (time, counter), newest first.
    found.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return found


def captures_in_scene(
    folder: Path, meta: CameraMeta, scene: str, kinds: tuple[str, ...] = ("jpg", "mp4")
) -> list[tuple[datetime, Path]]:
    """The camera's captures taken while it was in ``scene``, newest first."""
    return [(when, p) for when, _, p in _captures(folder, meta, scene, kinds)]


def latest_in_scene(folder: Path, meta: CameraMeta, scene: str) -> Path | None:
    """The camera's newest snapshot taken in ``scene``."""
    found = _captures(folder, meta, scene, ("jpg",))
    return found[0][2] if found else None


def _baseline_scene(meta: CameraMeta, base: Baseline) -> str:
    # Baselines saved before scenes existed belong to the scene of the time they were taken.
    return base.scene or period_at(meta, base.taken).scene


def baselines_in_scene(folder: Path, meta: CameraMeta, scene: str) -> list[str]:
    """Names of the camera's baselines saved in ``scene``."""
    found = []
    for name in baselines.list_baselines(folder):
        try:
            base = baselines.load_baseline(folder, name)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            continue  # unreadable: not usable either
        if _baseline_scene(meta, base) == scene:
            found.append(name)
    return found


def load_scene_baseline(folder: Path, meta: CameraMeta, scene: str, name: str) -> Baseline:
    """The baseline ``name``, if it was saved in ``scene``."""
    baselines.check_name(name)
    usable = baselines_in_scene(folder, meta, scene)
    if name not in usable:
        raise ValueError(f"baseline '{name}' not found; available: {', '.join(usable) or 'none'}")
    return baselines.load_baseline(folder, name)


# --- the scene index and timeline over all camera folders ---

Kind = Literal["snapshot", "recording", "baseline"]
_KINDS: dict[str, Kind] = {"jpg": "snapshot", "mp4": "recording"}


@dataclass(frozen=True)
class SceneInfo:
    scene: str
    cameras: list[str]  # configured cameras now in the scene
    removed: list[tuple[str, str]]  # (name, uid) of removed cameras that were in it
    first: datetime | None  # oldest / newest snapshot or recording
    last: datetime | None
    snapshots: int
    recordings: int
    baselines: int


@dataclass(frozen=True)
class TimelineEntry:
    time: datetime
    camera: str
    removed: bool
    kind: Kind
    path: Path


@dataclass(frozen=True)
class _Folder:
    camera: str
    removed: bool
    folder: Path
    meta: CameraMeta


def _folders(media_dir: Path, configured: Mapping[str, DeviceConfig]) -> list[_Folder]:
    names = {dev.uid: name for name, dev in configured.items()}
    found = []
    for uid in sorted(store.folder_names(media_dir)):
        if not store.is_uid(uid):
            continue
        meta = store.read_meta(media_dir / uid)
        if meta is None or meta.uid != uid:
            continue
        name = names.get(uid)
        found.append(_Folder(name or meta.name, name is None, media_dir / uid, meta))
    return found


def _items(f: _Folder) -> Iterator[tuple[str, datetime, int, Kind, Path]]:
    """(scene, time, counter, kind, path) of every capture and baseline in the folder."""
    try:
        entries = list(f.folder.iterdir())
    except OSError:
        entries = []
    for p in entries:
        m = _match(p)
        if m is None:
            continue
        try:
            if not p.is_file() or p.stat().st_size == 0:
                continue
        except OSError:
            continue
        yield period_at(f.meta, m[0]).scene, m[0], m[1], _KINDS[m[2]], p
    for name in baselines.list_baselines(f.folder):
        try:
            base = baselines.load_baseline(f.folder, name)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            continue
        yield _baseline_scene(f.meta, base), base.taken, 0, "baseline", base.path


def scene_index(media_dir: Path, configured: Mapping[str, DeviceConfig]) -> dict[str, SceneInfo]:
    """Every scene: of a configured camera now, or of any period in a camera folder."""
    cameras: dict[str, list[str]] = {}
    removed: dict[str, list[tuple[str, str]]] = {}
    counts: dict[str, dict[Kind, int]] = {}
    times: dict[str, list[datetime]] = {}
    for name, dev in configured.items():
        cameras.setdefault(dev.scene or name, []).append(name)
    for f in _folders(media_dir, configured):
        for period in f.meta.scenes:
            cameras.setdefault(period.scene, [])
            if f.removed and (f.camera, f.meta.uid) not in removed.get(period.scene, []):
                removed.setdefault(period.scene, []).append((f.camera, f.meta.uid))
        for scene, when, _, kind, _ in _items(f):
            cameras.setdefault(scene, [])
            c = counts.setdefault(scene, {"snapshot": 0, "recording": 0, "baseline": 0})
            c[kind] += 1
            if kind != "baseline":
                times.setdefault(scene, []).append(when)
    index = {}
    for scene in sorted(cameras):
        c = counts.get(scene, {"snapshot": 0, "recording": 0, "baseline": 0})
        t = times.get(scene, [])
        index[scene] = SceneInfo(
            scene,
            sorted(cameras[scene]),
            removed.get(scene, []),
            min(t, default=None),
            max(t, default=None),
            c["snapshot"],
            c["recording"],
            c["baseline"],
        )
    return index


def timeline(
    media_dir: Path,
    configured: Mapping[str, DeviceConfig],
    scene: str,
    start: datetime | None,
    end: datetime | None,
    limit: int,
) -> tuple[list[TimelineEntry], int]:
    """The scene's captures and baselines from ``start`` to ``end``, newest first: at most
    ``limit`` of them, and how many more there are."""
    found = []
    for f in _folders(media_dir, configured):
        for item_scene, when, counter, kind, path in _items(f):
            if item_scene != scene:
                continue
            if (start is not None and when < start) or (end is not None and when > end):
                continue
            found.append((when, counter, TimelineEntry(when, f.camera, f.removed, kind, path)))
    found.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [e for _, _, e in found[:limit]], max(len(found) - limit, 0)


def parse_bound(text: str, *, end: bool) -> datetime:
    """An ISO date (the whole day: its start, or its end if ``end``) or datetime; naive
    values are local time."""
    try:
        day = date.fromisoformat(text)  # also the compact YYYYMMDD
    except ValueError:
        pass
    else:
        return datetime.combine(day, time.max if end else time.min).astimezone()
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"invalid date '{text}': use YYYY-MM-DD or an ISO datetime") from None
    return when if when.tzinfo else when.astimezone()
