# Change log

Hand-maintained, newest first, and the source these commit messages are
written from. Each entry records why a change was made rather than what
changed -- the diff already says what.

Nothing here is authored under the RBIH account. This repository commits under
a personal identity (`git config --local user.name` / `user.email`), set at the
repository level so a global default cannot silently attribute work to the
wrong account.

---

## 2026-08-24 (latest) — "all three providers are OFF" when two were fine

Reported from the startup banner. Checked live: Groq HTTP 200, Gemini HTTP 200,
Ollama refused. Two of the three were working the whole time.

`is_alive()` returned one boolean per provider, so four different situations
printed the same word:

| Reality | Printed | What it actually needs |
|---|---|---|
| unreachable | OFF | investigate |
| 429 rate-limited | OFF | wait a minute |
| 401/403 | OFF | fix the key |
| no key configured | OFF | set one |

During the Groq rate-limit storm earlier that day, every provider read OFF at
once — which says "your setup is broken" when the truth was "you are sending
too fast", "no key" and "not installed" respectively. That banner is also a
boot snapshot that never refreshes, so a wrong verdict sits there all session.

- **`health()`** returns `ok` / `limited` / `bad_key` / `no_key` / `off`.
  `is_alive()` still returns booleans, so nothing that used it breaks.
- **429 is `limited`, not off.** The provider is up and the key is good.
- **One retry** before calling something off, since a cold start can beat the
  network up and people keep looking at that banner.
- **Ollama is labelled optional** in both displays. It is not installed on this
  machine, which is fine — it is the last-resort fallback behind two cloud
  providers, and an unexplained OFF reads like a fault.

12 new tests.

---

## 2026-08-24 (earlier) — the bot now picks its own session back up

Mid-conversation, in the chat:

    No provider selected. POST /providers/{id}/select first.

That is an instruction to a developer holding an HTTP client, shown to someone
buying biscuits — and it told a user who had already connected to go and
connect.

**Why it happened.** The commerce service keeps sessions in memory
(`_SESSIONS`, documented as "fine for a single-instance demo"). Restarting it —
which shipping the last three fixes required — wipes the selected provider and
the connected set. The bot, meanwhile, still has the choice on disk in
`core/session.py`. Neither side was wrong; nothing re-synced them, and only the
user noticed.

A user who connected once should not have to do it again because a process on
the same machine bounced. That is our bookkeeping, not their problem.

- **`_restore_provider`** re-selects and re-connects from the persisted choice.
  Connect is a cached-token read for an already-authorised account, so it costs
  a round trip rather than a fresh login.
- **`_run_commerce_turn`** detects a forgotten session, restores, and retries
  once **silently**. The user sees their answer, not an apology.
- **`/cart` and `/search`** self-heal the same way — `/cart` hit the identical
  wall.
- Only session loss triggers it. A 429 or a 503 does not, because reconnecting
  costs a round trip and on a cold token could block.
- If the restore genuinely fails, the message is plain and names the service
  the user actually chose.

**The matcher had a bug of its own.** `_friendly_outage` looked for "not
selected"; the service says "**No** provider selected", which contains no
"not". The exact string that prompted this fell through to the generic message.
Now matched on "provider selected" and "/select first" as well.

9 new tests.

---

## 2026-08-24 (earlier) — a 503 mid-checkout, in developer language

A shopper tapped PhonePe and got this, in the middle of paying:

    ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This
    model is currently experiencing high demand. Spikes in demand are usually
    temporary. Please try again later.', 'status': 'UNAVAILABLE'}}

Two separate failures.

