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
        return _err(data) or "Something went wrong reading the store list."

    selected = data.get("selected")
    lines = ["\U0001F3EA <b>Stores</b>", ""]
    for p in data["providers"]:
        if p["id"] == selected:
            mark, suffix = "\U0001F535", "  \u2190 <i>using this one</i>"
        else:
            mark, suffix = "\u26AA", ""
        state = (
            "connected" if p["connected"]
            else ("tap /connect " + p["id"] if p["auth_type"] != "none" else "ready")
        )
        lines.append(f"{mark} <b>{p['display_name']}</b>{suffix}")
        lines.append(f"     <i>{state}</i>")
        lines.append("")

    if not selected:
        lines.append("<i>Tell me which store to use, or /connect one.</i>")
    return "\n".join(lines).rstrip()


async def select(chat_id: int, provider_id: str) -> str:
    data = await _request("POST", f"/providers/{provider_id}/select", chat_id)
    err = _err(data)
    return err or f"\U0001F3EA Now shopping at <b>{data.get('selected')}</b>."


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
    return f"\u2705 Connected to <b>{provider_id}</b>. What are you after?"


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
        if data.get("status") == "needs_connection":
            return (
                "\U0001F510 I'm not logged in to that store yet.\n"
                "Run <code>/connect swiggy</code> and I'll try again."
            )
        return f"\u26A0\uFE0F Couldn't search: {data.get('error') or data.get('status') or 'unknown error'}"

    items = data.get("items") or []
    if not items:
        return f"Nothing came back for <b>{query}</b>. Try a different word?"

    in_stock = [i for i in items if i.get("available", True)]
    out_of_stock = [i for i in items if not i.get("available", True)]

    # "provider_id", not "provider" -- the BFF's /search returns the former, and
    # the wrong key here fails silently: every service would quietly render with
    # the generic style and nothing would ever look broken enough to notice.
    return _format_results(query, in_stock, out_of_stock, data.get("provider_id"))


# How each service labels its results, so a restaurant is not called a product
# and a bookable table is not called stock.
_RESULT_STYLE = {
    "swiggy":         ("\U0001F6D2", "in stock",  "Say which one and how many."),
    "swiggy_food":    ("\U0001F35B", "open now",  "Say which restaurant, or ask for its menu."),
    "swiggy_dineout": ("\U0001F37D",  "bookable",  "Say which one, and when you'd like the table."),
}


def _format_results(query, in_stock, out_of_stock, provider_id=None) -> str:
    icon, stock_word, hint = _RESULT_STYLE.get(
        provider_id or "", ("\U0001F50D", "available", "Say which one you want.")
    )

    lines = [f"{icon} <b>{query}</b>", ""]

    # Numbered, because the reply to a list is almost always "the second one"
    # and a number is the shortest way for the model to be told which.
    for n, item in enumerate(in_stock[:8], 1):
        price = item.get("price_display") or ""
        unit = item.get("unit") or ""
        lines.append(f"<b>{n}.</b>  {item['name']}")
        detail = "     "
        if price:
            detail += f"<b>{price}</b>"
        if unit:
            detail += f"   <i>{unit}</i>" if price else f"<i>{unit}</i>"
        if detail.strip():
            lines.append(detail)
        lines.append("")

    if out_of_stock:
        word = "out of stock" if stock_word == "in stock" else "unavailable"
        lines.append(f"<i>\u2026and {len(out_of_stock)} {word}</i>")
        lines.append("")

    priced = [i["price_minor"] for i in in_stock if i.get("price_minor") is not None]
    if priced:
        lines.append(f"\U0001F4B0 Cheapest {stock_word}: <b>\u20B9{min(priced) / 100:,.2f}</b>")
        lines.append("")
    lines.append(f"<i>{hint}</i>")
    return "\n".join(lines).rstrip()


async def cart(chat_id: int) -> str:
    data = await _request("GET", "/cart", chat_id)
    if err := _err(data):
        return err
    return _format_cart(data["cart"])


