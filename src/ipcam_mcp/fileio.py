"""Crash-safe file replacement shared by the config, camera.json and baseline writers."""

import contextlib
import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, data: str | bytes) -> None:
    """Replace ``path`` with ``data`` (text is UTF-8) so it is never seen half-written.

    The data goes to a temp file in the same folder, is flushed to disk, then renamed over
    ``path``. A replaced file keeps its permission bits; a new file is private (0600).
    Raises ``OSError``; the temp file is removed on any failure.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        with contextlib.suppress(FileNotFoundError):
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