**The 503 should never have reached the user at all.** Failover only matched
429/RESOURCE_EXHAUSTED/quota -- our own limit. A 503 is THEIR capacity, and it
is the case failover helps most: one model being busy says nothing about
another. It was the single failure mode that did not trigger it. Now `503`,
`UNAVAILABLE`, `OVERLOADED`, `500` and deadline errors all fail over, and after
exhausting every model the whole set is retried once after a short pause,
because Google's own message says spikes "are usually temporary". `400
INVALID_ARGUMENT` is still excluded -- it fails identically everywhere, so
retrying it burns every quota instead of one.

**And the raw error should never be shown to a shopper.** Surfacing the
provider's own words was right for `login-*.sh`, where the reader is the
operator debugging their own machine. Here the reader is someone buying
biscuits: the payload is unreadable, it is frightening next to a payment step,
and "model" is a word about our infrastructure that means nothing to them.

`_friendly_outage` now maps a failure to one of four plain messages, and every
one of them opens with the two facts that matter mid-checkout:

    I'm a bit overloaded right now.
    Nothing was charged and your cart is safe.

    Give me a few seconds and send that again.

The full error still goes to the log, where the operator can find it. Tests
assert that no message can leak a raw payload or the word "model", and that
every path says nothing was charged -- an unexplained failure at a payment step
otherwise reads as "did that go through?"

8 new tests.

---

## 2026-08-24 (later) — why GROQ_API_KEY was never the backup

Reported as "Gemini limit hit, that's why I put the backup Groq, why is it not
working". It was never wired to the thing that was failing.

**There are two LLM layers.** `core/llm_cascade.py` cascades Groq -> Gemini ->
Ollama, and it is the BOT's loop. The commerce agent -- which handles every
shopping turn -- is `agentic-commerce/backend/agent/loop.py`, and it is Gemini
only. The commerce side has never contained the string "Groq". Until the
fallthrough was removed, Groq appeared to be a backup only because a Gemini
failure silently handed the turn to the bot's loop, which is exactly the path
that invented dollar prices.

**Why the quota was hit at all.** The 429 names it precisely:

    quotaId: GenerateContentInputTokensPerModelPerMinute-FreeTier
    quotaDimensions: {"model": "gemini-3.5-flash-lite"}
    limit: 250000

An INPUT-token limit. History was never trimmed, so every turn re-uploaded the
whole conversation including each product-search payload verbatim. Ten searches
in, a single "add it to cart" was sending tens of thousands of tokens of
already-answered questions. The quota was exhausted by repetition, not by use.

- **`trim_history`** keeps the last `AGENT_MAX_USER_TURNS` (6) complete turns.
  It cuts only at a real user turn, which matters more than it looks: tool
  results are appended with `role="user"` as well, so role alone cannot identify
  them, and cutting mid-sequence would orphan a `function_response` whose
  `function_call` had been dropped -- turning a token problem into a hard 400.
- **Per-model failover.** Because the quota is per MODEL, a second Gemini model
  has its own bucket and is a genuine fallback rather than a retry against the
  same wall. `GEMINI_MODEL_FALLBACKS` is tried in order on 429. Only rate limits
  trigger it: a 400 would fail identically everywhere and retrying it just burns
  three quotas instead of one.

Deliberately NOT the Groq cascade. Groq and Gemini speak different
function-calling dialects, and converting the loop that drives the money path is
a change that can introduce a new class of bug. Same-SDK failover first, because
it cannot.

Verified: six rapid turns, `gemini-3.5-flash-lite` rate-limited twice, failover
fired, all six returned 200. Before this they were hard failures.

13 new tests.

---

## 2026-08-24 — Mimir was inventing prices in dollars

Reported as "it forgets things and cannot add to cart". The screenshots showed
worse: a cart the user never built, prices quoted as "$1.99" and "Cart total:
$6.97" for rupee-priced Indian groceries, and — when challenged — a fluent
explanation that dollars were "the store's default display" and would "be
converted to rupees at checkout". None of it was true.

**Root cause.** Swiggy added `create_address` and `delete_address` to a running
Instamart server. `Session.gemini_tools` caches the converted declarations once
per session, on the documented assumption that "the declarations never change
within a connection". They changed. The cached set then contained an array
property with no `items`, which Gemini rejects outright:

    400 INVALID_ARGUMENT ... function_declarations[2]
    .parameters.properties[lines].items: missing field

Every commerce turn 400'd, for days, with no way to rebuild the cache.

**What turned a bug into a liar.** `_run_commerce_turn` returned False on any
error it did not recognise, handing the turn to Mimir's own tool loop as a
graceful fallback. That loop shares none of the commerce agent's conversation,
so it re-ran `commerce_providers` and `commerce_search` from scratch every turn
— 225 times in one session — and asked "which product?" to a user who had just
named one. That is the "forgetting". When its own cascade ran dry (Groq is
8,000 tokens/minute; 24 rate-limit hits, then a Gemini timeout, then Ollama
refused) it fell to the regex classifier and to inventing answers.

Three fixes, each closing a different link in that chain:

- **`sanitize_json_schema`** fills in a permissive `items` for any bare array
  before conversion, and a tool whose schema still will not convert is skipped
  with a warning rather than taking every other tool down with it. A provider is
  entitled to publish `{"type": "array"}`; that should cost one tool, not the
  assistant.
- **The tool cache rebuilds.** A turn failing with a declaration error clears
  `gemini_tools`, rebuilds and retries once. A cache that can never be rebuilt
  turns one upstream edit into a permanently dead session.
- **No more silent fallback.** A commerce failure now says so, shows the
  provider's own error, and states the cart is untouched. Falling back to a
  model that cannot reach the store is worse than returning nothing, because
  the user cannot tell which one answered.

Also: the sidecar had been running since Thursday, three days behind the code —
no `/action/intent`, no Food, no Dineout. Restarted; `/health` now lists all
four providers.

Verified against the live account: search → "add the 58g one, 1 pack" →
`update_cart` fires and the item persists. No re-asking, real rupees.

18 new tests.

---

## 2026-08-22 — the wrong venv now says so, and Food grew two tools

Running `python3 main.py` from the agentic-commerce venv produced a bare
`ModuleNotFoundError: No module named 'telegram'`, which reads as a missing
dependency rather than the wrong interpreter. The two venvs are separate on
purpose — the sidecar pins `mcp==2.0.0` exactly, and merging those pins into
the bot would let a provider-SDK bump take the bot down — but nothing enforced
or explained that at the point of failure.

The obvious next step makes it worse. `pip install telegram` installs an
unrelated 879-byte package squatting the name on PyPI; the real one is
`python-telegram-bot`. It imports fine and then fails with "cannot import name
'Update' from 'telegram'", which looks like a version problem and is not one.

`main.py` now checks the interpreter before anything imports telegram: it
detects a missing library, detects the squatter by the absence of
`telegram.Update`, names the agentic-commerce venv specifically when that is
where you are, prints the exact command to use, and says not to install
`telegram`.

**Food went from 18 tools to 20** between 2026-08-21 and 2026-08-22.
`create_address` and `delete_address` appeared — the same two the Instamart
manifest records as documented-but-absent when it was verified. Swiggy ships
these servers continuously, so EXPECTED_TOOLS is a snapshot, not a contract,
and the ADR-3 drift check exists for exactly this. Manifest updated; the
discovery script now reports an exact match.

Note `delete_address` is destructive but not a money path, so it is NOT in
DANGEROUS_TOOLS — that set is documented as "tools that spend real money" and
widening it silently would blur what an approval prompt means.

---

## 2026-08-21 — Swiggy Food actually works

Food was registered, guarded, flag-checked and completely non-functional. Auth
was fine and all 17 expected tool names existed on the live server; every call
still failed. Three separate defects, none of which the existing tests could
see.

**1. `addressId` is required on six Food tools.** Swiggy's published reference
does not mark it required. The live schema does — `search_restaurants`,
`search_menu`, `get_restaurant_menu`, `get_food_cart`, `update_food_cart` and
`place_food_order` all answer a bare `addressId is required`. Instamart's
adapter has carried a `_resolve_address_id` since it was written; the shared
service adapter shipped without one. Now resolved from `get_addresses`, cached,
and injected — including into `call_raw_tool`, so the agent loop is covered when
the model forgets to call `get_addresses` first.

**2. Both new parsers raised TypeError on their first line.** `CanonicalProduct`
takes `provider_id` as its first field and neither manifest passed it;
`CanonicalCartItem` has no `unit_price` and no `raw`. Dineout had the same bug.
Neither parser had ever been run against a real response — the Food tests
asserted tool names, endpoints, guards and capability flags, and all of them
passed green over parsers that could not construct a single row.

**3. The field names were wrong, and the casing is not consistent within one
service.** Verified live and corrected:

| | expected | actual |
|---|---|---|
| restaurant id | `restaurantId` | `id` |
| open/closed | `isOpen` (bool) | `availabilityStatus: "OPEN"` (**string**) |
| cart item id | `itemId` | `menu_item_id` |
| cart total | `toPay` | `data.pricing.to_pay` |

`search_restaurants` returns camelCase, `get_food_cart` returns snake_case, and
`search_menu` mixes both in one object (`menu_item_id` beside `inStock`).

The availability one failed in the dangerous direction: no `isOpen` key meant
the `True` default won, and **every closed restaurant reported as open**. The
total failed in the safe direction — an em dash rather than a confident ₹0.00.

`costForTwo` arrives as the string `"₹1500 for two"`, which `to_money` cannot
parse and shouldn't: it is an estimate for two people, not a price anyone pays.
It now goes in `unit` alongside cuisine, rating and ETA, so the renderer never
announces "Cheapest: ₹1500" about a number that is not the cost of anything
orderable. Unit price reads `final_price`, not `total` — `total` includes addons
and would multiply them by the quantity a second time in `line_total_minor`.

**4. Switching service never connected it.** Connection is tracked per provider
id and Swiggy is three ids, so `/connect swiggy` left `swiggy_food` unconnected.
Choosing Food then failed with "Provider 'swiggy_food' is not connected", and
the bot's advice for that error was hardcoded to "try /connect swiggy" — which
the user had already done, and which would not have helped. `_apply_service` now
selects *and* connects (one shared token, so it is a cached read), and the error
names the service that is actually unconnected.

**Verified live**: search returns 10 restaurants, the cart parses to ₹442 with
its itemised bill (₹349 + ₹40 delivery + ₹52.73 tax), and a closed restaurant
now reads as closed.

Dineout's 12 tools are confirmed live too (one undocumented:
`render_restaurants_dineout`). Its response shapes remain unverified — reading
them needs a booking flow against a real restaurant, which commits to a table.

19 new tests, built from payloads captured off the live server rather than from
the docs, since the docs are what produced all of the above.

---

## 2026-08-21 — OAuth failures now say what the server said

`login-zepto.sh` died with a 30-line traceback ending in `HTTP Error 500:
Internal Server Error`. The response body, which urllib throws away, was:

    {"error":"server_error",
     "message":"OOM command not allowed when used memory > 'maxmemory'."}

That is Redis refusing writes at its `maxmemory` limit under a `noeviction`
policy — Zepto's authorization server accepts the registration and cannot store
the result. Every new client registration against them is failing, not just
ours. Verified: three attempts over 15s, all identical; their discovery
documents, JWKS and `/authorize` all still answer 200/307, so only the
registration endpoint is affected. The same code path against Swiggy's
`/auth/register` returns 201, which rules out our payload shape.

Nothing here was wrong, and nothing here could be changed to fix it. But the
error that surfaced could not tell you that, so:

- **`_post_json`** now wraps all three OAuth POSTs (registration, token
  exchange, token refresh). On failure it reads the server's body and raises
  `AuthServerError` carrying `status` and `body`, preferring the OAuth
  `error_description` / `message` field for the message text.
- **`is_provider_fault`** distinguishes 5xx from 4xx — the property that decides
  whether the user should change something or simply wait.
- **A timeout on every request.** `urlopen` defaults to none, and these run
  interactively behind a 180s browser wait, so a hung endpoint blocked the login
  forever with nothing on screen.
- **A 200 with no `client_id` no longer raises `KeyError`**, and no longer
  writes a cache file — a poisoned cache would have broken every later login
  with a different, more confusing error.
- Both `login-swiggy.sh` and `login-zepto.sh` catch it and print the cause plus
  whose fault it is, instead of a traceback. Ctrl-C is handled too.

The same failure now reads:

    [!!] Zepto client registration failed: HTTP 500 -- OOM command not allowed
         when used memory > 'maxmemory'.

         That is a fault on Zepto's side, not a problem with this machine or
         your account -- their auth server accepted the request and failed to
         store the result. Nothing here can fix it; try again later.

7 new tests.

---

## 2026-08-20 — Dineout booking, and an honest payment picker

**Booking had no way to be approved.** `/checkout/intent` reads a cart and
fingerprints it; Dineout has no `get_cart` at all, so `book_table` was blocked
by the loop with nothing able to arm it — safe, but the feature simply did not
work.

- `run_turn` now emits a `blocked` event carrying the refused tool's **name and
  arguments**. A caller told only "something was blocked" has to guess what to
  put in the approval prompt, and with no cart to read there is nothing else to
  reconstruct the request from. The *last* refusal is reported, not the first:
  if the model retried with corrected arguments, the approval must describe what
  it would actually do now.
- New `POST /action/intent` mints a single-use token bound to
  `action_fingerprint(provider, tool, args)` — the same idea as the cart
  fingerprint, for the same reason. Approving "book a table" would otherwise
  authorise whatever slot, restaurant and party size the agent sent next. Change
  the time and the token is void.
- The endpoint refuses any tool the provider does not itself declare dangerous.
  Accepting an arbitrary name would turn it into a way to pre-authorise any
  call, which is the opposite of its purpose.
- `_describe_action` shows every argument, listing the readable ones first and
  appending the rest rather than dropping them. A prompt that silently hides a
  parameter hides exactly what the user cannot check.

**Bookings are never auto-approved**, and the reason is not the amount. A free
reservation measures ₹0 against every rupee ceiling, so it would pass all of
them without limit. The routing keeps it out: `_confirm_action` never consults
`_autopay_decision`, and a test asserts that. Its permission action is
`commerce_booking`, distinct from `commerce_checkout`, so an approval for one is
not an approval for the other. Swiggy's docs also say a booking cannot be
cancelled through the MCP.

**`/pay` — and the answer to "can it pay by card or wallet automatically?"**

No, and not because of a design choice here:

- `get_payment_options` returns UPI apps, scan-QR and cash. Its own docs state
  that even *"UPI Collect and saved VPAs are not surfaced"*. There is no card,
  no wallet, no netbanking, no auto-debit. A card saved in the user's Swiggy
  account is not reachable from the MCP.
- `checkout` accepts `paymentMethod` of only `"UPI"` or `"Cash"`.
- For UPI, Swiggy returns a `upiIntentUrl` for the **human** to complete; the
  agent only polls `check_payment_status`.
- RBI's Authentication Mechanisms Directions, 2025 require two factors, at least
  one dynamic, on every domestic digital payment from 1 April 2026. A server
  holding a token is neither.

So `/pay` offers exactly two methods and labels which is hands-free: **cash on
delivery is the only combination that needs nothing at pay time**. Tests assert
the picker never grows a card or wallet button, since that would be a control
that cannot work.

**# VERIFY, raised priority.** Swiggy's `checkout` reference page now describes a
workflow step "verify cart total is below ₹1,000". The adapter currently
declares `max_order_value=None`, citing that same tool's text saying there is no
cart-value ceiling. Both cannot be current. One live read settles it; until then
Mimir's own `COMMERCE_MAX_ORDER_RUPEES` is doing the limiting.

12 new tests (9 bot, 3 sidecar).

---

## 2026-08-20 — Swiggy Food + Dineout, service choice, auto-approve

Swiggy runs three MCP servers behind one OAuth identity. Mimir spoke to one.

**Two money-path defects had to be fixed before the other two could be added
at all**, both the same shape: state that was per-class where it needed to be
per-service.

- `GuardedSwiggySession.armed` was a **class attribute**. With three services
  connectable in one process, arming Instamart's `checkout` also armed Food's
  `place_food_order` and Dineout's `book_table` — one approval, three unlocked
  money paths — and the auto-reset after a call disarmed a sibling mid-turn.
  Now per instance. `test_the_armed_flag_is_not_shared_state_on_the_class`
  asserts on the class dict rather than on two instances, because instance
  assignment shadows a class attribute and would have passed either way. That
  is how it hid.
- `SwiggyAdapter.call_raw_tool`'s DRY_RUN short-circuit compared against the
  literal `("checkout", "confirm_order")`. Food's spend tool is
  `place_food_order`, so **a DRY_RUN the operator believed was faking every
  spend would have placed a real order.** Now reads `self.dangerous_tools`.

**New providers** — `swiggy_food` and `swiggy_dineout`, registered separately so
each carries its own money-path set, capability flags and drift check. Dineout
declares `supports_checkout=False`: booking a table is not placing an order, and
`book_table` is gated as a spend anyway because a free reservation still commits
the user's evening and cannot be cancelled through the MCP.

Tool names come from Swiggy's published reference, **not a live `tools/list`** —
marked `# VERIFY` throughout. The docs are known-unreliable here: Instamart's
listed 16 tools against 14 live, and Swiggy's own pages disagree on the Food
count (14 vs 17). `scripts/discover_swiggy.py` diffs the live surface against a
manifest and flags any tool that looks like a spend but is not guarded.

