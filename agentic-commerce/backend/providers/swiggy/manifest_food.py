"""Swiggy Food normalization manifest.

Swiggy runs three MCP servers behind one OAuth identity (ADR-2 applies to each
independently):

    /im        Instamart      groceries       -> manifest.py
    /food      Food           restaurants     -> this module
    /dineout   Dineout        reservations    -> manifest_dineout.py

VERIFIED 2026-08-21 against the live server with a real account. The tool names
below were written from Swiggy's published reference and every one of them
turned out to exist; the response SHAPES did not match what the docs implied,
and the first draft of this module could not parse a single one of them.

Two traps worth keeping in mind when extending this:

  * **The casing is not consistent within one service.** search_restaurants
    returns camelCase (`avgRating`, `costForTwo`, `availabilityStatus`),
    get_food_cart returns snake_case (`menu_item_id`, `item_total`, `to_pay`),
    and search_menu mixes both in one object (`menu_item_id` beside `inStock`).
    Every lookup here therefore tries both spellings.

  * **`availabilityStatus` is a string, not a boolean.** The obvious
    `bool(r.get("isOpen", True))` finds no such key, falls back to the default
    and reports every closed restaurant as open -- failing in the direction that
    sends an order to a shut kitchen.
"""

from __future__ import annotations

from typing import Any, Optional

from backend.canonical.types import (
    CanonicalCart,
    CanonicalCartItem,
    CanonicalProduct,
    Money,
)
from backend.providers.swiggy.manifest import to_money

PROVIDER_ID = "swiggy_food"
MCP_URL = "https://mcp.swiggy.com/food"

# Tools that spend real money.
#
# Note `place_food_order` -- NOT `checkout`. Instamart's money tool is called
# `checkout`, and a guard sharing Instamart's set across services would never
# match this name. The block would not fire, nothing would raise, and the first
# sign of trouble would be a real order. This is exactly why DANGEROUS_TOOLS is
# declared per provider rather than as one global constant.
DANGEROUS_TOOLS = frozenset({"place_food_order", "confirm_order"})

# Canonical capability -> Swiggy Food MCP tool name. VERIFIED 2026-08-21.
TOOL_MAP = {
    "search": "search_restaurants",
    "search_dishes": "search_menu",
    "menu": "get_restaurant_menu",
    "get_cart": "get_food_cart",
    "update_cart": "update_food_cart",
    "clear_cart": "flush_food_cart",
    "checkout": "place_food_order",
    "payment_status": "check_payment_status",
    "confirm_order": "confirm_order",
    "payment_options": "get_payment_options",
    "get_addresses": "get_addresses",
    "orders": "get_food_orders",
    "order_details": "get_food_order_details",
    "track": "track_food_order",
    "coupons": "fetch_food_coupons",
    "apply_coupon": "apply_food_coupon",
}

# Tools that REQUIRE an addressId argument and error without one. Verified by
# calling each: "addressId is required". Food is address-scoped throughout --
# which restaurants deliver to you, and what delivery costs, both depend on it.
ADDRESS_SCOPED = frozenset({
    "search_restaurants", "search_menu", "get_restaurant_menu",
    "get_food_cart", "update_food_cart", "place_food_order",
})

# VERIFIED 2026-08-22: the live server exposes exactly these 20.
#
# It exposed 18 on 2026-08-21. `create_address` and `delete_address` appeared
# between the two runs -- the same two tools the Instamart manifest records as
# documented-but-absent when that one was verified. Swiggy ships these servers
# continuously, so this set is a snapshot rather than a contract, and the ADR-3
# drift check exists because of exactly this.
EXPECTED_TOOLS = {
    "get_addresses", "create_address", "delete_address",
    "search_restaurants", "search_menu", "get_restaurant_menu",
    "get_food_cart", "update_food_cart", "flush_food_cart",
    "fetch_food_coupons", "apply_food_coupon",
    "place_food_order", "get_payment_options", "check_payment_status",
    "confirm_order", "get_food_orders", "get_food_order_details",
    "track_food_order", "report_error",
    # Undocumented but present. Swiggy describes it as internal to their
    # order-success widget, "not for conversational use".
    "get_food_delivery_status",
}


