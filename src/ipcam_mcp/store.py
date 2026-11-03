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
import string
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

UID_PATTERN = r"^[a-z0-9]{8}$"
_UID_RE = re.compile(UID_PATTERN)
_ALPHABET = string.ascii_lowercase + string.digits
META = "camera.json"
log = logging.getLogger("ipcam_mcp")


@dataclass(frozen=True)
class CameraMeta:
    uid: str
    name: str
    created: datetime


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
        meta = CameraMeta(raw["uid"], raw["name"], datetime.fromisoformat(raw["created"]))
        if not (_UID_RE.fullmatch(meta.uid) and isinstance(meta.name, str)):
            raise ValueError("bad uid or name")
        return meta
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("%s: unreadable (%s)", path, type(e).__name__)
        return None


def write_meta(folder: Path, meta: CameraMeta) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    data = {"uid": meta.uid, "name": meta.name, "created": meta.created.isoformat()}
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


def ensure_folder(media_dir: Path, uid: str, name: str) -> Path:
    """The camera's folder, created if needed, with ``camera.json`` naming it ``name``."""
    folder = camera_dir(media_dir, uid)
    meta = read_meta(folder)
    if meta is None:
        created = datetime.now().astimezone().replace(microsecond=0)
        write_meta(folder, CameraMeta(uid, name, created))
    elif meta.name != name:
        write_meta(folder, CameraMeta(uid, name, meta.created))
    return folder