The existing `test_every_provider_can_actually_clear_its_cart` caught a real gap
on the way in: Food mapped `flush_food_cart` but had no `clear_cart`, so
emptying would have fallen back to `update_cart([])` — which reports success
without clearing.

**Service choice** — `/service` picks groceries, food or a table, with buttons on
first use and `/service food` once you know. Stored per chat AND selected on the
BFF; storing only one leaves the two disagreeing after either process restarts,
and the symptom is groceries being searched when the user asked for a table.
Results are labelled per service — a restaurant is not "in stock" — and numbered,
because the answer to a list is usually "the second one".

**Auto-approve** (`/autopay`) — off by default, and bounded on three axes: an
operator master switch that overrides any saved preference, a per-order limit the
user picks (clamped), and a **rolling daily total**. The daily cap is the
load-bearing one: a per-order limit alone bounds nothing, since an agent looping
on checkout places many small orders rather than one large one. Toggling off and
on does not reset the day's total, or the cap would be bypassed by flicking the
switch. An unreadable total is never auto-approved — `None` means "we could not
parse the price", not "it is free", and several manifests still carry `# VERIFY`
on that field.

Named honestly: **Swiggy's MCP exposes no wallet and no saved card.** Payment is
UPI (opens your app), scan-QR, or cash. This automates the approval, not the
payment, and the user is told so on every auto-approved order.

