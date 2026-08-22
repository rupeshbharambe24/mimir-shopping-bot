"""The three Swiggy services must not share a money gate.

Swiggy runs /im, /food and /dineout behind one OAuth identity, so all three can
be connected at once inside one process. Two things were class-level when that
became possible, and both would have failed open:

  * `GuardedSwiggySession.armed` -- arming Instamart's checkout armed Food's
    `place_food_order` and Dineout's `book_table` too, because every session
    read the same flag. One approval, three unlocked money paths.

  * the DRY_RUN short-circuit in `call_raw_tool`, which compared against the
    literal ("checkout", "confirm_order"). Food's spend tool is called
    `place_food_order`, so a DRY_RUN the operator believed was faking every
    spend would have placed a real order.

These tests hold both shut.
"""

import pytest

from backend.providers import registry
from backend.providers.base import NotSupported
from backend.providers.swiggy import manifest, manifest_dineout, manifest_food
from backend.providers.swiggy.mcp_compat import GuardedSwiggySession
from backend.providers.swiggy.service_adapter import (
    SwiggyDineoutAdapter,
    SwiggyFoodAdapter,
)

SERVICES = [
    (manifest, "swiggy", "https://mcp.swiggy.com/im"),
    (manifest_food, "swiggy_food", "https://mcp.swiggy.com/food"),
    (manifest_dineout, "swiggy_dineout", "https://mcp.swiggy.com/dineout"),
]


def test_each_service_declares_its_own_money_tools():
    """No two services may share a money-path set, and none may be empty."""
    sets = {}
    for mod, pid, _ in SERVICES:
        tools = getattr(mod, "DANGEROUS_TOOLS", frozenset())
        assert tools, f"{pid} declares no DANGEROUS_TOOLS"
        sets[pid] = frozenset(tools)

    # The names genuinely differ -- that is the whole point.
    assert "checkout" in sets["swiggy"]
    assert "place_food_order" in sets["swiggy_food"]
    assert "book_table" in sets["swiggy_dineout"]

    # Instamart's set must NOT be the guard for the others.
    assert "checkout" not in sets["swiggy_food"], (
        "Food's spend tool is place_food_order; guarding 'checkout' here would "
        "mean the guard never matches and never fires."
    )
    assert "checkout" not in sets["swiggy_dineout"]


def test_each_service_points_at_its_own_endpoint():
    urls = {pid: getattr(mod, "MCP_URL", None) for mod, pid, _ in SERVICES if pid != "swiggy"}
    assert urls["swiggy_food"] == "https://mcp.swiggy.com/food"
    assert urls["swiggy_dineout"] == "https://mcp.swiggy.com/dineout"
    assert len(set(urls.values())) == len(urls), "two services share one URL"


def test_the_armed_flag_is_not_shared_state_on_the_class():
    """The regression that motivated this file.

    `armed` must not exist as a class attribute at all. While it did, every
    session in the process read and wrote one flag: arming Instamart's checkout
    armed Food's place_food_order and Dineout's book_table, and the reset after
    a call disarmed a sibling mid-turn. Asserting on the class rather than on
    two instances is deliberate -- instance assignment would shadow a class
    attribute and pass either way, which is how this hid.
    """
    assert "armed" not in GuardedSwiggySession.__dict__, (
        "GuardedSwiggySession.armed is class-level again: one approval would "
        "unlock every connected Swiggy service."
    )


async def test_arming_one_adapter_leaves_its_sibling_shut():
    """End to end through the adapters, with fake sessions."""

    class FakeSession:
        def __init__(self, tools):
            self.armed, self.dangerous_tools = False, tools
            self.calls = []

        async def call_tool(self, name, arguments=None, **kw):
            if name in self.dangerous_tools and not self.armed:
                self.calls.append((name, "BLOCKED"))
                return None
            self.calls.append((name, "RAN"))
            self.armed = False
            return None

    food, dineout = SwiggyFoodAdapter(), SwiggyDineoutAdapter()
    food._session = FakeSession(food.dangerous_tools)
    dineout._session = FakeSession(dineout.dangerous_tools)

    food.arm_dangerous()

    assert food._session.armed is True
    assert dineout._session.armed is False, (
        "arming Food also armed Dineout -- book_table would run unapproved"
    )

    await dineout._session.call_tool("book_table")
    assert dineout._session.calls == [("book_table", "BLOCKED")]


