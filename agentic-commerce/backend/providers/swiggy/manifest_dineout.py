"""Swiggy Dineout normalization manifest — table reservations.

Dineout is the odd one of the three: it books tables rather than delivering
goods. Most of it maps onto the canonical shapes only loosely, so this manifest
deliberately maps LESS than the other two and leans on the raw tools for the
booking flow itself. Forcing a reservation into a shopping cart would produce a
tidy-looking abstraction that lies about what the user is committing to.

VERIFIED 2026-08-21 against the live server: 12 tools, every expected name
present plus an undocumented `render_restaurants_dineout`.

The RESPONSE SHAPES are still unverified -- reading them needs a booking flow
run against a real restaurant, which commits to a table. The parsers below
therefore try several spellings per field and preserve the raw payload, and
`_is_bookable` handles a status string as well as a boolean because Food turned
out to return `availabilityStatus: "OPEN"` rather than the boolean its docs
implied.
"""

from __future__ import annotations

from typing import Any

from backend.canonical.types import CanonicalCart, CanonicalProduct, Money
from backend.providers.swiggy.manifest import to_money

PROVIDER_ID = "swiggy_dineout"
MCP_URL = "https://mcp.swiggy.com/dineout"

# `book_table` commits the user to a reservation and `confirm_order` finalises a
# paid one. Neither is called "checkout", which is the whole reason this set is
# per-service -- see manifest_food.DANGEROUS_TOOLS.
#
# A free booking still commits the user's evening and their name to a
# restaurant, and Swiggy's docs say bookings cannot be cancelled through the
# MCP. "It costs nothing" is not the same as "it is safe to do unasked", so
# book_table is gated exactly like a spend.
DANGEROUS_TOOLS = frozenset({"book_table", "confirm_order"})

TOOL_MAP = {
    "search": "search_restaurants_dineout",
    "details": "get_restaurant_details",
    "slots": "get_available_slots",
    "create_cart": "create_cart",
    "book": "book_table",
    "payment_options": "get_payment_options",
    "payment_status": "check_payment_status",
    "confirm_order": "confirm_order",
    "booking_status": "get_booking_status",
    "get_addresses": "get_saved_locations",
}

# VERIFIED 2026-08-21: the live server exposes exactly these 12.
EXPECTED_TOOLS = {
    "search_restaurants_dineout", "get_restaurant_details", "get_saved_locations",
    "get_available_slots", "create_cart", "book_table",
    "get_payment_options", "check_payment_status", "confirm_order",
    "get_booking_status", "report_error",
    # Undocumented but present -- a rendering helper for their own widget.
    "render_restaurants_dineout",
}


def _first(d: dict[str, Any], *keys: str, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _is_bookable(raw: dict[str, Any]) -> bool:
    """Bookability, allowing for a status STRING as well as a flag.

    Food returns `availabilityStatus: "OPEN"` rather than a boolean, and reading
    it as one reports every closed venue as open. Dineout's live shape is not
    yet verified, so this handles both rather than assuming.
    """
    status = _first(raw, "availabilityStatus", "bookingStatus", "availability")
    if isinstance(status, str):
        return status.strip().upper() in ("OPEN", "AVAILABLE", "BOOKABLE")
    flag = _first(raw, "isBookable", "available", "isAvailable")
    return True if flag is None else bool(flag)


def parse_restaurants(payload: Any) -> list[CanonicalProduct]:
    """Bookable restaurants -> CanonicalProduct, so search renders like the rest."""
    if not isinstance(payload, dict):
        return []
    rows = _first(payload, "restaurants", "results", "data", "items", default=[]) or []
    out: list[CanonicalProduct] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        cuisines = _first(r, "cuisines", "cuisine", default=[])
        if isinstance(cuisines, list):
            cuisines = ", ".join(str(c) for c in cuisines if c)
        # provider_id is the FIRST positional field of CanonicalProduct and was
        # missing here, so every call raised TypeError before returning a row.
        # Same omission as the Food manifest had: neither parser had ever been
        # run against real data, only their tool names asserted.
        bits = [str(cuisines)] if cuisines else []
        if rating := _first(r, "avgRating", "rating"):
            bits.append(f"\u2605 {rating}")
        if cost := _first(r, "costForTwo", "costForTwoMessage"):
            bits.append(str(cost))
        out.append(CanonicalProduct(
            provider_id=PROVIDER_ID,
            product_id=str(_first(r, "id", "restaurantId", "restaurant_id", default="") or ""),
            name=str(_first(r, "name", "restaurantName", default="(unnamed)") or ""),
            # No price: a table has none, and costForTwo is an estimate for two
            # people rather than anything the user pays to reserve.
            price=Money(None),
            unit=" \u00b7 ".join(bits) or None,
            available=_is_bookable(r),
            raw=r,
        ))
    return out


def parse_slots(payload: Any) -> list[dict[str, Any]]:
    """Available booking slots, left as plain dicts.

    There is no canonical type for "7pm on Saturday for four people", and
    inventing one to satisfy a shape would be dishonest. The renderer reads
    these directly.
    """
    if not isinstance(payload, dict):
        return []
    rows = _first(payload, "slots", "availableSlots", "data", "results", default=[]) or []
    out = []
    for s in rows:
        if not isinstance(s, dict):
            continue
        out.append({
            "slot_id": str(_first(s, "slotId", "id", default="") or ""),
            "time": str(_first(s, "time", "slotTime", "startTime", default="") or ""),
            "date": str(_first(s, "date", "slotDate", default="") or ""),
            "available": bool(_first(s, "available", "isAvailable", default=True)),
            "raw": s,
        })
    return out


def parse_cart(payload: Any) -> CanonicalCart:
    """Dineout carts exist for paid reservations only; free bookings have none.

    Returned with an empty item list and whatever total is present, so the money
    guard upstream still has a number to check rather than a None it must
    interpret.
    """
    if not isinstance(payload, dict):
        return CanonicalCart(PROVIDER_ID, [], Money(None), raw=payload)
    cart = _first(payload, "cart", "data", default=payload) or payload
    total = to_money(_first(cart, "totalAmount", "grandTotal", "toPay", "total"))
    return CanonicalCart(PROVIDER_ID, [], total, raw=payload)