`_format_results` read `data["provider"]`; the BFF returns `provider_id`. Caught
before it shipped — it fails silently, rendering every service with the generic
fallback while nothing looks broken.

35 new tests (19 bot, 12 sidecar, plus rendering).

---

## 2026-08-20 — voice notes

Send a voice message; Mimir transcribes it with Sarvam and runs it as if typed.

- **`tools/voice.py`** — `transcribe()` never raises and never guesses. A failed
  transcription is reported; inventing a plausible order out of noise is the one
  behaviour that would make voice dangerous on a bot holding a real cart.
- **No transcoding.** Telegram voice notes are OGG/Opus and Sarvam accepts both,
  so the bytes go up exactly as they arrived — no ffmpeg dependency.
- **30 s ceiling, checked before the upload.** Sarvam's sync endpoint caps at
  30 s; Telegram hands us `voice.duration` in the update, so a 10-minute note
  costs nothing to refuse.
- **`saaras:v3`, env-overridable.** Ported reference code pinned `saarika:v2.5`,
  which Sarvam has since made legacy and is deprecating. This is the third
  hosted model to move under us after `llama-3.3-70b-versatile` and
  `gemini-2.0-flash`, so `SARVAM_MODEL` is configurable like the other two.
- **The transcript is echoed back.** Speech recognition is confident even when
  wrong, and Saaras auto-detects across 22 languages — the user has to see what
  Mimir thinks it heard before it acts.
