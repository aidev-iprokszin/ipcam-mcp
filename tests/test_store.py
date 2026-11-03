import json
import os
import re
from datetime import datetime

import pytest

from ipcam_mcp import store
from ipcam_mcp.store import (
    UID_PATTERN,
    CameraMeta,
    Period,
    camera_dir,
    check_deletable,
    delete_folder,
    ensure_folder,
    enter_scene,
    folder_names,
    new_uid,
    read_meta,
    removed_cameras,
    usage,
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
    assert (second.name, second.created) == ("study", first.created)
    assert second.current_scene == "office"  # a rename keeps the scene


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


# --- removed cameras, usage, delete ---


def _camera(media, uid, name, created=datetime(2025, 1, 1, 12, 0)):
    folder = media / uid
    write_meta(folder, CameraMeta(uid, name, created.astimezone()))
    return folder


def test_usage_counts(tmp_path):
    folder = _camera(tmp_path, "aaaaaaaa", "office")
    (folder / "office_20261103-111200.jpg").write_bytes(b"x" * 100)
    (folder / "study_20261103-111300.jpg").write_bytes(b"x" * 100)
    (folder / "office_20261103-111400.mp4").write_bytes(b"x" * 1000)
    (folder / "notes.txt").write_bytes(b"x" * 10)
    (folder / "baselines").mkdir()
    (folder / "baselines" / "door.jpg").write_bytes(b"x" * 50)
    (folder / "baselines" / "door.json").write_text("{}")
    (folder / "baselines" / "orphan.json").write_text("{}")
    used = usage(folder, read_meta(folder))
    assert (used.snapshots, used.recordings, used.baselines) == (2, 1, 1)
    assert used.size == sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
    assert used.uid == "aaaaaaaa" and used.name == "office" and used.folder == folder


def test_removed_cameras_excludes_configured_and_unreadable(tmp_path):
    _camera(tmp_path, "bbbbbbbb", "garage", datetime(2025, 3, 1))
    _camera(tmp_path, "aaaaaaaa", "garage", datetime(2025, 1, 1))
    _camera(tmp_path, "cccccccc", "office")
    (tmp_path / "dddddddd").mkdir()  # no camera.json
    (tmp_path / "not-a-uid").mkdir()
    found = removed_cameras(tmp_path, {"cccccccc"})
    assert [(c.uid, c.name) for c in found] == [("aaaaaaaa", "garage"), ("bbbbbbbb", "garage")]
    assert removed_cameras(tmp_path / "missing", set()) == []


def test_delete_folder_removes(tmp_path):
    folder = _camera(tmp_path, "aaaaaaaa", "office")
    (folder / "baselines").mkdir()
    (folder / "baselines" / "door.jpg").write_bytes(b"x")
    assert delete_folder(tmp_path, "aaaaaaaa") == []
    assert not folder.exists()
    assert delete_folder(tmp_path, "aaaaaaaa") == []  # already gone


def test_delete_folder_refuses_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.jpg").write_bytes(b"x")
    media = tmp_path / "media"
    media.mkdir()
    (media / "aaaaaaaa").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        delete_folder(media, "aaaaaaaa")
    assert (outside / "keep.jpg").exists()


def test_delete_folder_refuses_bad_uid(tmp_path):
    with pytest.raises(ValueError):
        delete_folder(tmp_path, "../x")


def test_delete_folder_with_symlinked_media_dir(tmp_path):
    real = tmp_path / "disk"
    folder = _camera(real, "aaaaaaaa", "office")
    link = tmp_path / "media"
    link.symlink_to(real)
    assert delete_folder(link, "aaaaaaaa") == []
    assert not folder.exists()


def test_delete_folder_reports_leftovers(tmp_path, monkeypatch):
    folder = _camera(tmp_path, "aaaaaaaa", "office")
    stuck = folder / "office_20261103-111200.jpg"
    stuck.write_bytes(b"x")

    def deny(path, *a, **kw):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(store.os, "unlink", deny)
    assert delete_folder(tmp_path, "aaaaaaaa") == [stuck]


def test_delete_folder_keeps_camera_json_when_files_remain(tmp_path, monkeypatch):
    """So a removed camera with leftovers stays listed and delete_camera can finish later."""
    folder = _camera(tmp_path, "aaaaaaaa", "office")
    stuck = folder / "office_20261103-111200.jpg"
    stuck.write_bytes(b"x")
    (folder / "office_20261103-111300.jpg").write_bytes(b"x")
    real_unlink = os.unlink

    def deny(path, *a, **kw):
        if str(path).endswith("111200.jpg"):
            raise PermissionError(13, "Permission denied", str(path))
        real_unlink(path, *a, **kw)

    monkeypatch.setattr(store.os, "unlink", deny)
    left = delete_folder(tmp_path, "aaaaaaaa")
    assert stuck in left
    assert read_meta(folder).name == "office"
    assert [c.uid for c in removed_cameras(tmp_path, set())] == ["aaaaaaaa"]


def test_check_deletable(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    media = tmp_path / "media"
    (media / "aaaaaaaa").mkdir(parents=True)
    (media / "bbbbbbbb").symlink_to(outside)
    check_deletable(media, "aaaaaaaa")
    check_deletable(media, "cccccccc")  # missing: nothing to refuse
    with pytest.raises(ValueError, match="symlink"):
        check_deletable(media, "bbbbbbbb")


# --- scene periods ---

T0 = datetime(2026, 1, 10, 9, 0).astimezone()
T1 = datetime(2026, 11, 3, 13, 0).astimezone()


def test_meta_without_scenes_has_implicit_period(tmp_path):
    folder = tmp_path / "aaaaaaaa"
    folder.mkdir()
    (folder / "camera.json").write_text(
        json.dumps({"uid": "aaaaaaaa", "name": "office", "created": T0.isoformat()})
    )
    meta = read_meta(folder)
    assert meta.scenes == (Period("office", T0),)
    assert meta.current_scene == "office"


def test_meta_scenes_roundtrip(tmp_path):
    meta = CameraMeta(
        "aaaaaaaa", "door-left", T0, (Period("garage", T0), Period("front-door", T1, "bbbbbbbb"))
    )
    write_meta(tmp_path / "aaaaaaaa", meta)
    assert read_meta(tmp_path / "aaaaaaaa") == meta
    raw = json.loads((tmp_path / "aaaaaaaa" / "camera.json").read_text())
    assert raw["scenes"][0] == {"scene": "garage", "since": T0.isoformat()}
    assert raw["scenes"][1]["continues"] == "bbbbbbbb"
    assert meta.current_scene == "front-door"


@pytest.mark.parametrize(
    "scenes",
    [[{"scene": "x"}], [{"since": "2026-01-01T00:00:00"}], "nope", [{"scene": 5, "since": "x"}]],
)
def test_read_meta_bad_period_is_none(tmp_path, scenes):
    (tmp_path / "camera.json").write_text(
        json.dumps({"uid": "aaaaaaaa", "name": "o", "created": T0.isoformat(), "scenes": scenes})
    )
    assert read_meta(tmp_path) is None


def test_ensure_folder_first_period_uses_scene(tmp_path):
    folder = ensure_folder(tmp_path, "aaaaaaaa", "door-left", scene="front-door")
    assert read_meta(folder).current_scene == "front-door"
    ensure_folder(tmp_path, "aaaaaaaa", "door-l")  # rename keeps the scene
    meta = read_meta(folder)
    assert meta.name == "door-l" and meta.current_scene == "front-door"


def test_enter_scene_appends_and_is_idempotent(tmp_path):
    ensure_folder(tmp_path, "aaaaaaaa", "cam", scene="garage")
    meta = enter_scene(tmp_path, "aaaaaaaa", "cam", "front-door")
    assert [p.scene for p in meta.scenes] == ["garage", "front-door"]
    assert enter_scene(tmp_path, "aaaaaaaa", "cam", "front-door") == meta
    assert read_meta(tmp_path / "aaaaaaaa") == meta


def test_enter_scene_continues_on_current_scene_updates_last_period(tmp_path):
    ensure_folder(tmp_path, "aaaaaaaa", "cam", scene="garage")
    meta = enter_scene(tmp_path, "aaaaaaaa", "cam", "garage", continues="bbbbbbbb")
    assert len(meta.scenes) == 1 and meta.scenes[0].continues == "bbbbbbbb"


def test_enter_scene_starts_at_next_second(tmp_path, monkeypatch):
    """Capture names have whole seconds: a capture in the second of the move stays with the
    old scene, so a compare in the new scene never uses a frame of the old place."""
    ensure_folder(tmp_path, "aaaaaaaa", "cam", scene="garage")
    now = datetime(2026, 11, 3, 13, 0, 5).astimezone()
    monkeypatch.setattr(store, "_now", lambda: now)
    meta = enter_scene(tmp_path, "aaaaaaaa", "cam", "front-door")
    assert meta.scenes[-1].since == datetime(2026, 11, 3, 13, 0, 6).astimezone()


def _backups(folder):
    return sorted(p.name for p in folder.glob("camera.json.bad-*"))


def test_ensure_folder_sets_aside_corrupt_meta(tmp_path, monkeypatch):
    folder = tmp_path / "aaaaaaaa"
    folder.mkdir()
    (folder / "camera.json").write_text("{not json")
    now = datetime(2026, 11, 3, 13, 0, 5).astimezone()
    monkeypatch.setattr(store, "_now", lambda: now)
    ensure_folder(tmp_path, "aaaaaaaa", "cam")
    assert _backups(folder) == ["camera.json.bad-20261103-130005"]
    assert (folder / "camera.json.bad-20261103-130005").read_text() == "{not json"
    assert read_meta(folder).name == "cam"


def test_set_aside_keeps_earlier_backup(tmp_path, monkeypatch):
    folder = tmp_path / "aaaaaaaa"
    folder.mkdir()
    now = datetime(2026, 11, 3, 13, 0, 5).astimezone()
    monkeypatch.setattr(store, "_now", lambda: now)
    (folder / "camera.json.bad-20261103-130005").write_text("first")
    (folder / "camera.json").write_text("second")
    ensure_folder(tmp_path, "aaaaaaaa", "cam")
    assert _backups(folder) == [
        "camera.json.bad-20261103-130005",
        "camera.json.bad-20261103-130005-2",
    ]
    assert (folder / "camera.json.bad-20261103-130005").read_text() == "first"


def test_missing_meta_creates_no_backup(tmp_path):
    folder = ensure_folder(tmp_path, "aaaaaaaa", "cam")
    assert _backups(folder) == []
    assert read_meta(folder).name == "cam"


def test_read_meta_naive_times_are_local(tmp_path):
    folder = tmp_path / "aaaaaaaa"
    folder.mkdir()
    (folder / "camera.json").write_text(
        json.dumps(
            {
                "uid": "aaaaaaaa",
                "name": "cam",
                "created": "2026-11-01T10:00:00",
                "scenes": [{"scene": "cam", "since": "2026-11-01T10:00:00"}],
            }
        )
    )
    meta = read_meta(folder)
    assert meta.created == datetime(2026, 11, 1, 10).astimezone()
    assert meta.scenes[0].since.tzinfo is not None
