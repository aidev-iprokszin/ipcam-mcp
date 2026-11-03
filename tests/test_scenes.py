from datetime import datetime

from ipcam_mcp.scenes import capture_time, captures_in_scene, latest_in_scene, period_at
from ipcam_mcp.store import CameraMeta, Period

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
