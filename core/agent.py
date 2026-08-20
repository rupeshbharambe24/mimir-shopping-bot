"""The model -> tool -> model loop.

The LLM is given real tools and decides which to call and in what order, which
is what makes "go to d drive projects and list the folders" work as one
message. Destructive tools are routed through Guard first, so the model can
propose them but only the user can authorize them.
"""

import json
import logging
import os

from config import Config
from core import paths, platform as host, session, tools_registry

logger = logging.getLogger(__name__)

# A shopping turn is legitimately more hops than a file operation: compare ->
# add to cart -> show the cart -> checkout -> report status is already five,
# before any correction. At 5 the loop ran out mid-flow and told the user it had
# failed on a turn where the order had in fact been placed -- the worst possible
# thing to be wrong about on a money path.
MAX_ITERATIONS = int(os.getenv("AGENT_MAX_ITERATIONS", "10"))

# Tool results are resent on every subsequent iteration, so a big directory
# listing or file read is paid for repeatedly. Groq's free tier allows 12k
# tokens/minute and an uncapped listing exhausts it in a couple of turns.
MAX_TOOL_RESULT_CHARS = 2000

_SYSTEM = """You are Mimir, a shopping assistant the user talks to over Telegram.

Use the tools to actually do what the user asks - never tell them to do it \
themselves.

How shopping works:
- commerce_search finds products and returns their product_id. ALWAYS search \
before adding something to the cart; never invent a product_id.
- commerce_add_to_cart adds to whatever is already in the cart.
- commerce_cart shows the cart and total. Show it to the user before checkout.
- commerce_checkout places the order. The user is shown the cart and total and \
must tap Approve, so do not ask them to confirm in chat first - just call it \
and the prompt appears.
- If the store is not connected, say so and suggest /connect. Do not retry \
in a loop.

If a tool returns an error, tell the user plainly what went wrong. Do not call \
the same tool over and over hoping for a different answer.

If the user denies the order, do not retry it or look for another route. Tell \
them it was not placed.

Reply in plain text, briefly - it is read on a phone. Prices in rupees."""

_MAC_PATHS = """The user thinks in ordinary macOS paths (/Users/you/Projects). \
You may pass absolute paths, ~ shorthand (~/Projects), or names relative to \
the current folder. There are no drive letters on this machine."""

_WSL_PATHS = """The user thinks in Windows paths (D:\\Projects). You may pass \
paths in any form: D:\\Projects, D:/Projects, /mnt/d/Projects, or "d drive \
projects". They all resolve correctly. Relative names resolve against the \
current folder. Show paths in Windows form."""


def _system_prompt(cwd: str) -> str:
    """The agent's system prompt, bound to whichever host it is running on."""
    return _SYSTEM


async def run(bot, chat_id: int, text: str, llm, guard, on_file=None) -> str:
    """Run the loop until the model answers. Returns the text to show the user.

    Raises RuntimeError when every LLM provider is unavailable, so the caller
    can fall back to the regex classifier.
    """
    # Defense in depth: the tool loop can read files, open the file manager and (with
    # approval) run code, so it must never execute for anyone but the owner --
    # even if a future caller forgets the @authorized decorator that also gates
    # this at the Telegram entry point.
    if chat_id != Config.TELEGRAM_CHAT_ID:
        logger.warning(f"agent.run refused for unauthorized chat_id={chat_id}")
        raise PermissionError("unauthorized chat")

    cwd = session.get_cwd(chat_id)
    messages = [
        {"role": "system", "content": _system_prompt(cwd)},
        {"role": "user", "content": text},
    ]

    denied: set[str] = set()
    collected: list[str] = []

    for iteration in range(MAX_ITERATIONS):
        response = await llm.ask_with_tools(messages, tools_registry.TOOL_SCHEMAS)
        calls = response.get("tool_calls") or []

        if not calls:
            answer = (response.get("text") or "").strip()
            if answer:
                return answer
            return "\n\n".join(collected) if collected else "No response."

        messages.append({
            "role": "assistant",
            "content": response.get("text") or "",
            "tool_calls": [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": json.dumps(c["args"])},
                }
                for c in calls
            ],
        })

        for call in calls:
            result = await _run_one(bot, chat_id, call, guard, denied, on_file)
            collected.append(result)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": _truncate(result),
            })

        logger.info(f"Agent iteration {iteration + 1}: ran {[c['name'] for c in calls]}")

    return (
        f"I could not finish that in {MAX_ITERATIONS} steps. "
        "Here is what I gathered:\n\n" + "\n\n".join(collected[-3:])
    )


