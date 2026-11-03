import json
from datetime import datetime

import pytest

from ipcam_mcp.config import DeviceConfig
from ipcam_mcp.scenes import (
    baselines_in_scene,
    capture_time,
    captures_in_scene,
    latest_in_scene,
    load_scene_baseline,
    parse_bound,
    period_at,
    scene_index,
    timeline,
)
from ipcam_mcp.store import CameraMeta, Period, write_meta

T0 = datetime(2026, 1, 10, 9, 0).astimezone()
T1 = datetime(2026, 11, 3, 13, 0).astimezone()
META = CameraMeta("aaaaaaaa", "cam", T0, (Period("garage", T0), Period("front-door", T1)))


def put(folder, name, data=b"x"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return folder / name


def test_capture_time_parses_and_rejects(tmp_path):
    assert (
        capture_time(tmp_path / "cam_20261103-130500.jpg")
        == datetime(2026, 11, 3, 13, 5).astimezone()
    )
    assert (
        capture_time(tmp_path / "cam_20261103-130500-2.mp4")
        == datetime(2026, 11, 3, 13, 5).astimezone()
    )
    assert capture_time(tmp_path / "notes.txt") is None
    assert capture_time(tmp_path / "cam_20261399-000000.jpg") is None


def test_period_at_before_first_is_first():
    assert period_at(META, datetime(2025, 1, 1).astimezone()).scene == "garage"
    assert period_at(META, datetime(2026, 5, 1).astimezone()).scene == "garage"
    assert period_at(META, datetime(2026, 11, 4).astimezone()).scene == "front-door"


def test_captures_in_scene_splits_by_period(tmp_path):
    old = put(tmp_path, "cam_20250101-000000.jpg")  # before the first period
    mid = put(tmp_path, "cam_20260601-120000.mp4")
    new = put(tmp_path, "cam_20261104-080000.jpg")
    put(tmp_path, "cam_20261104-090000.jpg", b"")  # reservation
    garage = [p for _, p in captures_in_scene(tmp_path, META, "garage")]
    assert garage == [mid, old]
    assert [p for _, p in captures_in_scene(tmp_path, META, "front-door")] == [new]
    assert [p for _, p in captures_in_scene(tmp_path, META, "garage", kinds=("jpg",))] == [old]


def test_latest_in_scene_counter_order_and_empty_skip(tmp_path):
    put(tmp_path, "cam_20261104-080000.jpg")
    want = put(tmp_path, "cam_20261104-080000-10.jpg")
    put(tmp_path, "cam_20261104-080000-2.jpg")
    put(tmp_path, "cam_20261104-090000.jpg", b"")
    put(tmp_path, "cam_20261104-100000.mp4")
    assert latest_in_scene(tmp_path, META, "front-door") == want
    assert latest_in_scene(tmp_path, META, "elsewhere") is None
    assert latest_in_scene(tmp_path / "missing", META, "front-door") is None


def test_latest_in_scene_ignores_name_prefix(tmp_path):
    """A uid folder holds captures taken under earlier names too (renames)."""
    put(tmp_path, "office_20261103-100000.jpg")
    want = put(tmp_path, "study_20261103-110000.jpg")
    put(tmp_path, "office_20261103-115959.mp4")
    put(tmp_path, "notes_x.jpg")
    assert latest_in_scene(tmp_path, META, "garage") == want
    assert latest_in_scene(tmp_path / "missing", META, "garage") is None


def baseline(folder, name, taken, scene=None):
    d = folder / "baselines"
    put(d, f"{name}.jpg")
    data = {"taken": taken.isoformat(), "quality": "high", "position": None}
    if scene is not None:
        data["scene"] = scene
    (d / f"{name}.json").write_text(json.dumps(data))


def test_baselines_in_scene_legacy_uses_period(tmp_path):
    baseline(tmp_path, "old", datetime(2026, 5, 1).astimezone())  # legacy: no scene, garage era
    baseline(tmp_path, "new", datetime(2026, 11, 4).astimezone())  # legacy: front-door era
    baseline(tmp_path, "tagged", datetime(2026, 11, 4).astimezone(), scene="garage")
    assert baselines_in_scene(tmp_path, META, "garage") == ["old", "tagged"]
    assert baselines_in_scene(tmp_path, META, "front-door") == ["new"]


def test_load_scene_baseline_ignores_other_scene(tmp_path):
    baseline(tmp_path, "door", T1, scene="garage")
    baseline(tmp_path, "gate", T1, scene="front-door")
    assert load_scene_baseline(tmp_path, META, "front-door", "gate").name == "gate"
    with pytest.raises(ValueError, match=r"baseline 'door' not found; available: gate$"):
        load_scene_baseline(tmp_path, META, "front-door", "door")


# --- scene index and timeline ---


def cam(name, uid, scene):
    return DeviceConfig(id=name, type="fake", host="10.0.0.9", user="u", password="p",
                        uid=uid, scene=scene)  # fmt: skip


def media(tmp_path):
    """door-l (aaaaaaaa): garage until T1, then front-door. door-r (bbbbbbbb): front-door.
    old-door (cccccccc): removed, front-door. shed (dddddddd): removed, only scene 'shed'."""
    write_meta(tmp_path / "aaaaaaaa", META)
    put(tmp_path / "aaaaaaaa", "cam_20260501-100000.jpg")  # garage
    put(tmp_path / "aaaaaaaa", "door-l_20261104-100000.jpg")  # front-door
    put(tmp_path / "aaaaaaaa", "door-l_20261104-110000.mp4")  # front-door
    baseline(tmp_path / "aaaaaaaa", "gate", datetime(2026, 11, 4, 12).astimezone(), "front-door")
    write_meta(
        tmp_path / "bbbbbbbb", CameraMeta("bbbbbbbb", "door-r", T1, (Period("front-door", T1),))
    )
    put(tmp_path / "bbbbbbbb", "door-r_20261105-090000.jpg")
    old = datetime(2025, 1, 1).astimezone()
    write_meta(
        tmp_path / "cccccccc", CameraMeta("cccccccc", "old-door", old, (Period("front-door", old),))
    )
    put(tmp_path / "cccccccc", "old-door_20250102-080000.jpg")
    write_meta(tmp_path / "dddddddd", CameraMeta("dddddddd", "shed", old, (Period("shed", old),)))
    put(tmp_path / "dddddddd", "shed_20250103-080000.jpg")
    put(tmp_path / "eeeeeeee", "x_20250103-080000.jpg")  # no camera.json: ignored
    return {
        "door-l": cam("door-l", "aaaaaaaa", "front-door"),
        "door-r": cam("door-r", "bbbbbbbb", "front-door"),
        "porch": cam("porch", "ffffffff", "porch"),  # configured, no folder yet
    }


def test_scene_index_configured_and_removed(tmp_path):
    index = scene_index(tmp_path, media(tmp_path))
    assert sorted(index) == ["front-door", "garage", "porch", "shed"]
    front = index["front-door"]
    assert front.cameras == ["door-l", "door-r"]
    assert front.removed == [("old-door", "cccccccc")]
    assert (front.snapshots, front.recordings, front.baselines) == (3, 1, 1)
    assert front.first == datetime(2025, 1, 2, 8).astimezone()
    assert front.last == datetime(2026, 11, 5, 9).astimezone()
    garage = index["garage"]
    assert garage.cameras == [] and garage.removed == []  # door-l left it; it is not removed
    assert (garage.snapshots, garage.recordings, garage.baselines) == (1, 0, 0)
    porch = index["porch"]
    assert porch.cameras == ["porch"] and porch.first is None and porch.snapshots == 0


def test_scene_index_removed_only_scene(tmp_path):
    shed = scene_index(tmp_path, media(tmp_path))["shed"]
    assert shed.cameras == [] and shed.removed == [("shed", "dddddddd")]
    assert shed.snapshots == 1


def test_timeline_range_limit_and_more(tmp_path):
    configured = media(tmp_path)
    entries, more = timeline(tmp_path, configured, "front-door", None, None, 50)
    assert [(e.camera, e.kind) for e in entries] == [
        ("door-r", "snapshot"),
        ("door-l", "baseline"),
        ("door-l", "recording"),
        ("door-l", "snapshot"),
        ("old-door", "snapshot"),
    ]
    assert more == 0
    entries, more = timeline(tmp_path, configured, "front-door", None, None, 2)
    assert [e.camera for e in entries] == ["door-r", "door-l"] and more == 3
    start = datetime(2026, 11, 4, 10, 30).astimezone()
    end = datetime(2026, 11, 4, 23, 59).astimezone()
    entries, more = timeline(tmp_path, configured, "front-door", start, end, 50)
    assert [e.kind for e in entries] == ["baseline", "recording"] and more == 0


def test_timeline_marks_removed_and_baselines(tmp_path):
    entries, _ = timeline(tmp_path, media(tmp_path), "front-door", None, None, 50)
    removed = [e for e in entries if e.removed]
    assert [(e.camera, e.path.name) for e in removed] == [
        ("old-door", "old-door_20250102-080000.jpg")
    ]
    base = next(e for e in entries if e.kind == "baseline")
    assert base.path == tmp_path / "aaaaaaaa" / "baselines" / "gate.jpg"
    assert base.time == datetime(2026, 11, 4, 12).astimezone()


def test_parse_bound_date_and_datetime_and_invalid():
    assert parse_bound("2026-11-03", end=False) == datetime(2026, 11, 3).astimezone()
    assert (
        parse_bound("2026-11-03", end=True)
        == datetime(2026, 11, 3, 23, 59, 59, 999999).astimezone()
    )
    assert parse_bound("2026-11-03T13:05", end=True) == datetime(2026, 11, 3, 13, 5).astimezone()
    aware = parse_bound("2026-11-03T13:05:00+00:00", end=False)
    assert aware.utcoffset().total_seconds() == 0
    with pytest.raises(
        ValueError, match="invalid date 'yesterday': use YYYY-MM-DD or an ISO datetime"
    ):
        parse_bound("yesterday", end=False)
