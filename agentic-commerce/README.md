# Agentic Commerce — Pluggable Multi-MCP Platform (backend foundations)

A vendor-agnostic backend for driving real agentic-commerce transactions across
multiple provider MCP servers through one common interface. **Swiggy Instamart**
is the first real provider; a **mock provider** proves the plugin seams without
any external calls.

This repository now contains **build steps 1–5** of the plan in
`ARCHITECTURE_DECISIONS.md`: canonical types + adapter interface, the Swiggy
port, the mock provider + registry + `tools/list` drift check (steps 1–3), the
concurrent comparison orchestrator + capability matrix (step 4), and the FastAPI
BFF + Flutter Web front-end (step 5). Remaining work is pre-production hardening
and the open product decisions (see the bottom of this file).

## Layout

```
backend/
  canonical/types.py          # provider-agnostic Canonical* types (ADR-2)
  providers/
    base.py                   # ProviderAdapter protocol + CapabilityFlags (ADR-1)
    registry.py               # loads providers.yaml, connects, tools/list drift check (ADR-3)
    swiggy/
      auth.py                 # OAuth 2.1 + PKCE + dynamic client registration (ported verbatim)
      mcp_compat.py           # mcp-SDK version shims + guarded session (ported verbatim)
      adapter.py              # ProviderAdapter for Swiggy
      manifest.py             # Swiggy tool-name + JSON-shape mapping onto Canonical* (ADR-2)
    _mock_provider/
      adapter.py              # in-memory fake provider (zero external calls)
      manifest.py
  agent/loop.py               # Gemini tool-calling loop, operates on ONE adapter (ADR-6)
  comparison/
    orchestrator.py           # concurrent fan-out comparison (ADR-4)
    matrix.py                 # capability matrix builder (FR3)
  app.py                      # FastAPI BFF: /compare, /matrix, /state, WS /chat (step 5)
flutter_web/                  # Flutter Web front-end: chat + compare + matrix (step 5)
providers.yaml                # the registry config (ADR-3)
docs/adr/                     # architecture decision records
tests/                        # test_pluggability.py + test_comparison.py (no network)
scripts/check_registry.py     # CLI: connect providers + print capability matrix
scripts/verify_swiggy.py      # read-only, PII-redacted capture of Swiggy's real shapes
```

## Setup

1. `python3.12 -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill in `GEMINI_API_KEY`; point `SWIGGY_TOKENS_PATH`
   at a valid token file (the existing `~/Downloads/.swiggy_tokens.json` works if
   still fresh).

`DRY_RUN=true` (the default) intercepts `checkout`/payment so **no real order is
ever placed** while you develop.

## Run — offline checks (no keys, no network)

```bash
pytest -q                     # 15 tests: pluggability + comparison + matrix
```

## Run — the web app

Terminal 1 — the backend:
```bash
.venv/bin/python -m uvicorn backend.app:app --port 8011
```
Terminal 2 — the Flutter Web front-end:
```bash
cd flutter_web && flutter run -d chrome --dart-define=BFF_URL=http://127.0.0.1:8011
# or serve the release build:  flutter build web && (cd build/web && python3 -m http.server 8080)
```
- **Compare** and **Capability Matrix** tabs work immediately (the mock provider
  runs offline; Swiggy shows `needs_connection` until you Connect it).
- **Chat** needs `GEMINI_API_KEY` set and a **Connected** provider (top bar).
  With Swiggy connected it drives the real Instamart tools; checkout stays faked
  while `DRY_RUN=true`.

## Verify Swiggy's live shapes (read-only, PII-redacted)

```bash
python -m scripts.check_registry   # tools/list drift check (connects to Swiggy)
python -m scripts.verify_swiggy    # masked field-name/shape capture
```

## Key design docs

- `ARCHITECTURE_DECISIONS.md` — the full ADR set (companion, lives with the plan).
- `docs/adr/ADR-007-checkout-payment-interface-shape.md` — records why `checkout`
  is modelled as an async payment intent + `poll_payment` (Option A) rather than
  a single synchronous call, **and how to revert to the synchronous shape** if
  that trade-off ever flips.
