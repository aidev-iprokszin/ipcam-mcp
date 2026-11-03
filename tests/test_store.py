import json
import re
from datetime import datetime

import pytest

from ipcam_mcp import store
from ipcam_mcp.store import (
    UID_PATTERN,
    CameraMeta,
    camera_dir,
    ensure_folder,
    folder_names,
    new_uid,
    read_meta,
    write_meta,
)


def _choices(monkeypatch, *uids: str) -> None:
    chars = iter("".join(uids))
    monkeypatch.setattr(store.secrets, "choice", lambda _alphabet: next(chars))


def test_new_uid_format():
    assert re.fullmatch(UID_PATTERN, new_uid(set()))


def test_new_uid_avoids_taken(monkeypatch):
    _choices(monkeypatch, "aaaaaaaa", "bbbbbbbb")
    assert new_uid({"aaaaaaaa"}) == "bbbbbbbb"


@pytest.mark.parametrize("bad", ["../x", "ABCDEFGH", "abc", "abcdefghi", "abcd/efg"])
def test_camera_dir_rejects_bad_uid(tmp_path, bad):
    with pytest.raises(ValueError):
        camera_dir(tmp_path, bad)


def test_camera_dir(tmp_path):
    assert camera_dir(tmp_path, "k3f9x2ab") == tmp_path / "k3f9x2ab"


def test_meta_roundtrip(tmp_path):
    meta = CameraMeta("k3f9x2ab", "office", datetime(2026, 11, 3, 11, 40).astimezone())
    folder = tmp_path / "k3f9x2ab"
    write_meta(folder, meta)
    assert read_meta(folder) == meta
    assert json.loads((folder / "camera.json").read_text())["name"] == "office"


@pytest.mark.parametrize("content", [None, "{", '{"uid": "k3f9x2ab", "created": "x"}', "[]"])
def test_read_meta_missing_or_corrupt_is_none(tmp_path, content):
    if content is not None:
        (tmp_path / "camera.json").write_text(content)
    assert read_meta(tmp_path) is None


def test_ensure_folder_creates_and_renames(tmp_path):
    folder = ensure_folder(tmp_path, "k3f9x2ab", "office")
    assert folder == tmp_path / "k3f9x2ab"
    first = read_meta(folder)
    assert first is not None and first.name == "office" and first.uid == "k3f9x2ab"

    ensure_folder(tmp_path, "k3f9x2ab", "study")
    second = read_meta(folder)
    assert second == CameraMeta("k3f9x2ab", "study", first.created)


def test_ensure_folder_rewrites_unreadable_meta(tmp_path):
    folder = tmp_path / "k3f9x2ab"
    folder.mkdir()
    (folder / "camera.json").write_text("{")
    ensure_folder(tmp_path, "k3f9x2ab", "office")
    meta = read_meta(folder)
    assert meta is not None and meta.name == "office"


def test_folder_names(tmp_path):
    assert folder_names(tmp_path / "nope") == set()
    (tmp_path / "k3f9x2ab").mkdir()
    (tmp_path / "file.jpg").write_bytes(b"x")
    assert folder_names(tmp_path) == {"k3f9x2ab"}
