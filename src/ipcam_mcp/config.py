"""Load ``cameras.toml`` and each camera's secrets env file."""

import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from ipcam_mcp.errors import ConfigError

DEFAULT_CONFIG = "~/.config/ipcam-mcp/cameras.toml"
DEFAULT_MEDIA_DIR = "~/.local/share/ipcam-mcp/media"
DEFAULT_SECRETS_DIR = "~/.config/ipcam-mcp/secrets"
log = logging.getLogger("ipcam_mcp")

REQUIRED_KEYS = ("type", "secrets_file", "host_var", "user_var", "password_var")
_UID_RE = re.compile(r"^[a-z0-9]{8}$")  # same as store.UID_PATTERN


@dataclass(frozen=True)
class DeviceConfig:
    id: str
    type: str
    host: str
    user: str
    password: str = field(repr=False)
    uid: str = ""
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    media_dir: Path
    devices: dict[str, DeviceConfig]
    allow_management: bool = True
    secrets_dir: Path = field(default_factory=lambda: Path(DEFAULT_SECRETS_DIR).expanduser())


def config_path() -> Path:
    """``IPCAM_MCP_CONFIG`` if set (and non-empty), else ``~/.config/ipcam-mcp/cameras.toml``."""
    return Path(os.environ.get("IPCAM_MCP_CONFIG") or DEFAULT_CONFIG).expanduser()


def load_config(path: Path | None = None, *, missing_ok: bool = False) -> AppConfig:
    """Load ``path``. A missing file is an error unless ``missing_ok``: then it is an
    empty config (no cameras, defaults), which ``add_camera`` can create on first run."""
    path = Path(path) if path is not None else config_path()
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        if not missing_ok:
            raise ConfigError(f"config file not found: {path}") from None
        raw = {}
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from None

    media_dir = Path(raw.get("media_dir", DEFAULT_MEDIA_DIR)).expanduser()
    devices_raw = raw.get("cameras", {})
    if not isinstance(devices_raw, dict):
        raise ConfigError(f"{path}: 'cameras' must be a table")
    checked: set[Path] = set()
    devices = {dev_id: _load_device(dev_id, spec, checked) for dev_id, spec in devices_raw.items()}
    owners: dict[str, str] = {}
    for dev_id, dev in devices.items():
        if dev.uid in owners:
            raise ConfigError(f"cameras '{owners[dev.uid]}' and '{dev_id}' share uid '{dev.uid}'")
        owners[dev.uid] = dev_id
    allow_management = raw.get("allow_management", True)
    if not isinstance(allow_management, bool):
        raise ConfigError(f"{path}: 'allow_management' must be true or false")
    secrets_dir = Path(raw.get("secrets_dir", DEFAULT_SECRETS_DIR)).expanduser()
    return AppConfig(
        media_dir=media_dir,
        devices=devices,
        allow_management=allow_management,
        secrets_dir=secrets_dir,
    )


def media_dir_of(path: Path) -> Path:
    """``media_dir`` from ``path`` (or the default), without loading the cameras."""
    try:
        raw = tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        raw = {}
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from None
    return Path(raw.get("media_dir", DEFAULT_MEDIA_DIR)).expanduser()


def _load_device(dev_id: str, spec: dict[str, Any], checked: set[Path]) -> DeviceConfig:
    uid = spec.get("uid")
    if uid is None:
        raise ConfigError(f"camera '{dev_id}': missing uid")
    if not isinstance(uid, str) or not _UID_RE.fullmatch(uid):
        raise ConfigError(f"camera '{dev_id}': invalid uid '{uid}' (8 of a-z, 0-9)")
    missing = [k for k in REQUIRED_KEYS if k not in spec]
    if missing:
        raise ConfigError(f"camera '{dev_id}': missing required key(s): {', '.join(missing)}")

    secrets_path = Path(spec["secrets_file"]).expanduser()
    if not secrets_path.is_file():
        raise ConfigError(f"camera '{dev_id}': secrets file not found: {secrets_path}")
    _warn_if_exposed(secrets_path, checked)
    secrets = dotenv_values(secrets_path, interpolate=False)

    def secret(var_key: str) -> str:
        var = spec[var_key]
        value = secrets.get(var)
        if not value:
            raise ConfigError(
                f"camera '{dev_id}': variable {var} is missing or empty in {secrets_path}"
            )
        return value

    settings = {k: v for k, v in spec.items() if k not in REQUIRED_KEYS and k != "uid"}
    return DeviceConfig(
        id=dev_id,
        uid=uid,
        type=spec["type"],
        host=secret("host_var"),
        user=secret("user_var"),
        password=secret("password_var"),
        settings=settings,
    )


def _warn_if_exposed(path: Path, checked: set[Path]) -> None:
    """Warn once per file (per load) if group/others can access it. Never reads contents."""
    resolved = path.resolve()
    if resolved in checked:
        return
    checked.add(resolved)
    try:
        mode = resolved.stat().st_mode
    except OSError:
        return  # best effort: never let the warning block loading
    if mode & 0o077:
        log.warning("secrets file %s is accessible by other users; run: chmod 600 %s", path, path)