def _truncate(result: str) -> str:
    """Trim a tool result before it re-enters the prompt on every later turn."""
    if len(result) <= MAX_TOOL_RESULT_CHARS:
        return result
    return (
        result[:MAX_TOOL_RESULT_CHARS]
        + f"\n... (truncated, {len(result)} chars total)"
    )


async def _run_one(bot, chat_id, call, guard, denied, on_file) -> str:
    name = call["name"]
    args = call.get("args") or {}

    # Checkout is gated, but not through the generic path below: that prompt can
    # only describe the tool's arguments, and commerce_checkout has none. Being
    # asked to approve "commerce_checkout" tells the user nothing about what
    # they are about to buy.
    if name == "commerce_checkout":
        return await _commerce_checkout(bot, chat_id, guard)

    if name in tools_registry.GATED_TOOLS:
        fingerprint = f"{name}:{json.dumps(args, sort_keys=True)}"
        if fingerprint in denied:
            # The model sometimes re-proposes a rejected action. Answer from
            # memory rather than prompting the user a second time.
            return f"The user already denied '{name}' with these arguments. Do not ask again."
        description = _describe(name, args)
        approved = await guard.request_permission(
            bot, chat_id, _guard_op(name), description, json.dumps(args)[:400],
        )
        if not approved:
            denied.add(fingerprint)
            return f"The user denied permission to {description}."

    # These two need the Telegram Bot object, so they are handled here rather
    # than in the registry, which is deliberately Telegram-agnostic.
    if name == "send_file":
        return await _send_file(chat_id, args, on_file)
    if name == "screenshot":
        return await _screenshot(on_file)

    return await tools_registry.execute(name, args, chat_id)


async def _send_file(chat_id, args, on_file) -> str:
    from tools import file_ops

    ok, target = file_ops.validate_path(
        str(args.get("path") or ""), cwd=session.get_cwd(chat_id),
    )
    if not ok:
        return f"Access denied: {args.get('path')}"
    path, error = file_ops.get_file_for_send(str(target))
    if error:
        return f"Cannot send {paths.display(str(target))}: {error}"
    if on_file is None:
        return "No delivery channel available."
    await on_file(path)
    return f"Sent {paths.display(path)} to the user."


async def _commerce_checkout(bot, chat_id, guard) -> str:
    """Place the order, gated on an approval bound to one exact cart.

    Two steps on purpose. `checkout_intent` snapshots the cart and mints a
    single-use token without spending anything, so the Telegram prompt can show
    the real line items and total. The token the user is approving is void if
    the cart changes before it is redeemed -- so an agent that adds something
    after the user has seen the total cannot spend against that approval.
    """
    from tools import commerce

    if blocked := await commerce.live_spending_blocked(chat_id):
        return blocked

    intent = await commerce.checkout_intent(chat_id)
    if not intent.get("ok"):
        return f"Cannot check out: {intent.get('error', 'unknown error')}"

    dry = "  [DRY RUN - no real order]" if intent.get("dry_run") else ""
    description = (
        f"place order on {intent['provider_id']} for {intent['total_display']}{dry}"
    )

    approved = await guard.request_permission(
        bot, chat_id, "commerce_checkout", description, intent.get("summary", ""),
    )
    if not approved:
        return "The user denied the order. Do not retry it or try another route."

    return await commerce.checkout(chat_id, intent["confirm_token"])


async def _screenshot(on_file) -> str:
    from tools import screenshot as screenshot_tool

    path, error = await screenshot_tool.take_screenshot()
    if error or not path:
        return f"Screenshot failed: {error}"
    if on_file is None:
        return "No delivery channel available."
    await on_file(path)
    return "Screenshot sent to the user."


def _guard_op(name: str) -> str:
    """Map a tool name onto the operation names Guard already knows."""
    return {
        "write_file": "write_file",
        "delete_file": "delete_file",
        "run_script": "run_script",
        "shell_command": "run_script",
        "kill_process": "run_script",
    }.get(name, "run_script")


def _describe(name: str, args: dict) -> str:
    if name == "shell_command":
        return f"run: {args.get('command', '')}"
    if name == "kill_process":
        return f"kill process: {args.get('name', 'most recent')}"
    target = args.get("path", "")
    win = paths.display(str(target)) if target else ""
    return {
        "write_file": f"write to {win}",
        "delete_file": f"delete {win}",
        "run_script": f"run script {win}",
    }.get(name, f"{name} {win}")
