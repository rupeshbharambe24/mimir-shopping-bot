"""Auto-approve: the one feature that removes a tap from the money path.

Every test here is about it failing CLOSED. The default is off, the operator can
forbid it outright, an unreadable total is never auto-approved, and there are two
independent ceilings -- per order and per day -- because a per-order limit alone
bounds nothing. An agent stuck retrying checkout places many small orders, not
one large one, and ten auto-approved ₹400 orders is ₹4,000 nobody tapped for.
"""

import datetime

import pytest

from config import Config
from core import bot, session

CHAT = 4242


@pytest.fixture(autouse=True)
def temp_session(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / "s.json")
    monkeypatch.setattr(session, "_cache", None)
    monkeypatch.setattr(Config, "COMMERCE_AUTOPAY_ENABLED", True)
    monkeypatch.setattr(Config, "COMMERCE_AUTOPAY_MAX_RUPEES", 500)
    monkeypatch.setattr(Config, "COMMERCE_AUTOPAY_DAILY_RUPEES", 1500)
    yield


def _on(limit):
    session.set_autopay(CHAT, enabled=True, limit_rupees=limit)


# ── off by default, and the operator wins ────────────────────────────

def test_off_unless_the_user_turns_it_on():
    ok, _ = bot._autopay_decision(CHAT, 10_00)
    assert ok is False
    assert session.get_autopay(CHAT)["enabled"] is False


def test_operator_switch_overrides_a_saved_preference(monkeypatch):
    """A stored setting must not outlive the operator's decision to allow it."""
    _on(500)
    assert bot._autopay_decision(CHAT, 100_00)[0] is True

    monkeypatch.setattr(Config, "COMMERCE_AUTOPAY_ENABLED", False)
    ok, _ = bot._autopay_decision(CHAT, 100_00)
    assert ok is False


# ── the ceilings ─────────────────────────────────────────────────────

@pytest.mark.parametrize("rupees,expected", [(1, True), (499, True), (500, True), (501, False)])
def test_per_order_limit_is_inclusive_and_binding(rupees, expected):
    _on(500)
    assert bot._autopay_decision(CHAT, rupees * 100)[0] is expected


def test_over_the_limit_says_why():
    _on(200)
    ok, note = bot._autopay_decision(CHAT, 350_00)
    assert ok is False
    assert "350" in note and "200" in note


def test_a_user_cannot_raise_the_limit_past_the_env_ceiling():
    session.set_autopay(CHAT, enabled=True, limit_rupees=999_999)
    # The stored value is whatever was asked for; the COMMAND clamps it. What
    # matters is that the decision never exceeds the operator's daily ceiling.
    ok, _ = bot._autopay_decision(CHAT, 5000_00)
    assert ok is False, "a stored limit above the daily cap must still be refused"


# ── the daily cap ────────────────────────────────────────────────────

def test_daily_cap_stops_a_run_of_small_orders():
    """The reason a per-order limit is not enough on its own."""
    _on(500)
    approved = 0
    for _ in range(10):
        ok, _note = bot._autopay_decision(CHAT, 400_00)
        if not ok:
            break
        session.record_autopay_spend(CHAT, 400)
        approved += 1
    assert approved == 3, f"{approved} x ₹400 auto-approved against a ₹1500 daily cap"
    assert session.autopay_remaining_today(CHAT, 1500) == 300


def test_toggling_off_and_on_does_not_reset_the_days_total():
    """Otherwise the daily cap is bypassed by flicking the switch."""
    _on(500)
    session.record_autopay_spend(CHAT, 1200)
    session.set_autopay(CHAT, enabled=False, limit_rupees=0)
    _on(500)
    assert session.autopay_remaining_today(CHAT, 1500) == 300


def test_the_daily_total_rolls_over(monkeypatch):
    _on(500)
    session.record_autopay_spend(CHAT, 1500)
    assert session.autopay_remaining_today(CHAT, 1500) == 0

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    monkeypatch.setattr(session, "_today", lambda: tomorrow.isoformat())
    assert session.autopay_remaining_today(CHAT, 1500) == 1500


# ── the uncertainty case ─────────────────────────────────────────────

def test_an_unreadable_total_is_never_auto_approved():
    """None means "we could not read the price", not "it is free".

    Several provider manifests still carry # VERIFY on their total field, so
    this is a live possibility rather than a theoretical one. Spending an amount
    nobody can state is exactly what must not happen without a human looking.
    """
    _on(500)
    ok, note = bot._autopay_decision(CHAT, None)
    assert ok is False
    assert "could not read" in note.lower()


def test_zero_is_read_as_a_price_not_as_missing():
    _on(500)
    assert bot._autopay_decision(CHAT, 0)[0] is True


# ── what it does and does not claim ──────────────────────────────────

def test_the_user_is_told_payment_is_not_automated():
    """Swiggy's MCP exposes no wallet. Auto-approve skips the tap; it cannot
    make a UPI payment happen by itself, and saying otherwise would set up a
    user to expect an order that never completes."""
    _on(500)
    ok, note = bot._autopay_decision(CHAT, 100_00)
    assert ok is True
    assert "upi" in note.lower() or "cash" in note.lower()


def test_service_choice_starts_unset_and_persists():
    assert session.get_service(CHAT) is None
    session.set_service(CHAT, "swiggy_food")
    assert session.get_service(CHAT) == "swiggy_food"


def test_every_offered_service_is_a_real_provider_id():
    """The picker must not offer an id providers.yaml has never heard of.

    Read with a regex rather than PyYAML on purpose: yaml is a commerce-side
    dependency and the two virtualenvs are separate so a provider-SDK bump
    cannot break the bot. Importing it here would quietly undo that.
    """
    import re
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[1] / "agentic-commerce" / "providers.yaml"
    ).read_text(encoding="utf-8")
    known = set(re.findall(r"^\s*-\s*id:\s*(\S+)", text, re.MULTILINE))
    assert known, "could not parse any provider ids"
    for pid, *_ in bot.SERVICES:
        assert pid in known, f"/service offers {pid!r}, which is not a registered provider"
