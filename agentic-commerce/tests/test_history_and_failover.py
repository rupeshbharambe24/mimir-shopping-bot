"""Why the Gemini quota kept blowing, and why Groq was not catching it.

The 429 names the quota precisely:

    quotaId: GenerateContentInputTokensPerModelPerMinute-FreeTier
    quotaDimensions: {"model": "gemini-3.5-flash-lite"}
    limit: 250000

It is an INPUT-token limit, per model, per minute. History was never trimmed,
so every turn re-uploaded the whole conversation including each product-search
payload verbatim. Ten searches in, a single "add it to cart" was sending tens
of thousands of tokens of already-answered questions. The quota was exhausted
by repetition, not by use.

And GROQ_API_KEY never applied here at all: the cascade lives in the bot's
SmartLLM, while the commerce agent is Gemini-only. Because the quota is per
MODEL, a second Gemini model is a genuine fallback with its own bucket.
"""

import pytest

from backend.agent import loop


class FakePart:
    def __init__(self, text=None, function_response=None):
        self.text = text
        self.function_response = function_response


class FakeContent:
    def __init__(self, role, parts):
        self.role = role
        self.parts = parts


def user_turn(text):
    return FakeContent("user", [FakePart(text=text)])


def model_turn(text="ok"):
    return FakeContent("model", [FakePart(text=text)])


def tool_result():
    """Appended with role="user" too -- which is why role alone cannot
    distinguish it from something the human typed."""
    return FakeContent("user", [FakePart(function_response={"result": "{}"})])


# ── telling a real turn from a tool result ───────────────────────────

def test_a_tool_result_is_not_mistaken_for_a_user_turn():
    assert loop._is_real_user_turn(user_turn("add it")) is True
    assert loop._is_real_user_turn(tool_result()) is False
    assert loop._is_real_user_turn(model_turn()) is False


# ── trimming ─────────────────────────────────────────────────────────

def _conversation(n):
    """n complete cycles of: user asks -> model calls a tool -> result -> reply."""
    h = []
    for i in range(n):
        h += [user_turn(f"q{i}"), model_turn(), tool_result(), model_turn()]
    return h


def test_short_conversations_are_left_alone():
    h = _conversation(3)
    before = list(h)
    loop.trim_history(h, max_user_turns=6)
    assert h == before


def test_long_conversations_keep_only_the_recent_turns():
    h = _conversation(20)
    loop.trim_history(h, max_user_turns=6)
    kept = [e for e in h if loop._is_real_user_turn(e)]
    assert len(kept) == 6
    assert kept[0].parts[0].text == "q14"
    assert kept[-1].parts[0].text == "q19"


def test_the_cut_never_orphans_a_function_response():
    """Cutting mid-sequence leaves a function_response whose function_call is
    gone, and Gemini rejects that outright -- turning a token problem into a
    hard 400."""
    h = _conversation(20)
    loop.trim_history(h, max_user_turns=4)
    assert loop._is_real_user_turn(h[0]), "history must begin at a real user turn"


def test_it_trims_in_place():
    """Session.history is held by reference; returning a new list would leave
    the caller still holding the untrimmed one."""
    h = _conversation(20)
    same = h
    loop.trim_history(h, max_user_turns=2)
    assert same is h
    assert len(h) < 80


def test_trimming_can_be_disabled():
    h = _conversation(20)
    loop.trim_history(h, max_user_turns=0)
    assert len([e for e in h if loop._is_real_user_turn(e)]) == 20


# ── model failover ───────────────────────────────────────────────────

@pytest.mark.parametrize("msg,expected", [
    ("429 RESOURCE_EXHAUSTED quota exceeded", True),
    ("You exceeded your current quota", True),
    ("400 INVALID_ARGUMENT missing field", False),
    ("Connection reset", False),
])
def test_only_rate_limits_are_treated_as_failover_worthy(msg, expected):
    assert loop._is_rate_limited(Exception(msg)) is expected


async def test_a_rate_limited_model_falls_through_to_the_next():
    tried = []

    class FakeModels:
        async def generate_content(self, *, model, contents, config):
            tried.append(model)
            if len(tried) == 1:
                raise Exception("429 RESOURCE_EXHAUSTED quota exceeded")
            return "answer"

    class FakeClient:
        aio = type("aio", (), {"models": FakeModels()})()

    out = await loop._generate_with_failover(FakeClient(), contents=[], config=None)
    assert out == "answer"
    assert len(tried) == 2 and tried[0] == loop.DEFAULT_MODEL


async def test_a_bad_request_is_not_retried_across_models():
    """It would fail identically everywhere and burn three quotas instead of one."""
    tried = []

    class FakeModels:
        async def generate_content(self, *, model, contents, config):
            tried.append(model)
            raise Exception("400 INVALID_ARGUMENT missing field")

    class FakeClient:
        aio = type("aio", (), {"models": FakeModels()})()

    with pytest.raises(Exception, match="INVALID_ARGUMENT"):
        await loop._generate_with_failover(FakeClient(), contents=[], config=None)
    assert len(tried) == 1


def test_fallback_models_are_distinct_from_the_primary():
    """Retrying the same model hits the same per-model bucket."""
    models = [loop.DEFAULT_MODEL] + [
        m for m in loop.MODEL_FALLBACKS if m != loop.DEFAULT_MODEL
    ]
    assert len(models) >= 2
    assert len(set(models)) == len(models)
