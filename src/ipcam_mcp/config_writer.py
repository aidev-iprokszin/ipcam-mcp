"""Edit ``cameras.toml`` sections and write per-camera secrets files.

TOML edits go through tomlkit so comments, ordering and formatting survive.
All file replacement is atomic.
"""

import contextlib
import os
import tempfile
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError

from ipcam_mcp.errors import ConfigError


def atomic_write(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` via a temp file next to its real target.

    A symlinked ``path`` stays a symlink: the file it points to is replaced.
    """
    target = Path(os.path.realpath(path))
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if target.exists():
            os.chmod(tmp_name, target.stat().st_mode & 0o7777)
        os.replace(tmp_name, target)
        tmp_name = None
    except OSError as exc:
        raise ConfigError(f"could not write {path}: {exc.strerror or exc}") from exc
    finally:
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


def _load(path: Path) -> tomlkit.TOMLDocument:
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc.strerror or exc}") from exc
    except TOMLKitError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def add_section(path: Path, cam_id: str, values: dict[str, str]) -> None:
    doc = _load(path)
    if "cameras" not in doc:
        doc["cameras"] = tomlkit.table(is_super_table=True)
    cameras = doc["cameras"]
    if cam_id in cameras:
        raise ConfigError(f"camera {cam_id!r} already exists in {path}")
    section = tomlkit.table()
    for key, value in values.items():
        section[key] = value
    # tomlkit adds a separating blank line only when the file does not already end in one.
    # Always add one of our own, so remove_section can undo this exactly.
    original = tomlkit.dumps(doc)
    cameras[cam_id] = section
    text = tomlkit.dumps(doc)
    if original.endswith("\n\n") and text.startswith(original):
        text = original + "\n" + text[len(original) :]
    atomic_write(path, text)


def rename_section(path: Path, old: str, new: str, updates: dict[str, str] | None = None) -> None:
    doc = _load(path)
    cameras = doc.get("cameras")
    if cameras is None or old not in cameras:
        raise ConfigError(f"camera {old!r} not found in {path}")
    if new in cameras:
        raise ConfigError(f"camera {new!r} already exists in {path}")
    # Rebuild the table in order so the renamed section keeps its position.
    items = list(cameras.items())
    for key, _ in items:
        del cameras[key]
    for key, value in items:
        if key == old:
            for k, v in (updates or {}).items():
                value[k] = v
            key = new
        cameras[key] = value
    atomic_write(path, tomlkit.dumps(doc))


def remove_section(path: Path, cam_id: str) -> None:
    doc = _load(path)
    cameras = doc.get("cameras")
    if cameras is None or cam_id not in cameras:
        raise ConfigError(f"camera {cam_id!r} not found in {path}")
    before = tomlkit.dumps(doc)
    del cameras[cam_id]
    after = tomlkit.dumps(doc)
    # Undo add_section: when the removed table was last, drop the one separator newline that
    # add_section put in front of it.
    if before.startswith(after) and after.endswith("\n\n"):
        after = after[:-1]
    atomic_write(path, after)


# Secrets files are read with dotenv interpolation off (see config.py), so ``${...}``
# in a value is literal.
def _quote_env(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_secrets_file(path: Path, values: dict[str, str]) -> None:
    """Create a new 0600 dotenv file; refuse to overwrite an existing one."""
    lines = [f"{key}={_quote_env(value)}" for key, value in values.items()]
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ConfigError(f"secrets file already exists: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not create secrets file {path}: {exc.strerror or exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as exc:
        with contextlib.suppress(OSError):
            path.unlink()
        raise ConfigError(f"could not write secrets file {path}: {exc.strerror or exc}") from exc
