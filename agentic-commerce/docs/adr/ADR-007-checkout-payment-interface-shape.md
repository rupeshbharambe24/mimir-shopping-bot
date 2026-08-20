# ADR-007: `checkout()` is an async payment intent + `poll_payment()`, not one synchronous call

- **Status:** Accepted (Option A)
- **Date:** 2026-08-20
- **Deciders:** project owner (explicit sign-off), with the A/B trade-off below
- **Supersedes:** the `checkout(confirm_token: str) -> CanonicalOrderResult`
  signature sketched in ADR-1 of `ARCHITECTURE_DECISIONS.md`
- **Related:** ADR-1 (Provider Adapter), ADR-5 (auth/capability flags), ADR-6
  (money-path safety scoped to deterministic backend state)

## Context

The `ProviderAdapter` sketch in ADR-1 modelled checkout as a single synchronous
call returning a finished order:

```python
async def checkout(self, confirm_token: str) -> CanonicalOrderResult
```

But the **real** Swiggy Instamart MCP payment flow (verified from
`mcp.swiggy.com/builders/docs/reference/instamart`) is not instantaneous or
fully agent-driven:

1. `checkout` returns **`status: "PENDING_PAYMENT"`** with a `upiIntentUrl` /
   `bridgeUrl` and polling hints (`pollingIntervalInMs`, `maxTimeToPollForInMs`)
   — *"Order not placed yet."*
2. The **human authorizes the payment out of band** — approves the UPI collect
   request / enters their UPI PIN in their own UPI app. The agent cannot do this
   (UPI/NPCI requires cardholder authorization on-device; there is no mandate or
   autopay in this flow).
3. The caller polls `check_payment_status`, then `confirm_order` transitions
   `PENDING_PAYMENT → PLACED`.

The docs also make two rules explicit that are properties of the *provider*, not
of our app: *"ALWAYS get explicit user confirmation before calling"* checkout,
and *"verify cart total is below ₹1000."*

## Decision

Adopt **Option A**:

```python
async def checkout(self, confirm_token: str) -> CanonicalPaymentIntent      # PENDING_PAYMENT + authorization_url + poll hints
async def poll_payment(self, intent) -> CanonicalOrderResult                 # → PLACED / FAILED
```

and move the money-path rules into provider-declared data on `CapabilityFlags`:

```python
requires_pre_confirmation: bool          # Swiggy: True
max_order_value: Optional[int]           # Swiggy: 100_000 paise (₹1000)
```

## Options considered

### Option A — async payment intent + `poll_payment` (CHOSEN)

**Pros**
- **Matches reality.** The interface can represent the "awaiting your UPI PIN"
  state the Flutter payment panel (plan §4/§5) needs — a waiting screen while
  the backend polls.
- **No core refactor later.** Swiggy adapter, mock provider, and the future
  Flutter UI all build on a stable contract; we don't re-cut the central
  abstraction at step 5 — the exact "rewrite every time" pain ADR-1 exists to
  prevent.
- **Safety rules become data, not Swiggy-specific code.** The ₹1000 cap and
  mandatory pre-confirmation are declared flags, so the **mock provider
  exercises the money path** too — step 3 genuinely tests pluggability of the
  risky flow, not just reads.
- **Future-proof.** "Agent proposes → human authorizes → confirm" is exactly
  where AP2 / Visa Trusted Agent Protocol are standardizing; a real provider #2
  slots in naturally.

**Cons**
- Deviates from the ADR-1 sketch — one extra ADR (this file) to review.
- Larger surface up front: two extra types (`CanonicalPaymentIntent`,
  `poll_payment`) before anything runs.
- Slightly speculative in a foundations-only pass where no UI consumes the
  payment flow yet (mitigated: the Swiggy docs already tell us the fields).

### Option B — synchronous `checkout(confirm_token) -> CanonicalOrderResult` (REJECTED)

**Pros**
- Zero deviation from the ADR-1 sketch; least to re-review.
- Smallest surface now; fewer types; fastest to a first runnable core.
- Mirrors the existing terminal script, whose loop blocks and polls internally.

**Cons**
- **Hides the state the UI needs.** A synchronous `checkout()` must either block
  the whole WebSocket turn while a human types their UPI PIN (feels frozen) or
  swallow the `PENDING_PAYMENT` phase.
- **Guarantees a refactor at step 5**, after Swiggy + mock adapters already
  depend on the interface — the costly version of what this architecture is
  meant to avoid.
- **Safety rules get hardcoded** as Swiggy branches, so the mock provider won't
  test them and ADR-6's property stays unproven until a real #2 arrives.

## Consequences

- `CanonicalPaymentIntent` and `PaymentStatus` are added to
  `backend/canonical/types.py`.
- `ProviderAdapter.checkout` returns an intent; `poll_payment` resolves it.
- Adapters that need no online authorization (e.g. a cash/COD path) return an
  intent with `authorization_url = None` and a status that `poll_payment`
  resolves immediately.
- The agent loop (ADR-6) reads `CapabilityFlags.requires_pre_confirmation` /
  `max_order_value` from the selected provider rather than special-casing.

## How to revert to Option B (if the trade-off ever flips)

Revert is deliberately mechanical and localized. Do this only if the async shape
proves to cost more than the refactor it avoids:

1. **Interface** — in `backend/providers/base.py`, change `checkout` back to
   `async def checkout(self, confirm_token: str) -> CanonicalOrderResult` and
   delete `poll_payment` from the `ProviderAdapter` Protocol.
2. **Adapters** — in each adapter (`swiggy/adapter.py`, `_mock_provider/adapter.py`),
   fold the body of `poll_payment` into the end of `checkout` so it blocks until
   the payment reaches a terminal state, and return the `CanonicalOrderResult`
   directly. Keep the internal polling helper — only the *public* shape changes.
3. **Types** — `CanonicalPaymentIntent` / `PaymentStatus` may remain (harmless)
   or be removed if unused. `CanonicalOrderResult` is unchanged either way, so
   nothing downstream of the terminal result needs to change.
4. **Safety flags** — `requires_pre_confirmation` / `max_order_value` are
   orthogonal to this axis; leave them on `CapabilityFlags` regardless (they are
   not part of Option B's cons).
5. **Tests** — update `tests/test_pluggability.py`'s checkout assertions from the
   two-step (intent → poll) flow to the single-call flow.

Because every consumer above the adapter uses `CanonicalOrderResult` for the
terminal state, and `poll_payment`'s logic lives in a reusable helper, the blast
radius of a revert is these five spots — no comparison-engine or UI rewrite.
