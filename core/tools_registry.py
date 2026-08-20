"""Tool schemas the LLM can call, and the dispatch that runs them.

Kept separate from agent.py so the loop logic and the tool surface can be read
and tested independently. Every executor returns a string -- the loop feeds it
straight back to the model as a tool result, so failures must come back as text
rather than exceptions or the model cannot recover from them.
"""

import logging
import os
from pathlib import Path

from core import paths, platform as host, session
from tools import commerce, file_ops, process_ops, system_info

logger = logging.getLogger(__name__)

GATED_TOOLS = frozenset({
    "write_file", "delete_file", "run_script", "shell_command", "kill_process",
    # Spends real money. agent.py intercepts this one before dispatch so the
    # approval prompt can show the actual cart and total (see _commerce_checkout).
    "commerce_checkout",
})


def _is_dir(path: Path) -> bool:
    """Wrapped so tests can stub filesystem checks."""
    return path.is_dir()


def _path_arg(args: dict, chat_id: int, key: str = "path") -> tuple[bool, Path | None, str]:
    """Resolve a path argument against the chat's cwd. Returns (ok, path, error)."""
    cwd = session.get_cwd(chat_id)
    raw = args.get(key)
    if raw is None or str(raw).strip() == "":
        return True, Path(cwd), ""
    ok, target = file_ops.validate_path(str(raw), cwd=cwd)
    if not ok:
        return False, None, f"Access denied or unreadable path: {raw}"
    return True, target, ""


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "cd",
            "description": (
                "Change the current working directory. Accepts absolute paths, "
                f"~ shorthand, or a name relative to the current folder. "
                f"Paths look like {host.PATH_STYLE}."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Target directory."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and folders. Omit path to list the current folder.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory to list."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tree",
            "description": "Show a nested directory tree. Omit path for the current folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_depth": {"type": "integer", "description": "Default 3."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file's contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Search all allowed locations for files whose name matches a query.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": "Send a file to the user over Telegram.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_explorer",
            "description": (
                f"Reveal a folder or file in {host.FILE_MANAGER} on the user's "
                "desktop. Omit path to open the current folder."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_files",
            "description": "List recently modified files. Omit path for the current folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "hours": {"type": "integer", "description": "Default 24."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "status",
            "description": "Report system status: CPU, memory, disk, LLM providers.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": f"Capture the desktop of {host.HOST_DESCRIPTION} and send it.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text to a file. Requires the user's approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file. Requires the user's approval.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": "Run a Python script. Requires the user's approval.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell_command",
            "description": f"Run a bash command on {host.HOST_DESCRIPTION}. Requires the user's approval.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_process",
            "description": "Kill a tracked background process. Requires the user's approval.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": [],
            },
        },
    },
]



