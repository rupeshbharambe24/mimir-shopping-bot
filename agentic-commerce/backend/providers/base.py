"""The ProviderAdapter abstraction (ADR-1) and CapabilityFlags (ADR-1/ADR-5).

Every provider is accessed through this one interface. Everything upstream
(comparison engine, capability matrix, the agent loop's tool wiring) is written
against this Protocol and the Canonical* types — never against a provider's raw
MCP tool names.

Deviations from the interface sketch in ARCHITECTURE_DECISIONS.md, all recorded
in ADRs:

* `checkout()` returns a `CanonicalPaymentIntent` and a separate
  `poll_payment()` resolves it — instead of a single synchronous
  `checkout() -> CanonicalOrderResult`. See docs/adr/ADR-007 for the full
  pros/cons and the revert path.
* `CapabilityFlags` carries `requires_pre_confirmation` and `max_order_value`
  so the money-path safety rules are provider-declared data (checked by the
  agent loop, ADR-6) rather than hardcoded `if provider == "swiggy"` branches.
* `call_raw_tool()` and `close()` are added as pragmatic necessities: the agent
  loop (ADR-6) drives the *selected* provider's raw MCP tools through the
  adapter, and connections need deterministic teardown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from backend.canonical.types import (
    CanonicalCart,
    CanonicalCartItem,
    CanonicalOrderResult,
    CanonicalPaymentIntent,
    CanonicalSearchQuery,
    CanonicalSearchResult,
)


class NotSupported(Exception):
    """Raised when a capability is called on a provider that doesn't declare it."""


@dataclass
class RawTool:
    """A provider's raw MCP tool as reported by tools/list — used for the
    capability matrix (FR3) and for wiring the agent loop's tool set."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class RawToolResult:
    """The normalized outcome of calling a raw MCP tool (what the agent loop
    feeds back to the model)."""

    text: str
    is_error: bool = False
    structured: Optional[dict[str, Any]] = None


@dataclass
class CapabilityFlags:
    """What a provider can do, declared in the registry (ADR-3) and verified
    against its real tools/list (ADR-3 drift check)."""

    # Can comparison include this provider BEFORE the user logs into it?
    # (ADR-5: Swiggy is False — its tools are address-scoped and need auth.)
    searchable_without_auth: bool = False
    supports_cart: bool = False
    supports_checkout: bool = False

    # --- money-path safety, provider-declared (ADR-6) ---
    # If True, the agent MUST obtain explicit user confirmation (a real UI
    # action / confirm token) before checkout is allowed to run.
    requires_pre_confirmation: bool = True
    # Hard ceiling (in paise) on a single order the agent may place without
    # extra friction. None == no provider-declared cap. Swiggy declares ₹1000.
    max_order_value: Optional[int] = None


@runtime_checkable
class ProviderAdapter(Protocol):
    """Common interface for every commerce provider."""

    id: str
    capability_flags: CapabilityFlags

    # --- lifecycle ---
    async def connect(self) -> None:
        """Open the provider's session (auth + MCP connect). Idempotent."""
        ...

    async def close(self) -> None:
        """Tear down the session. Idempotent."""
        ...

    # --- introspection (capability matrix + agent tool wiring) ---
    async def list_raw_tools(self) -> list[RawTool]:
        """The provider's actual MCP tools (from tools/list)."""
        ...

    async def call_raw_tool(self, name: str, arguments: dict[str, Any]) -> RawToolResult:
        """Execute one raw MCP tool. Used by the agent loop (ADR-6). Adapters
        MUST enforce the money-path guard here, not rely on the caller."""
        ...

    # --- canonical capabilities (comparison engine + UI) ---
    async def search(self, query: CanonicalSearchQuery) -> CanonicalSearchResult:
        ...

    async def get_cart(self) -> CanonicalCart:
        ...

    async def update_cart(self, items: list[CanonicalCartItem]) -> CanonicalCart:
        """Set the cart contents.

        NOTE (manifest-level semantic, ADR-2): some providers — Swiggy included —
        REPLACE the entire cart on update rather than merging. Adapters whose
        underlying tool is destructive must document it; callers that want
        incremental behavior must read-merge-write.
        """
        ...

    async def checkout(self, confirm_token: str) -> CanonicalPaymentIntent:
        """Create the order and begin payment.

        `confirm_token` is proof the user explicitly confirmed this spend (see
        `CapabilityFlags.requires_pre_confirmation`). Returns a
        CanonicalPaymentIntent in PENDING_PAYMENT; the human then authorizes the
        payment out of band and the caller drives `poll_payment()`.

        Raises `NotSupported` if `capability_flags.supports_checkout` is False.
        """
        ...

    async def poll_payment(self, intent: CanonicalPaymentIntent) -> CanonicalOrderResult:
        """Poll an in-flight payment to a terminal CanonicalOrderResult
        (PLACED / FAILED / …). Honors the intent's poll_interval/max_poll hints."""
        ...
