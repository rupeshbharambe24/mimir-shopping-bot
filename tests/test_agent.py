import pytest

from core import agent, session


class FakeLLM:
    """Returns canned responses in order, recording what it was asked."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def ask_with_tools(self, messages, tools):
        self.calls.append(list(messages))
        if not self._responses:
            return {"text": "done", "tool_calls": []}
        return self._responses.pop(0)


class FakeGuard:
    def __init__(self, approve=True):
        self.approve = approve
        self.requests = []

    def needs_permission(self, operation):
        return True

    async def request_permission(self, bot, chat_id, operation, description, details=""):
        self.requests.append((operation, description))
        return self.approve


@pytest.fixture(autouse=True)
def temp_session(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / "s.json")
    monkeypatch.setattr(session, "_cache", None)
    yield


async def test_single_tool_then_answer(monkeypatch):
    executed = []

    async def fake_execute(name, args, chat_id):
        executed.append((name, args))
        return "Folder listing here"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    llm = FakeLLM([
        {"text": None, "tool_calls": [{"id": "1", "name": "list_dir", "args": {"path": "/mnt/d"}}]},
        {"text": "Here are your folders.", "tool_calls": []},
    ])
    out = await agent.run(bot=None, chat_id=7, text="list d drive", llm=llm, guard=FakeGuard())

    assert executed == [("list_dir", {"path": "/mnt/d"})]
    assert out == "Here are your folders."


async def test_chained_tools(monkeypatch):
    executed = []

    async def fake_execute(name, args, chat_id):
        executed.append(name)
        return "ok"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    llm = FakeLLM([
        {"text": None, "tool_calls": [{"id": "1", "name": "cd", "args": {"path": "D:/Projects"}}]},
        {"text": None, "tool_calls": [{"id": "2", "name": "list_dir", "args": {}}]},
        {"text": "Done.", "tool_calls": []},
    ])
    await agent.run(
        bot=None, chat_id=7,
        text="go to d drive projects and list", llm=llm, guard=FakeGuard(),
    )
    assert executed == ["cd", "list_dir"]


async def test_gated_tool_requires_approval(monkeypatch):
    executed = []

    async def fake_execute(name, args, chat_id):
        executed.append(name)
        return "deleted"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)
    guard = FakeGuard(approve=False)

    llm = FakeLLM([
        {"text": None, "tool_calls": [
            {"id": "1", "name": "delete_file", "args": {"path": "/mnt/d/x.txt"}},
        ]},
        {"text": "I did not delete it.", "tool_calls": []},
    ])
    await agent.run(bot=None, chat_id=7, text="delete x", llm=llm, guard=guard)

    assert guard.requests, "guard should have been consulted"
    assert executed == [], "denied tool must never execute"


async def test_approved_gated_tool_executes(monkeypatch):
    executed = []

    async def fake_execute(name, args, chat_id):
        executed.append(name)
        return "deleted"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    llm = FakeLLM([
        {"text": None, "tool_calls": [
            {"id": "1", "name": "delete_file", "args": {"path": "/mnt/d/x.txt"}},
        ]},
        {"text": "Deleted.", "tool_calls": []},
    ])
    await agent.run(
        bot=None, chat_id=7, text="delete x", llm=llm, guard=FakeGuard(approve=True),
    )
    assert executed == ["delete_file"]


async def test_read_tool_is_never_gated(monkeypatch):
    async def fake_execute(name, args, chat_id):
        return "ok"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)
    guard = FakeGuard()

    llm = FakeLLM([
        {"text": None, "tool_calls": [{"id": "1", "name": "list_dir", "args": {}}]},
        {"text": "There.", "tool_calls": []},
    ])
    await agent.run(bot=None, chat_id=7, text="ls", llm=llm, guard=guard)
    assert guard.requests == [], "navigation must not prompt for permission"


async def test_denied_tool_is_not_retried(monkeypatch):
    async def fake_execute(name, args, chat_id):
        return "ok"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)
    guard = FakeGuard(approve=False)

    call = {"id": "1", "name": "delete_file", "args": {"path": "/mnt/d/x.txt"}}
    llm = FakeLLM([
        {"text": None, "tool_calls": [dict(call)]},
        {"text": None, "tool_calls": [dict(call)]},  # model stubbornly retries
        {"text": "Fine.", "tool_calls": []},
    ])
    await agent.run(bot=None, chat_id=7, text="delete x", llm=llm, guard=guard)

    assert len(guard.requests) == 1, "must not re-prompt for an identical denied call"


async def test_iteration_cap(monkeypatch):
    async def fake_execute(name, args, chat_id):
        return "again"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    class Looping:
        """Always asks for another tool, never answers."""

        async def ask_with_tools(self, messages, tools):
            return {"text": None, "tool_calls": [{"id": "x", "name": "list_dir", "args": {}}]}

    out = await agent.run(bot=None, chat_id=7, text="loop", llm=Looping(), guard=FakeGuard())
    assert "could not finish" in out.lower()


async def test_all_providers_down_raises():
    class Dead:
        async def ask_with_tools(self, messages, tools):
            raise RuntimeError("All providers failed")

    with pytest.raises(RuntimeError):
        await agent.run(bot=None, chat_id=7, text="hi", llm=Dead(), guard=FakeGuard())


async def test_cwd_is_in_system_prompt(monkeypatch):
    async def fake_execute(name, args, chat_id):
        return "ok"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)
    session.set_cwd(7, "/mnt/d/Projects")

    llm = FakeLLM([{"text": "hi", "tool_calls": []}])
    await agent.run(bot=None, chat_id=7, text="where am i", llm=llm, guard=FakeGuard())

    system = llm.calls[0][0]["content"]
    assert r"D:\Projects" in system


async def test_tool_results_are_fed_back_to_the_model(monkeypatch):
    async def fake_execute(name, args, chat_id):
        return "PROJECT_LISTING_MARKER"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    llm = FakeLLM([
        {"text": None, "tool_calls": [{"id": "1", "name": "list_dir", "args": {}}]},
        {"text": "Done.", "tool_calls": []},
    ])
    await agent.run(bot=None, chat_id=7, text="ls", llm=llm, guard=FakeGuard())

    second_turn = llm.calls[1]
    tool_messages = [m for m in second_turn if m.get("role") == "tool"]
    assert tool_messages and tool_messages[0]["content"] == "PROJECT_LISTING_MARKER"


async def test_large_tool_results_are_truncated_before_resending(monkeypatch):
    """Untruncated listings get resent each turn and exhaust Groq's TPM budget."""
    async def fake_execute(name, args, chat_id):
        return "x" * (agent.MAX_TOOL_RESULT_CHARS + 5000)

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    llm = FakeLLM([
        {"text": None, "tool_calls": [{"id": "1", "name": "list_dir", "args": {}}]},
        {"text": "Done.", "tool_calls": []},
    ])
    await agent.run(bot=None, chat_id=7, text="ls", llm=llm, guard=FakeGuard())

    tool_msg = [m for m in llm.calls[1] if m.get("role") == "tool"][0]
    assert len(tool_msg["content"]) < agent.MAX_TOOL_RESULT_CHARS + 200
    assert "truncated" in tool_msg["content"]


async def test_small_tool_results_are_not_truncated(monkeypatch):
    async def fake_execute(name, args, chat_id):
        return "short listing"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    llm = FakeLLM([
        {"text": None, "tool_calls": [{"id": "1", "name": "list_dir", "args": {}}]},
        {"text": "Done.", "tool_calls": []},
    ])
    await agent.run(bot=None, chat_id=7, text="ls", llm=llm, guard=FakeGuard())

    tool_msg = [m for m in llm.calls[1] if m.get("role") == "tool"][0]
    assert tool_msg["content"] == "short listing"
