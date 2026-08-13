import json

import pytest

from config import Config
from core import session


@pytest.fixture(autouse=True)
def temp_session_file(tmp_path, monkeypatch):
    """Point the module at a throwaway file and clear its cache each test."""
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(session, "_cache", None)
    yield


def test_defaults_to_workspace():
    assert session.get_cwd(123) == str(Config.WORKSPACE)


def test_set_then_get():
    session.set_cwd(123, "/mnt/d/Projects")
    assert session.get_cwd(123) == "/mnt/d/Projects"


def test_chats_are_isolated():
    session.set_cwd(1, "/mnt/d/one")
    session.set_cwd(2, "/mnt/d/two")
    assert session.get_cwd(1) == "/mnt/d/one"
    assert session.get_cwd(2) == "/mnt/d/two"


def test_survives_reload(monkeypatch):
    session.set_cwd(123, "/mnt/d/Projects")
    monkeypatch.setattr(session, "_cache", None)  # simulate restart
    assert session.get_cwd(123) == "/mnt/d/Projects"


def test_corrupt_file_degrades_to_default(monkeypatch):
    session.SESSION_FILE.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(session, "_cache", None)
    assert session.get_cwd(123) == str(Config.WORKSPACE)


def test_reset_restores_default():
    session.set_cwd(123, "/mnt/d/Projects")
    session.reset(123)
    assert session.get_cwd(123) == str(Config.WORKSPACE)


def test_write_is_atomic_no_temp_left_behind():
    session.set_cwd(123, "/mnt/d/Projects")
    leftovers = list(session.SESSION_FILE.parent.glob("*.tmp"))
    assert leftovers == []


def test_file_contents_are_readable_json():
    session.set_cwd(123, "/mnt/d/Projects")
    data = json.loads(session.SESSION_FILE.read_text(encoding="utf-8"))
    assert data["123"]["cwd"] == "/mnt/d/Projects"
