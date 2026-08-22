"""ProviderAdapter implementations for Swiggy Food and Swiggy Dineout.

All three Swiggy services share one OAuth identity, one token cache and one
transport; only the MCP URL, the tool names and the money-path tool set differ.
That shared shape lives in `SwiggyServiceAdapter` here, and Instamart keeps its
own hand-written adapter because its canonical mapping (address-scoped search,
spinId cart items) is genuinely different.

# VERIFY: the assumption that one token opens all three endpoints comes from
Swiggy publishing a single `/auth` base and one set of scopes for all of them.
It has not been exercised against /food or /dineout. If a service 401s on
connect, that assumption is what broke, and each service then needs its own
OAuthConfig with its own token cache.
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
from backend.providers.swiggy import auth, manifest_dineout, manifest_food
from backend.providers.swiggy.mcp_compat import (
    GuardedSwiggySession,
    extract_tool_result_text,
    open_swiggy_stream,
    result_is_error,
    result_structured_content,
)


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").strip().lower() in ("1", "true", "yes")


def _as_payload(result) -> Any:
    structured = result_structured_content(result)
    if structured is not None:
        return structured
    text = extract_tool_result_text(result)
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {"text": text}


class SwiggyServiceAdapter:
    """Shared plumbing for a Swiggy MCP service.

    Subclasses set `id`, `mcp_url`, `dangerous_tools`, `tool_map` and the
    capability flags. Nothing here hardcodes a tool name.
    """

    id: str = ""
    mcp_url: str = ""
    dangerous_tools: frozenset[str] = frozenset()
    tool_map: dict[str, str] = {}

    def __init__(self) -> None:
        self.capability_flags = CapabilityFlags(
            searchable_without_auth=False,
            supports_cart=True,
            supports_checkout=True,
            requires_pre_confirmation=True,
            # No provider-declared ceiling, matching Instamart: Swiggy states it
            # imposes none and instructs callers not to pre-empt its own
            # refusal. The client's own cap does that job.
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
        access_token = auth.get_access_token()

        stack = contextlib.AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(
                open_swiggy_stream(access_token, self.mcp_url)
            )
            session = await stack.enter_async_context(
                GuardedSwiggySession(
                    read_stream, write_stream, dangerous_tools=self.dangerous_tools
                )
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
            raise RuntimeError(f"{type(self).__name__}.connect() must be called first.")
        return self._session

    async def _resolve_address_id(self, preferred: Optional[str] = None) -> Optional[str]:
        """The addressId every address-scoped tool requires.

        Food errors with a bare "addressId is required" on search, cart and menu
        without it -- which is what made this adapter appear broken end to end
        while auth and the tool names were perfectly fine. Instamart's adapter
        has carried the same resolver since it was written; this one shipped
        without it because the published reference does not mark these
        parameters required.

        Falls back to the first saved address, matching Instamart. That is a
        real limitation, not a preference: Swiggy's own get_addresses result
        ends "Ask the user which address to use for delivery", and someone with
        a Home and an Office saved gets whichever Swiggy lists first. The agent
        loop drives the raw tools and can ask; this canonical path cannot, so it
        picks one rather than failing.
        """
        if preferred:
            return preferred
        if self._address_id_cache:
            return self._address_id_cache
        tool = self.tool_map.get("get_addresses")
        if not tool:
            return None
        payload = _as_payload(await self._require_session().call_tool(tool, {}))
        addrs = payload.get("addresses") if isinstance(payload, dict) else None
        if isinstance(addrs, list) and addrs and isinstance(addrs[0], dict):
            self._address_id_cache = str(addrs[0].get("id") or addrs[0].get("addressId") or "") or None
        return self._address_id_cache

    async def _with_address(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Add addressId to a call that needs one and does not have it.

        Applied in call_raw_tool as well as the canonical methods, so the agent
        loop benefits too: the model is told by the tool description to call
        get_addresses first, but when it forgets, the failure is an opaque
        "addressId is required" that it cannot always recover from.
        """
        scoped = getattr(self, "address_scoped_tools", frozenset())
        if name not in scoped or args.get("addressId"):
            return args
        address_id = await self._resolve_address_id()
        return {**args, "addressId": address_id} if address_id else args

    # ------------------------------------------------------------ introspection
    async def list_raw_tools(self) -> list[RawTool]:
        if self._tools_cache is not None:
            return self._tools_cache
        listed = await self._require_session().list_tools()
        tools = [
            RawTool(
                name=t.name,
                description=t.description or "",
                input_schema=getattr(t, "inputSchema", None)
                or getattr(t, "input_schema", None)
                or {},
            )
            for t in listed.tools
        ]
        self._tools_cache = tools
        return tools

    async def call_raw_tool(self, name: str, arguments: dict[str, Any]) -> RawToolResult:
        session = self._require_session()

        # DRY_RUN fakes every tool THIS service considers dangerous -- read from
        # self.dangerous_tools, never a literal, so a service is never dry-run
        # in name only. See adapter.SwiggyAdapter.call_raw_tool for the bug this
        # shape prevents.
        if _dry_run() and name in self.dangerous_tools:
            fake = {"status": "PLACED", "orderId": f"DRYRUN-{self.id}", "dryRun": True}
            return RawToolResult(text=json.dumps(fake), is_error=False, structured=fake)

        try:
            result = await session.call_tool(name, await self._with_address(name, arguments or {}))
        except Exception as e:
            return RawToolResult(text=f"Tool call raised an exception: {e}", is_error=True)
        return RawToolResult(
            text=extract_tool_result_text(result),
            is_error=result_is_error(result),
            structured=_as_payload(result),
        )

    def arm_dangerous(self) -> None:
        self._require_session().armed = True

    # -------------------------------------------------------- canonical methods
    async def get_cart(self) -> CanonicalCart:
        tool = self.tool_map.get("get_cart")
        if not tool:
            return CanonicalCart(self.id, [], Money(None))
        args = await self._with_address(tool, {})
        result = await self._require_session().call_tool(tool, args)
        return self._parse_cart(_as_payload(result))

    async def update_cart(self, items: list[CanonicalCartItem]) -> CanonicalCart:
        raise NotSupported(
            f"{self.id}: cart mutation goes through the raw tools. "
            "The canonical shape is used for reading and comparison only."
        )

    async def clear_cart(self) -> CanonicalCart:
        """Empty the cart with the service's dedicated tool.

        Present on every adapter for the same reason Instamart has one:
        `update_cart([])` succeeds without clearing, so a caller emptying a cart
        that way is told it is empty when it is not, then adds to a basket that
        already has items in it and orders more than intended. Food calls the
        tool `flush_food_cart`; the name is read from tool_map, never assumed.
        """
        tool = self.tool_map.get("clear_cart")
        if not tool:
            raise NotSupported(f"{self.id} has no cart to clear.")
        await self._require_session().call_tool(tool, await self._with_address(tool, {}))
        return await self.get_cart()

    async def checkout(self, confirm_token: str) -> CanonicalPaymentIntent:
        if self.capability_flags.requires_pre_confirmation and not confirm_token:
            raise PermissionError(
                "checkout requires an explicit user confirmation token "
                "(capability_flags.requires_pre_confirmation is True)."
            )
        if _dry_run():
            return CanonicalPaymentIntent(
                provider_id=self.id,
                order_id=f"DRYRUN-{self.id}",
                status=PaymentStatus.PENDING_PAYMENT,
                payment_ref="DRYRUN-PAAS",
                raw={"dryRun": True},
            )
        tool = self.tool_map["checkout"]
        args = await self._with_address(tool, {})
        self.arm_dangerous()
        result = await self._require_session().call_tool(tool, args)
        payload = _as_payload(result)
        return CanonicalPaymentIntent(
            provider_id=self.id,
            order_id=str(payload.get("orderId") or payload.get("order_id") or ""),
            status=PaymentStatus.PENDING_PAYMENT,
            raw=payload if isinstance(payload, dict) else {"raw": payload},
        )

    async def poll_payment(self, intent: CanonicalPaymentIntent) -> CanonicalOrderResult:
        if intent.raw.get("dryRun"):
            return CanonicalOrderResult(
                provider_id=self.id,
                order_id=intent.order_id,
                status=PaymentStatus.PLACED,
                total=Money(None),
                dry_run=True,
                raw=intent.raw,
            )
        tool = self.tool_map.get("payment_status")
        result = await self._require_session().call_tool(tool, {"orderId": intent.order_id})
        payload = _as_payload(result)
        raw_status = str(payload.get("status", "")).upper() if isinstance(payload, dict) else ""
        status = (
            PaymentStatus.PLACED
            if raw_status in ("PLACED", "SUCCESS", "PAID")
            else PaymentStatus.PENDING_PAYMENT
        )
        return CanonicalOrderResult(
            provider_id=self.id,
            order_id=intent.order_id,
            status=status,
            total=Money(None),
            raw=payload if isinstance(payload, dict) else {"raw": payload},
        )

    def _parse_cart(self, payload: Any) -> CanonicalCart:  # pragma: no cover - overridden
        return CanonicalCart(self.id, [], Money(None), raw=payload)


