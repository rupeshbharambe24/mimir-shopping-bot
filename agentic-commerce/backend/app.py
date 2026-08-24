"""FastAPI Backend-for-Frontend (step 5).

A thin server sitting between the Flutter Web app and both Gemini + the provider
MCPs. It keeps the provider bearer tokens and the Gemini key server-side (never
in the browser), runs the deterministic comparison/matrix code paths (ADR-4/FR3),
and streams the agent loop over a WebSocket.

Session model (flutter web plan §3.1): one in-memory Session per browser,
keyed by an `sid` cookie. Holds the selected provider, the set of connected
providers, per-session adapter instances, the Gemini history, and the checkout
`armed` flag. In-memory is fine for a single-instance demo — swap for Redis
before multiple concurrent users (plan §6).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from backend.canonical.types import (
    CanonicalCart,
    CanonicalCartItem,
    CanonicalPaymentIntent,
    CanonicalSearchQuery,
    Money,
    PaymentStatus,
    SearchStatus,
)
from backend.comparison import matrix as matrix_mod
from backend.comparison import orchestrator
from backend.providers import registry
from backend.providers.base import ProviderAdapter

load_dotenv()

app = FastAPI(title="Agentic Commerce BFF", version="0.1.0")

# Flutter Web dev server runs on a different localhost origin.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_ENTRIES = registry.load_registry()
_ENTRY_BY_ID = {e.id: e for e in _ENTRIES}


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

# How long a minted checkout confirmation stays valid. Matches RAVEN's
# PERMISSION_TIMEOUT default so a Telegram approval prompt that times out and a
# token that expires happen at the same moment rather than confusingly apart.
CONFIRM_TTL_S = 300


@dataclass
class PendingConfirmation:
    """A single-use authorization to spend, bound to one exact cart.

    The WS chat path arms checkout with a bare boolean (`Session.armed`). That is
    fine when a human is watching a live chat, but it authorizes *whatever the
    cart happens to contain at call time* — so an agent that adds an item between
    the user seeing the total and the call landing would spend against an
    approval the user never gave. This binds the approval to a fingerprint of the
    cart the user was actually shown; if the cart moves, the token is void.
    """

    token: str
    provider_id: str
    cart_fingerprint: str
    total_minor: Optional[int]
    summary: str
    created_at: float
    used: bool = False

    def is_valid_now(self) -> tuple[bool, str]:
        if self.used:
            return False, "This confirmation was already used."
        if time.time() - self.created_at > CONFIRM_TTL_S:
            return False, f"This confirmation expired (valid for {CONFIRM_TTL_S}s)."
        return True, ""


def action_fingerprint(provider_id: str, name: str, args: dict) -> str:
    """Stable hash of the exact call the user is agreeing to.

    The cart-bound token exists because "checkout is approved" authorises
    whatever the cart holds when the call lands. A booking has the same hole in
    a different shape: approving "book a table" would authorise whatever slot,
    restaurant and party size the agent sends next. Binding to the arguments
    closes it -- change the time and the token is void.
    """
    payload = json.dumps(
        {"provider": provider_id, "tool": name, "args": args},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def cart_fingerprint(cart: CanonicalCart) -> str:
    """Stable hash of what the user is agreeing to buy."""
    payload = json.dumps(
        {
            "provider": cart.provider_id,
            "total": cart.total.minor_units,
            "items": sorted(
                [[i.product_id, i.quantity, i.price.minor_units] for i in cart.items],
                key=lambda r: str(r[0]),
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def bill_lines(cart: CanonicalCart) -> list[dict[str, str]]:
    """The provider's own itemised bill, as label/value pairs.

    Worth surfacing rather than showing only a total: a Rs.21 basket can come to
    Rs.86 once handling and delivery are added, and a confirmation screen that
    shows only the final number gives the user no way to see why.
    """
    bill = cart.bill_breakdown or {}
    out: list[dict[str, str]] = []
    for line in bill.get("lineItems") or []:
        if isinstance(line, dict) and line.get("label"):
            out.append({"label": str(line["label"]), "value": str(line.get("value", ""))})
    return out


def cart_to_json(cart: CanonicalCart) -> dict[str, Any]:
    return {
        "provider_id": cart.provider_id,
        "subtotal_display": cart.subtotal.display(),
        "total_display": cart.total.display(),
        "total_minor": cart.total.minor_units,
        "bill_lines": bill_lines(cart),
        "items": [
            {
                "product_id": i.product_id,
                "name": i.name,
                "quantity": i.quantity,
                "price_display": i.price.display(),
                "line_total_minor": i.line_total_minor,
            }
            for i in cart.items
        ],
    }


def cart_summary_text(cart: CanonicalCart) -> str:
    """One-glance description for a confirmation prompt.

    Includes the provider's itemised bill when it gives one: the difference
    between a Rs.21 basket and a Rs.86 total is entirely fees, and the person
    tapping Approve should be able to see that rather than wonder.
    """
    if not cart.items:
        return "(empty cart)"
    lines = [f"{i.quantity} x {i.name} @ {i.price.display()}" for i in cart.items]
    for line in bill_lines(cart):
        lines.append(f"{line['label']}: {line['value']}")
    lines.append(f"Total: {cart.total.display()}")
    return "\n".join(lines)


@dataclass
class Session:
    sid: str
    selected: Optional[str] = None
    connected: set[str] = field(default_factory=set)
    adapters: dict[str, ProviderAdapter] = field(default_factory=dict)
    history: list = field(default_factory=list)
    armed: bool = False
    pending: dict[str, PendingConfirmation] = field(default_factory=dict)
    intents: dict[str, CanonicalPaymentIntent] = field(default_factory=dict)
    # Built once per session: listing a provider's tools costs a round trip and
    # the declarations never change within a connection.
    gemini_tools: Any = None
    genai_client: Any = None

    def adapter(self, provider_id: str) -> ProviderAdapter:
        if provider_id not in self.adapters:
            self.adapters[provider_id] = registry.build_adapter(_ENTRY_BY_ID[provider_id])
        return self.adapters[provider_id]


_SESSIONS: dict[str, Session] = {}


def _get_session(request: Request, response: Response) -> Session:
    # Accept the session id from a cookie, an X-Session-Id header, or a ?sid=
    # query param. The header/query paths let the Flutter Web client hold a
    # stable id without relying on cross-origin cookies.
    sid = (
        request.headers.get("x-session-id")
        or request.query_params.get("sid")
        or request.cookies.get("sid")
    )
    if not sid:
        sid = secrets.token_urlsafe(16)
    if sid not in _SESSIONS:
        _SESSIONS[sid] = Session(sid=sid)
    response.set_cookie("sid", sid, httponly=True, samesite="lax")
    return _SESSIONS[sid]


def _session_for_ws(ws: WebSocket) -> Session:
    sid = ws.cookies.get("sid") or ws.query_params.get("sid")
    if sid and sid in _SESSIONS:
        return _SESSIONS[sid]
    sid = sid or secrets.token_urlsafe(16)
    _SESSIONS.setdefault(sid, Session(sid=sid))
    return _SESSIONS[sid]


# --------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"ok": True, "dry_run": os.environ.get("DRY_RUN", "true"), "providers": list(_ENTRY_BY_ID)}


@app.get("/providers")
async def providers(request: Request, response: Response):
    s = _get_session(request, response)
    return {
        "selected": s.selected,
        "providers": [
            {
                "id": e.id,
                "display_name": e.display_name,
                "enabled": e.enabled,
                "auth_type": e.auth_type,
                "connected": e.id in s.connected,
                "flags": dataclasses.asdict(e.declared_flags),
            }
            for e in _ENTRIES
        ],
    }


@app.post("/providers/{provider_id}/select")
async def select_provider(provider_id: str, request: Request, response: Response):
    s = _get_session(request, response)
    if provider_id not in _ENTRY_BY_ID:
        return {"ok": False, "error": "unknown provider"}
    s.selected = provider_id
    return {"ok": True, "selected": s.selected}


@app.post("/providers/{provider_id}/connect")
async def connect_provider(provider_id: str, request: Request, response: Response):
    """Connect a provider for this session. For Swiggy this uses the cached
    OAuth token (or triggers the localhost login flow server-side). A production
    deployment would replace this with the popup + HTTPS-callback web flow."""
    s = _get_session(request, response)
    if provider_id not in _ENTRY_BY_ID:
        return {"ok": False, "error": "unknown provider"}
    try:
        await s.adapter(provider_id).connect()
        s.connected.add(provider_id)
        return {"ok": True, "connected": sorted(s.connected)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class CompareRequest(BaseModel):
    query: str
    timeout_s: float = orchestrator.DEFAULT_TIMEOUT_S


@app.post("/compare")
async def compare(req: CompareRequest, request: Request, response: Response):
    s = _get_session(request, response)
    adapters = {e.id: s.adapter(e.id) for e in _ENTRIES if e.enabled}
    targets = orchestrator.build_targets(_ENTRIES, adapters, s.connected)
    result = await orchestrator.compare(
        CanonicalSearchQuery(text=req.query), targets, per_provider_timeout_s=req.timeout_s
    )
    return {
        "query": result.query,
        "cheapest": result.cheapest_by_provider(),
        "rows": [
            {
                "provider_id": r.provider_id,
                "display_name": r.display_name,
                "status": r.status.value,
                "error": r.error,
                "items": [
                    {
                        "product_id": p.product_id,
                        "name": p.name,
                        "price_minor": p.price.minor_units,
                        "price_display": p.price.display(),
                        "unit": p.unit,
                        "available": p.available,
                        "image_url": p.image_url,
                    }
                    for p in r.items[:20]
                ],
            }
            for r in result.rows
        ],
    }


@app.get("/matrix")
async def matrix(request: Request, response: Response, live: bool = False):
    s = _get_session(request, response)
    adapters = {e.id: s.adapter(e.id) for e in _ENTRIES if e.enabled}
    rows = await matrix_mod.build_matrix(_ENTRIES, adapters, live=live)
    return {"live": live, "rows": matrix_mod.matrix_to_dict(rows)}


@app.get("/state")
async def state(request: Request, response: Response):
    """Snapshot for rehydrating the UI: selected/connected providers, and the
    selected provider's current cart (if connected)."""
    s = _get_session(request, response)
    cart_json = None
    if s.selected and s.selected in s.connected:
        try:
            cart = await s.adapter(s.selected).get_cart()
            cart_json = {
                "provider_id": cart.provider_id,
                "total_display": cart.total.display(),
                "items": [
                    {"product_id": i.product_id, "name": i.name, "quantity": i.quantity,
                     "price_display": i.price.display()}
                    for i in cart.items
                ],
            }
        except Exception as e:
            cart_json = {"error": f"{type(e).__name__}: {e}"}
    return {
        "selected": s.selected,
        "connected": sorted(s.connected),
        "armed": s.armed,
        "cart": cart_json,
    }


