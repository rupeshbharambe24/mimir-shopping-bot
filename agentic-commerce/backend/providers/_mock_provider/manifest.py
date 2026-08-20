"""Normalization manifest for the mock provider (ADR-2).

The mock deliberately uses DIFFERENT tool names and DIFFERENT JSON field names
than Swiggy (`sku` not `id`, `label` not `name`, `cost_paise` not `price`,
`search_items` not `search_products`). If the canonical types come out identical
to Swiggy's despite that, the abstraction is doing real work — which is the whole
point of the mock in step 3.
"""

from __future__ import annotations

from typing import Any

from backend.canonical.types import (
    CanonicalCart,
    CanonicalCartItem,
    CanonicalOrderResult,
    CanonicalPaymentIntent,
    CanonicalProduct,
    Money,
    PaymentStatus,
)

PROVIDER_ID = "mock"

TOOL_MAP = {
    "search": "search_items",
    "get_cart": "view_basket",
    "update_cart": "set_basket",
    "checkout": "place_order",
    "payment_status": "payment_state",
    "confirm_order": "finalize_order",
}

EXPECTED_TOOLS = set(TOOL_MAP.values())


def parse_products(raw_items: list[dict[str, Any]]) -> list[CanonicalProduct]:
    return [
        CanonicalProduct(
            provider_id=PROVIDER_ID,
            product_id=str(r["sku"]),
            name=str(r["label"]),
            price=Money(int(r["cost_paise"]), "INR"),  # mock reports paise directly
            available=bool(r.get("stock", True)),
            unit=r.get("qty_label"),
            raw=r,
        )
        for r in raw_items
    ]


def parse_cart(raw: dict[str, Any]) -> CanonicalCart:
    items = [
        CanonicalCartItem(
            product_id=str(r["sku"]),
            name=str(r["label"]),
            quantity=int(r["qty"]),
            price=Money(int(r["cost_paise"]), "INR"),
        )
        for r in raw.get("line_items", [])
    ]
    return CanonicalCart(
        provider_id=PROVIDER_ID,
        items=items,
        subtotal=Money(raw.get("sub_total_paise"), "INR"),
        total=Money(raw.get("grand_total_paise"), "INR"),
        bill_breakdown=raw.get("fees", {}),
        raw=raw,
    )


def parse_payment_intent(raw: dict[str, Any]) -> CanonicalPaymentIntent:
    return CanonicalPaymentIntent(
        provider_id=PROVIDER_ID,
        order_id=str(raw["order_ref"]),
        status=_status(raw.get("state")),
        authorization_url=raw.get("pay_link"),
        payment_ref=raw.get("txn"),
        poll_interval_ms=int(raw.get("poll_ms", 10)),
        max_poll_ms=int(raw.get("timeout_ms", 1000)),
        raw=raw,
    )


def parse_order_result(raw: dict[str, Any]) -> CanonicalOrderResult:
    return CanonicalOrderResult(
        provider_id=PROVIDER_ID,
        order_id=str(raw["order_ref"]),
        status=_status(raw.get("state")),
        total=Money(raw.get("grand_total_paise"), "INR"),
        raw=raw,
    )


def _status(state: Any) -> PaymentStatus:
    return {
        "AWAITING_PAYMENT": PaymentStatus.PENDING_PAYMENT,
        "SUCCESS": PaymentStatus.PLACED,
        "FAILED": PaymentStatus.FAILED,
    }.get(str(state).upper(), PaymentStatus.UNKNOWN)
