"""Provider-agnostic canonical types (ADR-2).

Everything upstream of a provider — the comparison engine, the capability
matrix, and (later) the Flutter UI — is written against these types, never
against a specific provider's raw MCP tool names or JSON shapes. Each provider's
manifest is responsible for mapping its real request/response JSON into and out
of these types.

Design notes
------------
* Money is stored as an integer number of **paise** (1 rupee = 100 paise) plus a
  currency code, so we never do float arithmetic on prices. Two providers that
  format prices differently ("₹49", "49.00", 4900) all normalize to the same
  integer here. `None` means "price unknown / not returned".
* Every canonical object keeps a `raw` dict of the provider's original payload.
  Nothing in the app should depend on `raw` (that would defeat the abstraction),
  but it's invaluable for debugging manifest mappings and for surfacing fields
  the canonical schema doesn't model yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Money:
    """An amount in the smallest currency unit (paise for INR)."""

    minor_units: Optional[int]  # None == unknown
    currency: str = "INR"

    @property
    def rupees(self) -> Optional[float]:
        return None if self.minor_units is None else self.minor_units / 100

    def display(self) -> str:
        if self.minor_units is None:
            return "—"
        symbol = "₹" if self.currency == "INR" else ""
        return f"{symbol}{self.minor_units / 100:.2f}"


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

@dataclass
class CanonicalSearchQuery:
    """A provider-agnostic search request."""

    text: str
    address_id: Optional[str] = None  # some providers (Swiggy) scope results to an address
    limit: int = 20


@dataclass
class CanonicalProduct:
    provider_id: str
    product_id: str
    name: str
    price: Money
    available: bool = True
    unit: Optional[str] = None         # e.g. "500 g", "1 L", "pack of 6"
    image_url: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


class SearchStatus(str, Enum):
    OK = "ok"
    NEEDS_CONNECTION = "needs_connection"  # auth-required provider, user not connected
    ERROR = "error"
    TIMED_OUT = "timed_out"


@dataclass
class CanonicalSearchResult:
    provider_id: str
    status: SearchStatus = SearchStatus.OK
    items: list[CanonicalProduct] = field(default_factory=list)
    error: Optional[str] = None


# --------------------------------------------------------------------------
# Cart
# --------------------------------------------------------------------------

@dataclass
class CanonicalCartItem:
    product_id: str
    name: str
    quantity: int
    price: Money  # unit price

    @property
    def line_total_minor(self) -> Optional[int]:
        if self.price.minor_units is None:
            return None
        return self.price.minor_units * self.quantity


@dataclass
class CanonicalCart:
    provider_id: str
    items: list[CanonicalCartItem] = field(default_factory=list)
    subtotal: Money = field(default_factory=lambda: Money(None))
    total: Money = field(default_factory=lambda: Money(None))
    # The full itemized bill (delivery fee, taxes, discounts …) kept raw because
    # its shape differs wildly per provider and the comparison table only needs
    # subtotal/total to be comparable.
    bill_breakdown: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Checkout / payment  (ADR-007: async intent + poll, not one blocking call)
# --------------------------------------------------------------------------

class PaymentStatus(str, Enum):
    PENDING_PAYMENT = "pending_payment"  # order created, awaiting the user's authorization
    PLACED = "placed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass
class CanonicalPaymentIntent:
    """Returned by ProviderAdapter.checkout().

    Models the reality that paying is NOT instantaneous: the order is created in
    a PENDING_PAYMENT state and the *human* must authorize the payment out of
    band (for Swiggy: approve the UPI collect / enter the UPI PIN in their UPI
    app). The caller then drives ProviderAdapter.poll_payment() until it
    resolves. See ADR-007 for why this shape was chosen over a single
    synchronous checkout() -> CanonicalOrderResult.
    """

    provider_id: str
    order_id: str
    status: PaymentStatus = PaymentStatus.PENDING_PAYMENT
    # Where/how the human authorizes payment (e.g. a UPI intent/deeplink or a
    # bridge URL rendered as a QR). None for cash-style flows that need no
    # online authorization.
    authorization_url: Optional[str] = None
    # Opaque reference the provider needs to poll payment status (Swiggy: paasId).
    payment_ref: Optional[str] = None
    poll_interval_ms: int = 2000
    max_poll_ms: int = 120_000
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CanonicalOrderResult:
    """Terminal result of a checkout, produced by poll_payment()."""

    provider_id: str
    order_id: str
    status: PaymentStatus
    total: Money = field(default_factory=lambda: Money(None))
    dry_run: bool = False  # True when DRY_RUN faked this instead of a real spend
    raw: dict[str, Any] = field(default_factory=dict)
