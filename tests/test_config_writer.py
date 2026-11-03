import os
import stat
import tomllib
from pathlib import Path

import pytest
from dotenv import dotenv_values

from ipcam_mcp import config_writer, store
from ipcam_mcp.config_writer import (
    add_section,
    atomic_write,
    ensure_uids,
    remove_section,
    rename_section,
    set_key,
    write_secrets_file,
)
from ipcam_mcp.errors import ConfigError

EXAMPLE = Path(__file__).parent.parent / "cameras.example.toml"


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    p = tmp_path / "cameras.toml"
    p.write_text(EXAMPLE.read_text())
    return p


def _cams(p: Path) -> dict:
    return tomllib.loads(p.read_text())["cameras"]


def test_add_preserves_comments(cfg):
    add_section(cfg, "garage", {"type": "tapo_onvif", "host_var": "HOST"})
    assert "# Copy to" in cfg.read_text()
    assert _cams(cfg)["garage"]["host_var"] == "HOST"
    assert "livingroom" in _cams(cfg)


def test_add_creates_cameras_table(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('media_dir = "/m"\n')
    add_section(p, "a", {"host_var": "H"})
    assert tomllib.loads(p.read_text()) == {"media_dir": "/m", "cameras": {"a": {"host_var": "H"}}}


def test_add_creates_missing_file_and_private_dir(tmp_path):
    p = tmp_path / "new" / "cameras.toml"
    add_section(p, "a", {"host_var": "H"})
    assert tomllib.loads(p.read_text()) == {"cameras": {"a": {"host_var": "H"}}}
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_add_duplicate_rejected(cfg):
    add_section(cfg, "garage", {"host_var": "HOST"})
    before = cfg.read_bytes()
    with pytest.raises(ConfigError):
        add_section(cfg, "garage", {"host_var": "HOST"})
    assert cfg.read_bytes() == before


def test_rename_keeps_values_and_applies_updates(cfg):
    before = _cams(cfg)["livingroom"]
    rename_section(cfg, "livingroom", "office", {"secrets_file": "/x/office.env"})
    cams = _cams(cfg)
    assert "livingroom" not in cams
    assert cams["office"]["secrets_file"] == "/x/office.env"
    for k, v in before.items():
        if k != "secrets_file":
            assert cams["office"][k] == v
    assert "# Tapo" in cfg.read_text()


def test_rename_keeps_position(cfg):
    add_section(cfg, "garage", {"host_var": "H"})
    rename_section(cfg, "livingroom", "office")
    assert list(_cams(cfg)) == ["office", "garage"]


def test_rename_errors(cfg):
    add_section(cfg, "garage", {"host_var": "H"})
    before = cfg.read_bytes()
    with pytest.raises(ConfigError):
        rename_section(cfg, "nope", "x")
    with pytest.raises(ConfigError):
        rename_section(cfg, "livingroom", "garage")
    assert cfg.read_bytes() == before


def test_remove(cfg):
    add_section(cfg, "garage", {"host_var": "H"})
    top = {k: v for k, v in tomllib.loads(cfg.read_text()).items() if k != "cameras"}
    remove_section(cfg, "livingroom")
    data = tomllib.loads(cfg.read_text())
    assert list(data["cameras"]) == ["garage"]
    assert {k: v for k, v in data.items() if k != "cameras"} == top
    with pytest.raises(ConfigError):
        remove_section(cfg, "livingroom")


@pytest.mark.parametrize("tail", ["", "\n", "\n\n"])
def test_add_then_remove_restores_original_bytes(tmp_path, tail):
    cfg = tmp_path / "cameras.toml"
    cfg.write_text('# top\n[cameras.a]\ntype = "t"  # c\n' + tail)
    original = cfg.read_bytes()
    add_section(cfg, "garage", {"host_var": "H"})
    remove_section(cfg, "garage")
    assert cfg.read_bytes() == original


def test_remove_middle_section_touches_only_its_lines(tmp_path):
    cfg = tmp_path / "cameras.toml"
    head = '# top\n[cameras.a]\ntype = "t"\n\n# about b\n'
    middle = "[cameras.b]\nx = 1\n\n"
    tail = "[cameras.c]\ny = 2  # keep\n\n# end\n"
    cfg.write_text(head + middle + tail)
    remove_section(cfg, "b")
    assert cfg.read_text() == head + tail


def test_atomic_write_keeps_mode_and_original_on_failure(tmp_path, monkeypatch):
    p = tmp_path / "f.toml"
    p.write_text("old")
    p.chmod(0o640)
    atomic_write(p, "new")
    assert p.read_text() == "new"
    assert p.stat().st_mode & 0o777 == 0o640

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(config_writer.os, "replace", boom)
    with pytest.raises(ConfigError):
        atomic_write(p, "newer")
    assert p.read_text() == "new"
    assert [x.name for x in tmp_path.iterdir()] == ["f.toml"]


def test_secrets_file_mode_and_exclusive(tmp_path):
    p = tmp_path / "sub" / "cam.env"
    write_secrets_file(p, {"HOST": "1.2.3.4", "USER": "", "PASSWORD": "x"})
    assert p.stat().st_mode & 0o777 == 0o600
    assert p.parent.stat().st_mode & 0o777 == 0o700
    assert dotenv_values(p)["USER"] == ""
    with pytest.raises(ConfigError):
        write_secrets_file(p, {"HOST": "x"})


@pytest.mark.parametrize(
    "password",
    ["p#ss", "a=b", 'q"uote', "s'q", "sp ace", "$HOME", "${HOME}", "back\\slash", "x${HOME}y"],
)
def test_secrets_round_trip(tmp_path, password):
    p = tmp_path / "s.env"
    write_secrets_file(p, {"USER": "u", "PASSWORD": password})
    values = dotenv_values(p, interpolate=False)
    assert values["PASSWORD"] == password
    assert values["USER"] == "u"
    assert os.stat(p).st_mode & 0o777 == 0o600


def test_secrets_write_failure_removes_file(tmp_path, monkeypatch):
    p = tmp_path / "s.env"
    real_fdopen = os.fdopen

    class FailingFile:
        def __init__(self, f):
            self._f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()

        def write(self, data):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(
        config_writer.os, "fdopen", lambda *a, **k: FailingFile(real_fdopen(*a, **k))
    )
    with pytest.raises(ConfigError):
        write_secrets_file(p, {"PASSWORD": "x"})
    assert not p.exists()
    monkeypatch.undo()
    write_secrets_file(p, {"PASSWORD": "x"})
    assert p.exists()


def test_atomic_write_through_symlink_keeps_link(tmp_path):
    real_dir = tmp_path / "dotfiles"
    real_dir.mkdir()
    target = real_dir / "cameras.toml"
    target.write_text("old")
    link_dir = tmp_path / "config"
    link_dir.mkdir()
    link = link_dir / "cameras.toml"
    link.symlink_to(target)

    atomic_write(link, "new")

    assert link.is_symlink()
    assert os.readlink(link) == str(target)
    assert target.read_text() == "new"
    assert [x.name for x in real_dir.iterdir()] == ["cameras.toml"]
    assert [x.name for x in link_dir.iterdir()] == ["cameras.toml"]


def test_atomic_write_new_file(tmp_path):
    p = tmp_path / "new.toml"
    atomic_write(p, "data")
    assert p.read_text() == "data"
    assert [x.name for x in tmp_path.iterdir()] == ["new.toml"]


def test_atomic_write_fsyncs_before_replace(tmp_path, monkeypatch):
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append("fsync")
        real_fsync(fd)

    def replace(src, dst):
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(config_writer.os, "fsync", fsync)
    monkeypatch.setattr(config_writer.os, "replace", replace)
    atomic_write(tmp_path / "f.toml", "data")
    assert events[:2] == ["fsync", "replace"]


# --- ensure_uids ---


def _uids(p: Path) -> dict:
    return {name: spec.get("uid") for name, spec in _cams(p).items()}


def test_ensure_uids_fills_only_missing_and_keeps_comments(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('# top\n[cameras.a]\nuid = "aaaaaaaa"\n\n# about b\n[cameras.b]\ntype = "t"\n')
    assigned = ensure_uids(p, set())
    assert list(assigned) == ["b"]
    assert _uids(p) == {"a": "aaaaaaaa", "b": assigned["b"]}
    assert "# about b" in p.read_text() and "# top" in p.read_text()


def test_ensure_uids_avoids_reserved_and_existing(tmp_path, monkeypatch):
    chars = iter("ccccccccaaaaaaaadddddddd")
    monkeypatch.setattr(store.secrets, "choice", lambda _alphabet: next(chars))
    p = tmp_path / "c.toml"
    p.write_text('[cameras.a]\nuid = "aaaaaaaa"\n[cameras.b]\ntype = "t"\n')
    assert ensure_uids(p, {"cccccccc"}) == {"b": "dddddddd"}


def test_ensure_uids_noop_does_not_write(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[cameras.a]\nuid = "aaaaaaaa"\n')
    before = p.stat().st_mtime_ns
    assert ensure_uids(p, set()) == {}
    assert ensure_uids(tmp_path / "missing.toml", set()) == {}
    assert p.stat().st_mtime_ns == before
    assert not (tmp_path / "missing.toml").exists()


def test_ensure_uids_unwritable(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('[cameras.b]\ntype = "t"\n')
    before = p.read_bytes()

    def fail(path, data):
        raise ConfigError(f"could not write {path}: Permission denied")

    monkeypatch.setattr(config_writer, "atomic_write", fail)
    with pytest.raises(ConfigError, match="must be writable"):
        ensure_uids(p, set())
    assert p.read_bytes() == before


def test_ensure_uids_cameras_not_a_table(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("cameras = 5\n")
    with pytest.raises(ConfigError, match="'cameras' must be a table"):
        ensure_uids(p, set())


def test_ensure_uids_skips_non_table_section(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[cameras]\ngarage = "x"\n')
    assert ensure_uids(p, set()) == {}  # load_config reports it


def test_set_key_keeps_comments(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('# top\n[cameras.a]\ntype = "t"  # keep\n')
    set_key(p, "a", "scene", "front-door")
    assert _cams(p)["a"] == {"type": "t", "scene": "front-door"}
    assert "# keep" in p.read_text() and "# top" in p.read_text()
    with pytest.raises(ConfigError, match="not found"):
        set_key(p, "nope", "scene", "x")