class SwiggyFoodAdapter(SwiggyServiceAdapter):
    """Restaurant food delivery — mcp.swiggy.com/food."""

    id = manifest_food.PROVIDER_ID
    mcp_url = manifest_food.MCP_URL
    dangerous_tools = manifest_food.DANGEROUS_TOOLS
    tool_map = manifest_food.TOOL_MAP

    address_scoped_tools = manifest_food.ADDRESS_SCOPED

    async def search(self, query: CanonicalSearchQuery) -> CanonicalSearchResult:
        tool = self.tool_map["search"]
        address_id = await self._resolve_address_id(query.address_id)
        if not address_id:
            return CanonicalSearchResult(
                self.id, SearchStatus.NEEDS_CONNECTION,
                error="No delivery address available; Swiggy Food is address-scoped.",
            )
        try:
            result = await self._require_session().call_tool(
                tool, {"query": query.text, "addressId": address_id}
            )
        except Exception as e:
            return CanonicalSearchResult(self.id, SearchStatus.ERROR, error=str(e))
        if result_is_error(result):
            return CanonicalSearchResult(
                self.id, SearchStatus.ERROR, error=extract_tool_result_text(result)
            )
        return CanonicalSearchResult(
            provider_id=self.id,
            status=SearchStatus.OK,
            items=manifest_food.parse_restaurants(_as_payload(result)),
        )

    def _parse_cart(self, payload: Any) -> CanonicalCart:
        return manifest_food.parse_cart(payload)


