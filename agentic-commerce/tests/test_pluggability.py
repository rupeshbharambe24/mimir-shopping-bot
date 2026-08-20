"""Proves the plugin seams (ARCHITECTURE_DECISIONS.md step 3) with ZERO network.

Everything here runs against the in-memory mock provider and the local registry.
The Swiggy adapter is only *imported* and inspected (never connected), so no
credentials or network are needed.
"""

from __future__ import annotations

import os

import pytest

from backend.canonical.types import Money, PaymentStatus, SearchStatus, CanonicalSearchQuery, CanonicalCartItem
from backend.providers import registry
from backend.providers.base import ProviderAdapter
from backend.providers._mock_provider.adapter import MockAdapter

# Keep the money path in DRY_RUN off for the mock (the mock is already fake and
# has no external side effects); DRY_RUN only matters for the Swiggy adapter.
os.environ.setdefault("DRY_RUN", "false")


# --------------------------------------------------------------------------
# Registry + conformance
# --------------------------------------------------------------------------

def test_registry_loads_both_providers():
    entries = registry.load_registry("providers.yaml")
    ids = {e.id for e in entries}
    assert {"swiggy", "mock"} <= ids


def test_both_adapters_conform_to_protocol():
    entries = registry.load_registry("providers.yaml")
    for adapter in registry.build_all(entries).values():
        # runtime_checkable Protocol: verifies the methods/attrs exist.
        assert isinstance(adapter, ProviderAdapter)


def test_declared_flags_match_adapter_code():
    """ADR-3 drift check on the flag axis — yaml must agree with the code."""
    entries = registry.load_registry("providers.yaml")
    adapters = registry.build_all(entries)
    for e in entries:
        if not e.enabled:
            continue
        mismatches = registry._compare_flags(e, adapters[e.id])
        assert mismatches == [], f"{e.id} flag drift: {mismatches}"


def test_adr5_search_without_auth_flags():
    entries = {e.id: e for e in registry.load_registry("providers.yaml")}
    assert entries["swiggy"].declared_flags.searchable_without_auth is False
    assert entries["mock"].declared_flags.searchable_without_auth is True


async def test_mock_drift_report_is_clean():
    entries = {e.id: e for e in registry.load_registry("providers.yaml")}
    entry = entries["mock"]
    adapter = registry.build_adapter(entry)
    report = await registry.check_provider(entry, adapter)
    assert report.connected is True
    assert report.missing_expected == []
    assert report.flag_mismatches == []
    assert report.ok is True


# --------------------------------------------------------------------------
# Normalization actually does work (different raw shape -> same canonical)
# --------------------------------------------------------------------------

async def test_mock_search_normalizes_foreign_field_names():
    adapter = MockAdapter()
    await adapter.connect()
    res = await adapter.search(CanonicalSearchQuery(text="milk"))
    assert res.status == SearchStatus.OK
    assert len(res.items) == 1
    p = res.items[0]
    # mock raw uses sku/label/cost_paise; canonical must expose id/name/price.
    assert p.product_id == "MOCK-MILK"
    assert p.name == "Toned Milk"
    assert p.price == Money(3300, "INR")
    assert p.price.display() == "₹33.00"


# --------------------------------------------------------------------------
# Money path: cart -> checkout (intent) -> poll (placed), plus guards
# --------------------------------------------------------------------------

async def test_full_two_step_checkout_flow():
    adapter = MockAdapter()
    await adapter.connect()
    cart = await adapter.update_cart([
        CanonicalCartItem("MOCK-MILK", "Toned Milk", quantity=2, price=Money(3300)),
        CanonicalCartItem("MOCK-BREAD", "Whole Wheat Bread", quantity=1, price=Money(4500)),
    ])
    assert cart.subtotal == Money(2 * 3300 + 4500, "INR")

    intent = await adapter.checkout(confirm_token="user-tapped-place-order")
    assert intent.status == PaymentStatus.PENDING_PAYMENT
    assert intent.authorization_url  # a real (fake) UPI-style authorization link

    result = await adapter.poll_payment(intent)
    assert result.status == PaymentStatus.PLACED
    assert result.order_id == intent.order_id


async def test_checkout_requires_confirmation_token():
    adapter = MockAdapter()
    await adapter.connect()
    await adapter.update_cart([CanonicalCartItem("MOCK-MILK", "Toned Milk", 1, Money(3300))])
    with pytest.raises(PermissionError):
        await adapter.checkout(confirm_token="")


async def test_checkout_refuses_over_cap_order():
    adapter = MockAdapter()
    await adapter.connect()
    # 100 x ₹180 apples = ₹18,000 -> far over the ₹1000 declared cap.
    await adapter.update_cart([CanonicalCartItem("MOCK-APPLE", "Apples", 100, Money(18000))])
    with pytest.raises(PermissionError):
        await adapter.checkout(confirm_token="user-tapped-place-order")
