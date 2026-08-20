"""In-memory mock ProviderAdapter — proves the plugin seams with ZERO network.

This is the "stub second provider" from ARCHITECTURE_DECISIONS.md step 3 / SPEC
Q3. It holds a tiny catalog and cart in memory, exposes raw tools whose names and
JSON shapes differ from Swiggy's, and implements the full two-step checkout →
poll_payment flow deterministically. Because it needs no auth and no external
service, it's what `tests/test_pluggability.py` runs against.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from backend.canonical.types import (
    CanonicalCart,
    CanonicalCartItem,
    CanonicalOrderResult,
    CanonicalPaymentIntent,
    CanonicalSearchQuery,
    CanonicalSearchResult,
    Money,
    PaymentStatus,
    SearchStatus,
)
from backend.providers._mock_provider import manifest
from backend.providers.base import (
    CapabilityFlags,
    NotSupported,
    RawTool,
    RawToolResult,
)

# A tiny fixed catalog in the mock's own raw shape.
_CATALOG = [
    {"sku": "MOCK-APPLE", "label": "Royal Gala Apples", "cost_paise": 18000, "qty_label": "4 pcs", "stock": True},
    {"sku": "MOCK-MILK", "label": "Toned Milk", "cost_paise": 3300, "qty_label": "500 ml", "stock": True},
    {"sku": "MOCK-BREAD", "label": "Whole Wheat Bread", "cost_paise": 4500, "qty_label": "400 g", "stock": True},
    {"sku": "MOCK-EGGS", "label": "Free-range Eggs", "cost_paise": 9000, "qty_label": "6 pcs", "stock": False},
]

_RAW_TOOLS = [
    RawTool("search_items", "Search the mock catalog", {"type": "object", "properties": {"query": {"type": "string"}}}),
    RawTool("view_basket", "View the current basket", {"type": "object", "properties": {}}),
    RawTool("set_basket", "Replace the basket contents", {"type": "object", "properties": {"lines": {"type": "array"}}}),
    RawTool("place_order", "Create an order and begin payment", {"type": "object", "properties": {}}),
    RawTool("payment_state", "Read payment state", {"type": "object", "properties": {"txn": {"type": "string"}}}),
    RawTool("finalize_order", "Finalize a paid order", {"type": "object", "properties": {"order_ref": {"type": "string"}}}),
]


class MockAdapter:
    id = manifest.PROVIDER_ID
    dangerous_tools = manifest.DANGEROUS_TOOLS

    def __init__(self) -> None:
        # A genuinely anonymous-searchable provider (ADR-5): appears in
        # comparisons without a login, unlike Swiggy. Declares a ₹1000 cap and
        # pre-confirmation so it exercises the same money-path guard.
        self.capability_flags = CapabilityFlags(
            searchable_without_auth=True,
            supports_cart=True,
            supports_checkout=True,
            requires_pre_confirmation=True,
            max_order_value=100_000,
        )
        self._connected = False
        self._cart_lines: list[dict[str, Any]] = []
        self._order_seq = 0

    # ---------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    # ------------------------------------------------------------ introspection
    async def list_raw_tools(self) -> list[RawTool]:
        return list(_RAW_TOOLS)

    def arm_dangerous(self) -> None:
        """Arm the guard for exactly one dangerous-tool call."""
        self._armed = True

    async def call_raw_tool(self, name: str, arguments: dict[str, Any]) -> RawToolResult:
        # Route raw calls through the same in-memory logic, enforcing the same
        # single-shot arm a real adapter does. This used to only CLAIM to guard:
        # the comment said it did, no check existed, and place_order ran
        # unconditionally -- so the pluggability proof never exercised the
        # money-path guard it was supposed to demonstrate.
        if name in self.dangerous_tools:
            if not getattr(self, "_armed", False):
                return RawToolResult(
                    text=f"BLOCKED: '{name}' requires explicit user confirmation before it can run.",
                    is_error=True,
                )
            self._armed = False  # single-shot, like GuardedSwiggySession
        try:
            if name == "search_items":
                payload = [r for r in _CATALOG if arguments.get("query", "").lower() in r["label"].lower()]
            elif name == "view_basket":
                payload = self._raw_cart()
            elif name == "place_order":
                payload = self._raw_place_order()
            elif name == "finalize_order":
                payload = self._raw_finalize(arguments.get("order_ref", ""))
            elif name == "payment_state":
                payload = self._raw_payment_state()
            else:
                return RawToolResult(text=f"unknown tool {name}", is_error=True)
        except Exception as e:
            return RawToolResult(text=str(e), is_error=True)
        return RawToolResult(text=json.dumps(payload), is_error=False, structured=payload)

    # -------------------------------------------------------- canonical methods
    async def search(self, query: CanonicalSearchQuery) -> CanonicalSearchResult:
        matches = [r for r in _CATALOG if query.text.lower() in r["label"].lower()]
        return CanonicalSearchResult(
            provider_id=self.id,
            status=SearchStatus.OK,
            items=manifest.parse_products(matches[: query.limit]),
        )

    async def get_cart(self) -> CanonicalCart:
        return manifest.parse_cart(self._raw_cart())

    async def update_cart(self, items: list[CanonicalCartItem]) -> CanonicalCart:
        # Replace semantics (same contract as Swiggy).
        by_sku = {r["sku"]: r for r in _CATALOG}
        self._cart_lines = [
            {
                "sku": it.product_id,
                "label": by_sku.get(it.product_id, {}).get("label", it.name),
                "qty": it.quantity,
                "cost_paise": by_sku.get(it.product_id, {}).get("cost_paise", it.price.minor_units or 0),
            }
            for it in items
        ]
        return manifest.parse_cart(self._raw_cart())

    async def clear_cart(self) -> CanonicalCart:
        self._cart_lines.clear()
        return await self.get_cart()

    async def checkout(self, confirm_token: str) -> CanonicalPaymentIntent:
        if not self.capability_flags.supports_checkout:
            raise NotSupported(f"{self.id} does not support checkout")
        if self.capability_flags.requires_pre_confirmation and not confirm_token:
            raise PermissionError("checkout requires an explicit user confirmation token.")
        cap = self.capability_flags.max_order_value
        total = self._raw_cart()["grand_total_paise"]
        if cap is not None and total is not None and total > cap:
            raise PermissionError(
                f"Cart total {Money(total).display()} exceeds cap {Money(cap).display()}."
            )
        return manifest.parse_payment_intent(self._raw_place_order())

    async def poll_payment(self, intent: CanonicalPaymentIntent) -> CanonicalOrderResult:
        # The mock user "authorizes" instantly: one poll resolves to SUCCESS.
        state = self._raw_payment_state()
        if manifest._status(state["state"]) == PaymentStatus.PLACED:
            return manifest.parse_order_result(self._raw_finalize(intent.order_id))
        return CanonicalOrderResult(self.id, intent.order_id, PaymentStatus.UNKNOWN, raw=state)

    # ---------------------------------------------------------------- raw state
    def _raw_cart(self) -> dict[str, Any]:
        subtotal = sum(l["cost_paise"] * l["qty"] for l in self._cart_lines)
        delivery = 0 if subtotal == 0 else 2500  # ₹25 flat, like a real bill line
        return {
            "line_items": list(self._cart_lines),
            "sub_total_paise": subtotal,
            "grand_total_paise": subtotal + delivery,
            "fees": {"delivery_paise": delivery},
        }

    def _raw_place_order(self) -> dict[str, Any]:
        self._order_seq += 1
        cart = self._raw_cart()
        return {
            "order_ref": f"MOCK-{self._order_seq:04d}",
            "state": "AWAITING_PAYMENT",
            "pay_link": "mockpay://authorize/scan-qr",
            "txn": f"TXN-{self._order_seq:04d}",
            "poll_ms": 10,
            "timeout_ms": 1000,
            "grand_total_paise": cart["grand_total_paise"],
        }

    def _raw_payment_state(self) -> dict[str, Any]:
        return {"state": "SUCCESS", "txn": f"TXN-{self._order_seq:04d}"}

    def _raw_finalize(self, order_ref: str) -> dict[str, Any]:
        cart = self._raw_cart()
        return {"order_ref": order_ref, "state": "SUCCESS", "grand_total_paise": cart["grand_total_paise"]}