_COMMERCE_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "commerce_providers",
            "description": (
                "List the shopping providers (e.g. Swiggy Instamart), which one "
                "is selected, and whether each is connected. Use this first when "
                "the user asks to shop or order something."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commerce_select",
            "description": "Choose which shopping provider to order from.",
            "parameters": {
                "type": "object",
                "properties": {"provider_id": {"type": "string", "description": "e.g. 'swiggy' or 'mock'."}},
                "required": ["provider_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commerce_connect",
            "description": (
                "Log in to a shopping provider. Needed before its cart or "
                "checkout can be used. May open a browser on the user's Mac."
            ),
            "parameters": {
                "type": "object",
                "properties": {"provider_id": {"type": "string"}},
                "required": ["provider_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commerce_search",
            "description": (
                "Search the store for a product. Returns product ids that "
                "commerce_add_to_cart needs. Always search before adding."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "e.g. 'milk', 'brown bread'."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commerce_cart",
            "description": "Show the current shopping cart and its total.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commerce_add_to_cart",
            "description": (
                "Add a product to the cart. Get product_id from commerce_compare "
                "first -- never invent one. Adds to what is already there."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "Exact id from commerce_search."},
                    "quantity": {"type": "integer", "description": "Default 1."},
                    "name": {"type": "string", "description": "Product name, for display."},
                    "price_minor": {"type": "integer", "description": "Unit price in paise, from commerce_search."},
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commerce_clear_cart",
            "description": "Empty the shopping cart.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commerce_checkout",
            "description": (
                "Place the order for everything in the cart. THIS SPENDS REAL "
                "MONEY. The user is shown the cart and total and must tap Approve. "
                "Show them the cart with commerce_cart before calling this."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commerce_order_status",
            "description": "Check the payment/delivery status of a placed order.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
]

_SYSTEM_SCHEMAS = TOOL_SCHEMAS

# Tools kept alongside the commerce ones in shopping mode: sending a file is how
# an invoice or receipt reaches the user, and status answers "are you alive?".
_SHOPPING_EXTRAS = {"send_file", "status"}

_MODE = os.getenv("BOT_MODE", "shopping").strip().lower()

if _MODE == "full":
    TOOL_SCHEMAS = _SYSTEM_SCHEMAS + _COMMERCE_SCHEMAS
else:
    TOOL_SCHEMAS = [
        s for s in _SYSTEM_SCHEMAS
        if s["function"]["name"] in _SHOPPING_EXTRAS
    ] + _COMMERCE_SCHEMAS


async def execute(name: str, args: dict, chat_id: int) -> str:
    """Run a tool and return a string result. Never raises."""
    try:
        return await _dispatch(name, args or {}, chat_id)
    except Exception as exc:
        logger.exception(f"Tool {name} failed")
        return f"Tool '{name}' failed: {type(exc).__name__}: {exc}"


async def _dispatch(name: str, args: dict, chat_id: int) -> str:
    if name == "cd":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        if not _is_dir(target):
            return f"Not a directory: {paths.display(str(target))}"
        session.set_cwd(chat_id, str(target))
        return f"Current folder is now {paths.display(str(target))}"

    if name == "list_dir":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        listing = file_ops.list_dir(str(target))
        return f"{paths.display(str(target))}\n\n{listing}"

    if name == "tree":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        depth = int(args.get("max_depth") or 3)
        return file_ops.tree(str(target), max_depth=depth)

    if name == "read_file":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        return file_ops.read_file(str(target))

    if name == "find_files":
        query = str(args.get("query") or "").strip()
        if not query:
            return "find_files needs a query."
        hits = file_ops.find_files(query)
        if not hits:
            return f"No files matching '{query}'."
        return "\n".join(paths.display(str(h)) for h in hits)

    if name == "recent_files":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        hours = int(args.get("hours") or 24)
        return file_ops.recent_files(str(target), hours=hours)

    if name == "status":
        return await system_info.get_status()

    if name == "open_explorer":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        shown = paths.display(str(target))
        rc, _, stderr = await process_ops.open_in_explorer(
            paths.to_native(str(target)), timeout=15
        )
        if rc != 0:
            return f"Could not open {shown}: {stderr.strip()[:200]}"
        return f"Opened {shown} in {host.FILE_MANAGER}."

    # send_file and screenshot need the Telegram Bot object, so agent.py
    # intercepts them before dispatch and they never reach here.
    if name in ("send_file", "screenshot"):
        return f"Tool '{name}' must be handled by the agent loop."

    if name == "write_file":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        return file_ops.write_file(str(target), str(args.get("content") or ""))

    if name == "delete_file":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        return file_ops.delete_file(str(target))

    if name == "run_script":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        rc, out, errout = await process_ops.run_script(str(target))
        return f"exit={rc}\n{out}\n{errout}".strip()

    if name == "shell_command":
        command = str(args.get("command") or "").strip()
        if not command:
            return "shell_command needs a command."
        cwd = session.get_cwd(chat_id)
        rc, out, errout = await process_ops.run_command(command, timeout=120, cwd=cwd)
        process_ops.add_to_history(command, rc, out or errout)
        return f"exit={rc}\n{out}\n{errout}".strip()

    if name == "kill_process":
        return await process_ops.kill_process(args.get("name"))

    # ── commerce (agentic-commerce BFF) ──────────────────────────
    if name == "commerce_providers":
        return await commerce.providers(chat_id)

    if name == "commerce_select":
        pid = str(args.get("provider_id") or "").strip()
        return await commerce.select(chat_id, pid) if pid else "commerce_select needs a provider_id."

    if name == "commerce_connect":
        pid = str(args.get("provider_id") or "").strip()
        return await commerce.connect(chat_id, pid) if pid else "commerce_connect needs a provider_id."

    if name == "commerce_search":
        query = str(args.get("query") or "").strip()
        return await commerce.search(chat_id, query) if query else "commerce_search needs a query."

    if name == "commerce_cart":
        return await commerce.cart(chat_id)

    if name == "commerce_add_to_cart":
        product_id = str(args.get("product_id") or "").strip()
        if not product_id:
            return "commerce_add_to_cart needs a product_id (get one from commerce_compare)."
        try:
            quantity = max(1, int(args.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        price = args.get("price_minor")
        return await commerce.add_to_cart(
            chat_id, product_id, quantity,
            str(args.get("name") or ""),
            int(price) if isinstance(price, (int, float)) else None,
        )

    if name == "commerce_clear_cart":
        return await commerce.clear_cart(chat_id)

    if name == "commerce_order_status":
        order_id = str(args.get("order_id") or "").strip()
        return await commerce.payment_status(chat_id, order_id) if order_id \
            else "commerce_order_status needs an order_id."

    # commerce_checkout is intercepted by agent.py (it needs the Telegram Bot to
    # run the approval prompt), so reaching here means it was called out of band.
    if name == "commerce_checkout":
        return "Tool 'commerce_checkout' must be handled by the agent loop."

    return f"Unknown tool: {name}"
