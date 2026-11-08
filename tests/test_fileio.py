import os

import pytest

from ipcam_mcp import fileio
from ipcam_mcp.fileio import write_atomic


def _record(monkeypatch) -> list[str]:
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append("fsync")
        real_fsync(fd)

    def replace(src, dst):
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(fileio.os, "fsync", fsync)
    monkeypatch.setattr(fileio.os, "replace", replace)
    return events


@pytest.mark.parametrize("data", ["text ✓", b"\xff\xd8bytes"])
def test_writes_text_and_bytes(tmp_path, data):
    p = tmp_path / "f"
    write_atomic(p, data)
    expected = data.encode() if isinstance(data, str) else data
    assert p.read_bytes() == expected
    assert [x.name for x in tmp_path.iterdir()] == ["f"]


def test_fsyncs_before_replace(tmp_path, monkeypatch):
    events = _record(monkeypatch)
    write_atomic(tmp_path / "f", b"data")
    assert events == ["fsync", "replace"]


def test_keeps_mode_of_replaced_file(tmp_path):
    p = tmp_path / "f"
    p.write_text("old")
    p.chmod(0o640)
    write_atomic(p, "new")
    assert p.stat().st_mode & 0o777 == 0o640


def test_new_file_is_private(tmp_path):
    p = tmp_path / "f"
    write_atomic(p, "new")
    assert p.stat().st_mode & 0o777 == 0o600


def test_failure_keeps_original_and_removes_temp(tmp_path, monkeypatch):
    p = tmp_path / "f"
    p.write_text("old")

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(fileio.os, "replace", boom)
    with pytest.raises(OSError, match="nope"):
        write_atomic(p, "new")
    assert p.read_text() == "old"
    assert [x.name for x in tmp_path.iterdir()] == ["f"]


def test_interrupted_write_removes_temp(tmp_path, monkeypatch):
    def interrupt(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(fileio.os, "fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        write_atomic(tmp_path / "f", "new")
    assert list(tmp_path.iterdir()) == []
