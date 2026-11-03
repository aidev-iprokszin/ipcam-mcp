import json
from datetime import datetime

import pytest

from ipcam_mcp.baselines import list_baselines, load_baseline, save_baseline


def touch(d, name, data=b"x"):
    p = d / name
    p.write_bytes(data)
    return p


def test_save_load_roundtrip(tmp_path):
    snap = touch(tmp_path, "cam_20261103-100000.jpg", b"jpegdata")
    saved = save_baseline(tmp_path, "door-closed", snap, "high", (0.1, -0.2), scene="cam")
    loaded = load_baseline(tmp_path, "door-closed")
    assert loaded.position == (0.1, -0.2)
    assert loaded.quality == "high"
    assert loaded.taken == saved.taken
    assert loaded.path.read_bytes() == b"jpegdata"
    data = json.loads(loaded.path.with_suffix(".json").read_text())
    assert set(data) == {"taken", "quality", "position", "scene"}
    assert list_baselines(tmp_path) == ["door-closed"]


def test_null_position(tmp_path):
    snap = touch(tmp_path, "cam_20261103-100000.jpg")
    save_baseline(tmp_path, "a", snap, "low", None)
    assert load_baseline(tmp_path, "a").position is None


def test_save_replaces(tmp_path):
    save_baseline(tmp_path, "a", touch(tmp_path, "s1.jpg", b"one"), "high", None)
    save_baseline(tmp_path, "a", touch(tmp_path, "s2.jpg", b"two"), "low", (0.0, 0.0))
    b = load_baseline(tmp_path, "a")
    assert b.path.read_bytes() == b"two"
    assert b.quality == "low"
    assert list_baselines(tmp_path) == ["a"]


@pytest.mark.parametrize("name", ["../x", "A", "", "a/b", "door\n"])
def test_invalid_name(tmp_path, name):
    snap = touch(tmp_path, "s.jpg")
    with pytest.raises(ValueError):
        save_baseline(tmp_path, name, snap, "high", None)
    with pytest.raises(ValueError):
        load_baseline(tmp_path, name)
    assert not (tmp_path / "baselines").exists()


def test_load_missing_lists_available(tmp_path):
    snap = touch(tmp_path, "s.jpg")
    save_baseline(tmp_path, "one", snap, "high", None)
    save_baseline(tmp_path, "two", snap, "high", None)
    with pytest.raises(ValueError, match="available: one, two"):
        load_baseline(tmp_path, "nope")


def test_save_records_scene(tmp_path):
    snap = touch(tmp_path, "cam_20261103-100000.jpg")
    saved = save_baseline(tmp_path, "door", snap, "high", None, scene="garage")
    assert saved.scene == "garage"
    assert load_baseline(tmp_path, "door").scene == "garage"
    assert json.loads((tmp_path / "baselines" / "door.json").read_text())["scene"] == "garage"


def test_naive_taken_is_local(tmp_path):
    save_baseline(tmp_path, "a", touch(tmp_path, "s.jpg"), "high", None)
    meta_path = tmp_path / "baselines" / "a.json"
    data = json.loads(meta_path.read_text())
    data["taken"] = "2026-11-01T10:00:00"
    meta_path.write_text(json.dumps(data))
    assert load_baseline(tmp_path, "a").taken == datetime(2026, 11, 1, 10).astimezone()
