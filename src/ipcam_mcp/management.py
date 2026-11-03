"""Add, rename and remove cameras in ``cameras.toml``, then reload the registry.

Every mutation is rolled back (config text restored, secrets file changes undone)
if the new config fails to load or the registry rejects it. Credentials never
appear in returned strings or raised messages.
"""

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import tomllib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values

from ipcam_mcp import config_writer, store
from ipcam_mcp.adapters import onvif
from ipcam_mcp.adapters.onvif import DeviceInfo
from ipcam_mcp.config import AppConfig, load_config, media_dir_of
from ipcam_mcp.errors import ConfigError
from ipcam_mcp.registry import Registry

log = logging.getLogger(__name__)

ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"
_ID_RE = re.compile(ID_PATTERN)
_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


@dataclass(frozen=True)
class Credentials:
    user: str
    password: str = field(repr=False)


CredentialPrompt = Callable[[str], Awaitable[Credentials | None]]
# (message, one description per target) -> index of the chosen target, "all", or None (cancel)
DeletePrompt = Callable[[str, list[str]], Awaitable[int | Literal["all"] | None]]
Prober = Callable[[str, str, str, int | None], Awaitable[DeviceInfo]]


def validate_id(cam_id: str) -> None:
    if not _ID_RE.fullmatch(cam_id):
        raise ValueError(f"invalid camera id {cam_id!r}: must match {ID_PATTERN}")


def validate_host(host: str) -> None:
    # The host is not echoed: a mistyped "user@host" would leak the user name.
    message = "invalid host: give an IP address or hostname, without scheme, user, port or path"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # Scoped IPv6 ("fe80::1%eth0") cannot be put into an RTSP URL reliably.
        if isinstance(addr, ipaddress.IPv6Address) and addr.scope_id:
            raise ValueError(message)
        return
    if not host or len(host) > 253 or not all(_LABEL_RE.fullmatch(p) for p in host.split(".")):
        raise ValueError(message)


def prepare_config(path: Path) -> AppConfig:
    """Give cameras without a uid one (written to ``path``), then load the config.

    A missing cameras.toml is an empty config: add_camera creates it.
    """
    media_dir = media_dir_of(path)
    reserved = store.folder_names(media_dir)
    for name, uid in config_writer.ensure_uids(path, reserved).items():
        log.info("camera '%s' got uid %s", name, uid)
        for old in sorted(reserved):
            meta = store.read_meta(media_dir / old) if store.is_uid(old) else None
            if meta is not None and meta.name == name:
                log.warning(
                    "camera '%s' got a new uid, but %s holds media of an earlier camera named "
                    "'%s'. If it is the same camera, set uid = \"%s\" in its section of %s",
                    name, media_dir / old, name, old, path,
                )  # fmt: skip
    return load_config(path, missing_ok=True)


