"""Gemini tool-calling loop, operating on ONE provider adapter (ADR-6).

Ported from `swiggy_agent_gemini.py`'s `run_turn`, with two changes required by
the architecture:

* It drives the *selected* provider's raw tools through `adapter.call_raw_tool`
  (ADR-1/ADR-6) instead of a hardcoded Swiggy `ClientSession`. The agent's tool
  set on any turn is exactly one provider's tools — comparison across providers
  is a separate, non-LLM code path (ADR-4, not built yet).
* The `/confirm` money-path arm is now checked against the provider's declared
  `CapabilityFlags.requires_pre_confirmation` (ADR-6), not a Swiggy-specific
  constant. The adapter still enforces the guard in-transport as a backstop.

Local tool execution means the provider's bearer token never goes to Google —
only tool names, schemas, and results do (unchanged from the original design).
"""

from __future__ import annotations

import json
import logging
import os

from google import genai
from google.genai import types as genai_types

from backend.providers.base import ProviderAdapter

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Models to try, in order, when the one above is rate-limited.
#
# The free-tier quota that bites is PER MODEL -- the 429 says so explicitly:
#
#   quotaId: GenerateContentInputTokensPerModelPerMinute-FreeTier
#   quotaDimensions: {"model": "gemini-3.5-flash-lite"}
#
# so a second model has its own 250,000 input-tokens-per-minute bucket and is a
# real fallback rather than a retry against the same wall. This is deliberately
# NOT the Groq/Ollama cascade the bot uses: those speak a different function-
# calling dialect, and converting mid-order is a change to the loop that drives
# the money path. Same-SDK failover first, because it is the change that cannot
# introduce a new class of bug.
MODEL_FALLBACKS = [
    m.strip() for m in os.environ.get(
        "GEMINI_MODEL_FALLBACKS", "gemini-flash-latest,gemini-2.5-flash"
    ).split(",") if m.strip()
]


def _is_rate_limited(exc: Exception) -> bool:
    blob = f"{type(exc).__name__}: {exc}"
    return "429" in blob or "RESOURCE_EXHAUSTED" in blob or "quota" in blob.lower()


async def _generate_with_failover(client, *, contents, config, emit=None):
    """Ask Gemini, moving to the next model when one is rate-limited.

    Only rate limits trigger failover. A bad request is a bug in what we sent
    and would fail identically on every model, so retrying it just burns three
    quotas instead of one.
    """
    models = [DEFAULT_MODEL] + [m for m in MODEL_FALLBACKS if m != DEFAULT_MODEL]
    last: Exception | None = None

    for name in models:
        try:
            return await client.aio.models.generate_content(
                model=name, contents=contents, config=config
            )
        except Exception as exc:
            if not _is_rate_limited(exc):
                raise
            last = exc
            logger.warning("Model %s rate-limited; trying the next one", name)
            if emit is not None:
                await emit({"type": "model_failover", "from": name})

    raise last if last else RuntimeError("no model available")

# Fallback only. The real set comes from the adapter (see _dangerous_for): a
# hardcoded set here would silently fail to guard any provider that names its
# order-placing tool differently.
_FALLBACK_DANGEROUS = frozenset({"checkout", "confirm_order"})


def _dangerous_for(adapter) -> frozenset:
    """The money-path tool names this provider declares."""
    declared = getattr(adapter, "dangerous_tools", None)
    if declared:
        return frozenset(declared)
    return _FALLBACK_DANGEROUS

SYSTEM_PROMPT = """You are a grocery-ordering assistant driving a single commerce
provider's tools (addresses, product search, cart, payment, checkout, tracking).

Rules:
- Confirm the delivery address before searching products if none is selected.
- Show the user the cart contents and total before suggesting checkout.
- If a checkout/confirm_order call comes back blocked, tell the user they must
  explicitly confirm (the app arms order placement) before you retry.
- Be concise and state prices/quantities clearly.
"""


