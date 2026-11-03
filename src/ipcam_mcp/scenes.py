"""Scenes: the place a camera watches. Which scene a capture belongs to, and scene-scoped
references for compare and baselines.

A capture's scene is the scene its camera was in at the capture's time (from the timestamp in
its filename, local time). Captures older than the first recorded period belong to the first
period, since they predate the record (e.g. migrated files).
"""

import re
from datetime import datetime
from pathlib import Path

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
