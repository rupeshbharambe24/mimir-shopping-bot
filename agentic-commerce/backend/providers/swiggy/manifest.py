"""Swiggy Instamart normalization manifest (ADR-2).

Maps Swiggy's real MCP tool names and their request/response JSON onto the
Canonical* types. This is the ONE place Swiggy-specific shape knowledge lives;
the comparison engine and UI never see raw Swiggy JSON.

⚠️  FIELD-MAPPING CAVEAT (read before trusting the numbers)
------------------------------------------------------------
Swiggy's published MCP reference lists the tools and what they do, but NOT the
exact response JSON field names. The mappers below therefore try several likely
keys and always fall back to preserving the raw payload. Every mapper is marked
`# VERIFY`. The correct way to pin these down is to run ONE live read
(`python -m scripts.check_registry` after connecting) and inspect the raw dicts,
then tighten the key lists here. This is exactly the "manifest effort is not
zero" risk called out in ARCHITECTURE_DECISIONS.md — concentrated here, not
scattered across the app.
"""

from __future__ import annotations

from typing import Any, Optional

from backend.canonical.types import (
    CanonicalCart,
    CanonicalCartItem,
    CanonicalOrderResult,
    CanonicalPaymentIntent,
    CanonicalProduct,
    Money,
    PaymentStatus,
)

PROVIDER_ID = "swiggy"

# Canonical capability -> Swiggy MCP tool name.
TOOL_MAP = {
    "search": "search_products",
    "get_cart": "get_cart",
    "update_cart": "update_cart",
    "clear_cart": "clear_cart",
    "checkout": "checkout",
    "payment_status": "check_payment_status",
    "confirm_order": "confirm_order",
    "payment_options": "get_payment_options",
    "get_addresses": "get_addresses",
}

# Tools we expect Swiggy's tools/list to contain, for the ADR-3 drift check.
# VERIFIED 2026-08-20 against the live server: it exposes 14 tools (NOT the 16
# the published docs summary listed). The docs listed create_address /
# delete_address / get_order_details which are absent, and omitted the present
# get_delivery_status. This set reflects the live reality.
EXPECTED_TOOLS = {
    "get_addresses", "search_products", "your_go_to_items", "get_cart",
    "update_cart", "clear_cart", "checkout", "get_orders", "track_order",
    "get_delivery_status", "report_error", "get_payment_options",
    "check_payment_status", "confirm_order",
}


# --------------------------------------------------------------------------
# Small coercion helpers
# --------------------------------------------------------------------------

