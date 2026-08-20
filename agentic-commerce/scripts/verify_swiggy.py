"""Read-only, PII-REDACTED capture of Swiggy's real tool list and response SHAPES.

Calls ONLY non-mutating tools (list, get_addresses, search_products, get_cart,
get_payment_options) and prints only the JSON STRUCTURE — field names and value
*types* — with every actual value masked ("<str>", "<num>"). No real address,
payment handle, price, or product name is ever emitted. This is all that's
needed to pin the manifest's field mappings. It never calls update_cart /
checkout / confirm_order / create_address / delete_address.

    python -m scripts.verify_swiggy
"""

from __future__ import annotations

import json

import anyio
from dotenv import load_dotenv

from backend.providers.swiggy.adapter import SwiggyAdapter


def shape(obj):
    """Return a value-redacted skeleton of `obj`: keys + types only."""
    if isinstance(obj, dict):
        return {k: shape(v) for k, v in obj.items()}
    if isinstance(obj, list):
        if not obj:
            return []
        skel = [shape(obj[0])]
        if len(obj) > 1:
            skel.append(f"...(+{len(obj) - 1} more, {len(obj)} total)")
        return skel
    if isinstance(obj, bool):
        return "<bool>"
    if isinstance(obj, (int, float)):
        return "<num>"
    if isinstance(obj, str):
        return "<str>"
    if obj is None:
        return None
    return f"<{type(obj).__name__}>"

# addressId is resolved at runtime from get_addresses and injected INTERNALLY —
# the id value itself is never printed (only the redacted response shapes are).
READONLY_CALLS = [
    ("get_addresses", {}),
    ("search_products", {"query": "milk", "addressId": "$ADDR"}),
    ("your_go_to_items", {"addressId": "$ADDR"}),
    ("get_cart", {}),
    ("get_payment_options", {"addressId": "$ADDR", "cartAmount": "$CART_TOTAL"}),
]


def _dump(label: str, obj) -> None:
    print(f"\n{'='*70}\n{label}\n{'='*70}")
    print(json.dumps(shape(obj), indent=2, ensure_ascii=False)[:4000])


def _resolve(args: dict, addr_id, cart_total) -> dict:
    out = {}
    for k, v in args.items():
        if v == "$ADDR":
            out[k] = addr_id
        elif v == "$CART_TOTAL":
            out[k] = cart_total
        else:
            out[k] = v
    return out


async def main():
    adapter = SwiggyAdapter()
    await adapter.connect()
    try:
        tools = await adapter.list_raw_tools()
        print("REAL TOOL LIST (%d):" % len(tools))
        for t in tools:
            props = list((t.input_schema or {}).get("properties", {}).keys())
            print(f"  • {t.name:22} params={props}  — {t.description[:70]}")

        # Resolve an addressId + cart total internally (values NOT printed).
        addr_res = await adapter.call_raw_tool("get_addresses", {})
        addrs = (addr_res.structured or {}).get("addresses", []) if addr_res.structured else []
        addr_id = addrs[0].get("id") if addrs else None
        cart_res = await adapter.call_raw_tool("get_cart", {})
        cart_total = (cart_res.structured or {}).get("cartTotalAmount") if cart_res.structured else None
        print(f"\n(resolved an addressId internally: {'yes' if addr_id else 'NONE FOUND'}; "
              f"cart total present: {'yes' if cart_total else 'no'})")

        for name, args in READONLY_CALLS:
            resolved = _resolve(args, addr_id, cart_total)
            res = await adapter.call_raw_tool(name, resolved)
            payload = res.structured if res.structured is not None else res.text
            _dump(f"{name}(...)  is_error={res.is_error}", payload)
    finally:
        await adapter.close()


if __name__ == "__main__":
    load_dotenv()
    anyio.run(main)
