"""Tests for the agentic-commerce integration.

These deliberately do NOT require the BFF to be running: the HTTP layer is
stubbed so the tests pin down MIMIR's side of the contract -- the safety
interlocks, the gating, and what the user is actually shown before money moves.
The live round-trip against a real BFF is covered separately.
"""

import pytest

from config import Config
from core import agent, guard as guard_mod, session, tools_registry
from tools import commerce


@pytest.fixture(autouse=True)
def temp_session(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / "s.json")
    monkeypatch.setattr(session, "_cache", None)
    monkeypatch.setattr(Config, "TELEGRAM_CHAT_ID", 7)
    yield


# ── tool surface ─────────────────────────────────────────────────

COMMERCE_TOOLS = {
    "commerce_providers", "commerce_select", "commerce_connect",
    "commerce_search", "commerce_cart", "commerce_add_to_cart",
    "commerce_clear_cart", "commerce_checkout", "commerce_order_status",
}


def test_all_commerce_tools_are_declared():
    names = {s["function"]["name"] for s in tools_registry.TOOL_SCHEMAS}
    assert COMMERCE_TOOLS <= names


def test_only_checkout_is_gated():
    """Gating cart edits would make ordering unusable; leaving checkout ungated
    would let the agent spend without asking. The line goes at money."""
    gated = COMMERCE_TOOLS & tools_registry.GATED_TOOLS
    assert gated == {"commerce_checkout"}


def test_checkout_is_a_write_op_for_the_guard():
    """Guard.needs_permission() returns False for anything not in WRITE_OPS, so
    a tool missing from that set is auto-approved with no prompt at all."""
    assert guard_mod.Guard().needs_permission("commerce_checkout") is True


def test_checkout_schema_warns_the_model_about_money():
    schema = next(
        s for s in tools_registry.TOOL_SCHEMAS
        if s["function"]["name"] == "commerce_checkout"
    )
    assert "real money" in schema["function"]["description"].lower()


async def test_checkout_is_not_dispatchable_through_the_registry():
    """It must go through agent.py, which owns the approval prompt. If the
    registry ever executed it directly, the gate would be bypassed."""
    out = await tools_registry.execute("commerce_checkout", {}, chat_id=7)
    assert "must be handled by the agent loop" in out


# ── the two-switch money interlock ───────────────────────────────

async def test_dry_run_on_permits_the_flow(monkeypatch):
    monkeypatch.setattr(commerce, "health",
                        _fake(lambda *a, **kw: {"ok": True, "dry_run": "true"}))
    assert await commerce.live_spending_blocked(7) is None


async def test_live_bff_without_mimirs_opt_in_is_refused(monkeypatch):
    """The BFF alone deciding to go live is not enough."""
    monkeypatch.setattr(commerce, "health",
                        _fake(lambda *a, **kw: {"ok": True, "dry_run": "false"}))
    monkeypatch.setattr(Config, "COMMERCE_ALLOW_LIVE", False)
    blocked = await commerce.live_spending_blocked(7)
    assert blocked is not None
    assert "COMMERCE_ALLOW_LIVE" in blocked


async def test_live_spending_needs_both_switches(monkeypatch):
    monkeypatch.setattr(commerce, "health",
                        _fake(lambda *a, **kw: {"ok": True, "dry_run": "false"}))
    monkeypatch.setattr(Config, "COMMERCE_ALLOW_LIVE", True)
    assert await commerce.live_spending_blocked(7) is None


async def test_unreachable_service_blocks_spending(monkeypatch):
    monkeypatch.setattr(commerce, "health",
                        _fake(lambda *a, **kw: {"ok": False, "error": "connection refused"}))
    assert await commerce.live_spending_blocked(7) is not None


# ── the approval prompt ──────────────────────────────────────────

class RecordingGuard:
    def __init__(self, decision):
        self.decision = decision
        self.prompts = []

    async def request_permission(self, bot, chat_id, op, description, details=""):
        self.prompts.append({"op": op, "description": description, "details": details})
        return self.decision