# --------------------------------------------------------------------------
# Cart + money path over REST
# --------------------------------------------------------------------------
# The WS /chat path drives these through an LLM. These endpoints expose the same
# capabilities to a non-browser client (RAVEN's Telegram agent), which brings its
# own tool-calling loop and its own tap-to-approve confirmation UI.


def _active_adapter(s: Session) -> tuple[Optional[ProviderAdapter], Optional[dict]]:
    """The selected+connected adapter, or (None, error-response)."""
    if not s.selected:
        return None, {"ok": False, "error": "No provider selected. POST /providers/{id}/select first."}
    if s.selected not in s.connected:
        return None, {"ok": False, "error": f"Provider '{s.selected}' is not connected. POST /providers/{s.selected}/connect first."}
    return s.adapter(s.selected), None


@app.get("/cart")
async def get_cart(request: Request, response: Response):
    s = _get_session(request, response)
    adapter, err = _active_adapter(s)
    if err:
        return err
    try:
        return {"ok": True, "cart": cart_to_json(await adapter.get_cart())}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class CartLine(BaseModel):
    product_id: str
    quantity: int
    name: str = ""
    price_minor: Optional[int] = None


class CartWrite(BaseModel):
    items: list[CartLine]


def _to_canonical_items(lines: list[CartLine]) -> list[CanonicalCartItem]:
    return [
        CanonicalCartItem(
            product_id=l.product_id,
            name=l.name or l.product_id,
            quantity=l.quantity,
            price=Money(l.price_minor),
        )
        for l in lines
    ]