class SwiggyDineoutAdapter(SwiggyServiceAdapter):
    """Table reservations — mcp.swiggy.com/dineout."""

    id = manifest_dineout.PROVIDER_ID
    mcp_url = manifest_dineout.MCP_URL
    dangerous_tools = manifest_dineout.DANGEROUS_TOOLS
    tool_map = manifest_dineout.TOOL_MAP

    def __init__(self) -> None:
        super().__init__()
        # Dineout books tables; there is no cart to speak of on a free
        # reservation, and no checkout in the delivery sense. Declaring
        # supports_checkout=False keeps the comparison engine and the capability
        # matrix honest about that -- book_table is still guarded as a spend via
        # DANGEROUS_TOOLS, which is the part that matters.
        self.capability_flags = CapabilityFlags(
            searchable_without_auth=False,
            supports_cart=False,
            supports_checkout=False,
            requires_pre_confirmation=True,
            max_order_value=None,
        )

    async def search(self, query: CanonicalSearchQuery) -> CanonicalSearchResult:
        try:
            result = await self._require_session().call_tool(
                self.tool_map["search"], {"query": query.text}
            )
        except Exception as e:
            return CanonicalSearchResult(self.id, SearchStatus.ERROR, error=str(e))
        if result_is_error(result):
            return CanonicalSearchResult(
                self.id, SearchStatus.ERROR, error=extract_tool_result_text(result)
            )
        return CanonicalSearchResult(
            provider_id=self.id,
            status=SearchStatus.OK,
            items=manifest_dineout.parse_restaurants(_as_payload(result)),
        )

    async def checkout(self, confirm_token: str) -> CanonicalPaymentIntent:
        raise NotSupported(
            "swiggy_dineout books tables rather than placing orders. "
            "Use book_table through the agent loop, which gates it as a spend."
        )

    def _parse_cart(self, payload: Any) -> CanonicalCart:
        return manifest_dineout.parse_cart(payload)