- **A spoken "yes" authorises nothing.** The transcript re-enters at
  `_run_agent_turn`, the same entry point typed text uses; approval is still a
  `callback_query`, which no transcriber can produce. `tests/test_voice.py`
  asserts `handle_voice` reaches `_run_agent_turn` and never
  `handle_callback` / `assistant_confirmed` / `commerce.checkout`.
- No text normalisation of transcripts. The obvious punctuation filter
  `[^\w\s-]` strips every matra out of Devanagari — Python's `\w` excludes
  combining marks — so "मुझे प्रोटीन" becomes "म झ प र ट न". Regression pinned.
- Registered in both modes, unlike the document receiver: speaking your order is
  part of the shopping surface.

19 new tests. README: Swiggy publishes **three** MCP servers (`/im`, `/food`,
`/dineout`) on one OAuth identity — we speak to one. Both Swiggy's and Zomato's
manifests state third-party app development is not permitted at this time.

---

## 2026-08-20 — the Telegram menu is commerce-only

`BOT_MODE` previously gated only the LLM's tool set; all 47 slash commands
stayed registered and every one of them stayed in the Telegram menu. The bot
presented itself as a machine controller that also shops.

- **`Config.BOT_MODE` / `Config.SHOPPING_ONLY`** — the switch moved out of
  `tools_registry` into `config.py`, so the menu, the handler registration and
  the model's tool set all read one value instead of three.
- **Menu**: 47 entries -> 10. `_COMMERCE_COMMANDS` (shop, search, cart,
  providers, connect, start, help, status, persona, stop) is the default;
  `_SYSTEM_COMMANDS` is appended only under `BOT_MODE=full`.
- **Registration is gated too, not just the menu.** Hiding a command that still
  runs when typed makes the menu a false statement about the bot. `/cmd`,
  `/write`, `/commit`, `/screenshot` and the rest now return *unknown command*
  in shopping mode.
