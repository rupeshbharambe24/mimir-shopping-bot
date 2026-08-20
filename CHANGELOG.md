# Change log

Hand-maintained because these changes are not committed yet. Everything below
lives in the working tree on branch `macos-support`, uncommitted, and runs
locally. When you are ready to commit, this file is the commit-message source.

Nothing here was authored under the RBIH account. No git identity is configured
in this repo (`git config user.name`/`user.email` are both unset), so no commit
can be attributed to anyone by accident.

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

## Test status

| Suite | Result |
|---|---|
| MIMIR | 103 passed, 21 skipped (skips are WSL-only) |
| agentic-commerce | 15 passed |