def _first(d: dict[str, Any], *keys: str, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _is_available(raw: dict[str, Any]) -> bool:
    """Availability, across the three spellings the Food API actually uses.

    Defaults to True only when NONE of them is present, which matches how the
    rest of this codebase treats an absent stock flag. The important part is
    that `availabilityStatus: "CLOSED"` is read as closed rather than skipped.
    """
    status = _first(raw, "availabilityStatus", "availability")
    if isinstance(status, str):
        return status.strip().upper() in ("OPEN", "AVAILABLE", "SERVICEABLE")
    flag = _first(raw, "inStock", "in_stock", "isOpen", "available")
    if flag is not None:
        return bool(int(flag)) if isinstance(flag, (int, str)) and str(flag).isdigit() else bool(flag)
    return True


def _restaurant_blurb(r: dict[str, Any]) -> str:
    """Cuisine, rating, ETA and cost-for-two as one descriptive line.

    This goes in `unit`, not `price`, on purpose. `costForTwo` arrives as the
    string "Rs.1500 for two" -- an estimate for two people, not a price anyone
    pays -- so putting it in `price` would make the renderer announce
    "Cheapest: Rs.1500" about a number that is not the cost of anything
    orderable.
    """
    bits: list[str] = []
    cuisines = _first(r, "cuisines", "cuisine")
    if isinstance(cuisines, list):
        cuisines = ", ".join(str(c) for c in cuisines if c)
    if cuisines:
        bits.append(str(cuisines))
    if (rating := _first(r, "avgRating", "rating")) is not None:
        bits.append(f"\u2605 {rating}")
    if eta := _first(r, "deliveryTimeRange", "deliveryTimeMinutes"):
        bits.append(f"{eta} mins" if str(eta).isdigit() else str(eta))
    if cost := _first(r, "costForTwo", "costForTwoMessage"):
        bits.append(str(cost))
    if offer := _first(r, "offer"):
        bits.append(str(offer))
    return " \u00b7 ".join(bits)


def parse_restaurants(payload: Any) -> list[CanonicalProduct]:
    """Restaurants mapped onto CanonicalProduct.

    A restaurant is not a product, and this is a bounded compromise so the
    comparison engine and the Telegram renderer stay unchanged. `price` is
    deliberately left empty -- see _restaurant_blurb. Ordering never goes
    through this shape (it goes through the raw tools), so nothing on the money
    path depends on the analogy holding.
    """
    if not isinstance(payload, dict):
        return []
    rows = _first(payload, "restaurants", "results", "data", "items", default=[]) or []
    out: list[CanonicalProduct] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append(CanonicalProduct(
            provider_id=PROVIDER_ID,
            product_id=str(_first(r, "id", "restaurantId", "restaurant_id", default="") or ""),
            name=str(_first(r, "name", "restaurantName", "title", default="(unnamed)") or ""),
            price=Money(None),
            available=_is_available(r),
            unit=_restaurant_blurb(r) or None,
            image_url=_first(r, "imageUrl", "image_url"),
            raw=r,
        ))
    return out


def parse_dishes(payload: Any) -> list[CanonicalProduct]:
    """Menu items -> CanonicalProduct. This analogy IS exact: a dish has a price.

    VERIFIED shape: {"items": [{"name", "price", "menu_item_id", "inStock",
    "restaurant_id", "restaurant_name", "rating", "imageUrl"}]}.
    """
    if not isinstance(payload, dict):
        return []
    rows = _first(payload, "items", "dishes", "menuItems", "results", default=[]) or []
    out: list[CanonicalProduct] = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        bits = []
        if venue := _first(it, "restaurant_name", "restaurantName"):
            bits.append(str(venue))
        if rating := _first(it, "rating", "avgRating"):
            bits.append(f"\u2605 {rating}")
        out.append(CanonicalProduct(
            provider_id=PROVIDER_ID,
            product_id=str(_first(it, "menu_item_id", "itemId", "id", default="") or ""),
            name=str(_first(it, "name", "itemName", default="(unnamed)") or ""),
            price=to_money(_first(it, "price", "finalPrice", "final_price", "defaultPrice")),
            available=_is_available(it),
            unit=" \u00b7 ".join(bits) or None,
            image_url=_first(it, "imageUrl", "image_url"),
            raw=it,
        ))
    return out


# Bill labels, in the order Swiggy's own bill shows them. Kept as data so the
# renderer prints the provider's breakdown rather than a guess at one: the gap
# between an item total and what you actually pay is entirely these lines, and
# a confirmation screen showing only the final number explains none of it.
_PRICING_LABELS = (
    ("item_total", "Item Total"),
    ("delivery_charge", "Delivery"),
    ("packing_charge", "Packing"),
    ("taxes_and_charges", "Taxes and Charges"),
    ("discount", "Discount"),
)


def parse_cart(payload: Any) -> CanonicalCart:
    """Food cart -> CanonicalCart.

    VERIFIED shape: {"data": {"cart_id", "items": [{"menu_item_id", "name",
    "quantity", "final_price", "subtotal", "total", "in_stock"}], "pricing":
    {"item_total", "delivery_charge", "taxes_and_charges", "to_pay"}}}.

    Note `to_pay`, snake_case. The first draft looked for `toPay` among five
    camelCase spellings, found none, and returned a total of None -- which the
    renderer shows as an em dash. A cart that reads as "no total" is at least
    visible; the same bug returning 0 would have been far worse.
    """
    if not isinstance(payload, dict):
        return CanonicalCart(PROVIDER_ID, [], Money(None), Money(None), raw={})

    data = _first(payload, "data", "cart", default=payload) or payload
    rows = _first(data, "items", "cartItems", "lineItems", default=[]) or []

    items: list[CanonicalCartItem] = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        items.append(CanonicalCartItem(
            product_id=str(_first(it, "menu_item_id", "itemId", "id", default="") or ""),
            name=str(_first(it, "name", "itemName", default="(unnamed)") or ""),
            quantity=int(_first(it, "quantity", "qty", default=1) or 1),
            # final_price is the per-unit price after item-level discounts.
            # `total` includes addons, so using it as a UNIT price would
            # multiply addons by the quantity a second time in line_total_minor.
            price=to_money(_first(it, "final_price", "price", "subtotal")),
        ))

    pricing = _first(data, "pricing", "bill", "billDetails", default={}) or {}
    line_items = [
        {"label": label, "value": to_money(pricing[key]).display()}
        for key, label in _PRICING_LABELS
        if isinstance(pricing, dict) and pricing.get(key) is not None
    ]

    total = to_money(_first(pricing, "to_pay", "toPay", "grandTotal", "total"))
    subtotal = to_money(_first(pricing, "item_total", "itemTotal", "subtotal"))

    return CanonicalCart(
        provider_id=PROVIDER_ID,
        items=items,
        subtotal=subtotal,
        total=total,
        bill_breakdown={"lineItems": line_items} if line_items else {},
        raw=data if isinstance(data, dict) else {},
    )