- `handle_message`'s two developer preambles moved into
  `_handle_system_text_modes`, skipped in shopping mode. The file picker
  consumed any bare number — in a shopping chat "2" means two packs far more
  often than it means the second search result. Unreachable today because
  `/find` and `/send` are unregistered; now unreachable by decision.
- The **document receiver** is full-mode only: saving whatever arrives into the
  workspace is a filesystem write with no part in shopping. Receipts still go
  out through the `send_file` tool, which stays in `_SHOPPING_EXTRAS`.
- `CallbackQueryHandler` stays registered in both modes — the Approve/Cancel
  tap on an order is a `callback_query`, and it is the one thing on the money
  path that must always land.
- Kept: `/persona` (every entry in `PERSONAS` is a shopping persona, so it is a
  tone dial on this surface) and `/status` (the `status` *tool* is already in
  `_SHOPPING_EXTRAS` for the same reason — "are you alive?" is fair mid-order).
- README: `/compare` in the shopping table was renamed to `/search` three
  entries ago and never updated here.

No files deleted; `BOT_MODE=full` restores everything.

---

## 2026-08-20 — macOS support

Ported MIMIR from the Windows/WSL2 split (agent in a Linux VM, desktop + GPU +
Ollama on the Windows host) to running natively on macOS, without dropping WSL
support.

**New: `core/platform.py`** — the only OS-aware module. Screen capture, opening
paths/URLs, clipboard, port scanning, GPU reporting, and the prompt text that
tells the model what machine it is on. Everything else is host-agnostic.

| Area | Before (WSL) | Now (macOS) |
|---|---|---|
| Screenshot | PowerShell + `System.Drawing` | `screencapture -x` |
| Clipboard | `clip.exe` | `pbcopy` |
| File manager | `powershell Start-Process` | `open` |
| Port scan | generated `.ps1`, run via PowerShell | `asyncio.open_connection` |
| GPU | `nvidia-smi` via PowerShell | `system_profiler` + `sysctl` |
| Service | systemd unit | launchd LaunchAgent |

**Bugs found and fixed along the way** (independent of the port):

- `config.py` read `/etc/resolv.conf` to find the Windows host IP. macOS has
  that file too, so `OLLAMA_URL` silently resolved to the router
  (`http://30.30.30.1:11434`) and every local-model call hung until timeout.
  Now gated on `IS_WSL`.
- `paths.normalize()` never expanded `~`. `~/Projects` became
  `<cwd>/~/Projects` and was rejected as outside the allowed roots. Barely
  mattered on WSL where files lived under `/mnt`; breaks constantly on macOS.
- A blank value in `.env` (`WORKSPACE=`) returned `""` rather than the default,
  collapsing the workspace to `.`. Added `_env()`, which treats blank as unset.
- `config.py` on macOS now allowlists `/Volumes/*`, skipping the boot-volume
  symlink that would otherwise widen the allowlist to the entire filesystem.
- `start-mimir.sh` hardcoded `/mnt/c/Users/rupes/ai-agent`. Now repo-relative.

**Prompts.** `core/agent.py`, `core/bot.py` and several `tools_registry.py`
tool descriptions told the model it was on WSL2 and instructed it to emit
`powershell.exe`. On a Mac that produced fluent, well-formed, unrunnable
commands. All now bound to `core/platform.py`. A test
(`test_mac_prompt_text_never_mentions_windows`) guards against regressions —
it caught a "there is NO powershell.exe here" line that was priming the model
by negation.

**Other:** `shell_command` prefers Homebrew's bash 5 over macOS's bash 3.2;
AirPlay's occupation of ports 5000/7000 is documented in the planner rules;
dead code removed (`_DRIVE_PATTERNS`, `process_ops._run_exec`).

**Tests:** `tests/conftest.py` adds `wsl_only`/`mac_only` markers and an
`ALLOWED_ROOT` fixture, so host-specific assertions skip rather than fail on
the wrong machine. New `tests/test_platform.py`.

### Files
```
new:  core/platform.py, tests/conftest.py, tests/test_platform.py,
      com.mimir.agent.plist.template
edit: config.py, core/{agent,bot,brain,notebook,paths,tools_registry}.py,
      tools/{process_ops,screenshot,system_info}.py,
      setup.sh, start-mimir.sh, README.md, .env.example, .gitignore,
      tests/{test_agent,test_file_ops_paths,test_paths,
             test_regression_navigation,test_tools_registry}.py
```

---

## 2026-08-20 — agentic-commerce integration

MIMIR can now search, compare, cart and transact across commerce providers
(Swiggy Instamart + a mock provider) via the **agentic-commerce** platform at
`~/Projects/Experiments/agentic-commerce`.

**Shape:** agentic-commerce runs as a **separate process** with its own venv.
Its `requirements.txt` pins `mcp` exactly because an unpinned bump silently
broke its transport once; keeping those pins out of MIMIR's environment means a
provider-SDK problem cannot take the bot down. MIMIR talks to it over loopback
HTTP.

```
Telegram -> MIMIR agent loop -> commerce_* tools -> HTTP :8011
         -> agentic-commerce BFF -> ProviderAdapter -> Swiggy MCP / mock
```