def sanitize_json_schema(node):
    """Make a provider's JSON Schema acceptable to Gemini.

    Gemini rejects the WHOLE request -- not the one bad declaration -- if any
    array-typed property omits `items`:

        400 INVALID_ARGUMENT ... function_declarations[2].parameters
        .properties[lines].items: missing field.

    A provider is free to publish `{"type": "array"}` with no `items`; it is
    valid JSON Schema and means "an array of anything". Gemini disagrees, and
    the cost of that disagreement is total: every turn 400s, so the commerce
    agent never runs at all. One provider tightening or loosening a schema
    should not be able to take the whole assistant down, so bare arrays get an
    explicit permissive `items` rather than being passed through.

    Returns a new structure; the provider's own schema is left untouched.
    """
    if isinstance(node, list):
        return [sanitize_json_schema(v) for v in node]
    if not isinstance(node, dict):
        return node

    out = {k: sanitize_json_schema(v) for k, v in node.items()}

    declared = out.get("type")
    types = declared if isinstance(declared, list) else [declared]
    if "array" in types and "items" not in out:
        # Permissive rather than guessed. Inventing a concrete element type
        # would make the model confidently send the wrong shape.
        out["items"] = {"type": "string"}
    return out


async def build_gemini_tools(adapter: ProviderAdapter) -> list:
    """Translate the selected provider's raw MCP tools into plain Gemini
    function declarations (no live objects to deep-copy)."""
    raw_tools = await adapter.list_raw_tools()
    decls = []
    for t in raw_tools:
        schema = sanitize_json_schema(t.input_schema or {})
        try:
            parameters = genai_types.Schema.from_json_schema(
                json_schema=genai_types.JSONSchema(**schema)
            )
        except Exception as exc:
            # Skip the one tool rather than lose every tool. A missing tool
            # degrades the assistant; an unconvertible one kills it outright.
            logger.warning(
                "Skipping tool %r: its schema will not convert (%s: %s)",
                t.name, type(exc).__name__, exc,
            )
            continue
        decls.append({
            "name": t.name,
            "description": t.description or "",
            "parameters": parameters,
        })
    return [genai_types.Tool(function_declarations=decls)]


# How many complete user turns of context to carry. The quota that actually
# bites is INPUT tokens per minute (Gemini free tier: 250,000/min for one
# model), and history was never trimmed -- so every turn re-sent the entire
# conversation including every product-search payload verbatim. Ten searches in
# and a single "add it to cart" was uploading tens of thousands of tokens of
# already-answered questions, which is what exhausted the quota rather than any
# real volume of use.
MAX_USER_TURNS = int(os.environ.get("AGENT_MAX_USER_TURNS", "6"))


def _is_real_user_turn(entry) -> bool:
    """A message the human typed, as opposed to a tool result.

    Tool results are appended with role="user" too, so the role alone cannot
    tell them apart -- they carry function_response parts instead of text. That
    distinction is what makes trimming safe: cutting mid-sequence would leave a
    function_response whose matching function_call had been dropped, and Gemini
    rejects that outright.
    """
    if getattr(entry, "role", None) != "user":
        return False
    for part in getattr(entry, "parts", None) or []:
        if getattr(part, "function_response", None) is not None:
            return False
        if getattr(part, "text", None):
            return True
    return False


def trim_history(history: list, max_user_turns: int = None) -> list:
    """Drop the oldest complete turns, in place, keeping recent context.

    Cuts only at a real user turn so every function_call keeps its matching
    function_response. Returns the same list object -- callers hold a reference
    to Session.history and expect it mutated.
    """
    limit = MAX_USER_TURNS if max_user_turns is None else max_user_turns
    if limit <= 0:
        return history

    starts = [i for i, e in enumerate(history) if _is_real_user_turn(e)]
    if len(starts) <= limit:
        return history

    cut = starts[-limit]
    if cut > 0:
        del history[:cut]
    return history


