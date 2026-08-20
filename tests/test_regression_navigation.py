"""The exact messages that failed before LLM navigation existed.

Each asserts the tool sequence a correct model would produce is actually
executed, and that path arguments survive normalization intact.
"""

import pytest

from core import agent, paths, session
from conftest import ALLOWED_ROOT, wsl_only

CWD = f"{ALLOWED_ROOT}/ai-agent"


@pytest.fixture(autouse=True)
def temp_session(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / "s.json")
    monkeypatch.setattr(session, "_cache", None)
    from config import Config
    monkeypatch.setattr(Config, "TELEGRAM_CHAT_ID", 7)
    yield


class ScriptedLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    async def ask_with_tools(self, messages, tools):
        return self._responses.pop(0)


class AlwaysApprove:
    async def request_permission(self, *a, **kw):
        return True


@wsl_only
@pytest.mark.parametrize("raw,expected", [
    # "list all folders in D:/Projects" used to yield "/Projects"
    ("D:/Projects", "/mnt/d/Projects"),
    # "go to d drive and projects folder" used to yield "/mnt/d/it"
    ("d drive projects", "/mnt/d/projects"),
    # "show me all folders in d drive projects" used to drop "projects"
    (r"D:\Projects", "/mnt/d/Projects"),
])
def test_paths_that_used_to_be_mangled(raw, expected):
    assert paths.normalize(raw, CWD) == expected


async def test_go_to_d_drive_projects_and_list(monkeypatch):
    """The original failing message, end to end through the loop."""
    executed = []

    async def fake_execute(name, args, chat_id):
        executed.append((name, args))
        return "ok"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    llm = ScriptedLLM([
        {"text": None, "tool_calls": [
            {"id": "1", "name": "cd", "args": {"path": r"D:\Projects"}},
        ]},
        {"text": None, "tool_calls": [
            {"id": "2", "name": "list_dir", "args": {}},
        ]},
        {"text": "Those are the folders in D:\\Projects.", "tool_calls": []},
    ])

    out = await agent.run(
        bot=None, chat_id=7,
        text="go to d drive and projects folder and send me list of all folders in it",
        llm=llm, guard=AlwaysApprove(),
    )

    assert [name for name, _ in executed] == ["cd", "list_dir"]
    assert "D:\\Projects" in out


async def test_open_in_explorer_is_not_a_directory_listing(monkeypatch):
    """'open d drive projects in explorer' used to classify as list_dir."""
    executed = []

    async def fake_execute(name, args, chat_id):
        executed.append(name)
        return "Opened D:\\Projects in Explorer."

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    llm = ScriptedLLM([
        {"text": None, "tool_calls": [
            {"id": "1", "name": "open_explorer", "args": {"path": "d drive projects"}},
        ]},
        {"text": "Opened it.", "tool_calls": []},
    ])

    await agent.run(
        bot=None, chat_id=7,
        text="open d drive projects in explorer",
        llm=llm, guard=AlwaysApprove(),
    )
    assert executed == ["open_explorer"]


async def test_bare_ls_follows_cd_not_workspace(monkeypatch):
    """'/ls' used to always list the ai-agent folder regardless of context."""
    seen = {}

    def fake_list_dir(path, **kw):
        seen["path"] = path
        return "(empty directory)"

    from core import tools_registry
    monkeypatch.setattr(tools_registry.file_ops, "list_dir", fake_list_dir)
    monkeypatch.setattr(tools_registry, "_is_dir", lambda p: True)

    target = f"{ALLOWED_ROOT}/Projects"
    await tools_registry.execute("cd", {"path": target}, chat_id=7)
    await tools_registry.execute("list_dir", {}, chat_id=7)

    assert seen["path"] == target
    assert "ai-agent" not in seen["path"]
