from pathlib import Path

from tools import file_ops


def test_windows_path_resolves_to_wsl_mount():
    ok, target = file_ops.validate_path(r"D:\Projects")
    assert ok is True
    assert target == Path("/mnt/d/Projects")


def test_windows_path_is_not_treated_as_workspace_relative():
    """The old bug: D:\\Projects silently became WORKSPACE/D:\\Projects."""
    _, target = file_ops.validate_path(r"D:\Projects")
    assert "ai-agent" not in str(target)
    assert str(target).startswith("/mnt/d")


def test_spoken_drive_resolves():
    ok, target = file_ops.validate_path("d drive projects")
    assert ok is True
    assert target == Path("/mnt/d/projects")


def test_relative_path_resolves_against_supplied_cwd():
    ok, target = file_ops.validate_path("core", cwd="/mnt/d/Projects/raven")
    assert ok is True
    assert target == Path("/mnt/d/Projects/raven/core")


def test_empty_path_rejected():
    ok, _ = file_ops.validate_path("")
    assert ok is False


def test_path_outside_allowed_roots_rejected():
    ok, _ = file_ops.validate_path("/etc/shadow")
    assert ok is False


def test_symlink_escaping_allowed_root_is_rejected(tmp_path, monkeypatch):
    """A symlink planted inside an allowed root must not grant access outside it.

    The lexical normalizer alone cannot see through symlinks, so validate_path
    has to dereference before the prefix check.
    """
    from config import Config

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified", encoding="utf-8")

    (allowed / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(Config, "ALLOWED_PATHS", [allowed])

    ok, _ = file_ops.validate_path(str(allowed / "escape" / "secret.txt"))
    assert ok is False, "symlink out of an allowed root must be denied"


def test_symlink_staying_inside_allowed_root_is_permitted(tmp_path, monkeypatch):
    from config import Config

    allowed = tmp_path / "allowed"
    (allowed / "real").mkdir(parents=True)
    (allowed / "real" / "ok.txt").write_text("fine", encoding="utf-8")
    (allowed / "link").symlink_to(allowed / "real", target_is_directory=True)
    monkeypatch.setattr(Config, "ALLOWED_PATHS", [allowed])

    ok, _ = file_ops.validate_path(str(allowed / "link" / "ok.txt"))
    assert ok is True, "a symlink that stays inside the root is legitimate"


def test_nonexistent_path_inside_allowed_root_still_validates(tmp_path, monkeypatch):
    """write_file targets files that do not exist yet."""
    from config import Config

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(Config, "ALLOWED_PATHS", [allowed])

    ok, _ = file_ops.validate_path(str(allowed / "brand_new.txt"))
    assert ok is True