_INTENT = {
    "ok": True,
    "confirm_token": "tok-abc",
    "provider_id": "mock",
    "summary": "2 x Toned Milk @ ₹33.00\nTotal: ₹66.00",
    "total_display": "₹66.00",
    "dry_run": True,
}


def _fake(fn):
    async def _inner(*a, **kw):
        return fn(*a, **kw)
    return _inner


def _patch_intent(monkeypatch, spent: list):
    monkeypatch.setattr(commerce, "live_spending_blocked", _fake(lambda *a, **kw: None))
    monkeypatch.setattr(commerce, "checkout_intent", _fake(lambda *a, **kw: dict(_INTENT)))

    async def _checkout(chat_id, token):
        spent.append(token)
        return "Order created: X-1"
    monkeypatch.setattr(commerce, "checkout", _checkout)


async def test_approval_prompt_shows_the_total_and_line_items(monkeypatch):
    """Approving a bare 'commerce_checkout' would tell the user nothing about
    what they are buying, so the prompt is built from a real cart snapshot."""
    spent: list = []
    _patch_intent(monkeypatch, spent)
    g = RecordingGuard(decision=True)

    await agent._commerce_checkout(bot=None, chat_id=7, guard=g)

    assert len(g.prompts) == 1
    prompt = g.prompts[0]
    assert prompt["op"] == "commerce_checkout"
    assert "₹66.00" in prompt["description"]
    assert "Toned Milk" in prompt["details"]


async def test_approval_spends_with_the_minted_token(monkeypatch):
    spent: list = []
    _patch_intent(monkeypatch, spent)
    await agent._commerce_checkout(bot=None, chat_id=7, guard=RecordingGuard(True))
    assert spent == ["tok-abc"]


async def test_denial_spends_nothing(monkeypatch):
    spent: list = []
    _patch_intent(monkeypatch, spent)
    out = await agent._commerce_checkout(bot=None, chat_id=7, guard=RecordingGuard(False))
    assert spent == []
    assert "denied" in out.lower()


async def test_blocked_spending_never_reaches_the_prompt(monkeypatch):
    """If the interlock says no, the user should not be asked to approve
    something that was never going to be allowed."""
    spent: list = []
    _patch_intent(monkeypatch, spent)
    monkeypatch.setattr(
        commerce, "live_spending_blocked", _fake(lambda *a, **kw: "REFUSING: unsafe"),
    )
    g = RecordingGuard(decision=True)
    out = await agent._commerce_checkout(bot=None, chat_id=7, guard=g)
    assert g.prompts == []
    assert spent == []
    assert "REFUSING" in out


async def test_failed_intent_never_prompts(monkeypatch):
    spent: list = []
    _patch_intent(monkeypatch, spent)
    monkeypatch.setattr(
        commerce, "checkout_intent",
        _fake(lambda *a, **kw: {"ok": False, "error": "Cart is empty; nothing to check out."}),
    )
    g = RecordingGuard(decision=True)
    out = await agent._commerce_checkout(bot=None, chat_id=7, guard=g)
    assert g.prompts == []
    assert spent == []
    assert "Cart is empty" in out


# ── transport errors surface as text, never exceptions ───────────

async def test_unreachable_service_returns_actionable_text(monkeypatch):
    """tools_registry's contract: executors return strings. An exception here
    is unrecoverable for the model."""
    def _boom(*a, **kw):
        raise ConnectionError("nope")
    monkeypatch.setattr(commerce.aiohttp, "ClientSession", _boom)
    out = await tools_registry.execute("commerce_cart", {}, chat_id=7)
    assert isinstance(out, str)
    assert "Commerce error" in out or "not running" in out


async def test_add_to_cart_requires_a_product_id():
    out = await tools_registry.execute("commerce_add_to_cart", {}, chat_id=7)
    assert "product_id" in out


async def test_search_requires_a_query():
    out = await tools_registry.execute("commerce_search", {}, chat_id=7)
    assert "needs a query" in out
