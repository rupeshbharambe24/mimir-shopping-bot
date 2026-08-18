"""Tool schemas the LLM can call, and the dispatch that runs them.

Kept separate from agent.py so the loop logic and the tool surface can be read
and tested independently. Every executor returns a string -- the loop feeds it
straight back to the model as a tool result, so failures must come back as text
rather than exceptions or the model cannot recover from them.
"""

import logging
from pathlib import Path

from core import paths, session
from tools import file_ops, process_ops, system_info

logger = logging.getLogger(__name__)

GATED_TOOLS = frozenset({
    "write_file", "delete_file", "run_script", "shell_command", "kill_process",
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
                "Change the current working directory. Accepts Windows paths "
                "(D:\\Projects), WSL paths (/mnt/d/Projects), spoken drives "
                "('d drive projects'), or a name relative to the current folder."
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
            "description": "Search all allowed drives for files whose name matches a query.",
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
                "Open a folder or file in Windows Explorer on the user's desktop. "
                "Omit path to open the current folder."
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
            "description": "Capture the user's Windows desktop and send it.",
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
            "description": "Run a bash command in WSL. Requires the user's approval.",
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
            return f"Not a directory: {paths.to_windows(str(target))}"
        session.set_cwd(chat_id, str(target))
        return f"Current folder is now {paths.to_windows(str(target))}"

    if name == "list_dir":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        listing = file_ops.list_dir(str(target))
        return f"{paths.to_windows(str(target))}\n\n{listing}"

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
        return "\n".join(paths.to_windows(str(h)) for h in hits)

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
        win = paths.to_windows(str(target))
        rc, _, stderr = await process_ops.open_in_explorer(win, timeout=15)
        if rc != 0:
            return f"Could not open {win}: {stderr.strip()[:200]}"
        return f"Opened {win} in Explorer."

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

    return f"Unknown tool: {name}"