def _first(d: dict[str, Any], *keys: str, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def to_money(value: Any, currency: str = "INR") -> Money:
    """Coerce a Swiggy price into Money (paise).

    Swiggy Instamart expresses numeric prices in RUPEES (e.g. mrp/offerPrice =
    49 means ₹49) and its cart/bill totals as display STRINGS (e.g. "₹347").
    Both normalize to paise here. (# VERIFY-VALUE: the rupees-vs-paise unit for
    the numeric fields was not confirmable from the value-redacted capture; it
    follows Instamart's usual convention. Confirm with one real value if a price
    ever looks off by 100x.)
    """
    if value is None:
        return Money(None, currency)
    try:
        if isinstance(value, bool):
            return Money(None, currency)
        if isinstance(value, (int, float)):
            return Money(round(value * 100), currency)
        s = str(value).strip().replace("₹", "").replace(",", "")
        if not s:
            return Money(None, currency)
        return Money(round(float(s) * 100), currency)
    except (ValueError, TypeError):
        return Money(None, currency)


# --------------------------------------------------------------------------
# Response mappers (raw Swiggy JSON -> Canonical*)
# --------------------------------------------------------------------------

def parse_products(structured: Any) -> list[CanonicalProduct]:
    """Flatten Swiggy search results to purchasable canonical products.

    VERIFIED 2026-08-20 shape: {"products": [{"displayName", "productId",
    "variations": [{"spinId","skuId","quantityDescription","price": {"mrp",
    "offerPrice","unitLevelPrice"}, "isInStockAndAvailable","imageUrl"}, ...]}]}.
    Each *variation* is a distinct purchasable SKU, so we emit one
    CanonicalProduct per variation, keyed by spinId (the finest-grained id the
    cart operates on).
    """
    products = _extract_list(structured, "products")
    out: list[CanonicalProduct] = []
    for prod in products:
        if not isinstance(prod, dict):
            continue
        base_name = str(_first(prod, "displayName", "name", default="(unnamed)"))
        variations = prod.get("variations") or []
        if not isinstance(variations, list) or not variations:
            # Product with no variation array — fall back to product-level fields.
            variations = [prod]
        for var in variations:
            if not isinstance(var, dict):
                continue
            price_obj = var.get("price") if isinstance(var.get("price"), dict) else {}
            out.append(CanonicalProduct(
                provider_id=PROVIDER_ID,
                product_id=str(_first(var, "spinId", "skuId", default=_first(prod, "productId", default=""))),
                name=base_name,
                price=to_money(_first(price_obj, "offerPrice", "mrp")
                               if price_obj else _first(var, "offerPrice", "mrp", "price")),
                available=bool(_first(var, "isInStockAndAvailable", default=_first(prod, "inStock", default=True))),
                unit=_opt_str(_first(var, "quantityDescription", "quantity", "variant")),
                image_url=_opt_str(_first(var, "imageUrl", "image")),
                raw={"product": prod, "variation": var},
            ))
    return out


def parse_cart(structured: Any) -> CanonicalCart:
    """VERIFIED 2026-08-20 shape: {"cartTotalAmount": "<display str>", "items":
    [{"spinId","skuId","productId","itemName","itemVariant","quantity","mrp",
    "discountedFinalPrice","isInStockAndAvailable"}], "billBreakdown":
    {"lineItems":[{"label","value"}], "toPay": {"label","value"}}, "cartId"}.
    """
    data = structured if isinstance(structured, dict) else {}
    raw_items = _extract_list(data, "items")
    items: list[CanonicalCartItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        items.append(CanonicalCartItem(
            product_id=str(_first(raw, "spinId", "skuId", "productId", default="")),
            name=str(_first(raw, "itemName", "name", default="(unnamed)")),
            quantity=int(_first(raw, "quantity", "qty", default=1) or 1),
            price=to_money(_first(raw, "discountedFinalPrice", "mrp", "price")),
        ))
    bill = data.get("billBreakdown") if isinstance(data.get("billBreakdown"), dict) else {}
    # `cartTotalAmount` is the authoritative payable total (display string).
    total = to_money(_first(data, "cartTotalAmount"))
    if total.minor_units is None:
        # Fall back to the bill's "toPay" value.
        to_pay = bill.get("toPay") if isinstance(bill.get("toPay"), dict) else {}
        total = to_money(to_pay.get("value"))
    # No dedicated subtotal field; sum line items as a best effort.
    line_sum = sum(i.line_total_minor or 0 for i in items) or None
    return CanonicalCart(
        provider_id=PROVIDER_ID,
        items=items,
        subtotal=Money(line_sum) if line_sum is not None else Money(None),
        total=total,
        bill_breakdown=bill,
        raw=data,
    )


def parse_payment_intent(structured: Any) -> CanonicalPaymentIntent:
    """# VERIFY against a live checkout response.

    Expected (from the docs): status PENDING_PAYMENT, a upiIntentUrl/bridgeUrl,
    a paasId + orderId, and pollingIntervalInMs / maxTimeToPollForInMs.
    """
    data = structured if isinstance(structured, dict) else {}
    status_raw = str(_first(data, "status", "orderStatus", default="")).upper()
    return CanonicalPaymentIntent(
        provider_id=PROVIDER_ID,
        order_id=str(_first(data, "orderId", "order_id", "id", default="")),
        status=_map_payment_status(status_raw),
        authorization_url=_opt_str(_first(data, "upiIntentUrl", "bridgeUrl", "paymentUrl")),
        payment_ref=_opt_str(_first(data, "paasId", "paas_id", "paymentRef", "transactionId")),
        poll_interval_ms=int(_first(data, "pollingIntervalInMs", "pollInterval", default=2000) or 2000),
        max_poll_ms=int(_first(data, "maxTimeToPollForInMs", "maxPollMs", default=120_000) or 120_000),
        raw=data,
    )


def parse_order_result(structured: Any) -> CanonicalOrderResult:
    """# VERIFY against a live check_payment_status / confirm_order response."""
    data = structured if isinstance(structured, dict) else {}
    status_raw = str(_first(data, "status", "orderStatus", "paymentStatus", default="")).upper()
    return CanonicalOrderResult(
        provider_id=PROVIDER_ID,
        order_id=str(_first(data, "orderId", "order_id", "id", default="")),
        status=_map_payment_status(status_raw),
        total=to_money(_first(data, "total", "grandTotal", "payableAmount")),
        raw=data,
    )


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------

def _map_payment_status(raw: str) -> PaymentStatus:
    raw = (raw or "").upper()
    if raw in ("PLACED", "SUCCESS", "COMPLETED", "CONFIRMED", "PAID"):
        return PaymentStatus.PLACED
    if raw in ("PENDING_PAYMENT", "PENDING", "AWAITING_PAYMENT", "IN_PROGRESS"):
        return PaymentStatus.PENDING_PAYMENT
    if raw in ("FAILED", "DECLINED", "ERROR"):
        return PaymentStatus.FAILED
    if raw in ("CANCELLED", "CANCELED"):
        return PaymentStatus.CANCELLED
    return PaymentStatus.UNKNOWN


def _extract_list(structured: Any, *keys: str) -> list:
    if isinstance(structured, list):
        return structured
    if isinstance(structured, dict):
        for k in keys:
            v = structured.get(k)
            if isinstance(v, list):
                return v
        # sometimes nested one level under "data"/"result"
        for wrapper in ("data", "result", "response"):
            inner = structured.get(wrapper)
            if isinstance(inner, (dict, list)):
                found = _extract_list(inner, *keys)
                if found:
                    return found
    return []


def _opt_str(v: Any) -> Optional[str]:
    return None if v is None else str(v)