@app.post("/cart/set")
async def set_cart(body: CartWrite, request: Request, response: Response):
    """Replace the cart wholesale."""
    s = _get_session(request, response)
    adapter, err = _active_adapter(s)
    if err:
        return err
    try:
        if not body.items:
            # Emptying is not "update to an empty list": Swiggy's update_cart
            # silently ignores that and returns the cart unchanged, so the caller
            # is told it worked. Providers expose a dedicated tool; use it.
            clear = getattr(adapter, "clear_cart", None)
            if clear is not None:
                return {"ok": True, "cart": cart_to_json(await clear())}
        cart = await adapter.update_cart(_to_canonical_items(body.items))
        return {"ok": True, "cart": cart_to_json(cart)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/cart/add")
async def add_to_cart(body: CartWrite, request: Request, response: Response):
    """Add items, preserving what is already in the cart.

    ADR-2 records that some providers -- Swiggy among them -- REPLACE the entire
    cart on update rather than merging, and that callers wanting incremental
    behaviour must read-merge-write. Doing that merge here rather than in every
    client means an "add milk" from any front-end cannot silently wipe a cart the
    user spent five minutes filling.
    """
    s = _get_session(request, response)
    adapter, err = _active_adapter(s)
    if err:
        return err
    try:
        existing = await adapter.get_cart()
        merged: dict[str, CanonicalCartItem] = {i.product_id: i for i in existing.items}
        for line in _to_canonical_items(body.items):
            if line.product_id in merged:
                prev = merged[line.product_id]
                merged[line.product_id] = CanonicalCartItem(
                    product_id=prev.product_id,
                    name=prev.name or line.name,
                    quantity=prev.quantity + line.quantity,
                    # Keep the price we already knew; a caller adding a line does
                    # not necessarily know the provider's current unit price.
                    price=prev.price if prev.price.minor_units is not None else line.price,
                )
            else:
                merged[line.product_id] = line
        cart = await adapter.update_cart(list(merged.values()))
        result = {"ok": True, "cart": cart_to_json(cart)}

        # Providers cap quantities against live stock and say so in the payload
        # rather than failing the call. Swiggy returns quantity=1 with
        # maxQuantityMessage "That's all we have in stock at the moment!" for a
        # request of 3. Quietly returning a smaller cart than was asked for is
        # the wrong outcome for something the user is about to pay for.
        granted = {i.product_id: i.quantity for i in cart.items}
        shortfalls = []
        for line in _to_canonical_items(body.items):
            want = merged[line.product_id].quantity
            got = granted.get(line.product_id, 0)
            if got < want:
                shortfalls.append({
                    "product_id": line.product_id,
                    "requested": want,
                    "granted": got,
                    "reason": _stock_note(cart, line.product_id),
                })
        if shortfalls:
            result["shortfalls"] = shortfalls
        return result
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class SearchRequest(BaseModel):
    query: str
    limit: int = 20


@app.post("/search")
async def search_selected(body: SearchRequest, request: Request, response: Response):
    """Search only the selected provider.

    /compare fans out across every provider, which is the right shape when
    several are connected and you want a price table. With a single real
    provider it is the wrong shape: it spends a round trip on providers that
    answer `needs_connection`, and it returns a cross-provider "cheapest" number
    that means nothing when there is only one of them.
    """
    s = _get_session(request, response)
    adapter, err = _active_adapter(s)
    if err:
        return err
    try:
        result = await adapter.search(
            CanonicalSearchQuery(text=body.query, limit=body.limit)
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return {
        "ok": result.status == SearchStatus.OK,
        "provider_id": result.provider_id,
        "status": result.status.value,
        "error": result.error,
        "items": [
            {
                "product_id": p.product_id,
                "name": p.name,
                "price_minor": p.price.minor_units,
                "price_display": p.price.display(),
                "unit": p.unit,
                "available": p.available,
            }
            for p in result.items[: body.limit]
        ],
    }


def _stock_note(cart: CanonicalCart, product_id: str) -> str:
    """The provider's own explanation for a capped quantity, if it gave one."""
    for raw in (cart.raw or {}).get("items") or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("spinId") or raw.get("skuId") or "") == product_id:
            return str(raw.get("maxQuantityMessage") or "").strip()
    return ""


@app.post("/checkout/intent")
async def checkout_intent(request: Request, response: Response):
    """Snapshot the cart and mint a single-use confirmation token.

    This does NOT spend anything. The caller shows `summary`/`total_display` to
    the human, gets an explicit approval, then calls POST /checkout with the
    token. Splitting it this way is what lets the approval be bound to an exact
    cart instead of to "whatever is in the cart when checkout lands".
    """
    s = _get_session(request, response)
    adapter, err = _active_adapter(s)
    if err:
        return err

    flags = adapter.capability_flags
    if not flags.supports_checkout:
        return {"ok": False, "error": f"Provider '{s.selected}' does not support checkout."}

    try:
        cart = await adapter.get_cart()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if not cart.items:
        return {"ok": False, "error": "Cart is empty; nothing to check out."}

    cap = flags.max_order_value
    if cap is not None and cart.total.minor_units is not None and cart.total.minor_units > cap:
        return {
            "ok": False,
            "error": (
                f"Cart total {cart.total.display()} exceeds the provider cap of "
                f"{Money(cap).display()}. Refusing to mint a confirmation."
            ),
        }

    pc = PendingConfirmation(
        token=secrets.token_urlsafe(24),
        provider_id=s.selected,
        cart_fingerprint=cart_fingerprint(cart),
        total_minor=cart.total.minor_units,
        summary=cart_summary_text(cart),
        created_at=time.time(),
    )
    s.pending[pc.token] = pc
    return {
        "ok": True,
        "confirm_token": pc.token,
        "provider_id": pc.provider_id,
        "summary": pc.summary,
        "total_display": cart.total.display(),
        "total_minor": pc.total_minor,
        "expires_in_s": CONFIRM_TTL_S,
        "dry_run": os.environ.get("DRY_RUN", "true").strip().lower() in ("1", "true", "yes"),
        "cart": cart_to_json(cart),
    }


class CheckoutRequest(BaseModel):
    confirm_token: str


@app.post("/checkout")
async def do_checkout(body: CheckoutRequest, request: Request, response: Response):
    """Spend, but only against a valid, unused, cart-matching confirmation."""
    s = _get_session(request, response)
    adapter, err = _active_adapter(s)
    if err:
        return err

    pc = s.pending.get(body.confirm_token)
    if pc is None:
        return {"ok": False, "error": "Unknown confirmation token."}
    ok, why = pc.is_valid_now()
    if not ok:
        return {"ok": False, "error": why}
    if pc.provider_id != s.selected:
        return {"ok": False, "error": f"Confirmation was for '{pc.provider_id}', but '{s.selected}' is selected."}

    # Re-read the cart and compare. The approval was for a specific basket at a
    # specific price; if either moved, the human never agreed to this purchase.
    try:
        cart = await adapter.get_cart()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if cart_fingerprint(cart) != pc.cart_fingerprint:
        pc.used = True  # burn it: the thing that was approved no longer exists
        return {
            "ok": False,
            "error": (
                "The cart changed after the confirmation was issued "
                f"(approved {Money(pc.total_minor).display()}, now {cart.total.display()}). "
                "Confirm again."
            ),
        }

    pc.used = True  # burn BEFORE spending, so a retry can never double-order
    try:
        intent = await adapter.checkout(confirm_token=pc.token)
    except PermissionError as e:
        return {"ok": False, "error": f"Blocked by the provider's money-path guard: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    s.intents[intent.order_id] = intent
    return {
        "ok": True,
        "order_id": intent.order_id,
        "status": intent.status.value,
        "authorization_url": intent.authorization_url,
        "poll_interval_ms": intent.poll_interval_ms,
        "dry_run": bool((intent.raw or {}).get("dryRun")),
    }


@app.get("/payment/{order_id}")
async def payment_status(order_id: str, request: Request, response: Response):
    """Drive an in-flight payment to a terminal state."""
    s = _get_session(request, response)
    adapter, err = _active_adapter(s)
    if err:
        return err
    intent = s.intents.get(order_id)
    if intent is None:
        return {"ok": False, "error": f"No in-flight payment for order '{order_id}'."}
    try:
        result = await adapter.poll_payment(intent)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if result.status in (PaymentStatus.PLACED, PaymentStatus.FAILED, PaymentStatus.CANCELLED):
        s.intents.pop(order_id, None)
    return {
        "ok": True,
        "order_id": result.order_id,
        "status": result.status.value,
        "total_display": result.total.display(),
        "dry_run": result.dry_run,
    }


# --------------------------------------------------------------------------
# Agent turn over REST
# --------------------------------------------------------------------------
# The same Gemini tool-calling loop the WebSocket /chat path runs, exposed to a
# non-browser client. This matters more than it looks: the loop drives the
# provider's RAW MCP tools -- all 14 of Swiggy's, including your_go_to_items,
# get_orders, track_order and get_payment_options. The canonical REST surface
# above deliberately exposes only the handful of operations that are comparable
# ACROSS providers, which is the right abstraction for a price table and the
# wrong one for a conversation. A client that drives only the canonical tools
# loses most of what the provider can actually do.


class AgentMessage(BaseModel):
    text: str
    armed: bool = False


# Gemini rejects the whole request, naming a function declaration, when the
# cached tool schema no longer matches what it will accept. Matched on text
# because the SDK raises a generic ClientError for it.
_STALE_TOOL_MARKERS = (
    "function_declarations",
    "INVALID_ARGUMENT",
    "missing field",
)


def _looks_like_stale_tools(exc: Exception) -> bool:
    blob = f"{type(exc).__name__}: {exc}"
    return any(marker in blob for marker in _STALE_TOOL_MARKERS)


@app.post("/agent/message")
async def agent_message(body: AgentMessage, request: Request, response: Response):
    s = _get_session(request, response)
    adapter, err = _active_adapter(s)
    if err:
        return err
    if not os.environ.get("GEMINI_API_KEY"):
        return {"ok": False, "error": "GEMINI_API_KEY is not set on the commerce service."}

    from google import genai

    from backend.agent import loop as agent_loop

    try:
        if s.genai_client is None:
            s.genai_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        if s.gemini_tools is None:
            s.gemini_tools = await agent_loop.build_gemini_tools(adapter)
    except Exception as e:
        return {"ok": False, "error": f"Could not prepare the agent: {type(e).__name__}: {e}"}

    events: list[dict] = []

    async def emit(ev):
        events.append(ev)

    try:
        reply = await agent_loop.run_turn(
            s.genai_client, adapter, s.gemini_tools, s.history,
            body.text, armed=body.armed, emit=emit,
        )
    except Exception as e:
        # The tool declarations are cached for the life of the session, on the
        # assumption that a provider's tools do not change under a live
        # connection. Swiggy disproved that: they added create_address and
        # delete_address to a running server, the cached declarations went
        # stale, and Gemini answered 400 INVALID_ARGUMENT to every single turn
        # for days. A cache that can never be rebuilt turns one upstream change
        # into a permanently dead session.
        if _looks_like_stale_tools(e):
            logger.warning("Rebuilding tool declarations after: %s", e)
            s.gemini_tools = None
            try:
                s.gemini_tools = await agent_loop.build_gemini_tools(adapter)
                reply = await agent_loop.run_turn(
                    s.genai_client, adapter, s.gemini_tools, s.history,
                    body.text, armed=body.armed, emit=emit,
                )
            except Exception as retry_exc:
                return {"ok": False, "error": f"{type(retry_exc).__name__}: {retry_exc}"}
        else:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Did the loop refuse a money-path tool for want of confirmation? The caller
    # owns the confirmation UI, so it needs to know to ask.
    blocked = any(
        ev.get("type") == "tool_result" and ev.get("isError")
        and "BLOCKED" in (ev.get("text") or "")
        for ev in events
    )
    blocked_events = [ev for ev in events if ev.get("type") == "blocked"]
    # The LAST refused call, not the first: if the model retried with corrected
    # arguments, the approval must describe what it would actually do now.
    blocked_action = (
        {"name": blocked_events[-1]["name"], "args": blocked_events[-1].get("args") or {}}
        if blocked_events else None
    )

    return {
        "ok": True,
        "reply": reply,
        "blocked_on_confirmation": blocked,
        "blocked_action": blocked_action,
        "tools_used": [ev["name"] for ev in events if ev.get("type") == "tool_call"],
    }


class ActionIntentRequest(BaseModel):
    name: str
    args: dict = {}


@app.post("/action/intent")
async def action_intent(body: ActionIntentRequest, request: Request, response: Response):
    """Mint a single-use token for one exact non-cart commitment.

    The sibling of /checkout/intent, for providers that commit a user to
    something without a cart to read -- Dineout books tables and has no
    get_cart. Spends nothing.

    The tool named here MUST be one the provider itself declares dangerous.
    Accepting any tool name would turn this endpoint into a way to pre-authorise
    an arbitrary call, which is the opposite of what it is for.
    """
    s = _get_session(request, response)
    adapter, err = _active_adapter(s)
    if err:
        return err

    dangerous = set(getattr(adapter, "dangerous_tools", ()) or ())
    if body.name not in dangerous:
        return {
            "ok": False,
            "error": (
                f"'{body.name}' is not a money-path tool for {s.selected}; "
                "it needs no confirmation and none will be minted."
            ),
        }

    pc = PendingConfirmation(
        token=secrets.token_urlsafe(24),
        provider_id=s.selected,
        cart_fingerprint=action_fingerprint(s.selected, body.name, body.args),
        total_minor=None,
        summary=_describe_action(body.name, body.args),
        created_at=time.time(),
    )
    s.pending[pc.token] = pc
    return {
        "ok": True,
        "confirm_token": pc.token,
        "summary": pc.summary,
        "tool": body.name,
        "args": body.args,
        "expires_in_s": CONFIRM_TTL_S,
        "dry_run": os.environ.get("DRY_RUN", "true").strip().lower() in ("1", "true", "yes"),
    }


# Argument names worth showing a human, in the order they read naturally in a
# sentence. Anything else is appended afterwards rather than dropped: an
# approval prompt that silently hides a parameter is worse than an untidy one.
_ACTION_FIELDS = (
    ("restaurantName", "Restaurant"), ("restaurant", "Restaurant"),
    ("restaurantId", "Restaurant id"),
    ("date", "Date"), ("slotTime", "Time"), ("time", "Time"), ("slotId", "Slot"),
    ("partySize", "Party size"), ("guests", "Guests"), ("covers", "Guests"),
    ("addressId", "Address"), ("paymentMethod", "Payment"), ("intentApp", "UPI app"),
)


def _describe_action(name: str, args: dict) -> str:
    verb = {
        "book_table": "Book a table",
        "place_food_order": "Place this food order",
        "checkout": "Place this order",
        "confirm_order": "Confirm this order",
    }.get(name, f"Run {name}")

    lines = [verb, ""]
    shown = set()
    for key, label in _ACTION_FIELDS:
        if key in args and args[key] not in (None, ""):
            lines.append(f"{label}: {args[key]}")
            shown.add(key)
    for key, value in sorted(args.items()):
        if key not in shown and value not in (None, "", [], {}):
            lines.append(f"{key}: {value}")
    return "\n".join(lines).rstrip()


@app.post("/agent/reset")
async def agent_reset(request: Request, response: Response):
    """Forget the conversation history (not the cart)."""
    s = _get_session(request, response)
    s.history = []
    return {"ok": True}


# --------------------------------------------------------------------------
# WebSocket chat (ADR-6: one selected provider drives the agent)
# --------------------------------------------------------------------------

@app.websocket("/chat")
async def chat(ws: WebSocket):
    await ws.accept()
    s = _session_for_ws(ws)

    if not os.environ.get("GEMINI_API_KEY"):
        await ws.send_json({"type": "error", "message": "GEMINI_API_KEY not set on the server."})
        await ws.close()
        return
    if not s.selected:
        await ws.send_json({"type": "error", "message": "Select a provider first (POST /providers/{id}/select)."})
        await ws.close()
        return
    if s.selected not in s.connected:
        await ws.send_json({"type": "error", "message": f"Provider '{s.selected}' not connected."})
        await ws.close()
        return

    # Lazily set up Gemini + tools for the selected provider.
    from google import genai

    from backend.agent import loop as agent_loop

    adapter = s.adapter(s.selected)
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    gemini_tools = await agent_loop.build_gemini_tools(adapter)
    await ws.send_json({"type": "ready", "provider": s.selected,
                        "tools": sum(len(t.function_declarations or []) for t in gemini_tools)})

    async def emit(ev):
        await ws.send_json(ev)

    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")
            if mtype == "confirm_checkout":
                s.armed = True
                await ws.send_json({"type": "armed", "message": "Order placement armed for your next message."})
                continue
            if mtype != "message":
                await ws.send_json({"type": "error", "message": f"unknown message type {mtype!r}"})
                continue

            text = (msg.get("text") or "").strip()
            if not text:
                continue
            try:
                reply = await agent_loop.run_turn(
                    client, adapter, gemini_tools, s.history, text, armed=s.armed, emit=emit
                )
                await ws.send_json({"type": "text", "content": reply})
            except Exception as e:
                await ws.send_json({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                s.armed = False  # single-message arm
    except WebSocketDisconnect:
        pass
