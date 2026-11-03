"""On-disk media layout: one folder per camera uid.

``<media_dir>/<uid>/`` holds the camera's snapshots and recordings, ``baselines/`` and a
``camera.json`` with its uid, current name and creation time. The uid never changes, so a
camera's history stays its own when it is renamed, and a later camera with the same name
does not inherit it.
"""

import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import string
import tempfile
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

UID_PATTERN = r"^[a-z0-9]{8}$"
_UID_RE = re.compile(UID_PATTERN)
_ALPHABET = string.ascii_lowercase + string.digits
META = "camera.json"
log = logging.getLogger("ipcam_mcp")


@dataclass(frozen=True)
class Period:
    """From ``since`` on, the camera watched ``scene``; ``continues``: declared predecessor."""

    scene: str
    since: datetime
    continues: str | None = None


@dataclass(frozen=True)
class CameraMeta:
    uid: str
    name: str
    created: datetime
    scenes: tuple[Period, ...] = ()

    def __post_init__(self) -> None:
        if not self.scenes:  # before scenes existed: the name was the scene, since creation
            object.__setattr__(self, "scenes", (Period(self.name, self.created),))

    @property
    def current_scene(self) -> str:
        return self.scenes[-1].scene


@dataclass(frozen=True)
class StoredCamera:
    """A camera folder and what it holds."""

    uid: str
    name: str
    created: datetime
    folder: Path
    snapshots: int
    recordings: int
    baselines: int
    size: int
    scene: str = ""  # the scene it was last in


_CAPTURE_RE = re.compile(r"^.+_\d{8}-\d{6}(?:-\d+)?\.(jpg|mp4)$")


def is_uid(text: str) -> bool:
    return bool(_UID_RE.fullmatch(text))


def new_uid(taken: Collection[str]) -> str:
    while True:
        uid = "".join(secrets.choice(_ALPHABET) for _ in range(8))
        if uid not in taken:
            return uid


def folder_names(media_dir: Path) -> set[str]:
    try:
        return {p.name for p in media_dir.iterdir() if p.is_dir()}
    except OSError:
        return set()


def camera_dir(media_dir: Path, uid: str) -> Path:
    if not _UID_RE.fullmatch(uid):
        raise ValueError(f"invalid camera uid {uid!r}")
    return media_dir / uid


def read_meta(folder: Path) -> CameraMeta | None:
    path = folder / META
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        periods = tuple(_period(p) for p in raw.get("scenes") or ())
        meta = CameraMeta(raw["uid"], raw["name"], datetime.fromisoformat(raw["created"]), periods)
        if not (_UID_RE.fullmatch(meta.uid) and isinstance(meta.name, str)):
            raise ValueError("bad uid or name")
        return meta
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("%s: unreadable (%s)", path, type(e).__name__)
        return None


def _period(raw: object) -> Period:
    if not isinstance(raw, dict) or not isinstance(raw.get("scene"), str):
        raise TypeError("bad scene period")
    continues = raw.get("continues")
    if continues is not None and not (isinstance(continues, str) and _UID_RE.fullmatch(continues)):
        raise ValueError("bad continues")
    return Period(raw["scene"], datetime.fromisoformat(raw["since"]), continues)


def _period_json(p: Period) -> dict:
    data = {"scene": p.scene, "since": p.since.isoformat()}
    if p.continues:
        data["continues"] = p.continues
    return data


def write_meta(folder: Path, meta: CameraMeta) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    data = {
        "uid": meta.uid,
        "name": meta.name,
        "created": meta.created.isoformat(),
        "scenes": [_period_json(p) for p in meta.scenes],
    }
    # Own small atomic write: config_writer imports this module (see #17 for a shared helper).
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{META}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, folder / META)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def ensure_folder(media_dir: Path, uid: str, name: str, scene: str | None = None) -> Path:
    """The camera's folder, created if needed, with ``camera.json`` naming it ``name``.

    A new folder starts in ``scene`` (default: ``name``). For an existing one only the name
    is updated; scene changes go through ``enter_scene``.
    """
    folder = camera_dir(media_dir, uid)
    meta = read_meta(folder)
    if meta is None:
        created = _now()
        write_meta(folder, CameraMeta(uid, name, created, (Period(scene or name, created),)))
    elif meta.name != name:
        write_meta(folder, replace(meta, name=name))
    return folder


