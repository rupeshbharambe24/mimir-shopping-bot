"""ProviderAdapter implementation for Swiggy Instamart.

Wraps the ported OAuth (auth.py), MCP transport + guard (mcp_compat.py), and
normalization (manifest.py) behind the common ProviderAdapter interface. The
Swiggy-specific mechanics are unchanged from the working script; only the shape
they're exposed through is new (ADR-1).
"""

from __future__ import annotations

import contextlib
import json
import os
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
from backend.providers.base import (
    CapabilityFlags,
    NotSupported,
    RawTool,
    RawToolResult,
)
from backend.providers.swiggy import auth, manifest
from backend.providers.swiggy.mcp_compat import (
    GuardedSwiggySession,
    extract_tool_result_text,
    make_blocked_result,
    open_swiggy_stream,
    result_is_error,
    result_structured_content,
)


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").strip().lower() in ("1", "true", "yes")


class SwiggyAdapter:
    """Implements the ProviderAdapter Protocol for Swiggy Instamart."""

    id = manifest.PROVIDER_ID
    # Read by the agent loop's money-path guard; see manifest.DANGEROUS_TOOLS.
    dangerous_tools = manifest.DANGEROUS_TOOLS

    def __init__(self) -> None:
        # ADR-5: Swiggy's tools are address-scoped and require an authenticated
        # session, so it CANNOT be searched anonymously. It requires
        # pre-confirmation.
        #
        # max_order_value is None because Swiggy declares no ceiling -- its
        # checkout tool documentation says so explicitly and instructs callers
        # not to gate on cart total. A ₹1000 cap was previously declared here
        # and would have refused ordinary grocery orders. A spending limit is a
        # legitimate thing for a *client* to impose; it is not a fact about
        # this provider.
        self.capability_flags = CapabilityFlags(
            searchable_without_auth=False,
            supports_cart=True,
            supports_checkout=True,
            requires_pre_confirmation=True,
            max_order_value=None,
        )
        self._stack: Optional[contextlib.AsyncExitStack] = None
        self._session: Optional[GuardedSwiggySession] = None
        self._tools_cache: Optional[list[RawTool]] = None
        self._address_id_cache: Optional[str] = None

    # ---------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        if self._session is not None:
            return
        # NOTE: get_access_token() is synchronous and, on first login with no
        # cached token, opens a browser and blocks up to 180s. Acceptable at
        # startup for this foundations build; a service would offload this to a
        # thread / a proper web OAuth callback (flutter web plan §3.2).
        access_token = auth.get_access_token()

        stack = contextlib.AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(
                open_swiggy_stream(access_token)
            )
            session = await stack.enter_async_context(
                GuardedSwiggySession(read_stream, write_stream)
            )
            await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self._session = session

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None
        self._tools_cache = None

    def _require_session(self) -> GuardedSwiggySession:
        if self._session is None:
            raise RuntimeError("SwiggyAdapter.connect() must be called first.")
        return self._session

    async def _resolve_address_id(self, preferred: Optional[str] = None) -> Optional[str]:
        """search_products / your_go_to_items are address-scoped and REQUIRE an
        addressId (verified: they error without one). Use the caller's choice, or
        fall back to the first saved address."""
        if preferred:
            return preferred
        if self._address_id_cache:
            return self._address_id_cache
        session = self._require_session()
        result = await session.call_tool(manifest.TOOL_MAP["get_addresses"], {})
        payload = _as_payload(result)
        addrs = payload.get("addresses", []) if isinstance(payload, dict) else []
        if addrs and isinstance(addrs[0], dict):
            self._address_id_cache = addrs[0].get("id")
        return self._address_id_cache

    # ------------------------------------------------------------ introspection
    async def list_raw_tools(self) -> list[RawTool]:
        if self._tools_cache is not None:
            return self._tools_cache
        session = self._require_session()
        listed = await session.list_tools()
        tools: list[RawTool] = []
        for t in listed.tools:
            schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None) or {}
            tools.append(RawTool(name=t.name, description=t.description or "", input_schema=schema))
        self._tools_cache = tools
        return tools

    async def call_raw_tool(self, name: str, arguments: dict[str, Any]) -> RawToolResult:
        session = self._require_session()

        # DRY_RUN: never let a dangerous tool actually run, even if armed.
        if _dry_run() and name in ("checkout", "confirm_order"):
            fake = {"status": "PLACED", "orderId": "DRYRUN-ORDER", "dryRun": True}
            return RawToolResult(text=json.dumps(fake), is_error=False, structured=fake)

        try:
            result = await session.call_tool(name, arguments or {})
        except Exception as e:  # keep the loop alive; surface as an error result
            return RawToolResult(text=f"Tool call raised an exception: {e}", is_error=True)
        return RawToolResult(
            text=extract_tool_result_text(result),
            is_error=result_is_error(result),
            structured=_as_payload(result),
        )

    def arm_dangerous(self) -> None:
        """Arm the guard for exactly one dangerous-tool call (checkout/confirm_order)."""
        GuardedSwiggySession.armed = True

    # -------------------------------------------------------- canonical methods
    async def search(self, query: CanonicalSearchQuery) -> CanonicalSearchResult:
        session = self._require_session()
        # addressId is REQUIRED (verified: search errors without it).
        address_id = await self._resolve_address_id(query.address_id)
        if not address_id:
            return CanonicalSearchResult(
                self.id, SearchStatus.NEEDS_CONNECTION,
                error="No delivery address available; Swiggy search is address-scoped.",
            )
        args: dict[str, Any] = {"query": query.text, "addressId": address_id, "offset": 0}
        try:
            result = await session.call_tool(manifest.TOOL_MAP["search"], args)
        except Exception as e:
            return CanonicalSearchResult(self.id, SearchStatus.ERROR, error=str(e))
        if result_is_error(result):
            return CanonicalSearchResult(
                self.id, SearchStatus.ERROR, error=extract_tool_result_text(result)
            )
        return CanonicalSearchResult(
            provider_id=self.id,
            status=SearchStatus.OK,
            items=manifest.parse_products(_as_payload(result)),
        )

    async def get_cart(self) -> CanonicalCart:
        session = self._require_session()
        result = await session.call_tool(manifest.TOOL_MAP["get_cart"], {})
        return manifest.parse_cart(_as_payload(result))

    async def update_cart(self, items: list[CanonicalCartItem]) -> CanonicalCart:
        # NOTE (ADR-2): Swiggy's update_cart REPLACES the whole cart. Callers who
        # want incremental behavior must read-merge-write before calling this.
        session = self._require_session()
        address_id = await self._resolve_address_id()
        payload = {
            "selectedAddressId": address_id,  # verified param name
            # VERIFY-WRITE: item field names not confirmed — update_cart is a
            # mutating tool and was NOT exercised during read-only verification.
            # canonical product_id == spinId (see manifest.parse_products).
            "items": [{"spinId": it.product_id, "quantity": it.quantity} for it in items],
        }
        result = await session.call_tool(manifest.TOOL_MAP["update_cart"], payload)
        return manifest.parse_cart(_as_payload(result))

    async def clear_cart(self) -> CanonicalCart:
        """Empty the cart using the provider's dedicated tool.

        update_cart({"items": []}) does NOT clear a Swiggy cart -- the call
        succeeds and the cart comes back unchanged, so a caller emptying it that
        way is told the cart is empty when it is not. Swiggy exposes a separate
        clear_cart tool for this; it was mapped in the manifest but never
        called. Getting this wrong on a shopping flow means the user adds to a
        basket they believe is empty and orders more than they intended.
        """
        session = self._require_session()
        await session.call_tool(manifest.TOOL_MAP["clear_cart"], {})
        return await self.get_cart()

    async def checkout(self, confirm_token: str) -> CanonicalPaymentIntent:
        if not self.capability_flags.supports_checkout:
            raise NotSupported(f"{self.id} does not support checkout")

        # --- money-path guard (ADR-6), driven by provider-declared flags ---
        if self.capability_flags.requires_pre_confirmation and not confirm_token:
            raise PermissionError(
                "checkout requires an explicit user confirmation token "
                "(capability_flags.requires_pre_confirmation is True)."
            )
        cap = self.capability_flags.max_order_value
        if cap is not None:
            cart = await self.get_cart()
            if cart.total.minor_units is not None and cart.total.minor_units > cap:
                raise PermissionError(
                    f"Cart total {cart.total.display()} exceeds the provider cap "
                    f"of {Money(cap).display()}; refusing to checkout."
                )

        if _dry_run():
            return CanonicalPaymentIntent(
                provider_id=self.id,
                order_id="DRYRUN-ORDER",
                status=PaymentStatus.PENDING_PAYMENT,
                authorization_url=None,
                payment_ref="DRYRUN-PAAS",
                raw={"dryRun": True},
            )

        session = self._require_session()
        # Capture addressId + cartId now; poll_payment needs them later.
        cart_raw = _as_payload(await session.call_tool(manifest.TOOL_MAP["get_cart"], {}))
        address_id = await self._resolve_address_id()
        cart_id = cart_raw.get("cartId") if isinstance(cart_raw, dict) else None

        self.arm_dangerous()  # single-shot; guard auto-resets after the call
        # VERIFY-WRITE: checkout params verified as {addressId, paymentMethod,
        # intentApp, generateUPIQR}; payment-method SELECTION is a step-5 UI
        # concern (get_payment_options -> user picks). Defaulting to a UPI QR.
        result = await session.call_tool(manifest.TOOL_MAP["checkout"], {
            "addressId": address_id,
            "paymentMethod": "UPI",
            "generateUPIQR": True,
        })
        if result_is_error(result):
            raise RuntimeError(f"checkout failed: {extract_tool_result_text(result)}")
        intent = manifest.parse_payment_intent(_as_payload(result))
        # Stash context poll_payment needs (addressId/cartId aren't in the intent).
        intent.raw.setdefault("_ctx", {}).update({"addressId": address_id, "cartId": cart_id})
        return intent

    async def poll_payment(self, intent: CanonicalPaymentIntent) -> CanonicalOrderResult:
        if _dry_run() or (intent.raw or {}).get("dryRun"):
            return CanonicalOrderResult(
                provider_id=self.id,
                order_id=intent.order_id,
                status=PaymentStatus.PLACED,
                dry_run=True,
                raw={"dryRun": True},
            )

        import anyio  # local import; only needed on the live path

        session = self._require_session()
        ctx = (intent.raw or {}).get("_ctx", {})
        address_id, cart_id = ctx.get("addressId"), ctx.get("cartId")
        deadline = intent.max_poll_ms
        waited = 0
        last_payload: Any = {}
        while waited < deadline:
            # VERIFY-WRITE: check_payment_status params verified as {paasId,
            # orderId, addressId, cartId, lat, lng, finalize}.
            status_result = await session.call_tool(manifest.TOOL_MAP["payment_status"], {
                "paasId": intent.payment_ref, "orderId": intent.order_id,
                "addressId": address_id, "cartId": cart_id,
            })
            last_payload = _as_payload(status_result)
            order = manifest.parse_order_result(last_payload)
            if order.status == PaymentStatus.PLACED:
                # confirm_order moves the pre-PLACED order to PLACED.
                self.arm_dangerous()
                txn = last_payload.get("transactionId") if isinstance(last_payload, dict) else None
                confirm = await session.call_tool(manifest.TOOL_MAP["confirm_order"], {
                    "orderId": intent.order_id, "transactionId": txn,
                    "paasId": intent.payment_ref, "addressId": address_id, "cartId": cart_id,
                })
                return manifest.parse_order_result(_as_payload(confirm))
            if order.status in (PaymentStatus.FAILED, PaymentStatus.CANCELLED):
                return order
            await anyio.sleep(intent.poll_interval_ms / 1000)
            waited += intent.poll_interval_ms

        return CanonicalOrderResult(
            provider_id=self.id,
            order_id=intent.order_id,
            status=PaymentStatus.UNKNOWN,
            raw=last_payload if isinstance(last_payload, dict) else {"raw": last_payload},
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _as_payload(result) -> Any:
    """Best-effort structured payload from a CallToolResult: prefer the SDK's
    structured content, else parse the text block as JSON, else keep the text."""
    structured = result_structured_content(result)
    if structured is not None:
        return structured
    text = extract_tool_result_text(result)
    if not text:
        return {}
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {"text": text}


# Runtime Protocol conformance is asserted in tests/test_pluggability.py
# (isinstance against the @runtime_checkable ProviderAdapter). Kept out of import
# time so importing the adapter has no side effects.