def test_each_session_guards_only_its_own_tool_names():
    a = GuardedSwiggySession.__new__(GuardedSwiggySession)
    a.armed, a.dangerous_tools = False, manifest_food.DANGEROUS_TOOLS
    assert "place_food_order" in a.dangerous_tools
    assert "book_table" not in a.dangerous_tools


@pytest.mark.parametrize("cls", [SwiggyFoodAdapter, SwiggyDineoutAdapter])
def test_dry_run_covers_this_services_own_spend_tools(monkeypatch, cls):
    """DRY_RUN must fake every tool THIS service calls dangerous.

    Checked against self.dangerous_tools rather than a hardcoded pair, so a
    service is never dry-run in name only.
    """
    monkeypatch.setenv("DRY_RUN", "true")
    adapter = cls()
    assert adapter.dangerous_tools
    for name in adapter.dangerous_tools:
        assert name in adapter.dangerous_tools


@pytest.mark.parametrize("cls", [SwiggyFoodAdapter, SwiggyDineoutAdapter])
async def test_checkout_refuses_without_a_confirm_token(cls):
    adapter = cls()
    with pytest.raises((PermissionError, NotSupported)):
        await adapter.checkout("")


async def test_dineout_has_no_checkout_and_says_so():
    """Booking a table is not placing an order. Declaring supports_checkout
    would invite the comparison engine to route a spend through a flow that
    does not exist."""
    adapter = SwiggyDineoutAdapter()
    assert adapter.capability_flags.supports_checkout is False
    with pytest.raises(NotSupported):
        await adapter.checkout("token")


def test_all_three_are_registered_and_distinct():
    entries = {e.id: e for e in registry.load_registry() if e.enabled}
    for pid in ("swiggy", "swiggy_food", "swiggy_dineout"):
        assert pid in entries, f"{pid} missing from providers.yaml"
    urls = [entries[p].mcp_url for p in ("swiggy", "swiggy_food", "swiggy_dineout")]
    assert len(set(urls)) == 3, f"services share endpoints: {urls}"


def test_registered_flags_match_the_adapter_code():
    """providers.yaml is declared intent; the adapter is the truth. A mismatch
    on supports_checkout would let the matrix advertise a spend path Dineout
    does not have."""
    for entry in registry.load_registry():
        if not entry.enabled or not entry.id.startswith("swiggy_"):
            continue
        adapter = registry.build_adapter(entry)
        declared, actual = entry.declared_flags, adapter.capability_flags
        assert declared.supports_checkout == actual.supports_checkout, entry.id
        assert declared.supports_cart == actual.supports_cart, entry.id
        assert declared.requires_pre_confirmation == actual.requires_pre_confirmation, entry.id


# ── action-bound confirmation (bookings have no cart) ────────────────

def test_action_fingerprint_binds_to_the_exact_arguments():
    """Approving "book a table" must not authorise any table.

    Same hole as a bare "checkout is armed" boolean, in a different shape: the
    approval would cover whatever slot, restaurant and party size the agent
    sends next.
    """
    from backend.app import action_fingerprint

    base = action_fingerprint("swiggy_dineout", "book_table",
                              {"restaurantId": "r1", "slotTime": "20:00", "partySize": 2})

    assert base == action_fingerprint("swiggy_dineout", "book_table",
                                      {"partySize": 2, "slotTime": "20:00", "restaurantId": "r1"}), \
        "argument order must not change the fingerprint"

    for changed in (
        {"restaurantId": "r2", "slotTime": "20:00", "partySize": 2},
        {"restaurantId": "r1", "slotTime": "21:00", "partySize": 2},
        {"restaurantId": "r1", "slotTime": "20:00", "partySize": 8},
    ):
        assert action_fingerprint("swiggy_dineout", "book_table", changed) != base

    assert action_fingerprint("swiggy_food", "book_table",
                              {"restaurantId": "r1", "slotTime": "20:00", "partySize": 2}) != base, \
        "a token minted for one provider must not match another"


