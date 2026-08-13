import json

from core.llm_cascade import SmartLLM


def test_parse_groq_tool_call():
    data = {"choices": [{"message": {
        "content": None,
        "tool_calls": [{
            "id": "call_1",
            "function": {"name": "cd", "arguments": json.dumps({"path": "D:/Projects"})},
        }],
    }}]}
    out = SmartLLM._parse_groq(data)
    assert out["tool_calls"] == [{"id": "call_1", "name": "cd", "args": {"path": "D:/Projects"}}]
    assert out["text"] is None


def test_parse_groq_plain_text():
    data = {"choices": [{"message": {"content": "all done"}}]}
    out = SmartLLM._parse_groq(data)
    assert out["text"] == "all done"
    assert out["tool_calls"] == []


def test_parse_groq_malformed_arguments_degrade_to_empty():
    data = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c", "function": {"name": "cd", "arguments": "{not json"}},
    ]}}]}
    out = SmartLLM._parse_groq(data)
    assert out["tool_calls"][0]["args"] == {}


def test_parse_gemini_function_call():
    data = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "list_dir", "args": {"path": "/mnt/d"}}},
    ]}}]}
    out = SmartLLM._parse_gemini(data)
    assert out["tool_calls"][0]["name"] == "list_dir"
    assert out["tool_calls"][0]["args"] == {"path": "/mnt/d"}


def test_parse_gemini_text():
    data = {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}
    out = SmartLLM._parse_gemini(data)
    assert out["text"] == "hello"
    assert out["tool_calls"] == []


def test_parse_ollama_tool_call():
    data = {"message": {"content": "", "tool_calls": [
        {"function": {"name": "cd", "arguments": {"path": "/mnt/d"}}},
    ]}}
    out = SmartLLM._parse_ollama(data)
    assert out["tool_calls"][0]["name"] == "cd"
    assert out["tool_calls"][0]["args"] == {"path": "/mnt/d"}


def test_parse_ollama_text():
    data = {"message": {"content": "done"}}
    out = SmartLLM._parse_ollama(data)
    assert out["text"] == "done"


def test_parse_ollama_string_arguments_are_decoded():
    data = {"message": {"content": "", "tool_calls": [
        {"function": {"name": "cd", "arguments": json.dumps({"path": "/mnt/d"})}},
    ]}}
    out = SmartLLM._parse_ollama(data)
    assert out["tool_calls"][0]["args"] == {"path": "/mnt/d"}


async def test_ask_with_tools_raises_when_all_providers_fail(monkeypatch):
    llm = SmartLLM()

    async def boom(messages, tools):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm, "_tools_groq", boom)
    monkeypatch.setattr(llm, "_tools_gemini", boom)
    monkeypatch.setattr(llm, "_tools_ollama", boom)

    import pytest
    with pytest.raises(RuntimeError, match="All providers failed"):
        await llm.ask_with_tools([], [])


async def test_ask_with_tools_falls_through_to_second_provider(monkeypatch):
    llm = SmartLLM()

    async def boom(messages, tools):
        raise RuntimeError("groq down")

    async def ok(messages, tools):
        return {"text": "from gemini", "tool_calls": []}

    monkeypatch.setattr(llm, "_tools_groq", boom)
    monkeypatch.setattr(llm, "_tools_gemini", ok)

    result = await llm.ask_with_tools([], [])
    assert result["text"] == "from gemini"
    assert llm.get_status() == "Gemini"
