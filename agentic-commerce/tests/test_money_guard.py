"""Every provider must guard its own money-path tools.

The guard used to be a module-level `DANGEROUS_TOOLS = {"checkout",
"confirm_order"}` -- Swiggy's names -- consulted for every provider. Any
provider naming the same operation differently slipped straight through: the
set simply never matched, nothing raised, and the block silently did not fire.
The mock provider (`place_order` / `finalize_order`) was in exactly that state,
so the "pluggability proof" was proving the guard worked while never invoking
it.

A money gate that fails open is worse than no gate, because everything upstream
is written believing it holds. These tests assert it fails closed instead.
"""

import pytest

from backend.providers import registry
from backend.providers._mock_provider.adapter import MockAdapter

# Anything a provider might plausibly call its order-placing tool.
_MONEY_WORDS = ("checkout", "order", "place", "pay", "purchase", "buy", "confirm")


def _looks_like_money_tool(name: str) -> bool:
    lowered = name.lower()
    # "payment_state" / "get_orders" read; they do not spend.
    if lowered.startswith(("get_", "list_", "view_", "track_", "check_", "search_")):
        return False
    if lowered.endswith(("_status", "_state", "_options", "_history")):
        return False
    return any(w in lowered for w in _MONEY_WORDS)


def test_every_registered_provider_declares_its_money_tools():
    for entry in registry.load_registry():
        if not entry.enabled:
            continue
        adapter = registry.build_adapter(entry)
        declared = getattr(adapter, "dangerous_tools", None)
        assert declared, (
            f"{entry.id} declares no dangerous_tools, so the money-path guard "
            f"falls back to another provider's tool names and will not fire."
        )


async def test_mock_declared_tools_match_its_real_tool_names():
    """A declared set that does not match the provider's actual tool names is
    the same failure with extra steps."""
    adapter = MockAdapter()
    actual = {t.name for t in await adapter.list_raw_tools()}
    money = {n for n in actual if _looks_like_money_tool(n)}
    assert money, "fixture problem: the mock should expose order-placing tools"
    missing = money - set(adapter.dangerous_tools)
    assert not missing, f"mock exposes unguarded money tools: {sorted(missing)}"


async def test_mock_blocks_its_money_tools_when_not_armed():
    adapter = MockAdapter()
    for name in sorted(adapter.dangerous_tools):
        result = await adapter.call_raw_tool(name, {})
        assert result.is_error, f"{name} executed without being armed"
        assert "BLOCKED" in result.text


async def test_mock_allows_exactly_one_call_per_arm():
    """Arming is single-shot. If it stuck, one approval would authorise every
    subsequent order."""
    adapter = MockAdapter()
    adapter.arm_dangerous()
    first = await adapter.call_raw_tool("place_order", {})
    assert not first.is_error, "armed call should go through"

    second = await adapter.call_raw_tool("place_order", {})
    assert second.is_error, "the arm should not survive into a second call"
    assert "BLOCKED" in second.text


async def test_read_only_tools_are_never_blocked():
    """Guarding too much is its own failure: it teaches the model that BLOCKED
    is noise to retry past."""
    adapter = MockAdapter()
    result = await adapter.call_raw_tool("search_items", {"query": "milk"})
    assert not result.is_error


def test_swiggy_and_mock_declare_different_names():
    """The whole point: the guard follows the provider, not one vendor."""
    from backend.providers._mock_provider import manifest as mock_manifest
    from backend.providers.swiggy import manifest as swiggy_manifest

    assert mock_manifest.DANGEROUS_TOOLS != swiggy_manifest.DANGEROUS_TOOLS
    assert not (mock_manifest.DANGEROUS_TOOLS & swiggy_manifest.DANGEROUS_TOOLS)


# ── emptying the cart must actually empty it ─────────────────────
# update_cart({"items": []}) does NOT clear a Swiggy cart: the call succeeds and
# the cart comes back unchanged, so the caller is told it worked. On a shopping
# flow that means the user adds to a basket they believe is empty and orders
# more than they intended. Providers expose a dedicated tool; these assert every
# adapter has one and that it works.

def test_every_provider_can_actually_clear_its_cart():
    for entry in registry.load_registry():
        if not entry.enabled:
            continue
        adapter = registry.build_adapter(entry)
        assert hasattr(adapter, "clear_cart"), (
            f"{entry.id} has no clear_cart, so emptying falls back to "
            f"update_cart([]) -- which reports success without clearing."
        )


async def test_mock_clear_cart_empties_the_cart():
    from backend.canonical.types import CanonicalCartItem, Money

    adapter = MockAdapter()
    await adapter.connect()
    await adapter.update_cart([CanonicalCartItem("MOCK-MILK", "Toned Milk", 2, Money(3300))])
    assert (await adapter.get_cart()).items, "fixture problem: cart should have an item"

    after = await adapter.clear_cart()
    assert after.items == [], "clear_cart left items behind"
    assert (await adapter.get_cart()).items == [], "cart re-read is not empty"