def _format_cart(c: dict[str, Any]) -> str:
    if not c.get("items"):
        return (
            "\U0001F6D2 Your cart is empty.\n\n"
            "<i>Tell me what you're after and I'll find it.</i>"
        )
    lines = ["\U0001F6D2 <b>Your cart</b>", ""]
    for item in c["items"]:
        lines.append(f"  <b>{item['quantity']}\u00D7</b>  {item['name']}")
        lines.append(f"        <i>{item['price_display']} each</i>")
    lines.append("")
    lines.append("<code>\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500</code>")

    # The store's own itemised bill, so the jump from basket to total is
    # explained rather than just asserted. Rendered in a <pre> block with the
    # value right-aligned: on a phone the fees are the whole point of showing
    # this, and a ragged left-aligned column is where the eye stops reading.
    bill = list(c.get("bill_lines") or [])
    if not bill and c.get("subtotal_display") != c.get("total_display"):
        bill = [{"label": "Subtotal", "value": c.get("subtotal_display", "")}]

    if bill:
        width = max(len(str(b["label"])) for b in bill) + 2
        rows = [f"{str(b['label']):<{width}}{str(b['value']):>9}" for b in bill]
        lines.append("<pre>" + "\n".join(rows) + "</pre>")

    lines.append(f"\U0001F4B3  <b>To pay    {c.get('total_display', '\u2014')}</b>")
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

    head = "\u2705 <b>Added.</b>"
    # Say so when the store gave less than was asked for, and why. It caps
    # against live stock rather than refusing, so the difference is otherwise
    # invisible until the total looks wrong.
    for short in data.get("shortfalls") or []:
        reason = short.get("reason") or "that's all they had in stock"
        head = (
            f"\u26A0\uFE0F <b>Only {short['granted']} available</b> "
            f"(you asked for {short['requested']})\n<i>{reason}</i>"
        )
    return head + "\n\n" + _format_cart(data["cart"])


async def clear_cart(chat_id: int) -> str:
    data = await _request("POST", "/cart/set", chat_id, {"items": []})
    if err := _err(data):
        return err
    return "\U0001F5D1 Cart emptied."


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

async def action_intent(chat_id: int, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Mint a confirmation for a commitment that has no cart behind it.

    Dineout's book_table is the case this exists for: it commits the user to a
    time, a table and a restaurant, and Swiggy's docs say a booking cannot be
    cancelled through the MCP -- but there is no get_cart to snapshot, so
    /checkout/intent has nothing to fingerprint.
    """
    return await _request("POST", "/action/intent", chat_id, {"name": name, "args": args})


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
    dry = "\n\n<i>\u26A0\uFE0F Dry run \u2014 no real order was placed.</i>" if data.get("dry_run") else ""
    out = [f"\U0001F9FE <b>Order {data['order_id']}</b>", f"<i>{data['status'].replace('_', ' ')}</i>"]
    if data.get("authorization_url"):
        out += ["", "\U0001F4B3 <b>Pay here to complete it:</b>", data["authorization_url"]]
    out += ["", "<i>Ask me \u201cwhere's my order?\u201d any time.</i>" + dry]
    return "\n".join(out)


async def payment_status(chat_id: int, order_id: str) -> str:
    data = await _request("GET", f"/payment/{order_id}", chat_id, timeout=_SLOW_TIMEOUT)
    if err := _err(data):
        return err
    icon = {"placed": "\u2705", "failed": "\u274C", "cancelled": "\U0001F6AB"}.get(
        data["status"], "\u23F3"
    )
    tag = "  <i>(dry run)</i>" if data.get("dry_run") else ""
    return (
        f"{icon} <b>Order {data['order_id']}</b>\n"
        f"{data['status'].replace('_', ' ').title()}  \u00B7  {data.get('total_display', '')}{tag}"
    )


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
