# ADR-008: Telegram as a front-end, alongside Flutter Web

- **Status:** Accepted
- **Date:** 2026-08-20
- **Deciders:** project owner
- **Related:** ADR-1 (Provider Adapter), ADR-4 (comparison orchestrator),
  ADR-6 (money-path safety), ADR-007 (async payment intent)

## Context

The platform was built with a Flutter Web front-end talking to the FastAPI BFF
(step 5). A second front-end now exists: **Mimir**, a Telegram bot, which drives
the same BFF over loopback HTTP.

This is not a replacement. The two front-ends have genuinely different shapes,
and the decision worth recording is *which properties each one has* — because
one of them changes a security property of the money path, and one of them
leaks data the other never did.

## Decision

Telegram becomes a first-class front-end for **conversational shopping and the
money path**. Flutter Web remains the front-end for **comparison and the
capability matrix**. Neither is deprecated.

Plain Telegram messages are routed to the BFF's Gemini agent loop
(`POST /agent/message`), which drives the selected provider's **raw MCP tools**.
Mimir retains ownership of exactly one decision: whether to spend.

## Why Telegram is a better home for the money path

**The confirm affordance is out-of-band, and that is the whole point.**
An inline-keyboard tap arrives as a `callback_query` — a different Telegram
update type from the `message` the model reads and writes. The model can emit
any text it likes and cannot produce a `callback_query`. Typing "yes" into a
chat is model-forgeable; tapping a button is not.

This is a stronger guarantee than the web app's `confirm_checkout` WebSocket
message, which travels the same channel as everything else.

**ADR-007's async payment shape fits Telegram natively.**
`checkout → PENDING_PAYMENT → poll → terminal result` maps onto: send the UPI QR
as a photo, poll in the background, push "order placed". The web app had to keep
a WebSocket open for the same flow. Telegram does not — the transport is already
a long-lived push channel.

**Operationally cheaper.** Mobile-first with push notifications, no browser, no
CORS, no cookies, no Flutter build or deploy step. It reuses Mimir's existing
`@authorized` chat lock, `session.py`, and `Guard` scaffolding rather than
adding new ones.

## What Telegram is worse at

**Comparison is the wrong shape for chat.** `/compare` returns N providers × up
to 20 products with images and prices. The Flutter matrix tab renders that well.
A 4096-character text message does not. This is real capability lost, not a
cosmetic difference, and it is why the Flutter front-end is retained rather than
retired.

**Event streaming will hit rate limits.** The WebSocket path emits a `tool_call`
and a `tool_result` event per hop. Telegram caps roughly 30 messages/second
globally and about 1/second sustained per chat. Emitting one message per event
will be throttled; those events must be batched into a single edited message.

**The OAuth flow cannot run inside a handler.** `get_access_token()` is
synchronous and blocks up to 180 seconds waiting for a browser callback on
localhost. Inside a `python-telegram-bot` handler that stalls the event loop for
the whole window — and there is no browser on the user's phone to complete it
anyway. One-time login stays a desktop step (`login-swiggy.sh`,
`login-zepto.sh`).

**Two tool-calling loops exist.** Mimir's `core/agent.py` and the BFF's
`backend/agent/loop.py` both do tool calling. One must own the turn or they nest
awkwardly. Resolved by routing shopping to the BFF loop and leaving Mimir's loop
for everything else.

## Security: what actually crosses the wire

Bot chats are **never** Telegram Secret Chats — there is no end-to-end
encryption available to a bot. Everything below is visible to Telegram the
company, in addition to the LLM provider.

| Data | Where it comes from |
|---|---|
| Delivery addresses | `_resolve_address_id`, `get_addresses` results |
| Cart contents, quantities, totals | `CanonicalCart` |
| Order IDs, `paasId` payment refs | `CanonicalPaymentIntent` |
| UPI authorization URLs / QR images | `authorization_url` |
| **Arbitrary PII in raw payloads** | canonical types retain raw provider JSON |

**That last row is the leak nobody plans for.** `CanonicalProduct.raw`,
`CanonicalCart.raw` and `CanonicalOrderResult.raw` deliberately retain the
provider's original payload — this is by design (ADR-2: "invaluable for
debugging manifest mappings"). But those payloads contain phone numbers, full
addresses and names. A live `get_cart` response observed during development
carried `mobile`, `flatNo` and a full postal address.

Mimir's `MAX_TOOL_RESULT_CHARS = 2000` **truncates but does not redact**.
Whatever lands in the first 2000 characters goes to Telegram and to the LLM.
Truncation is a token-budget control that has been quietly load-bearing as a
privacy control, which is not a property it actually has.

### Correctly not exposed

The provider bearer tokens stay on local disk (`SWIGGY_TOKENS_PATH`,
`ZEPTO_TOKENS_PATH`) and the Gemini key stays server-side. Tool execution is
local, so only tool *names, schemas and results* reach the model — never
credentials. This property is worth stating explicitly so a future refactor does
not trade it away without noticing.

## Four sharp edges to fix before extending this further

1. **Redact `raw` before it leaves the process.** Add a redaction pass over
   canonical objects at the BFF boundary that strips known PII keys (`mobile`,
   `phone`, `flatNo`, `address`, `name`, `email`, `lat`, `lng`) from `.raw`
   before serialization. Redact at the boundary, not at render time, so every
   front-end inherits it. Do not rely on `MAX_TOOL_RESULT_CHARS` for this.

2. **Batch streaming events into one edited message.** Accumulate `tool_call` /
   `tool_result` events and edit a single status message at a bounded rate
   (roughly 1/second per chat) rather than sending one message per event.

3. **Make the money guard provider-declared, not global.** Done in this change
   set: each manifest declares `DANGEROUS_TOOLS` and the loop reads it from the
   adapter. Recorded here because the failure mode is silent — a provider naming
   its order tool differently was not blocked and nothing raised. The mock
   provider (`place_order`, `finalize_order`) was in exactly that state while
   carrying a comment claiming it was guarded.

4. **Bind the confirmation to a cart fingerprint.** Done in this change set. A
   bare `armed` boolean authorizes whatever the cart holds when the call lands,
   so an agent adding an item between the user reading the total and tapping
   Approve would spend against an approval never given. `POST /checkout/intent`
   fingerprints the exact cart shown; the token is void if it moves.

## Consequences

- Two front-ends must be kept working. The BFF is the shared contract; neither
  front-end may grow provider-specific knowledge.
- The approval UI is stronger on Telegram than on the web. If the web app is
  ever the only front-end again, its confirm affordance needs revisiting.
- `raw` payload redaction is now a known outstanding gap, tracked as edge 1.

## Note on ADR-007's ₹1000 cap

ADR-007 records, as a provider property, *"verify cart total is below ₹1000."*
The **live** Swiggy `checkout` tool description now states the opposite:

> "NO CART-VALUE LIMIT: Do NOT refuse, warn about, or gate checkout on the cart
> total — there is no value ceiling... If the order is not permitted for this
> account, checkout itself fails and returns the reason; relay that error
> instead of pre-empting it."

The declared `max_order_value: 100000` was refusing ordinary grocery orders and
has been set to `null`, with the ceiling moved to the client
(`COMMERCE_MAX_ORDER_RUPEES`). A self-imposed spending limit is a legitimate
thing for a client to want; it is not a fact about the provider. ADR-007's
reasoning about the *payment shape* is unaffected — only the cap claim is
superseded.