### Changes in MIMIR
- **`tools/commerce.py`** (new) — async client for the BFF. One BFF session per
  Telegram chat (`mimir-<chat_id>`), so provider and cart persist across
  messages. Every function returns a string; transport failures come back as
  text, never exceptions, because the agent loop feeds results to the model.
- **9 commerce tools** in `core/tools_registry.py`.
- **5 commands** in `core/bot.py`: `/shop`, `/compare`, `/cart`, `/providers`,
  `/connect`. Factored `_run_agent_turn()` out of the message handler so
  `/shop` and a plain message take exactly the same path.
- **`core/agent.py`** — `_commerce_checkout()` intercepts checkout before the
  generic gate, so the approval prompt shows the real line items and total
  rather than a bare tool name.
- **`core/guard.py`** — `commerce_checkout` added to `WRITE_OPS`. Without this
  `needs_permission()` returns `False` and the gate is **silently open**;
  `GATED_TOOLS` alone is not enough.
- **`config.py`** — `COMMERCE_BFF_URL`, `COMMERCE_ALLOW_LIVE`.
- **`start-commerce.sh`**, **`com.mimir.commerce.plist.template`** (new).
- **`get-chat-id.sh`**, **`login-swiggy.sh`** (new) — setup helpers.

### Changes in agentic-commerce (NOT a git repo — no version control there)
Originals backed up before editing.

- **`backend/app.py`** — added the REST money path, which existed only inside
  the WebSocket chat loop before: `GET /cart`, `POST /cart/add`,
  `POST /cart/set`, `POST /checkout/intent`, `POST /checkout`,
  `GET /payment/{order_id}`.
- **`POST /cart/add` does read-merge-write.** ADR-2 records that Swiggy
  *replaces* the whole cart on update. Without the merge, "add milk" silently
  wipes a full basket. Doing it server-side means no client can trip on it.
- **`.env`** — `DRY_RUN` was `false` (real orders enabled); set back to `true`.
  Swiggy token paths moved from `~/Downloads` (a folder that gets cleaned) to
  the project dir, which its `.gitignore` already protects.
- **`.venv` rebuilt on Python 3.14** — it pointed at `python3.12`, which is no
  longer installed, so nothing in the project ran. All 15 of its tests pass on
  3.14 and `pip check` is clean.

### The money path
Checkout is the only commerce operation requiring approval. Cart edits are not
gated — gating every "add milk" would make ordering unusable, so the friction
sits exactly where money moves.

Five independent layers:

| # | Layer | Stops |
|---|---|---|
| 1 | `COMMERCE_ALLOW_LIVE` + BFF `DRY_RUN` | Both must agree before anything is real |
| 2 | `GATED_TOOLS` + `Guard` | No order without a tap |
| 3 | Cart-bound confirm token | Single-use, 300s TTL, void if the cart changes |
| 4 | `CapabilityFlags.max_order_value` | Provider ceiling (Swiggy ₹1000) |
| 5 | `GuardedSwiggySession` | In-transport last line |

Layer 3 is the non-obvious one. A plain "armed" boolean authorizes *whatever
the cart holds when the call lands*, so an agent adding an item between you
reading the total and tapping Approve spends against an approval you never
gave. `POST /checkout/intent` fingerprints the exact cart you were shown; if it
moves, the token is dead and you are re-asked with the new total.

Verified live against the mock provider: cart-change invalidation, token reuse
rejection, cap enforcement, and the full intent → checkout → poll → placed path.

**Tests:** `tests/test_commerce.py` (17 tests) covering the interlocks, the
gating, and what the user is shown before money moves.

---

## 2026-08-20 — fixes found by running it with real keys

- **Groq's `llama-3.3-70b-versatile` is retired** — it returned HTTP 404
  `model_not_found`, so the primary provider was dead on arrival. This is
  exactly the failure the `GEMINI_MODEL` comment warns about, but `GROQ_MODEL`
  had been left hardcoded. Now `_env("GROQ_MODEL", "openai/gpt-oss-120b")`,
  verified to support tool calling. Check
  <https://console.groq.com/docs/models> if it 404s again.
- **`MAX_ITERATIONS` raised 5 -> 10** (env: `AGENT_MAX_ITERATIONS`). A shopping
  turn is compare -> add -> show cart -> checkout -> report, which is already
  five hops. At 5 the loop ran out mid-flow and told the user it had failed on a
  turn where the order *had* been placed — the worst thing to be wrong about on
  a money path.
- **Bounded 429 retry for Groq.** The free tier allows 8k tokens/minute and the
  24 tool schemas cost ~1,644 tokens on *every* iteration, so a multi-step turn
  hits the limit routinely. Groq states the exact wait ("try again in 4.65s");
  the cascade now honours it once, up to 12s, instead of falling through to
  Gemini over a four-second speed bump.
- **Swiggy token path** moved out of `~/Downloads` to the project directory,
  which the project's `.gitignore` already protects.
- New helpers: `get-chat-id.sh`, `login-swiggy.sh`.

---

## 2026-08-20 (later) — the Telegram integration was making the bot worse

Reported from live use: `commerce_compare` crashing every turn, and the rich
behaviour that worked standalone (offer a list of biscuits to pick from,
suggest what goes into a samosa) had disappeared.

