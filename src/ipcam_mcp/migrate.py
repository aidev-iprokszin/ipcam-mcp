"""One-time move of the flat media layout (before uids) into per-camera folders.

Old layout: ``<media_dir>/<name>_YYYYmmdd-HHMMSS[-N].jpg|.mp4`` and
``<media_dir>/baselines/<name>/``. Each name's files move to ``<media_dir>/<uid>/``: the
configured camera's uid, or a new uid (a removed camera) for names no longer configured.
Nothing is overwritten or deleted; anything else in ``media_dir`` is left alone.
"""

import contextlib
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ipcam_mcp import store

log = logging.getLogger("ipcam_mcp")
_FLAT_RE = re.compile(r"^(?P<name>.+)_(?P<ts>\d{8}-\d{6})(?:-\d+)?\.(?:jpg|mp4)$")


@dataclass
class MigrationReport:
    moved: dict[str, int] = field(default_factory=dict)
    removed_cameras: list[str] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    error: str | None = None


@dataclass
class _Group:
    captures: list[Path] = field(default_factory=list)
    baselines: Path | None = None


def _groups(media_dir: Path) -> dict[str, _Group]:
    groups: dict[str, _Group] = {}
    for p in media_dir.iterdir():
        if (m := _FLAT_RE.match(p.name)) and p.is_file():
            groups.setdefault(m["name"], _Group()).captures.append(p)
    legacy = media_dir / "baselines"
    if legacy.is_dir():
        for d in legacy.iterdir():
            if d.is_dir() and not d.is_symlink():
                groups.setdefault(d.name, _Group()).baselines = d
    return groups


def _created(captures: list[Path]) -> datetime:
    stamps = [m["ts"] for p in captures if (m := _FLAT_RE.match(p.name))]
    if not stamps:
        return datetime.now().astimezone().replace(microsecond=0)
    return datetime.strptime(min(stamps), "%Y%m%d-%H%M%S").astimezone()


def migrate_flat_layout(
    media_dir: Path, configured: Mapping[str, str], scenes: Mapping[str, str] | None = None
) -> MigrationReport:
    """Move old-style media into uid folders; ``configured`` maps camera name -> uid, and
    ``scenes`` camera name -> scene (default: the name)."""
    report = MigrationReport()
    try:
        groups = _groups(media_dir)
    except OSError:
        return report  # no media_dir yet
    if not groups:
        return report

    existing = store.folder_names(media_dir)
    # Removed cameras from an earlier, interrupted run keep collecting their files.
    earlier = {}
    for uid in sorted(existing - set(configured.values())):
        meta = store.read_meta(media_dir / uid) if re.fullmatch(store.UID_PATTERN, uid) else None
        if meta is not None:
            earlier.setdefault(meta.name, uid)
    taken = existing | set(configured.values())

    try:
        _migrate_groups(media_dir, configured, scenes or {}, groups, earlier, taken, report)
    except OSError as e:  # stop; the rest stays in the old layout and is retried next start
        report.error = f"could not migrate {e.filename or media_dir}: {e.strerror or e}"
        return report
    with contextlib.suppress(OSError):
        (media_dir / "baselines").rmdir()
    return report


def _migrate_groups(
    media_dir: Path,
    configured: Mapping[str, str],
    scenes: Mapping[str, str],
    groups: dict[str, _Group],
    earlier: dict[str, str],
    taken: set[str],
    report: MigrationReport,
) -> None:
    for name in sorted(groups):
        group = groups[name]
        uid = configured.get(name) or earlier.get(name)
        if uid is None:
            uid = store.new_uid(taken)
            taken.add(uid)
        if name not in configured:
            report.removed_cameras.append(name)
        folder = store.camera_dir(media_dir, uid)
        if store.meta_for_update(folder) is None:
            created = _created(group.captures)
            period = store.Period(scenes.get(name, name), created)
            store.write_meta(folder, store.CameraMeta(uid, name, created, (period,)))

        moves = [(p, folder / p.name) for p in sorted(group.captures)]
        if group.baselines is not None:
            moves += [
                (p, folder / "baselines" / p.name)
                for p in sorted(group.baselines.iterdir())
                if p.is_file()
            ]
        count = 0
        for src, dst in moves:
            if dst.exists():
                report.skipped.append(src)
                continue
            try:
                dst.parent.mkdir(exist_ok=True)
                os.rename(src, dst)
            except OSError:
                if count:
                    report.moved[name] = count
                raise
            count += 1
        if count:
            report.moved[name] = count
        if group.baselines is not None:
            with contextlib.suppress(OSError):
                group.baselines.rmdir()  # only if empty
