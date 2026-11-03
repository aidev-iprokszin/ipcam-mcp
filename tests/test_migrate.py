from datetime import date

from ipcam_mcp import migrate
from ipcam_mcp.migrate import migrate_flat_layout
from ipcam_mcp.store import read_meta

U = "k3f9x2ab"


def put(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def office_flat(m):
    put(m / "office_20261103-111200.jpg")
    put(m / "office_20261103-111200-2.jpg")
    put(m / "office_20261103-111300.mp4")
    put(m / "baselines" / "office" / "door.jpg")
    put(m / "baselines" / "office" / "door.json", b"{}")


def test_configured_camera_moves_into_its_uid(tmp_path):
    m = tmp_path / "media"
    office_flat(m)
    report = migrate_flat_layout(m, {"office": U})
    assert tree(m) == [
        f"{U}/baselines/door.jpg",
        f"{U}/baselines/door.json",
        f"{U}/camera.json",
        f"{U}/office_20261103-111200-2.jpg",
        f"{U}/office_20261103-111200.jpg",
        f"{U}/office_20261103-111300.mp4",
    ]
    assert read_meta(m / U).name == "office"
    assert not (m / "baselines").exists()
    assert report.moved == {"office": 5}
    assert report.removed_cameras == [] and report.error is None


def test_empty_legacy_baselines_folder_is_migrated_and_removed(tmp_path):
    m = tmp_path / "media"
    (m / "baselines" / "office").mkdir(parents=True)
    migrate_flat_layout(m, {"office": U})
    assert read_meta(m / U).name == "office"
    assert not (m / "baselines").exists()


def test_unconfigured_group_becomes_removed_camera(tmp_path):
    m = tmp_path / "media"
    put(m / "garage_20250101-000000.jpg")
    put(m / "garage_20250301-000000.jpg")
    report = migrate_flat_layout(m, {})
    (folder,) = [p for p in m.iterdir() if p.is_dir()]
    meta = read_meta(folder)
    assert meta.name == "garage" and meta.uid == folder.name
    assert meta.created.date() == date(2025, 1, 1)
    assert report.removed_cameras == ["garage"]


def test_names_with_underscore_group_correctly(tmp_path):
    m = tmp_path / "media"
    put(m / "office_test_20261103-111200.jpg")
    migrate_flat_layout(m, {"office": U})
    assert not (m / U).exists()
    (folder,) = [p for p in m.iterdir() if p.is_dir()]
    assert read_meta(folder).name == "office_test"


def test_unrelated_files_untouched(tmp_path):
    m = tmp_path / "media"
    put(m / "notes.txt")
    put(m / "office_notes.txt")
    put(m / "zzzzzzzz" / "camera.json", b"{}")
    put(m / "baselines" / "README")
    before = tree(m)
    report = migrate_flat_layout(m, {"office": U})
    assert tree(m) == before
    assert report.moved == {}


def test_idempotent(tmp_path):
    m = tmp_path / "media"
    office_flat(m)
    migrate_flat_layout(m, {"office": U})
    before = tree(m)
    report = migrate_flat_layout(m, {"office": U})
    assert report.moved == {} and tree(m) == before


def test_missing_or_empty_media_dir_noop(tmp_path):
    assert migrate_flat_layout(tmp_path / "missing", {"office": U}).moved == {}
    assert not (tmp_path / "missing").exists()
    (tmp_path / "empty").mkdir()
    assert migrate_flat_layout(tmp_path / "empty", {"office": U}).moved == {}
    assert list((tmp_path / "empty").iterdir()) == []


def test_existing_target_skipped_not_overwritten(tmp_path):
    m = tmp_path / "media"
    put(m / "office_20261103-111200.jpg", b"flat")
    put(m / U / "office_20261103-111200.jpg", b"already")
    report = migrate_flat_layout(m, {"office": U})
    assert (m / "office_20261103-111200.jpg").read_bytes() == b"flat"
    assert (m / U / "office_20261103-111200.jpg").read_bytes() == b"already"
    assert report.skipped == [m / "office_20261103-111200.jpg"]


def test_failed_move_stops_and_reports(tmp_path, monkeypatch):
    m = tmp_path / "media"
    office_flat(m)
    real_rename = migrate.os.rename
    calls = 0

    def flaky(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError(13, "Permission denied")
        real_rename(src, dst)

    monkeypatch.setattr(migrate.os, "rename", flaky)
    report = migrate_flat_layout(m, {"office": U})
    assert report.error is not None and "Permission denied" in report.error
    assert any(p.parent == m for p in m.glob("office_*"))  # rest still in the old layout

    monkeypatch.setattr(migrate.os, "rename", real_rename)
    report = migrate_flat_layout(m, {"office": U})
    assert report.error is None
    assert not list(m.glob("office_*")) and not (m / "baselines").exists()
    assert len(list((m / U).glob("office_*"))) == 3


def test_unwritable_target_reports_instead_of_raising(tmp_path, monkeypatch):
    m = tmp_path / "media"
    office_flat(m)

    def deny(folder, meta):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(migrate.store, "write_meta", deny)
    report = migrate_flat_layout(m, {"office": U})
    assert report.error is not None and "Permission denied" in report.error
    assert (m / "office_20261103-111200.jpg").exists()


def test_corrupt_camera_json_is_set_aside(tmp_path):
    m = tmp_path / "media"
    office_flat(m)
    put(m / U / "camera.json", b"{not json")
    migrate_flat_layout(m, {"office": U})
    backups = list((m / U).glob("camera.json.bad-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"{not json"
    assert read_meta(m / U).name == "office"
