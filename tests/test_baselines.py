import json

import pytest

from ipcam_mcp.baselines import latest_snapshot, list_baselines, load_baseline, save_baseline


def touch(d, name, data=b"x"):
    p = d / name
    p.write_bytes(data)
    return p


def test_latest_snapshot_ignores_prefix_ids(tmp_path):
    touch(tmp_path, "office_20261103-100000.jpg")
    want = touch(tmp_path, "office_20261103-110000.jpg")
    touch(tmp_path, "office_test_20261103-120000.jpg")
    assert latest_snapshot(tmp_path, "office") == want


def test_latest_none(tmp_path):
    touch(tmp_path, "office_test_20261103-120000.jpg")
    assert latest_snapshot(tmp_path, "office") is None
    assert latest_snapshot(tmp_path / "missing", "office") is None


def test_save_load_roundtrip(tmp_path):
    snap = touch(tmp_path, "cam_20261103-100000.jpg", b"jpegdata")
    saved = save_baseline(tmp_path, "cam", "door-closed", snap, "high", (0.1, -0.2))
    loaded = load_baseline(tmp_path, "cam", "door-closed")
    assert loaded.position == (0.1, -0.2)
    assert loaded.quality == "high"
    assert loaded.taken == saved.taken
    assert loaded.path.read_bytes() == b"jpegdata"
    data = json.loads(loaded.path.with_suffix(".json").read_text())
    assert set(data) == {"taken", "quality", "position"}
    assert list_baselines(tmp_path, "cam") == ["door-closed"]


def test_null_position(tmp_path):
    snap = touch(tmp_path, "cam_20261103-100000.jpg")
    save_baseline(tmp_path, "cam", "a", snap, "low", None)
    assert load_baseline(tmp_path, "cam", "a").position is None


def test_save_replaces(tmp_path):
    save_baseline(tmp_path, "cam", "a", touch(tmp_path, "s1.jpg", b"one"), "high", None)
    save_baseline(tmp_path, "cam", "a", touch(tmp_path, "s2.jpg", b"two"), "low", (0.0, 0.0))
    b = load_baseline(tmp_path, "cam", "a")
    assert b.path.read_bytes() == b"two"
    assert b.quality == "low"
    assert list_baselines(tmp_path, "cam") == ["a"]


@pytest.mark.parametrize("name", ["../x", "A", "", "a/b", "door\n"])
def test_invalid_name(tmp_path, name):
    snap = touch(tmp_path, "s.jpg")
    with pytest.raises(ValueError):
        save_baseline(tmp_path, "cam", name, snap, "high", None)
    with pytest.raises(ValueError):
        load_baseline(tmp_path, "cam", name)
    assert not (tmp_path / "baselines").exists()


def test_load_missing_lists_available(tmp_path):
    snap = touch(tmp_path, "s.jpg")
    save_baseline(tmp_path, "cam", "one", snap, "high", None)
    save_baseline(tmp_path, "cam", "two", snap, "high", None)
    with pytest.raises(ValueError, match="available: one, two"):
        load_baseline(tmp_path, "cam", "nope")


def test_latest_snapshot_counts_same_second_suffix_as_newer(tmp_path):
    for name in ["cam_20261102-170509.jpg", "cam_20261102-170509-2.jpg",
                 "cam_20261102-170509-10.jpg", "cam_20261102-170508.jpg"]:  # fmt: skip
        (tmp_path / name).write_bytes(b"x")
    assert latest_snapshot(tmp_path, "cam") == tmp_path / "cam_20261102-170509-10.jpg"


def test_latest_snapshot_skips_empty_files(tmp_path):
    want = touch(tmp_path, "cam_20261102-170508.jpg")
    touch(tmp_path, "cam_20261102-170509.jpg", b"")  # in-progress or crashed capture
    assert latest_snapshot(tmp_path, "cam") == want