def enter_scene(
    media_dir: Path, uid: str, name: str, scene: str, continues: str | None = None
) -> CameraMeta:
    """Record that the camera watches ``scene`` from now on (optionally as the successor of
    ``continues``). Re-entering the current scene only sets ``continues`` on its period."""
    folder = ensure_folder(media_dir, uid, name, scene)
    meta = read_meta(folder)
    assert meta is not None  # ensure_folder just wrote it
    last = meta.scenes[-1]
    if last.scene == scene:
        if continues is None or last.continues == continues:
            return meta
        periods = (*meta.scenes[:-1], replace(last, continues=continues))
    else:
        # Capture names have whole seconds: the new period starts at the next one, so a
        # capture taken in the second of the move stays with the old scene.
        periods = (*meta.scenes, Period(scene, _now() + timedelta(seconds=1), continues))
    meta = replace(meta, scenes=periods)
    write_meta(folder, meta)
    return meta


def usage(folder: Path, meta: CameraMeta) -> StoredCamera:
    snapshots = recordings = baselines = size = 0
    with contextlib.suppress(OSError):
        for p in folder.iterdir():
            if p.is_file() and (m := _CAPTURE_RE.match(p.name)):
                snapshots += m[1] == "jpg"
                recordings += m[1] == "mp4"
        b = folder / "baselines"
        if b.is_dir():
            baselines = sum(1 for j in b.glob("*.json") if (b / f"{j.stem}.jpg").is_file())
        size = sum(
            p.stat().st_size for p in folder.rglob("*") if p.is_file() and not p.is_symlink()
        )
    return StoredCamera(
        meta.uid,
        meta.name,
        meta.created,
        folder,
        snapshots,
        recordings,
        baselines,
        size,
        meta.current_scene,
    )


def removed_cameras(media_dir: Path, configured_uids: Collection[str]) -> list[StoredCamera]:
    """Camera folders with a readable ``camera.json`` whose uid is not configured."""
    found = []
    for uid in sorted(folder_names(media_dir) - set(configured_uids)):
        if not is_uid(uid):
            continue
        folder = media_dir / uid
        meta = read_meta(folder)
        if meta is not None and meta.uid == uid:
            found.append(usage(folder, meta))
    return sorted(found, key=lambda c: c.created)


def check_deletable(media_dir: Path, uid: str) -> Path:
    """The camera's folder, if it may be deleted: a real directory directly inside
    ``media_dir`` (``media_dir`` itself may be a symlink, e.g. to another disk)."""
    folder = camera_dir(media_dir, uid)
    if folder.is_symlink():
        raise ValueError(f"refusing to delete {folder}: it is a symlink; delete it by hand")
    if folder.exists() and (not folder.is_dir() or folder.resolve().parent != media_dir.resolve()):
        raise ValueError(f"refusing to delete {folder}: not a camera folder in {media_dir}")
    return folder


def delete_folder(media_dir: Path, uid: str) -> list[Path]:
    """Delete ``<media_dir>/<uid>``; returns the paths that could not be deleted.

    ``camera.json`` goes last, so a folder with leftovers is still listed as a removed camera
    and can be deleted again later.
    """
    folder = check_deletable(media_dir, uid)
    if not folder.exists():
        return []
    left: list[Path] = []

    def collect(_func, path, _exc) -> None:
        left.append(Path(path))

    for entry in sorted(folder.iterdir()):
        if entry.name == META:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, onexc=collect)
        else:
            try:
                os.unlink(entry)
            except OSError:
                left.append(entry)
    if left:
        return left
    for path, remove in ((folder / META, os.unlink), (folder, os.rmdir)):
        try:
            remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            left.append(path)
    return left
