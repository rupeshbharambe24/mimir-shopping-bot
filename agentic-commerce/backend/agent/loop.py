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
import os

from google import genai
from google.genai import types as genai_types

from backend.providers.base import ProviderAdapter

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

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


async def build_gemini_tools(adapter: ProviderAdapter) -> list:
    """Translate the selected provider's raw MCP tools into plain Gemini
    function declarations (no live objects to deep-copy)."""
    raw_tools = await adapter.list_raw_tools()
    decls = []
    for t in raw_tools:
        decls.append({
            "name": t.name,
            "description": t.description or "",
            "parameters": genai_types.Schema.from_json_schema(
                json_schema=genai_types.JSONSchema(**(t.input_schema or {}))
            ),
        })
    return [genai_types.Tool(function_declarations=decls)]


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

    config = genai_types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=gemini_tools,
        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
    )

    requires_confirm = adapter.capability_flags.requires_pre_confirmation
    dangerous = _dangerous_for(adapter)

    for _ in range(10):
        response = await client.aio.models.generate_content(
            model=DEFAULT_MODEL, contents=history, config=config
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
