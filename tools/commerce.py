"""Client for the agentic-commerce BFF.

The commerce platform runs as a separate process with its own pinned
dependencies (`mcp==2.0.0`, `google-genai`, `httpx2`). Those pins exist because
an unpinned `mcp` bump silently broke its transport once; merging them into the
bot's environment would put that failure mode back on the table and let a
provider-SDK problem take the Telegram bot down with it. So MIMIR talks to it
over HTTP on loopback instead of importing it.

Every function returns a plain string, because these are called straight from
`core.tools_registry` and the agent loop feeds the return value back to the
model as a tool result -- an exception there is unrecoverable for the model.

Session mapping: one BFF session per Telegram chat, keyed `mimir-<chat_id>`, so
the selected provider and cart survive across messages.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import aiohttp

from config import Config

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=45)
# Connecting a provider can involve an OAuth handshake; searching fans out
# across providers. Both deserve longer than a cart read.
_SLOW_TIMEOUT = aiohttp.ClientTimeout(total=200)
# An agent turn can chain several MCP round trips before it answers.
_AGENT_TIMEOUT = aiohttp.ClientTimeout(total=240)


class CommerceUnavailable(Exception):
    """The BFF is not reachable. Raised only by `require_healthy`."""


def _sid(chat_id: int) -> str:
    return f"mimir-{chat_id}"


async def _request(
    method: str,
    path: str,
    chat_id: int,
    payload: Optional[dict] = None,
    timeout: aiohttp.ClientTimeout = _TIMEOUT,
) -> dict[str, Any]:
    """Call the BFF. Never raises -- transport problems come back as
    {"ok": False, "error": ...} so callers have one shape to handle."""
    url = f"{Config.COMMERCE_BFF_URL}{path}"
    headers = {"X-Session-Id": _sid(chat_id)}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, json=payload, headers=headers) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    return {"ok": False, "error": f"BFF returned HTTP {resp.status}: {text[:300]}"}
                try:
                    return json.loads(text)
                except ValueError:
                    return {"ok": False, "error": f"BFF returned non-JSON: {text[:300]}"}
    except aiohttp.ClientConnectorError:
        return {
            "ok": False,
            "error": (
                f"Commerce service is not running at {Config.COMMERCE_BFF_URL}. "
                "Start it with: bash start-commerce.sh"
            ),
        }
    except TimeoutError:
        return {"ok": False, "error": "Commerce service timed out."}
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
        logger.exception("commerce request failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _err(data: dict) -> Optional[str]:
    if not data.get("ok", False):
        return f"Commerce error: {data.get('error', 'unknown error')}"
    return None


# ── read-only ────────────────────────────────────────────────────

async def health(chat_id: int) -> dict[str, Any]:
    return await _request("GET", "/health", chat_id)


async def providers(chat_id: int) -> str:
    data = await _request("GET", "/providers", chat_id)
    if "providers" not in data:
        return _err(data) or "Commerce error: malformed /providers response."
    lines = ["COMMERCE PROVIDERS", ""]
    selected = data.get("selected")
    for p in data["providers"]:
        mark = "*" if p["id"] == selected else " "
        state = "connected" if p["connected"] else (
            "not connected" if p["auth_type"] != "none" else "no auth needed"
        )
        lines.append(f" {mark} {p['display_name']}  [{p['id']}]  -- {state}")
        flags = p.get("flags", {})
        caps = []
        if flags.get("supports_cart"):
            caps.append("cart")
        if flags.get("supports_checkout"):
            caps.append("checkout")
        if flags.get("searchable_without_auth"):
            caps.append("anonymous search")
        cap = flags.get("max_order_value")
        if cap:
            caps.append(f"max order Rs.{cap / 100:.0f}")
        if caps:
            lines.append(f"      {', '.join(caps)}")
    if selected:
        lines.append("")
        lines.append(f"Selected: {selected}  (* marks it)")
    else:
        lines.append("")
        lines.append("No provider selected yet.")
    return "\n".join(lines)


async def select(chat_id: int, provider_id: str) -> str:
    data = await _request("POST", f"/providers/{provider_id}/select", chat_id)
    err = _err(data)
    return err or f"Selected provider '{data.get('selected')}'."


async def connect(chat_id: int, provider_id: str) -> str:
    data = await _request(
        "POST", f"/providers/{provider_id}/connect", chat_id, timeout=_SLOW_TIMEOUT
    )
    if err := _err(data):
        return (
            f"{err}\n\n"
            "If this provider uses OAuth, the login opens a browser window on the "
            "Mac itself and must be completed there. Use /remote for a remote "
            "desktop link if you are away from the machine."
        )
    return f"Connected: {', '.join(data.get('connected', []))}"


async def search(chat_id: int, query: str) -> str:
    """Search the selected provider.

    Replaces the earlier cross-provider compare. With one real provider a
    comparison spends a round trip on providers that answer needs_connection and
    reports a "cheapest across providers" figure that compares nothing. It also
    carried a live bug: a connected provider returning only unpriced or
    out-of-stock items yields a None price, and ranking those against integers
    raised TypeError mid-turn, which the model then retried until it ran out of
    iterations and rate limit.
    """
    data = await _request("POST", "/search", chat_id, {"query": query}, timeout=_SLOW_TIMEOUT)

    if not data.get("ok"):
        status = data.get("status")
        if status == "needs_connection":
            return (
                f"Not connected to the store yet. Use /connect swiggy "
                f"(or ask me to connect) and try again."
            )
        return f"Search failed: {data.get('error') or status or 'unknown error'}"

    items = data.get("items") or []
    if not items:
        return f"No results for '{query}'."

    in_stock = [i for i in items if i.get("available", True)]
    out_of_stock = [i for i in items if not i.get("available", True)]

    lines = [f"Results for '{query}':", ""]
    for item in in_stock[:10]:
        unit = f"  ({item['unit']})" if item.get("unit") else ""
        lines.append(f"  {item['price_display']:>10}  {item['name']}{unit}")
        lines.append(f"              id: {item['product_id']}")
    if out_of_stock:
        lines.append("")
        lines.append(f"  ({len(out_of_stock)} more out of stock)")

    # Priced, in-stock items only. A provider can legitimately return an item
    # with no price; ranking None against an int is what crashed before.
    priced = [i["price_minor"] for i in in_stock if i.get("price_minor") is not None]
    if priced:
        lines.append("")
        lines.append(f"Cheapest in stock: Rs.{min(priced) / 100:.2f}")

    return "\n".join(lines)


async def cart(chat_id: int) -> str:
    data = await _request("GET", "/cart", chat_id)
    if err := _err(data):
        return err
    return _format_cart(data["cart"])


def _format_cart(c: dict[str, Any]) -> str:
    if not c.get("items"):
        return "Cart is empty."
    lines = [f"CART ({c['provider_id']})", ""]
    for item in c["items"]:
        lines.append(f"  {item['quantity']} x {item['name']}  @ {item['price_display']}")
    lines.append("")
    lines.append(f"  Subtotal: {c.get('subtotal_display', '-')}")
    lines.append(f"  Total:    {c.get('total_display', '-')}")
    return "\n".join(lines)


# ── cart mutation ────────────────────────────────────────────────

async def add_to_cart(
    chat_id: int,
    product_id: str,
    quantity: int = 1,
    name: str = "",
    price_minor: Optional[int] = None,
) -> str:
    payload = {
        "items": [{
            "product_id": product_id,
            "quantity": quantity,
            "name": name,
            "price_minor": price_minor,
        }]
    }
    data = await _request("POST", "/cart/add", chat_id, payload)
    if err := _err(data):
        return err
    return "Added to cart.\n\n" + _format_cart(data["cart"])


async def clear_cart(chat_id: int) -> str:
    data = await _request("POST", "/cart/set", chat_id, {"items": []})
    if err := _err(data):
        return err
    return "Cart cleared."


# ── conversational shopping ──────────────────────────────────────

async def assistant(chat_id: int, text: str) -> dict[str, Any]:
    """Send a message to the commerce agent and get its reply.

    This drives the provider's RAW MCP tools (all 14 of Swiggy's), not the
    narrow canonical set. That distinction is the whole feature: the canonical
    surface exposes only what is comparable across providers, so a client using
    it alone cannot reach your_go_to_items, get_orders, track_order or
    get_payment_options -- which is most of what makes the assistant useful.
    """
    return await _request("POST", "/agent/message", chat_id, {"text": text}, timeout=_AGENT_TIMEOUT)


async def assistant_confirmed(chat_id: int, text: str) -> dict[str, Any]:
    """Re-send a turn with the money path armed, after the user approved."""
    return await _request(
        "POST", "/agent/message", chat_id, {"text": text, "armed": True},
        timeout=_AGENT_TIMEOUT,
    )


async def reset_conversation(chat_id: int) -> str:
    data = await _request("POST", "/agent/reset", chat_id)
    return "Conversation reset." if data.get("ok") else (_err(data) or "Reset failed.")


# ── money path ───────────────────────────────────────────────────

async def checkout_intent(chat_id: int) -> dict[str, Any]:
    """Snapshot the cart and mint a confirmation token. Spends nothing.

    Returns the raw dict (not a string) because the caller needs `summary` and
    `confirm_token` separately to build the Telegram approval prompt.
    """
    return await _request("POST", "/checkout/intent", chat_id)


async def checkout(chat_id: int, confirm_token: str) -> str:
    data = await _request("POST", "/checkout", chat_id, {"confirm_token": confirm_token})
    if err := _err(data):
        return err
    tag = "  [DRY RUN - no real order]" if data.get("dry_run") else ""
    return (
        f"Order created: {data['order_id']}\n"
        f"Status: {data['status']}{tag}\n"
        + (f"Authorize payment here: {data['authorization_url']}\n"
           if data.get("authorization_url") else "")
        + f"\nUse commerce_order_status with order_id={data['order_id']} to follow it."
    )


async def payment_status(chat_id: int, order_id: str) -> str:
    data = await _request("GET", f"/payment/{order_id}", chat_id, timeout=_SLOW_TIMEOUT)
    if err := _err(data):
        return err
    tag = "  [DRY RUN]" if data.get("dry_run") else ""
    return f"Order {data['order_id']}: {data['status']}  {data.get('total_display', '')}{tag}"


async def over_spending_cap(total_minor: Optional[int]) -> Optional[str]:
    """Why this total exceeds Mimir's own ceiling, or None.

    Separate from the provider's flags on purpose: Swiggy declares no
    cart-value limit and tells callers not to gate on it. This is the user's
    own guard rail, so it is configured here rather than inferred from the
    provider.
    """
    cap = Config.COMMERCE_MAX_ORDER_RUPEES
    if not cap or total_minor is None:
        return None
    if total_minor > cap * 100:
        return (
            f"This order is Rs.{total_minor / 100:.2f}, over your Rs.{cap} limit. "
            "Raise COMMERCE_MAX_ORDER_RUPEES in .env if that is intended."
        )
    return None


async def live_spending_blocked(chat_id: int) -> Optional[str]:
    """Why a real spend must not proceed, or None if it may.

    Two independent switches have to agree: the BFF's own DRY_RUN must be off
    AND MIMIR's COMMERCE_ALLOW_LIVE must be on. Either one left at its safe
    default stops real money moving, so a single flag flipped by accident in
    either repository cannot place an order.
    """
    info = await health(chat_id)
    if not info.get("ok"):
        return info.get("error") or "Commerce service is not reachable."

    bff_dry_run = str(info.get("dry_run", "true")).strip().lower() in ("1", "true", "yes")
    if bff_dry_run:
        return None  # nothing real can happen; safe to proceed

    if not Config.COMMERCE_ALLOW_LIVE:
        return (
            "REFUSING to checkout: the commerce service has DRY_RUN=false (real "
            "orders enabled) but MIMIR's COMMERCE_ALLOW_LIVE is not set. Set "
            "COMMERCE_ALLOW_LIVE=true in MIMIR's .env only when you intend to "
            "spend real money."
        )
    return None
