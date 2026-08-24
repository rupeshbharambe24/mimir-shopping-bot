"""Dineout booking, and what the payment picker is allowed to claim.

A table booking is not a purchase, and treating it as one breaks in both
directions: /checkout/intent has no cart to read, so booking would block
forever; and auto-approve is bounded in rupees, so a free booking measures zero
against every ceiling and would be waved through without limit.

Swiggy's docs say a booking cannot be cancelled through the MCP. "It costs
nothing" is not the same as "it is safe to do unasked".
"""

import pytest

from config import Config
from core import bot, session

CHAT = 909


@pytest.fixture(autouse=True)
def temp_session(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / "s.json")
    monkeypatch.setattr(session, "_cache", None)
    yield


# ── the approval describes the actual booking ────────────────────────

def test_the_headline_names_place_time_and_party_size():
    """An approval that says only "book a table" authorises any table."""
    line = bot._booking_headline({
        "name": "book_table",
        "args": {"restaurantName": "Toit", "slotTime": "20:00", "partySize": 4},
    })
    assert "Toit" in line and "20:00" in line and "4" in line


def test_a_headline_survives_missing_fields():
    line = bot._booking_headline({"name": "book_table", "args": {}})
    assert "table" in line.lower()


def test_an_unknown_action_still_names_itself():
    line = bot._booking_headline({"name": "some_new_tool", "args": {}})
    assert "some_new_tool" in line


# ── booking is never auto-approved ───────────────────────────────────

def test_a_free_booking_is_not_eligible_for_auto_approve(monkeypatch):
    """The routing, not the amount, is what keeps bookings out of autopay.

    A ₹0 booking passes every rupee ceiling trivially, so if it reached
    _autopay_decision it would be approved without limit. It must go down the
    _confirm_action path instead, which always asks.
    """
    monkeypatch.setattr(Config, "COMMERCE_AUTOPAY_ENABLED", True)
    session.set_autopay(CHAT, enabled=True, limit_rupees=500)

    # Demonstrates the hazard: zero sails through the money gate.
    assert bot._autopay_decision(CHAT, 0)[0] is True

    # So the booking path must not consult it at all.
    import inspect
    src = inspect.getsource(bot._confirm_action)
    assert "_autopay_decision" not in src, (
        "bookings must not route through auto-approve: every rupee ceiling "
        "reads zero for a free reservation"
    )
    assert "request_permission" in src


def test_booking_uses_its_own_permission_action_name():
    """Distinct from commerce_checkout so an approval for one is not an
    approval for the other."""
    import inspect
    src = inspect.getsource(bot._confirm_action)
    assert "commerce_booking" in src


# ── the payment picker must not promise what the rail cannot do ──────

def test_the_payment_picker_offers_only_what_swiggy_returns():
    """get_payment_options returns UPI apps, scan-QR and cash. Nothing else.

    Offering a card or a wallet would be a button that cannot work.
    """
    methods = {m for m, *_ in bot.PAYMENT_CHOICES}
    assert methods == {"Cash", "UPI"}
    for forbidden in ("card", "wallet", "netbanking", "saved"):
        assert not any(forbidden in str(c).lower() for c in bot.PAYMENT_CHOICES)


def test_only_cash_is_described_as_needing_nothing():
    cash = next(c for c in bot.PAYMENT_CHOICES if c[0] == "Cash")
    upi = next(c for c in bot.PAYMENT_CHOICES if c[0] == "UPI")
    assert "automatic" in cash[4].lower()
    assert "approval" in upi[4].lower() or "app" in upi[4].lower()


def test_the_summary_states_plainly_that_there_is_no_card(monkeypatch):
    """A user asking for card auto-pay should read the answer here, not
    discover it when an order stalls."""
    text = bot._payment_summary(CHAT).lower()
    assert "no card" in text
    assert "wallet" in text
    assert "rbi" in text or "two factors" in text


def test_the_preference_persists_and_starts_unset():
    assert session.get_payment_pref(CHAT)["method"] == ""
    session.set_payment_pref(CHAT, "Cash")
    assert session.get_payment_pref(CHAT)["method"] == "Cash"


# ── never fall back to a model that cannot reach the store ───────────

def test_a_commerce_failure_does_not_hand_the_turn_to_mimirs_own_model():
    """The defect that made Mimir quote dollars for Indian groceries.

    _run_commerce_turn used to `return False` on any error it did not
    recognise, handing the turn to Mimir's own tool loop as a "graceful"
    fallback. That loop shares none of the commerce agent's conversation, so it
    re-ran commerce_providers and commerce_search from scratch every turn and
    asked "which product?" to a user who had just named one. When its own
    cascade ran dry it invented a cart and priced it in US dollars, then
    explained the dollars away as "the store's default display".

    A shopping assistant that cannot reach the store has to say so. Falling
    back to a model that will confidently make up prices is worse than
    returning nothing, because the user cannot tell which one they got.
    """
    import inspect

    from core import bot

    src = inspect.getsource(bot._run_commerce_turn)
    body = src.split("data = await commerce.assistant", 1)[1]
    # Strip comments: the fix documents the old `return False` in prose, and a
    # naive substring match would find the explanation rather than the code.
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )

    assert "return False" not in code, (
        "_run_commerce_turn still falls through to Mimir's own agent on a "
        "commerce failure; that path fabricates prices"
    )
    assert "can't reach the store" in body, "the user is not told the store is unreachable"
    assert "cart is untouched" in body.lower() or "untouched" in body


def test_the_unreachable_message_shows_the_real_error():
    """Surfacing the provider's own error is what turned a week of confusion
    into a five-minute diagnosis, the same lesson as AuthServerError."""
    import inspect

    from core import bot

    src = inspect.getsource(bot._run_commerce_turn)
    assert "html.escape(error" in src, "the underlying error is not shown to the user"
