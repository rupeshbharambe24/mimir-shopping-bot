"""The model -> tool -> model loop.

The LLM is given real tools and decides which to call and in what order, which
is what makes "go to d drive projects and list the folders" work as one
message. Destructive tools are routed through Guard first, so the model can
propose them but only the user can authorize them.
"""

import json
import logging

from core import paths, session, tools_registry

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 5

# Tool results are resent on every subsequent iteration, so a big directory
# listing or file read is paid for repeatedly. Groq's free tier allows 12k
# tokens/minute and an uncapped listing exhausts it in a couple of turns.
MAX_TOOL_RESULT_CHARS = 2000

_SYSTEM = """You control the user's Windows machine through a WSL2 agent called RAVEN.

Use the provided tools to actually do what the user asks - never tell them to \
run a command themselves, and never claim you cannot navigate.

The user thinks in Windows paths (D:\\Projects). You may pass paths in any \
form: D:\\Projects, D:/Projects, /mnt/d/Projects, or "d drive projects". They \
all resolve correctly. Relative names resolve against the current folder.

The current folder is {cwd_win}.

Chain tools when a request needs more than one step. For example, "go to d \
drive projects and list the folders" is cd followed by list_dir.

Always prefer the read-only tools. To see what is in a folder use list_dir or \
tree; to locate a file use find_files. Only use shell_command or run_script \
when no other tool can do the job - they interrupt the user for approval, so \
reaching for them to list or search files wastes their time.

If the user denies permission, do not retry the same action and do not look \
for a way around it. Tell them what you did not do.

When you have the answer, reply in plain text. Keep replies short - they are \
read on a phone. Show paths in Windows form."""


async def run(bot, chat_id: int, text: str, llm, guard, on_file=None) -> str:
    """Run the loop until the model answers. Returns the text to show the user.

    Raises RuntimeError when every LLM provider is unavailable, so the caller
    can fall back to the regex classifier.
    """
    cwd = session.get_cwd(chat_id)
    messages = [
        {"role": "system", "content": _SYSTEM.format(cwd_win=paths.to_windows(cwd))},
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
        return f"Cannot send {paths.to_windows(str(target))}: {error}"
    if on_file is None:
        return "No delivery channel available."
    await on_file(path)
    return f"Sent {paths.to_windows(path)} to the user."


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
    win = paths.to_windows(str(target)) if target else ""
    return {
        "write_file": f"write to {win}",
        "delete_file": f"delete {win}",
        "run_script": f"run script {win}",
    }.get(name, f"{name} {win}")