class Manager:
    def __init__(
        self,
        config_path: Path,
        registry: Registry,
        probe: Prober = onvif.probe,
        load: Callable[[Path], AppConfig] = prepare_config,
    ):
        self._config_path = config_path
        self._registry = registry
        self._probe = probe
        self._load = load
        self._lock = asyncio.Lock()

    async def add(
        self,
        cam_id: str,
        host: str,
        prompt: CredentialPrompt | None,
        onvif_port: int | None = None,
    ) -> str:
        validate_id(cam_id)
        validate_host(host)
        async with self._lock:
            config = self._load(self._config_path)
            if cam_id in config.devices:
                raise ValueError(f"camera '{cam_id}' already exists")
            env_path = config.secrets_dir / f"{cam_id}.env"

            if prompt is None:
                if env_path.exists():
                    users = [
                        other
                        for other, spec in self._sections().items()
                        if self._is_managed(spec, env_path)
                    ]
                    if users:
                        raise ValueError(
                            f"{env_path} is already used by camera '{users[0]}'; pick another name"
                        )
                creds = self._file_credentials(cam_id, host, env_path)
                if isinstance(creds, str):
                    return creds
            elif env_path.exists():
                raise ValueError(
                    f"{env_path} already exists; fill it in and call add_camera with "
                    'credentials_via="file", or delete it to use the form'
                )
            else:
                creds = await prompt(
                    f"Login for camera '{cam_id}' at {host}: the camera's local ONVIF/RTSP "
                    "account (not a cloud login). The password is visible while typing "
                    "but is not sent to the AI model."
                )
                if creds is None:
                    return "cancelled; nothing was changed"

            info = await self._probe(host, creds.user, creds.password, onvif_port)

            undo: list[Callable[[], None]] = []
            if prompt is not None:
                config_writer.write_secrets_file(
                    env_path, {"HOST": host, "USER": creds.user, "PASSWORD": creds.password}
                )
                undo.append(lambda: env_path.unlink(missing_ok=True))
            taken = store.folder_names(config.media_dir) | {d.uid for d in config.devices.values()}
            section: dict[str, Any] = {
                "uid": store.new_uid(taken),
                "type": "onvif",
                "secrets_file": str(env_path),
                "host_var": "HOST",
                "user_var": "USER",
                "password_var": "PASSWORD",
                "onvif_port": info.onvif_port,
                "ptz": info.ptz,
                **onvif.known_settings(info.manufacturer, info.model),
            }
            await self._mutate(
                lambda: config_writer.add_section(self._config_path, cam_id, section), undo
            )
            store.ensure_folder(config.media_dir, section["uid"], cam_id)
            caps = next(c["capabilities"] for c in self._registry.list() if c["id"] == cam_id)
            return (
                f"added '{cam_id}' ({info.manufacturer} {info.model}); "
                f"capabilities: {', '.join(caps)}; onvif_port {info.onvif_port}"
            )

    async def rename(self, old: str, new: str) -> str:
        validate_id(new)
        async with self._lock:
            config = self._load(self._config_path)
            if old not in config.devices:
                raise ValueError(f"camera '{old}' not found")
            if new in config.devices:
                raise ValueError(f"camera '{new}' already exists")

            old_path = config.secrets_dir / f"{old}.env"
            new_path = config.secrets_dir / f"{new}.env"
            managed = self._is_managed(self._sections()[old], old_path)
            if managed and new_path.exists():
                raise ConfigError(f"secrets file already exists: {new_path}")

            undo: list[Callable[[], None]] = []
            updates: dict[str, str] | None = None
            if managed:
                try:
                    os.rename(old_path, new_path)
                except OSError as exc:
                    raise ConfigError(
                        f"could not rename {old_path} to {new_path}: {exc.strerror or exc}"
                    ) from exc
                undo.append(lambda: os.rename(new_path, old_path))
                updates = {"secrets_file": str(new_path)}

            await self._mutate(
                lambda: config_writer.rename_section(self._config_path, old, new, updates), undo
            )
            # Media stay in the camera's uid folder; only its recorded name changes.
            uid = config.devices[old].uid
            if (config.media_dir / uid).is_dir():
                store.ensure_folder(config.media_dir, uid, new)
            if self._removed_named(config, new):
                return (
                    f"renamed '{old}' to '{new}'; an earlier, removed camera named '{new}' "
                    "keeps its files separately"
                )
            return f"renamed '{old}' to '{new}'"

    async def remove(self, cam_id: str) -> str:
        async with self._lock:
            config = self._load(self._config_path)
            if cam_id not in config.devices:
                raise ValueError(f"camera '{cam_id}' not found")
            owned = self._owned_secrets(cam_id, config.secrets_dir)

            await self._mutate(lambda: config_writer.remove_section(self._config_path, cam_id))
            result = f"removed '{cam_id}'"
            if owned is not None:
                try:
                    owned.unlink(missing_ok=True)
                    result += f" and deleted {owned}"
                except OSError as exc:
                    result += f"; could not delete {owned}: {exc.strerror or exc}"
            folder = config.media_dir / config.devices[cam_id].uid
            if folder.is_dir():
                result += f"; files kept in {folder}; delete_camera('{cam_id}') deletes them"
            return result

    async def removed(self) -> list[store.StoredCamera]:
        """Removed cameras whose files are still in media_dir."""
        async with self._lock:
            config = self._load(self._config_path)
            return store.removed_cameras(config.media_dir, {d.uid for d in config.devices.values()})

    async def delete(self, name: str, confirm: DeletePrompt | None) -> str:
        """Delete the configured camera ``name`` and/or removed cameras of that name, with their
        files, once the user approves through ``confirm``. Without it nothing happens."""
        async with self._lock:
            config = self._load(self._config_path)
            targets = self._delete_targets(config, name)
            if not targets:
                raise ValueError(f"no camera or leftover files named '{name}'")
            if confirm is None:
                return self._delete_by_hand(name, targets)
            if len(targets) == 1:
                message = f"Delete '{name}' and its files? This cannot be undone."
            else:
                message = (
                    f"Several cameras named '{name}' have files. Which should be deleted? "
                    "This cannot be undone."
                )
            choice = await confirm(message, [t.description for t in targets])
            if choice is None:
                return "cancelled; nothing was deleted"
            chosen = targets if choice == "all" else [targets[choice]]

            files = 0
            left: list[Path] = []
            notes: list[str] = []
            for target in chosen:
                files += target.files
                if target.configured:
                    note = await self._drop_configured(name, config)
                    if note:
                        notes.append(note)
                left += store.delete_folder(config.media_dir, target.uid)
            result = f"deleted '{name}' ({files} file{'s' * (files != 1)})"
            if left:
                shown = ", ".join(str(p) for p in left[:5]) + (" …" if len(left) > 5 else "")
                result += f"; could not delete: {shown}"
            return "; ".join([result, *notes])

    def _delete_targets(self, config: AppConfig, name: str) -> list["_DeleteTarget"]:
        targets = []
        dev = config.devices.get(name)
        if dev is not None:
            folder = config.media_dir / dev.uid
            meta = store.CameraMeta(dev.uid, name, datetime.now().astimezone())
            used = store.usage(folder, meta)
            owned = self._owned_secrets(name, config.secrets_dir)
            extra = "config entry and its secrets file" if owned else "config entry"
            targets.append(
                _DeleteTarget(
                    dev.uid,
                    True,
                    f"{name} (configured): {_counts(used)}; {extra}",
                    folder,
                    _files(folder),
                )
            )
        configured = {d.uid for d in config.devices.values()}
        for used in store.removed_cameras(config.media_dir, configured):
            if used.name == name:
                added = used.created.date().isoformat()
                targets.append(
                    _DeleteTarget(
                        used.uid,
                        False,
                        f"{name} (removed, added {added}): {_counts(used)}",
                        used.folder,
                        _files(used.folder),
                    )
                )
        labels = [t.description for t in targets]
        return [
            replace(t, description=f"{t.description} [{t.uid}]")
            if labels.count(t.description) > 1
            else t
            for t in targets
        ]

    def _delete_by_hand(self, name: str, targets: list["_DeleteTarget"]) -> str:
        steps = []
        if any(t.configured for t in targets):
            steps.append(
                f"remove the [cameras.{name}] section from {self._config_path} (or call remove_camera)"
            )
        steps.append("delete " + ", ".join(str(t.folder) for t in targets))
        return (
            "This client can't show the approval form, so nothing was deleted. "
            "To delete it by hand: " + "; then ".join(steps)
        )

    async def _drop_configured(self, name: str, config: AppConfig) -> str | None:
        """Remove ``name`` from the config (and its managed secrets file) while holding its
        camera lock, so a running capture finishes first. Returns a note on failure."""
        owned = self._owned_secrets(name, config.secrets_dir)
        async with self._registry.lock(name):
            await self._mutate(
                lambda: config_writer.remove_section(self._config_path, name),
                close_retired=False,  # needs the camera lock held here
            )
            note = None
            if owned is not None:
                try:
                    owned.unlink(missing_ok=True)
                except OSError as exc:
                    note = f"could not delete {owned}: {exc.strerror or exc}"
        await self._registry.close_retired()
        return note

    async def reload(self) -> list[dict]:
        async with self._lock:
            self._registry.reload(self._load(self._config_path))
            await self._registry.close_retired()
            return self._registry.list()

    def _file_credentials(self, cam_id: str, host: str, env_path: Path) -> Credentials | str:
        """Credentials from ``<id>.env``, or the instruction to fill it in."""
        instruction = (
            f"Fill in USER and PASSWORD in {env_path}, then call add_camera again "
            f"with name '{cam_id}', the same host and credentials_via=\"file\"."
        )
        if not env_path.exists():
            config_writer.write_secrets_file(env_path, {"HOST": host, "USER": "", "PASSWORD": ""})
            return instruction
        values = dotenv_values(env_path, interpolate=False)
        user, password = values.get("USER"), values.get("PASSWORD")
        if not user or not password:
            return instruction
        if values.get("HOST") != host:
            raise ValueError(
                f"HOST in {env_path} does not match {host}; "
                "fix the file or call add_camera with the host it contains"
            )
        return Credentials(user, password)

    def _sections(self) -> dict[str, Any]:
        """Raw ``[cameras.*]`` tables; ``AppConfig`` does not keep ``secrets_file``."""
        try:
            text = self._config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        return tomllib.loads(text).get("cameras", {})

    def _owned_secrets(self, name: str, secrets_dir: Path) -> Path | None:
        """``<secrets_dir>/<name>.env`` if this camera's section uses it and no other does."""
        path = secrets_dir / f"{name}.env"
        sections = self._sections()
        if self._is_managed(sections[name], path) and not any(
            self._is_managed(spec, path) for other, spec in sections.items() if other != name
        ):
            return path
        return None

    @staticmethod
    def _removed_named(config: AppConfig, name: str) -> bool:
        """Whether a removed camera (uid folder not in the config) was called ``name``."""
        configured = {d.uid for d in config.devices.values()}
        for uid in store.folder_names(config.media_dir) - configured:
            with contextlib.suppress(ValueError):
                meta = store.read_meta(store.camera_dir(config.media_dir, uid))
                if meta is not None and meta.name == name:
                    return True
        return False

    @staticmethod
    def _is_managed(spec: dict[str, Any], managed_path: Path) -> bool:
        return Path(spec["secrets_file"]).expanduser().resolve() == managed_path.resolve()

    async def _mutate(
        self,
        change: Callable[[], None],
        undo: Sequence[Callable[[], None]] = (),
        *,
        close_retired: bool = True,
    ) -> None:
        """Apply ``change`` and reload; on failure restore the config and run ``undo``.
        Afterwards (unless ``close_retired`` is false), close the connections of replaced or
        removed cameras."""
        original: str | None = None
        existed = self._config_path.exists()
        try:
            if existed:
                original = self._config_path.read_bytes().decode("utf-8")
            change()
            self._registry.reload(self._load(self._config_path))
        except Exception:
            try:
                if original is not None:
                    config_writer.atomic_write(self._config_path, original)
                elif not existed:  # first run: the change created the file
                    self._config_path.unlink(missing_ok=True)
            except Exception:
                log.exception("rollback: could not restore %s", self._config_path)
            for step in reversed(undo):
                try:
                    step()
                except Exception:
                    log.exception("rollback: undo step failed")
            raise
        if close_retired:
            await self._registry.close_retired()


@dataclass(frozen=True)
class _DeleteTarget:
    uid: str
    configured: bool
    description: str
    folder: Path
    files: int


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def _counts(used: store.StoredCamera) -> str:
    return (
        f"{_plural(used.snapshots, 'snapshot')}, {_plural(used.recordings, 'recording')}, "
        f"{_plural(used.baselines, 'baseline')}, {used.size / 1_000_000:.1f} MB"
    )


def _files(folder: Path) -> int:
    try:
        return sum(1 for p in folder.rglob("*") if p.is_file() and p.name != store.META)
    except OSError:
        return 0