**Crash.** `TypeError: '<' not supported between 'NoneType' and 'int'` in
`tools/commerce.py`. `cheapest_by_provider()` returns `None` for a provider
that is connected and returns items where every item is out of stock or has no
price. Swiggy did exactly that, and ranking `None` against an `int` raised. The
model then retried the failing tool until it exhausted both its iteration
budget and the Groq rate limit. This was code added for the integration; the
standalone system never had it.

**The real regression, and a design error.** The canonical `ProviderAdapter`
surface (ADR-2) exposes what is *comparable across providers* — search, cart,
checkout. Swiggy's MCP server actually exposes **14 tools**, including
`your_go_to_items`, `get_orders`, `track_order`, `get_payment_options`,
`get_addresses` and `clear_cart`. `backend/agent/loop.py` fed Gemini all 14,
which is why it worked well. The integration fed a Groq-driven loop only the 9
canonical wrappers, discarding most of the provider's capability. A canonical
layer is the right abstraction for a price table and the wrong one for a
conversation.

It could not be fixed by handing the raw tools to Groq either: their schemas
total ~5,367 tokens per call against Groq's free-tier 8,000/minute.

**Fix.** `POST /agent/message` on the BFF exposes the existing Gemini loop over
REST. Plain Telegram messages are routed there, so the assistant drives the
provider's real tools again. Mimir keeps the one decision the loop must not
make alone: when the loop is blocked on confirmation, Mimir mints a cart-bound
intent, shows the line items and total, and waits for the Approve tap.

`commerce_compare` is gone. `commerce_search` searches the selected provider
(`POST /search`). Comparison belongs to a setup with more than one real
provider; with one it spends round trips on providers that answer
`needs_connection` and reports a "cheapest" that compares nothing.

**Also:** the two-switch interlock proved itself — `DRY_RUN` had been flipped
back to `false` in agentic-commerce's `.env`, and `COMMERCE_ALLOW_LIVE=false`
refused the spend.

---

## 2026-08-20 — rebrand to Mimir, shopping-first

- RAVEN -> MIMIR everywhere: 134 occurrences across 21 files, plus filenames
  (`start-mimir.sh`, `com.mimir.*.plist.template`), launchd labels, the
  `MIMIR_OPEN_PATH`/`MIMIR_SHOT_PATH` env vars and the `mimir-<chat_id>`
  session key. Note: a blind rename first produced "a mimir delivering exactly
  what's needed" and "bird/mimir metaphors" — Mimir is the Norse keeper of the
  well of wisdom, not a bird. The persona was rewritten by hand.
- Persona, `/start` and `/help` are a shopping assistant, not a machine
  controller.
- `BOT_MODE` (default `shopping`) keeps the developer tools out of the LLM's
  tool set; they remain available as slash commands. `BOT_MODE=full` restores
  them. This halves the per-iteration schema cost (1,644 -> 807 tokens), which
  was a direct cause of mid-order rate limiting.
- `/compare` -> `/search`.

---

## 2026-08-20 (later still) — checkout produced a payment message with no link

Two separate faults, both visible only on a live run.

**1. The approval handed the order to the wrong code.** On Approve, Mimir called
the canonical `checkout()`, which hardcodes
`{paymentMethod: "UPI", generateUPIQR: true}` and never calls
`get_payment_options`. Swiggy's checkout tool documentation is explicit:

> "Never assume or default a payment method — this tool REJECTS a call with no
> payment method... call get_payment_options so the user picks their UPI app,
> then pass paymentMethod="UPI" + intentApp (or generateUPIQR for desktop QR)."

The adapter itself carries a `VERIFY-WRITE` note admitting those parameters were
never exercised, and `parse_payment_intent` a `# VERIFY against a live checkout
response` note over guessed field names (`upiIntentUrl`/`bridgeUrl`). So the
order went out through an unverified stub and no authorization URL came back —
hence a message saying "click the UPI link below" with nothing below it. Asking
for a QR just repeated the same failing call, which is why it re-prompted.

Fixed by re-running the turn with the money path armed instead: the agent that
was blocked completes the order through the documented flow. Verified live —
`get_cart` -> itemised bill, "checkout" -> `get_payment_options` offering 6 UPI
apps, QR and COD, "pay with Google Pay" -> `checkout` blocked pending approval,
with the approval prompt showing the real line items.

**2. A provider cap that the provider denies having.** `providers.yaml` and the
adapter declared `max_order_value: 100000` (Rs.1000), attributed to "docs". The
checkout tool documents the opposite:

> "NO CART-VALUE LIMIT: Do NOT refuse, warn about, or gate checkout on the cart
> total — there is no value ceiling... relay that error instead of pre-empting
> it."

Rs.1000 refuses ordinary grocery orders. The provider is now described
accurately (`max_order_value: null`) and the ceiling moved to the client as
`COMMERCE_MAX_ORDER_RUPEES` (default 5000) — a self-imposed guard rail is a
legitimate thing for a client to want, but it is not a fact about the provider.

**Safety note:** `DRY_RUN=false` throughout this period, so the failed attempts
were against the real endpoint. Order history confirms nothing was placed — the
only order on the account predates the testing and was already delivered.

---

## Test status

| Suite | Result |
|---|---|
| MIMIR | 103 passed, 21 skipped (skips are WSL-only) |
| agentic-commerce | 15 passed |