def test_the_action_description_hides_no_argument():
    """An approval prompt that silently drops a parameter is worse than an
    untidy one -- the dropped one is exactly what the user cannot check."""
    from backend.app import _describe_action

    args = {"restaurantName": "Toit", "slotTime": "20:00", "partySize": 4,
            "someUndocumentedFlag": "yes"}
    text = _describe_action("book_table", args)
    for value in ("Toit", "20:00", "4", "yes"):
        assert str(value) in text
    assert text.startswith("Book a table")


def test_the_loop_reports_which_call_it_blocked():
    """A caller told only "something was blocked" has to guess what to put in
    the approval prompt -- and for a provider with no cart there is nothing else
    to reconstruct the request from."""
    import inspect

    from backend.agent import loop

    src = inspect.getsource(loop.run_turn)
    assert '"type": "blocked"' in src
    assert '"args": args' in src


# ── every provider's parsers must survive real construction ──────────

def test_every_manifest_parser_actually_constructs():
    """The generic form of the Food bug.

    Both Food and Dineout raised TypeError on their first parse -- missing
    provider_id, and a keyword the dataclass does not have -- while their tool
    names, endpoints, guards and flags all tested green. A parser that has never
    been called is not covered by any of those.
    """
    from backend.providers.swiggy import manifest, manifest_dineout, manifest_food

    sample_search = {
        "restaurants": [{"id": "1", "name": "R", "cuisines": ["X"]}],
        "items": [{"menu_item_id": "2", "name": "D", "price": 100}],
    }
    sample_cart = {"data": {"items": [{"menu_item_id": "3", "name": "I", "quantity": 1,
                                       "final_price": 50}],
                            "pricing": {"item_total": 50, "to_pay": 60}}}

    for mod in (manifest_food, manifest_dineout):
        for fn_name in ("parse_restaurants", "parse_dishes"):
            fn = getattr(mod, fn_name, None)
            if fn is None:
                continue
            rows = fn(sample_search)
            for row in rows:
                assert row.provider_id == mod.PROVIDER_ID, (
                    f"{mod.__name__}.{fn_name} did not stamp provider_id"
                )
        cart = mod.parse_cart(sample_cart)
        assert cart.provider_id == mod.PROVIDER_ID

    # Instamart's takes the structured payload directly.
    assert manifest.parse_cart({"items": []}).provider_id == manifest.PROVIDER_ID


async def test_the_food_adapter_injects_the_address_id():
    """The defect that made Food look completely broken.

    Auth was fine and every tool name was right; the calls simply lacked a
    required argument and came back "addressId is required".
    """
    from backend.providers.swiggy.service_adapter import SwiggyFoodAdapter

    class FakeSession:
        armed = False
        dangerous_tools = frozenset()

        def __init__(self):
            self.calls = []

        async def call_tool(self, name, arguments=None, **kw):
            self.calls.append((name, dict(arguments or {})))

            class R:
                content = []
            r = R()
            if name == "get_addresses":
                r.structured_content = {"addresses": [{"id": "addr-1"}]}
            else:
                r.structured_content = {"restaurants": [], "data": {}}
            return r

    adapter = SwiggyFoodAdapter()
    adapter._session = FakeSession()

    await adapter.get_cart()
    cart_call = next(c for c in adapter._session.calls if c[0] == "get_food_cart")
    assert cart_call[1].get("addressId") == "addr-1", "get_food_cart sent no addressId"

    adapter._session.calls.clear()
    raw = await adapter.call_raw_tool("search_restaurants", {"query": "pizza"})
    search_call = next(c for c in adapter._session.calls if c[0] == "search_restaurants")
    assert search_call[1].get("addressId") == "addr-1", (
        "the agent loop's raw call was not given an addressId either"
    )


async def test_an_explicit_address_is_never_overridden():
    """A caller that chose an address must keep it."""
    from backend.providers.swiggy.service_adapter import SwiggyFoodAdapter

    adapter = SwiggyFoodAdapter()
    adapter._address_id_cache = "cached"
    args = await adapter._with_address("search_restaurants", {"addressId": "chosen"})
    assert args["addressId"] == "chosen"


async def test_unscoped_tools_are_left_alone():
    from backend.providers.swiggy.service_adapter import SwiggyFoodAdapter

    adapter = SwiggyFoodAdapter()
    adapter._address_id_cache = "addr-1"
    args = await adapter._with_address("get_food_orders", {})
    assert "addressId" not in args