async def run_turn(client, adapter: ProviderAdapter, gemini_tools, history, user_input, *, armed: bool, emit=None):
    """One user turn: ask Gemini, execute any tool calls it makes against the
    adapter, feed results back, repeat until it produces text (max 10 hops).

    `emit`, if given, is an async callable(event: dict) used to stream progress
    (tool_call / tool_result events) to a client (e.g. a WebSocket). When it's
    None the loop prints to stdout instead (terminal REPL)."""
    async def _emit(ev):
        if emit is not None:
            await emit(ev)
        else:
            if ev["type"] == "tool_call":
                print(f"  -> [tool call] {ev['name']}({json.dumps(ev['args'])})")
            elif ev["type"] == "tool_result":
                print(f"  <- [tool result] {ev['text'][:300]}")

    history.append(genai_types.Content(role="user", parts=[genai_types.Part(text=user_input)]))
    trim_history(history)

    config = genai_types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=gemini_tools,
        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
    )

    requires_confirm = adapter.capability_flags.requires_pre_confirmation
    dangerous = _dangerous_for(adapter)

    for _ in range(10):
        response = await _generate_with_failover(
            client, contents=history, config=config, emit=emit
        )
        if not response.candidates:
            return "(no response)"

        content = response.candidates[0].content
        history.append(content)

        function_calls = [p.function_call for p in (content.parts or []) if p.function_call]
        if not function_calls:
            return response.text or "(no text response)"

        # Every function_call MUST get a matching function_response, even on
        # failure, or the model gets stuck retrying it forever next turn.
        response_parts = []
        for fc in function_calls:
            args = dict(fc.args or {})
            is_dangerous = fc.name in dangerous
            await _emit({"type": "tool_call", "name": fc.name, "args": args})

            if is_dangerous and requires_confirm and not armed:
                result_text = (
                    f"BLOCKED: '{fc.name}' needs explicit user confirmation. "
                    "Ask the user to confirm the order (which arms placement), then retry."
                )
                is_error = True
                # Report the exact call that was refused, arguments included.
                # A caller told only "something was blocked" has to guess what
                # to put in the approval prompt, and for a provider with no cart
                # to read -- Dineout books tables, it has no get_cart -- there is
                # nothing else to reconstruct the request from.
                await _emit({"type": "blocked", "name": fc.name, "args": args})
            else:
                if is_dangerous and armed and hasattr(adapter, "arm_dangerous"):
                    adapter.arm_dangerous()  # single-shot arm of the in-transport guard
                res = await adapter.call_raw_tool(fc.name, args)
                result_text, is_error = res.text, res.is_error

            await _emit({"type": "tool_result", "name": fc.name, "text": result_text, "isError": is_error})
            response_parts.append(genai_types.Part.from_function_response(
                name=fc.name, response={"result": result_text, "isError": is_error},
            ))
        history.append(genai_types.Content(role="user", parts=response_parts))

    return "(stopped after too many tool calls in one turn -- try breaking your request up)"


async def run_repl(adapter: ProviderAdapter):
    """Terminal REPL mirroring the original script, but provider-agnostic.
    Requires GEMINI_API_KEY. Type /confirm to arm the next message for checkout."""
    import anyio

    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("Set GEMINI_API_KEY first (https://aistudio.google.com/apikey).")

    await adapter.connect()
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    history: list = []
    gemini_tools = await build_gemini_tools(adapter)
    n = sum(len(t.function_declarations or []) for t in gemini_tools)
    print(f"Agent ready on provider '{adapter.id}' ({n} tools). '/confirm' arms checkout; 'quit' exits.\n")

    armed = False
    try:
        while True:
            user_input = (await anyio.to_thread.run_sync(lambda: input("you> ").strip()))
            if user_input.lower() in {"quit", "exit"}:
                break
            if user_input == "/confirm":
                armed = True
                print("(checkout armed for your next message only)")
                continue
            if not user_input:
                continue
            try:
                reply = await run_turn(client, adapter, gemini_tools, history, user_input, armed=armed)
            except Exception as e:
                print(f"(turn failed: {e})")
                continue
            finally:
                armed = False  # arm is single-message
            print(f"agent> {reply}\n")
    finally:
        await adapter.close()


def main():
    """`python -m backend.agent.loop` — runs the REPL against Swiggy."""
    import anyio
    from dotenv import load_dotenv

    from backend.providers.swiggy.adapter import SwiggyAdapter

    load_dotenv()
    anyio.run(run_repl, SwiggyAdapter())


if __name__ == "__main__":
    main()
