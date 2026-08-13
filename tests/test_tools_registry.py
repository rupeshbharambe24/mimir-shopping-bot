from pathlib import Path

import pytest

from core import session, tools_registry


@pytest.fixture(autouse=True)
def temp_session(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / "s.json")
    monkeypatch.setattr(session, "_cache", None)
    yield


def test_schemas_are_wellformed():
    names = set()
    for schema in tools_registry.TOOL_SCHEMAS:
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"
        names.add(fn["name"])
    # Every gated tool must actually be declared
    assert tools_registry.GATED_TOOLS <= names


def test_gated_set_is_exactly_the_destructive_tools():
    assert tools_registry.GATED_TOOLS == frozenset({
        "write_file", "delete_file", "run_script",
        "shell_command", "kill_process",
    })


async def test_cd_updates_session_and_reports_windows_path(monkeypatch):
    monkeypatch.setattr(
        tools_registry.file_ops, "validate_path",
        lambda p, cwd=None: (True, Path("/mnt/d/Projects")),
    )
    monkeypatch.setattr(tools_registry, "_is_dir", lambda p: True)
    out = await tools_registry.execute("cd", {"path": r"D:\Projects"}, chat_id=7)
    assert session.get_cwd(7) == "/mnt/d/Projects"
    assert r"D:\Projects" in out


async def test_cd_rejects_missing_directory(monkeypatch):
    monkeypatch.setattr(
        tools_registry.file_ops, "validate_path",
        lambda p, cwd=None: (True, Path("/mnt/d/nope")),
    )
    monkeypatch.setattr(tools_registry, "_is_dir", lambda p: False)
    out = await tools_registry.execute("cd", {"path": "nope"}, chat_id=7)
    assert "not a directory" in out.lower()
    assert session.get_cwd(7) != "/mnt/d/nope"


async def test_list_dir_defaults_to_session_cwd(monkeypatch):
    session.set_cwd(7, "/mnt/d/Projects")
    seen = {}

    def fake_list_dir(path, **kw):
        seen["path"] = path
        return "(empty directory)"

    monkeypatch.setattr(tools_registry.file_ops, "list_dir", fake_list_dir)
    await tools_registry.execute("list_dir", {}, chat_id=7)
    assert seen["path"] == "/mnt/d/Projects"


async def test_unknown_tool_returns_error_string_not_raise():
    out = await tools_registry.execute("no_such_tool", {}, chat_id=7)
    assert "unknown tool" in out.lower()


async def test_tool_exception_is_caught_and_returned(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(tools_registry.file_ops, "list_dir", boom)
    out = await tools_registry.execute("list_dir", {"path": "/mnt/d"}, chat_id=7)
    assert "disk on fire" in out


async def test_find_files_reports_windows_paths(monkeypatch):
    monkeypatch.setattr(
        tools_registry.file_ops, "find_files",
        lambda q, **kw: [Path("/mnt/d/Projects/a.py")],
    )
    out = await tools_registry.execute("find_files", {"query": "a"}, chat_id=7)
    assert r"D:\Projects\a.py" in out


async def test_find_files_requires_query():
    out = await tools_registry.execute("find_files", {}, chat_id=7)
    assert "needs a query" in out
