import asyncio
import functools
import html
import logging
import re
from pathlib import Path

from telegram import (
    Update,
    BotCommand,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest

from config import Config
from core.guard import Guard
from core.llm_cascade import SmartLLM
from core.notebook import NotebookManager
from core import brain, agent, paths, session
from core import platform as host
from core.monitor import ProcessMonitor
from tools import commerce, file_ops, process_ops, screenshot, system_info, git_ops, voice

logger = logging.getLogger(__name__)

guard = Guard()
llm = SmartLLM()
monitor = ProcessMonitor()
notebook_mgr = NotebookManager()

MAX_MSG = 4000  # Telegram limit is 4096, leave headroom

# Examples shown in help text. They have to match the machine the user is on:
# a Mac user told to type "d drive projects" will type it, and get nothing.
if host.IS_MAC:
    _EXAMPLE_DIR = "~/Projects"
    _EXAMPLE_PYTHON = "~/.venvs/ml/bin/python"
    _EXAMPLE_PHRASE = "go to my projects folder and list the folders"
    _PATH_HELP = (
        "Paths accept /Users/you/Projects, ~/Projects, or a name\n"
        "relative to the current folder. /ls and /tree with no\n"
        "argument use the current folder."
    )
else:
    _EXAMPLE_DIR = "/mnt/d/projects"
    _EXAMPLE_PYTHON = "/mnt/d/commonenv/Scripts/python.exe"
    _EXAMPLE_PHRASE = "go to d drive projects and list the folders"
    _PATH_HELP = (
        "Paths accept any spelling: D:\\Projects, D:/Projects,\n"
        "/mnt/d/Projects, 'd drive projects', or a name relative\n"
        "to the current folder. /ls and /tree with no argument use\n"
        "the current folder."
    )


# ── Persona system ────────────────────────────────────────────────

_AGENT_CONTEXT = (
    "You help the user shop: finding products, comparing what is in stock, "
    "building a cart and placing orders. You can also send them files and "
    "report your own status. Keep replies short - they are read on a phone. "
    "Show prices and quantities plainly."
)

PERSONAS = {
    "mimir": (
        "You are Mimir, a sharp and efficient shopping assistant. "
        "Named for the Norse keeper of the well of wisdom: you give straight "
        "counsel, not sales patter. Direct, no fluff, occasionally dry. "
        "You never pressure the user into buying anything. "
        + _AGENT_CONTEXT
    ),
    "formal": (
        "You are a professional shopping assistant. Clear, structured and "
        "business-appropriate. No humour, just precise and helpful information. "
        + _AGENT_CONTEXT
    ),
    "friendly": (
        "You are a warm, upbeat shopping assistant. Conversational and helpful, "
        "like a good shop assistant who knows the stock. Never pushy. "
        + _AGENT_CONTEXT
    ),
    "brief": (
        "You are extremely concise. Answer in as few words as possible. "
        "Prices and product names, nothing else unless asked. "
        + _AGENT_CONTEXT
    ),
}

DEFAULT_PERSONA = "mimir"

# File to persist persona across restarts
_PERSONA_FILE = Config.WORKSPACE / ".persona"


def _load_persona() -> tuple[str, str]:
    """Load active persona from disk. Returns (name, system_prompt)."""
    try:
        if _PERSONA_FILE.exists():
            content = _PERSONA_FILE.read_text(encoding="utf-8").strip()
            # First line = name, rest = prompt (for custom)
            lines = content.split("\n", 1)
            name = lines[0].strip()
            if name == "custom" and len(lines) > 1:
                return "custom", lines[1].strip() + " " + _AGENT_CONTEXT
            if name in PERSONAS:
                return name, PERSONAS[name]
    except Exception:
        pass
    return DEFAULT_PERSONA, PERSONAS[DEFAULT_PERSONA]


def _save_persona(name: str, custom_prompt: str = ""):
    """Save active persona to disk."""
    try:
        if name == "custom":
            _PERSONA_FILE.write_text(f"custom\n{custom_prompt}", encoding="utf-8")
        else:
            _PERSONA_FILE.write_text(name, encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to save persona: {e}")


def get_persona() -> str:
    """Get the active system prompt."""
    _, prompt = _load_persona()
    return prompt


# ── Reminders ─────────────────────────────────────────────────────

_reminders: list[dict] = []  # {task: asyncio.Task, time: float, message: str}


# ── Auth decorator ────────────────────────────────────────────────

def authorized(func):
    """Only allow messages from the configured chat ID."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if not chat or chat.id != Config.TELEGRAM_CHAT_ID:
            if chat:
                logger.warning(f"Unauthorized access from chat_id={chat.id}")
            return
        return await func(update, context)
    return wrapper


# ── Helpers ───────────────────────────────────────────────────────

async def typing(update):
    """Show typing indicator."""
    try:
        await update.effective_chat.send_action("typing")
    except Exception:
        pass


def md_to_html(text: str) -> str:
    """Convert LLM markdown output to Telegram-compatible HTML."""
    import html as _html

    # First, extract code blocks so we don't mess with their contents
    code_blocks = []
    def _save_code(m):
        code_blocks.append(m.group(2))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    # Save fenced code blocks: ```lang\ncode\n```
    result = re.sub(r"```(\w*)\n?(.*?)```", _save_code, text, flags=re.DOTALL)

    # Escape HTML entities in non-code text
    result = _html.escape(result)

    # Headings: ### text -> <b>text</b>
    result = re.sub(r"^#{1,4}\s+(.+)$", r"<b>\1</b>", result, flags=re.MULTILINE)

    # Bold: **text** -> <b>text</b>
    result = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", result)

    # Italic: *text* -> <i>text</i> (but not inside words)
    result = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"<i>\1</i>", result)

    # Inline code: `text` -> <code>text</code>
    result = re.sub(r"`([^`]+?)`", r"<code>\1</code>", result)

    # Restore code blocks as <pre>
    for i, code in enumerate(code_blocks):
        escaped_code = _html.escape(code.strip())
        result = result.replace(f"\x00CODEBLOCK{i}\x00", f"<pre>{escaped_code}</pre>")

    return result


async def reply(message, text, use_html=False):
    """Send a text reply, splitting into chunks if too long."""
    parse_mode = "HTML" if use_html else None
    if len(text) <= MAX_MSG:
        try:
            await message.reply_text(text, parse_mode=parse_mode)
        except Exception:
            # Fallback to plain text if HTML parsing fails
            await message.reply_text(text)
    else:
        for i in range(0, len(text), MAX_MSG):
            chunk = text[i : i + MAX_MSG]
            try:
                await message.reply_text(chunk, parse_mode=parse_mode)
            except Exception:
                await message.reply_text(chunk)


async def reply_smart(message, text):
    """Send LLM response with markdown converted to Telegram HTML."""
    html_text = md_to_html(text)
    await reply(message, html_text, use_html=True)


async def _handle_send_file(update: Update, search_info: str, context=None):
    """Search for and send a file based on natural language description."""
    parts = search_info.split("::", 1)
    directory = parts[0] if parts else ""
    query = parts[1] if len(parts) > 1 else ""

    if not query or len(query.strip()) < 2:
        await update.message.reply_text(
            "I couldn't figure out which file you want.\n"
            "Try: /send <exact-path>\n"
            f"Or describe it more: 'send me the report.pptx from {_EXAMPLE_DIR}'"
        )
        return

    await update.message.reply_text(f"Searching for '{query.strip()}' in {directory} ...")

    matches = file_ops.search_files(directory, query)

    if not matches:
        await update.message.reply_text(
            f"No files matching '{query.strip()}' found in {directory}\n\n"
            f"Tip: use /ls {directory} to see what's there."
        )
        return

    if len(matches) == 1:
        # Single match — send it directly
        path = str(matches[0])
        fpath, error = file_ops.get_file_for_send(path)
        if error:
            await update.message.reply_text(f"Found {matches[0].name} but can't send: {error}")
            return
        with open(fpath, "rb") as f:
            await update.message.reply_document(document=f, filename=matches[0].name)
    else:
        # Multiple matches — list them and ask user to pick
        lines = [f"Found {len(matches)} files:\n"]
        for i, m in enumerate(matches, 1):
            sz = m.stat().st_size
            if sz < 1024:
                size_str = f"{sz}B"
            elif sz < 1024 * 1024:
                size_str = f"{sz // 1024}KB"
            else:
                size_str = f"{sz // (1024 * 1024)}MB"
            lines.append(f"  {i}. {m.name}  ({size_str})\n     {m.parent}")
        lines.append(f"\nReply with a number to send that file.")
        await reply(update.message, "\n".join(lines))
        if context:
            context.chat_data["pending_files"] = [str(m) for m in matches]


def _resolve_repo_path(context) -> str:
    """Figure out the git repo path from context or fall back to WORKSPACE."""
    last_file = context.chat_data.get("last_file_path")
    if last_file:
        p = Path(last_file)
        # Walk up to find .git
        check = p if p.is_dir() else p.parent
        while check != check.parent:
            if (check / ".git").exists():
                return str(check)
            check = check.parent
    return str(Config.WORKSPACE)


def _dir_arg(update, context) -> str:
    """Resolve a command's path argument against the chat's current folder.

    Bare commands (`/ls` with no argument) fall back to the session's current
    folder rather than WORKSPACE, which is what makes `cd` then `ls` behave the
    way anyone would expect.
    """
    cwd = session.get_cwd(update.effective_chat.id)
    raw = " ".join(context.args) if context.args else ""
    return paths.normalize(raw, cwd) or cwd


# ── /start and /help ─────────────────────────────────────────────

@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await typing(update)
    await reply(
        update.message,
        "\U0001F44B <b>Hi, I'm Mimir</b>\n"
        "<i>your shopping assistant</i>\n"
        "\n"
        "Just tell me what you need, the way you'd tell a person:\n"
        "\n"
        "  \u2022  <i>\u201cdo we have milk and bread?\u201d</i>\n"
        "  \u2022  <i>\u201cadd 2 packs of bourbon biscuits\u201d</i>\n"
        "  \u2022  <i>\u201cwhat's in my cart?\u201d</i>\n"
        "  \u2022  <i>\u201cI'm making samosas \u2014 what do I need?\u201d</i>\n"
        "\n"
        "\U0001F512  <b>Before anything is ordered</b>, I'll show you the\n"
        "full cart and total, and wait for you to tap <b>Approve</b>.\n"
        "No tap, no order. Every time.\n"
        "\n"
        "\U0001F4A1  <code>/help</code> \u2014 everything I can do\n"
        "\U0001F944  <code>/service</code> \u2014 groceries, food, or a table\n"
        "\U0001F6D2  <code>/providers</code> \u2014 which stores are connected",
        use_html=True,
    )


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(
        update.message,
        "\U0001F6D2  <b>Shopping with Mimir</b>\n"
        "\n"
        "<b>Just talk to me.</b> No commands needed:\n"
        "  <i>\u201cfind brown bread\u201d</i>\n"
        "  <i>\u201cadd 2 of the cheapest\u201d</i>\n"
        "  <i>\u201cwhat did I order last week?\u201d</i>\n"
        "  <i>\u201ccheckout\u201d</i>\n"
        "\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "\n"
        "<b>Shortcuts</b>\n"
        "  <code>/service</code>  \u2014  groceries \u00b7 food \u00b7 a table\n"
        "  <code>/search</code> <i>item</i>  \u2014  look something up\n"
        "  <code>/cart</code>  \u2014  what's in your basket\n"
        "  <code>/autopay</code>  \u2014  auto-approve small orders\n"
        "  <code>/providers</code>  \u2014  connected stores\n"
        "  <code>/connect</code> <i>store</i>  \u2014  log in to a store\n"
        "\n"
        "<b>\U0001F512 About ordering</b>\n"
        "I'll always show you the items and the total first,\n"
        "then wait for your <b>Approve</b> tap. I never place an\n"
        "order on my own, and I won't ask twice if you say no.\n"
        "\n"
        "<b>Other</b>\n"
        "  <code>/status</code>  \u2014  am I working?\n"
        "  <code>/persona</code> <i>name</i>  \u2014  mimir \u00b7 formal \u00b7 friendly \u00b7 brief\n"
        "  <code>/stop</code>  \u2014  shut me down",
        use_html=True,
    )


# ── LLM commands ──────────────────────────────────────────────────

@authorized
async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        await update.message.reply_text("Usage: /ask <your question>")
        return
    await typing(update)
    thinking = await update.message.reply_text("Processing...")
    response = await llm.ask(prompt, system_prompt=get_persona())
    try:
        await thinking.delete()
    except Exception:
        pass
    await reply_smart(update.message, response or "No response.")


@authorized
async def cmd_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        await update.message.reply_text("Usage: /code <your question>")
        return
    await typing(update)
    thinking = await update.message.reply_text("Analyzing code...")
    response = await llm.ask_code(prompt)
    try:
        await thinking.delete()
    except Exception:
        pass
    await reply_smart(update.message, response or "No response.")


# ── File commands ─────────────────────────────────────────────────

@authorized
async def cmd_read(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /read <filepath>")
        return
    filepath = _dir_arg(update, context)
    context.chat_data["last_file_path"] = filepath
    content = file_ops.read_file(filepath)
    await reply(update.message, f"{paths.display(filepath)}:\n\n{content}")


@authorized
async def cmd_ls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dirpath = _dir_arg(update, context)
    content = file_ops.list_dir(dirpath)
    await reply(update.message, f"{paths.display(dirpath)}:\n\n{content}")


@authorized
async def cmd_write(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    # Parse: /write <path>\n<content>
    remainder = text[len("/write"):].strip()
    if not remainder:
        await update.message.reply_text("Usage:\n/write <path>\n<content on next lines>")
        return

    lines = remainder.split("\n", 1)
    filepath = lines[0].strip()
    content = lines[1] if len(lines) > 1 else ""

    if not content:
        await update.message.reply_text("No content provided.\nUsage:\n/write <path>\n<content on next lines>")
        return

    approved = await guard.request_permission(
        context.bot, update.effective_chat.id,
        "write_file",
        f"Write to {filepath}",
        f"{len(content)} chars:\n{content[:200]}",
    )
    if approved:
        result = file_ops.write_file(filepath, content)
        await update.message.reply_text(result)
        context.chat_data["last_file_path"] = filepath
        # Offer git commit if inside a repo
        await _offer_git_commit(update, context, filepath, f"update {Path(filepath).name}")


@authorized
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filepath = " ".join(context.args) if context.args else ""
    if not filepath:
        await update.message.reply_text("Usage: /delete <filepath>")
        return

    approved = await guard.request_permission(
        context.bot, update.effective_chat.id,
        "delete_file",
        f"Delete {filepath}",
    )
    if approved:
        result = file_ops.delete_file(filepath)
        await update.message.reply_text(result)


@authorized
async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args) if context.args else ""
    if not raw:
        await update.message.reply_text("Usage: /send <filepath or search terms>")
        return

    # If it looks like an actual path, try direct send
    if "/" in raw or "\\" in raw or raw.startswith("/mnt"):
        path, error = file_ops.get_file_for_send(raw)
        if not error:
            with open(path, "rb") as f:
                await update.message.reply_document(document=f, filename=Path(path).name)
            return

    # Otherwise, search for the file
    await typing(update)

    # Extract drive hint (e.g., "from r drive" or "r drive"). Drive words name
    # nothing on a Mac, so matching them there would search a path that cannot
    # exist instead of falling through to the all-locations search.
    drive_match = None if host.IS_MAC else re.search(r"\b([a-zA-Z])\s*drive\b", raw.lower())
    if drive_match:
        search_dir = f"/mnt/{drive_match.group(1).lower()}"
        # Remove drive reference from search query
        query = re.sub(r"\b[a-zA-Z]\s*drive\b", "", raw.lower())
        query = re.sub(r"\b(from|the|in|at|on|my)\b", "", query)
        query = re.sub(r"\s+", " ", query).strip()
    else:
        search_dir = None
        query = re.sub(r"\b(from|the|in|at|on|my)\b", "", raw.lower()).strip()

    if not query or len(query) < 2:
        await update.message.reply_text("Please be more specific about which file you want.")
        return

    await update.message.reply_text(f"Searching for '{query}' ...")

    # Search in specific drive or all allowed paths
    if search_dir:
        matches = file_ops.search_files(search_dir, query)
    else:
        matches = file_ops.find_files(query)

    if not matches:
        await update.message.reply_text(f"No files matching '{query}' found.")
        return

    if len(matches) == 1:
        fpath, error = file_ops.get_file_for_send(str(matches[0]))
        if error:
            await update.message.reply_text(f"Found {matches[0].name} but can't send: {error}")
            return
        with open(fpath, "rb") as f:
            await update.message.reply_document(document=f, filename=matches[0].name)
    else:
        lines = [f"Found {len(matches)} files:\n"]
        for i, m in enumerate(matches, 1):
            sz = m.stat().st_size
            if sz < 1024 * 1024:
                size_str = f"{sz // 1024}KB"
            else:
                size_str = f"{sz // (1024 * 1024)}MB"
            lines.append(f"  {i}. {m.name}  ({size_str})\n     {m.parent}")
        lines.append(f"\nReply with a number to send that file.")
        await reply(update.message, "\n".join(lines))
        # Store matches so user can reply with a number
        context.chat_data["pending_files"] = [str(m) for m in matches]


@authorized
async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search for files across all allowed paths. Usage: /find <name>"""
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /find <filename or keyword>")
        return

    searching = await update.message.reply_text(f"Searching for '{query}'...")
    matches = file_ops.find_files(query)

    if not matches:
        try:
            await searching.edit_text(f"No files found matching '{query}'.")
        except Exception:
            await update.message.reply_text(f"No files found matching '{query}'.")
        return

    lines = [f"Found {len(matches)} file(s) matching '{query}':\n"]
    for i, m in enumerate(matches, 1):
        try:
            sz = m.stat().st_size
            if sz < 1024:
                size_str = f"{sz}B"
            elif sz < 1024 * 1024:
                size_str = f"{sz // 1024}KB"
            else:
                size_str = f"{sz // (1024 * 1024)}MB"
        except OSError:
            size_str = "?"
        lines.append(f"  {i}. {m.name}  ({size_str})\n     {m.parent}")

    result = "\n".join(lines)
    try:
        await searching.edit_text(result)
    except Exception:
        await reply(update.message, result)


@authorized
async def cmd_tree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show directory tree. Usage: /tree <path> or /tree (current folder)"""
    dirpath = _dir_arg(update, context)
    result = file_ops.tree(dirpath)
    await reply(update.message, f"{paths.display(dirpath)}:\n\n{result}")


@authorized
async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recently modified files. Usage: /recent or /recent <path>"""
    dirpath = _dir_arg(update, context)
    result = file_ops.recent_files(dirpath)
    await reply(update.message, result)


# ── Process commands ─────────────────────────────────────────────

@authorized
async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filepath = " ".join(context.args) if context.args else ""
    if not filepath:
        await update.message.reply_text("Usage: /run <script.py>")
        return

    approved = await guard.request_permission(
        context.bot, update.effective_chat.id,
        "run_script",
        f"Run script: {filepath}",
    )
    if approved:
        await monitor.run_and_report(context.bot, update.effective_chat.id, filepath)


@authorized
async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run script with live output streaming. Usage: /watch <script.py>"""
    filepath = " ".join(context.args) if context.args else ""
    if not filepath:
        await update.message.reply_text("Usage: /watch <script.py>")
        return

    approved = await guard.request_permission(
        context.bot, update.effective_chat.id,
        "run_script",
        f"Watch script: {filepath}",
    )
    if not approved:
        return

    status_msg = await update.message.reply_text(f"Running {filepath} (live output)...")
    chunk_count = 0

    async def output_callback(chunk: str):
        nonlocal chunk_count, status_msg
        chunk_count += 1
        text = chunk.strip()
        if not text:
            return
        # Truncate chunk for Telegram
        if len(text) > MAX_MSG - 100:
            text = text[:MAX_MSG - 100] + "\n... (truncated)"
        try:
            if chunk_count <= 1:
                await status_msg.edit_text(f"OUTPUT:\n{text}")
            else:
                await context.bot.send_message(
                    update.effective_chat.id,
                    f"OUTPUT (chunk {chunk_count}):\n{text}",
                )
        except Exception as e:
            logger.warning(f"Watch callback error: {e}")

    returncode, full_output = await process_ops.watch_script(
        filepath, callback=output_callback,
    )

    status = "completed" if returncode == 0 else f"failed (exit {returncode})"
    await context.bot.send_message(
        update.effective_chat.id,
        f"Script {status}.",
    )


@authorized
async def cmd_tail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Monitor a log file. Usage: /tail <logfile>"""
    filepath = " ".join(context.args) if context.args else ""
    if not filepath:
        await update.message.reply_text("Usage: /tail <logfile>")
        return

    target = Path(filepath)
    if not target.exists():
        await update.message.reply_text(f"File not found: {filepath}")
        return

    await update.message.reply_text(f"Tailing {filepath} (send /kill to stop)...")

    async def tail_callback(new_lines: str):
        text = new_lines.strip()
        if not text:
            return
        if len(text) > MAX_MSG - 50:
            text = text[:MAX_MSG - 50] + "\n..."
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                f"TAIL {Path(filepath).name}:\n{text}",
            )
        except Exception as e:
            logger.warning(f"Tail callback error: {e}")

    # Run tail as a background asyncio task
    tail_task = asyncio.create_task(
        process_ops.tail_file(filepath, callback=tail_callback)
    )

    # Create a fake process-like object for tracking
    class _TailHandle:
        def __init__(self, task):
            self._task = task
            self.pid = id(task)
            self.returncode = None
        def terminate(self):
            self._task.cancel()
            self.returncode = -1

    handle = _TailHandle(tail_task)
    process_ops.track_process(
        f"tail:{Path(filepath).name}", handle, f"tail {filepath}", str(target.parent)
    )


@authorized
async def cmd_procs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show running background processes."""
    result = process_ops.get_running_procs()
    await update.message.reply_text(result)


@authorized
async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kill a background process. Usage: /kill or /kill <name> or /kill all"""
    name = " ".join(context.args) if context.args else None

    if name and name.lower() == "all":
        approved = await guard.request_permission(
            context.bot, update.effective_chat.id,
            "run_script",
            "Kill ALL background processes",
        )
        if approved:
            result = await process_ops.kill_process("all")
            await update.message.reply_text(result)
        return

    if name:
        approved = await guard.request_permission(
            context.bot, update.effective_chat.id,
            "run_script",
            f"Kill process: {name}",
        )
        if approved:
            result = await process_ops.kill_process(name)
            await update.message.reply_text(result)
    else:
        # Kill most recent
        approved = await guard.request_permission(
            context.bot, update.effective_chat.id,
            "run_script",
            "Kill most recent background process",
        )
        if approved:
            result = await process_ops.kill_process(None)
            await update.message.reply_text(result)


@authorized
async def cmd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run any shell command. Usage: /cmd <command>"""
    command = " ".join(context.args) if context.args else ""
    if not command:
        await update.message.reply_text("Usage: /cmd <shell command>")
        return

    approved = await guard.request_permission(
        context.bot, update.effective_chat.id,
        "run_script",
        f"Run command: {command}",
        command,
    )
    if not approved:
        return

    running = await update.message.reply_text(f"Running: {command[:100]}...")
    returncode, stdout, stderr = await process_ops.run_command(command, timeout=120)

    # Add to history
    output_preview = stdout.strip() or stderr.strip()
    process_ops.add_to_history(command, returncode, output_preview)

    output = stdout.strip() or "(no output)"
    if returncode != 0:
        err = stderr.strip()
        if err:
            output += f"\n\nSTDERR:\n{err}"
        output = f"EXIT CODE: {returncode}\n\n{output}"

    if len(output) > MAX_MSG - 50:
        output = output[:MAX_MSG - 50] + "\n... (truncated)"

    try:
        await running.edit_text(output)
    except Exception:
        await reply(update.message, output)


@authorized
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show command history."""
    result = process_ops.get_history()
    await update.message.reply_text(result)


# ── Notebook commands ────────────────────────────────────────────

@authorized
async def cmd_nb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Notebook commands. Usage: /nb <subcommand> [args]"""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Notebook commands:\n\n"
            "  /nb open <path>  - Load a .ipynb file\n"
            "  /nb run          - Run all cells\n"
            "  /nb run 5        - Run cell 5\n"
            "  /nb run 5+       - Run from cell 5 onward\n"
            "  /nb cell 5       - Show cell 5 source\n"
            "  /nb edit 5       - Edit cell (send new content next)\n"
            "  /nb out 5        - Show cell 5 output\n"
            "  /nb status       - Kernel status\n"
            "  /nb stop         - Shutdown kernel\n"
            "  /nb env <path>   - Set Python env for kernel\n"
        )
        return

    subcmd = args[0].lower()
    rest = args[1:]

    if subcmd == "open":
        if not rest:
            await update.message.reply_text("Usage: /nb open <path/to/notebook.ipynb>")
            return
        nb_path = " ".join(rest)
        msg = await update.message.reply_text(f"Loading {nb_path}...")
        result = await notebook_mgr.open_notebook(nb_path)
        try:
            await msg.edit_text(result)
        except Exception:
            await update.message.reply_text(result)
        # Show cell list
        cell_list = notebook_mgr.get_cell_list()
        await reply(update.message, cell_list)

    elif subcmd == "run":
        if not rest:
            # Run all cells
            approved = await guard.request_permission(
                context.bot, update.effective_chat.id,
                "run_script",
                "Run all notebook cells",
            )
            if not approved:
                return

            # Start kernel if not running
            if not notebook_mgr.is_running:
                start_msg = await update.message.reply_text("Starting kernel...")
                start_result = await notebook_mgr.start_kernel()
                try:
                    await start_msg.edit_text(start_result)
                except Exception:
                    await update.message.reply_text(start_result)
                if "Failed" in start_result or "not installed" in start_result:
                    return

            status_msg = await update.message.reply_text("Running all cells...")

            async def run_callback(cell_idx, output, success):
                status = "OK" if success else "ERROR"
                text = f"[Cell {cell_idx + 1} - {status}]\n{output}"
                if len(text) > MAX_MSG - 20:
                    text = text[:MAX_MSG - 20] + "\n..."
                try:
                    await context.bot.send_message(update.effective_chat.id, text)
                except Exception as e:
                    logger.warning(f"Notebook callback error: {e}")

            results = await notebook_mgr.run_all(callback=run_callback)

            # Summary
            total = len(results)
            ok_count = sum(1 for _, _, ok in results if ok)
            fail_count = total - ok_count
            summary = f"Notebook run complete: {ok_count}/{total} cells OK"
            if fail_count:
                summary += f", {fail_count} failed"
            try:
                await status_msg.edit_text(summary)
            except Exception:
                await update.message.reply_text(summary)

        else:
            cell_spec = rest[0]

            if cell_spec.endswith("+"):
                # Run from cell N onward
                try:
                    start_idx = int(cell_spec[:-1]) - 1  # convert 1-based to 0-based
                except ValueError:
                    await update.message.reply_text("Usage: /nb run 5+ (run from cell 5 onward)")
                    return

                approved = await guard.request_permission(
                    context.bot, update.effective_chat.id,
                    "run_script",
                    f"Run notebook cells from {start_idx + 1} onward",
                )
                if not approved:
                    return

                if not notebook_mgr.is_running:
                    start_result = await notebook_mgr.start_kernel()
                    await update.message.reply_text(start_result)
                    if "Failed" in start_result or "not installed" in start_result:
                        return

                async def run_from_callback(cell_idx, output, success):
                    status = "OK" if success else "ERROR"
                    text = f"[Cell {cell_idx + 1} - {status}]\n{output}"
                    if len(text) > MAX_MSG - 20:
                        text = text[:MAX_MSG - 20] + "\n..."
                    try:
                        await context.bot.send_message(update.effective_chat.id, text)
                    except Exception as e:
                        logger.warning(f"Notebook callback error: {e}")

                results = await notebook_mgr.run_from(start_idx, callback=run_from_callback)
                total = len(results)
                ok_count = sum(1 for _, _, ok in results if ok)
                await update.message.reply_text(
                    f"Run complete: {ok_count}/{total} cells OK (from cell {start_idx + 1})"
                )

            else:
                # Run a single cell
                try:
                    cell_idx = int(cell_spec) - 1  # convert 1-based to 0-based
                except ValueError:
                    await update.message.reply_text("Usage: /nb run <cell_number>")
                    return

                approved = await guard.request_permission(
                    context.bot, update.effective_chat.id,
                    "run_script",
                    f"Run notebook cell {cell_idx + 1}",
                )
                if not approved:
                    return

                if not notebook_mgr.is_running:
                    start_result = await notebook_mgr.start_kernel()
                    await update.message.reply_text(start_result)
                    if "Failed" in start_result or "not installed" in start_result:
                        return

                running_msg = await update.message.reply_text(f"Running cell {cell_idx + 1}...")
                output = await notebook_mgr.run_cell(cell_idx)
                if len(output) > MAX_MSG - 20:
                    output = output[:MAX_MSG - 20] + "\n..."
                try:
                    await running_msg.edit_text(output)
                except Exception:
                    await reply(update.message, output)

    elif subcmd == "cell":
        if not rest:
            # Show all cells list
            result = notebook_mgr.get_cell_list()
            await reply(update.message, result)
            return
        try:
            cell_idx = int(rest[0]) - 1
        except ValueError:
            await update.message.reply_text("Usage: /nb cell <number>")
            return
        result = notebook_mgr.get_cell_source(cell_idx)
        await reply(update.message, result)

    elif subcmd == "edit":
        if not rest:
            await update.message.reply_text("Usage: /nb edit <cell_number>\nThen send the new content as your next message.")
            return
        try:
            cell_idx = int(rest[0]) - 1
        except ValueError:
            await update.message.reply_text("Usage: /nb edit <cell_number>")
            return
        # Show current content
        current = notebook_mgr.get_cell_source(cell_idx)
        await reply(update.message, f"Current content:\n{current}\n\nSend new content now (your next message will replace this cell).")
        context.chat_data["nb_edit_cell"] = cell_idx

    elif subcmd == "out":
        if not rest:
            await update.message.reply_text("Usage: /nb out <cell_number>")
            return
        try:
            cell_idx = int(rest[0]) - 1
        except ValueError:
            await update.message.reply_text("Usage: /nb out <cell_number>")
            return
        result = notebook_mgr.get_cell_output(cell_idx)
        await reply(update.message, result)

    elif subcmd == "status":
        is_running = notebook_mgr.is_running
        nb_name = notebook_mgr.notebook_name
        env = notebook_mgr._current_env or "none"
        status = "running" if is_running else "stopped"
        await update.message.reply_text(
            f"Notebook: {nb_name}\n"
            f"Kernel: {status}\n"
            f"Python: {env}"
        )

    elif subcmd == "stop":
        result = await notebook_mgr.stop_kernel()
        await update.message.reply_text(result)

    elif subcmd == "env":
        if not rest:
            await update.message.reply_text(f"Usage: /nb env <path/to/python>\nExample: /nb env {_EXAMPLE_PYTHON}")
            return
        python_path = " ".join(rest)
        msg = await update.message.reply_text(f"Starting kernel with {python_path}...")
        result = await notebook_mgr.start_kernel(python_path)
        try:
            await msg.edit_text(result)
        except Exception:
            await update.message.reply_text(result)

    else:
        await update.message.reply_text(f"Unknown notebook subcommand: {subcmd}\nUse /nb for help.")


# ── Git commands ─────────────────────────────────────────────────

@authorized
async def cmd_commit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Git commit. Usage: /commit or /commit <message>"""
    message = " ".join(context.args) if context.args else ""
    repo = _resolve_repo_path(context)

    if not await git_ops.is_git_repo(repo):
        await update.message.reply_text(f"Not a git repo: {repo}\nTip: set last_file_path by using /read on a file in a repo.")
        return

    # Show status first
    status = await git_ops.git_status(repo)
    if "clean" in status.lower() and "nothing" in status.lower():
        await update.message.reply_text(f"[{repo}]\n{status}")
        return

    if not message:
        # Show diff and auto-generate message
        diff = await git_ops.git_diff(repo)
        await reply(update.message, f"Changes in {repo}:\n\n{status}\n\n{diff}")

        generating = await update.message.reply_text("Generating commit message...")
        prompt = (
            f"Generate a short git commit message (one line, max 72 chars) for these changes:\n\n"
            f"Status:\n{status}\n\nDiff:\n{diff[:2000]}"
        )
        message = await llm.ask(prompt, system_prompt="You are a git commit message writer. Output only the commit message, no quotes, no explanation.")
        message = message.strip().strip('"').strip("'").split("\n")[0][:72]
        try:
            await generating.edit_text(f"Auto-generated message: {message}")
        except Exception:
            await update.message.reply_text(f"Auto-generated message: {message}")

    approved = await guard.request_permission(
        context.bot, update.effective_chat.id,
        "run_script",
        f"Git commit in {repo}",
        f"Message: {message}\n\n{status}",
    )
    if approved:
        result = await git_ops.git_commit(repo, message)
        await update.message.reply_text(result)


@authorized
async def cmd_diff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show git diff."""
    repo = _resolve_repo_path(context)
    if not await git_ops.is_git_repo(repo):
        await update.message.reply_text(f"Not a git repo: {repo}")
        return

    # Show both unstaged and staged
    unstaged = await git_ops.git_diff(repo, staged=False)
    staged = await git_ops.git_diff(repo, staged=True)
    result = f"[{repo}]\n\nUnstaged:\n{unstaged}\n\nStaged:\n{staged}"
    await reply(update.message, result)


@authorized
async def cmd_gitlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent commits. Usage: /gitlog or /gitlog 20"""
    count = 10
    if context.args:
        try:
            count = int(context.args[0])
        except ValueError:
            pass
    repo = _resolve_repo_path(context)
    if not await git_ops.is_git_repo(repo):
        await update.message.reply_text(f"Not a git repo: {repo}")
        return
    result = await git_ops.git_log(repo, count)
    await reply(update.message, f"[{repo}] Last {count} commits:\n\n{result}")


@authorized
async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Undo last commit (soft reset). Needs approval."""
    repo = _resolve_repo_path(context)
    if not await git_ops.is_git_repo(repo):
        await update.message.reply_text(f"Not a git repo: {repo}")
        return

    # Show what will be undone
    log = await git_ops.git_log(repo, 1)
    approved = await guard.request_permission(
        context.bot, update.effective_chat.id,
        "run_script",
        f"Undo last commit in {repo}",
        f"Will undo: {log}",
    )
    if approved:
        result = await git_ops.git_undo_last(repo)
        await update.message.reply_text(result)


# ── Git auto-commit helper ───────────────────────────────────────

async def _offer_git_commit(update: Update, context: ContextTypes.DEFAULT_TYPE, filepath: str, description: str):
    """After approved file writes, offer to commit if inside a git repo."""
    try:
        p = Path(filepath)
        check = p if p.is_dir() else p.parent
        repo_path = None
        while check != check.parent:
            if (check / ".git").exists():
                repo_path = str(check)
                break
            check = check.parent

        if repo_path:
            await update.message.reply_text(
                f"File is in git repo ({repo_path}).\n"
                f"Use /commit to commit changes, or /diff to review."
            )
    except Exception:
        pass


# ── Quality of life commands ─────────────────────────────────────

@authorized
async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reveal a file/folder in the desktop file manager. Usage: /open <path>"""
    target = _dir_arg(update, context)
    shown = paths.display(target)
    returncode, _, stderr = await process_ops.open_in_explorer(
        paths.to_native(target), timeout=15
    )
    if returncode != 0:
        await update.message.reply_text(f"Failed to open {shown}: {stderr.strip()[:200]}")
        return
    await update.message.reply_text(f"Opened: {shown}")


@authorized
async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change the current folder. Usage: /cd <path>"""
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            f"Current folder: {paths.display(session.get_cwd(chat_id))}"
        )
        return
    target = _dir_arg(update, context)
    ok, resolved = file_ops.validate_path(target, cwd=session.get_cwd(chat_id))
    if not ok:
        await update.message.reply_text(f"Access denied: {target}")
        return
    if not resolved.is_dir():
        await update.message.reply_text(f"Not a directory: {paths.display(str(resolved))}")
        return
    session.set_cwd(chat_id, str(resolved))
    await update.message.reply_text(f"Now in: {paths.display(str(resolved))}")


@authorized
async def cmd_pwd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the current folder."""
    cwd = session.get_cwd(update.effective_chat.id)
    await update.message.reply_text(paths.display(cwd))


@authorized
async def cmd_clip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Copy text to the desktop clipboard. Usage: /clip <text>"""
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Usage: /clip <text to copy>")
        return
    try:
        returncode, _, stderr = await process_ops.copy_to_clipboard(text, timeout=10)
        if returncode == 0:
            preview = text[:100] + ("..." if len(text) > 100 else "")
            await update.message.reply_text(f"Copied to clipboard: {preview}")
        else:
            await update.message.reply_text(f"Clipboard error: {stderr}")
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


@authorized
async def cmd_bookmark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save a bookmark. Usage: /bookmark <name> <path>"""
    args = context.args or []
    if len(args) < 2:
        # Show existing bookmarks
        bookmarks = context.bot_data.get("bookmarks", {})
        if not bookmarks:
            await update.message.reply_text("No bookmarks saved.\nUsage: /bookmark <name> <path>")
            return
        lines = ["Bookmarks:\n"]
        for name, path in bookmarks.items():
            lines.append(f"  {name} -> {path}")
        await update.message.reply_text("\n".join(lines))
        return

    name = args[0]
    path = " ".join(args[1:])

    if "bookmarks" not in context.bot_data:
        context.bot_data["bookmarks"] = {}

    context.bot_data["bookmarks"][name] = path
    await update.message.reply_text(f"Bookmark saved: {name} -> {path}")


@authorized
async def cmd_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List directory of a bookmark. Usage: /go <name>"""
    name = " ".join(context.args) if context.args else ""
    if not name:
        # List all bookmarks
        bookmarks = context.bot_data.get("bookmarks", {})
        if not bookmarks:
            await update.message.reply_text("No bookmarks. Use /bookmark <name> <path> to create one.")
            return
        lines = ["Bookmarks:\n"]
        for bname, bpath in bookmarks.items():
            lines.append(f"  /go {bname} -> {bpath}")
        await update.message.reply_text("\n".join(lines))
        return

    bookmarks = context.bot_data.get("bookmarks", {})
    if name not in bookmarks:
        await update.message.reply_text(f"Bookmark '{name}' not found.\nUse /bookmark to see all bookmarks.")
        return

    path = bookmarks[name]
    content = file_ops.list_dir(path)
    await reply(update.message, f"{name} ({path}):\n\n{content}")


# ── Agent Control ─────────────────────────────────────────────────

@authorized
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cleanly shut down the agent. Systemd will NOT restart (exit code 0)."""
    await update.message.reply_text(
        "[ MIMIR OFFLINE ]\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Going dark. Use Start MIMIR.bat to bring me back."
    )
    logger.info("Clean shutdown requested via /stop command.")
    # Give Telegram time to deliver the message
    await asyncio.sleep(2)
    # Exit with code 0 — systemd on-failure policy won't restart on clean exit
    import os as _os
    _os._exit(0)


# ── Livestream & Remote ────────────────────────────────────────────

_livestream_task: asyncio.Task | None = None
_livestream_last_msg_id: int | None = None


@authorized
async def cmd_livestream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stream desktop screenshots to Telegram.

    /livestream start     — start streaming (default 5s interval)
    /livestream start 3   — stream every 3 seconds
    /livestream stop      — stop streaming
    /livestream           — show status
    """
    global _livestream_task, _livestream_last_msg_id
    args = context.args or []

    if not args:
        status = "ACTIVE" if _livestream_task and not _livestream_task.done() else "STOPPED"
        await update.message.reply_text(
            f"[ LIVESTREAM: {status} ]\n\n"
            f"/livestream start <seconds> — begin (default 5s)\n"
            f"/livestream stop — end stream"
        )
        return

    subcmd = args[0].lower()

    if subcmd == "stop":
        if _livestream_task and not _livestream_task.done():
            _livestream_task.cancel()
            _livestream_task = None
            await update.message.reply_text("Livestream stopped.")
        else:
            await update.message.reply_text("No livestream running.")
        return

    if subcmd == "start":
        # Parse interval
        interval = 5
        if len(args) > 1:
            try:
                interval = max(3, int(args[1]))  # minimum 3 seconds
            except ValueError:
                pass

        # Stop existing stream if any
        if _livestream_task and not _livestream_task.done():
            _livestream_task.cancel()
            await asyncio.sleep(0.5)

        await update.message.reply_text(
            f"[ LIVESTREAM STARTED ]\n"
            f"Interval: {interval}s\n"
            f"Send /livestream stop to end."
        )

        async def _stream_loop():
            global _livestream_last_msg_id
            chat_id = Config.TELEGRAM_CHAT_ID
            bot = context.bot
            frame = 0

            while True:
                try:
                    path, error = await screenshot.take_screenshot()
                    if error:
                        logger.warning(f"Livestream screenshot error: {error}")
                        await asyncio.sleep(interval)
                        continue

                    frame += 1

                    # Delete previous screenshot to keep chat clean
                    if _livestream_last_msg_id:
                        try:
                            await bot.delete_message(chat_id, _livestream_last_msg_id)
                        except Exception:
                            pass

                    # Send new screenshot
                    with open(path, "rb") as f:
                        msg = await bot.send_photo(
                            chat_id, photo=f,
                            caption=f"LIVE  |  frame {frame}  |  /livestream stop to end"
                        )
                    _livestream_last_msg_id = msg.message_id

                except asyncio.CancelledError:
                    # Clean exit
                    if _livestream_last_msg_id:
                        try:
                            await bot.send_message(chat_id, "Livestream ended.")
                        except Exception:
                            pass
                    _livestream_last_msg_id = None
                    return
                except Exception as e:
                    logger.error(f"Livestream error: {e}")

                await asyncio.sleep(interval)

        _livestream_task = asyncio.create_task(_stream_loop())
        return

    await update.message.reply_text("Usage: /livestream start [seconds] or /livestream stop")


@authorized
async def cmd_remote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send Chrome Remote Desktop access link."""
    await update.message.reply_text(
        "[ REMOTE DESKTOP ]\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Open on your phone:\n"
        "  remotedesktop.google.com/access\n\n"
        "Or use the Chrome Remote Desktop app.\n\n"
        "Setup (one-time on PC):\n"
        "  1. Chrome > remotedesktop.google.com/access\n"
        "  2. Set up remote access > install host\n"
        "  3. Set name + PIN\n\n"
        "Then access from any device with same Google account."
    )


# ── GPU & Reminders ───────────────────────────────────────────────

@authorized
async def cmd_gpu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show GPU status (NVIDIA). Usage: /gpu"""
    await typing(update)
    summary = await host.gpu_summary()
    if not summary:
        await update.message.reply_text(
            "[ GPU STATUS ]\n━━━━━━━━━━━━━━\n\n"
            "No GPU details available on this host."
        )
        return

    # Apple Silicon shares one memory pool between CPU and GPU and exposes no
    # per-process utilisation without sudo, so there is no bar to draw -- the
    # chip and its memory are the whole story.
    if host.IS_MAC:
        await update.message.reply_text(
            "[ GPU STATUS ]\n━━━━━━━━━━━━━━\n\n"
            f"{summary}\n\n"
            "Apple Silicon uses unified memory; live GPU load requires sudo."
        )
        return

    returncode, stdout, stderr = await process_ops.run_command(
        'powershell.exe -NoProfile -Command "'
        'nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu '
        '--format=csv,noheader,nounits"',
        timeout=10,
    )
    if returncode != 0 or not stdout.strip():
        await update.message.reply_text(
            f"[ GPU STATUS ]\n━━━━━━━━━━━━━━\n\n{summary}"
        )
        return

    lines = ["[ GPU STATUS ]", "━━━━━━━━━━━━━━", ""]
    for i, row in enumerate(stdout.strip().split("\n")):
        parts = [p.strip() for p in row.split(",")]
        if len(parts) >= 5:
            name, util, mem_used, mem_total, temp = parts[:5]
            mem_pct = int(float(mem_used) / float(mem_total) * 100) if float(mem_total) > 0 else 0
            bar_len = 15
            filled = int(bar_len * mem_pct / 100)
            mem_bar = "█" * filled + "░" * (bar_len - filled)
            lines.append(f"GPU {i}: {name}")
            lines.append(f"  Load:  {util}%")
            lines.append(f"  VRAM:  [{mem_bar}] {mem_used}/{mem_total} MB ({mem_pct}%)")
            lines.append(f"  Temp:  {temp}°C")
        else:
            lines.append(row)

    await update.message.reply_text("\n".join(lines))


@authorized
async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a reminder. Usage: /remind <minutes> <message>"""
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /remind <minutes> <message>\nExample: /remind 30 check training")
        return

    try:
        minutes = int(args[0])
    except ValueError:
        await update.message.reply_text("First argument must be minutes (number).")
        return

    message = " ".join(args[1:])

    async def _fire_reminder():
        await asyncio.sleep(minutes * 60)
        try:
            await context.bot.send_message(
                Config.TELEGRAM_CHAT_ID,
                f"[ REMINDER ]\n━━━━━━━━━━━━\n\n{message}\n\n(set {minutes}m ago)"
            )
        except Exception as e:
            logger.error(f"Reminder failed: {e}")

    task = asyncio.create_task(_fire_reminder())
    _reminders.append({"task": task, "minutes": minutes, "message": message})

    await update.message.reply_text(f"Reminder set. I'll notify you in {minutes} minutes.")


# ── Persona command ──────────────────────────────────────────────

@authorized
async def cmd_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch or customize MIMIR's personality.

    /persona              — show current + available
    /persona <name>       — switch to preset
    /persona set <prompt> — set custom personality
    /persona reset        — back to default (mimir)
    """
    args = context.args or []

    if not args:
        # Show current persona and list presets
        name, _ = _load_persona()
        lines = [
            "[ PERSONA ]\n"
            "━━━━━━━━━━━━━\n",
            f"Active: {name}\n",
            "Available presets:\n",
        ]
        for pname in PERSONAS:
            marker = " ← active" if pname == name else ""
            # First sentence of each persona as preview
            preview = PERSONAS[pname].split(". ")[0] + "."
            lines.append(f"  {pname}{marker}\n    {preview}\n")
        lines.append(
            "Usage:\n"
            "  /persona <name> — switch preset\n"
            "  /persona set <your prompt> — custom\n"
            "  /persona reset — back to mimir"
        )
        await reply(update.message, "\n".join(lines))
        return

    subcmd = args[0].lower()

    if subcmd == "reset":
        _save_persona(DEFAULT_PERSONA)
        await update.message.reply_text(f"Persona reset to: {DEFAULT_PERSONA}")
        return

    if subcmd == "set":
        custom = " ".join(args[1:]).strip()
        if not custom:
            await update.message.reply_text("Usage: /persona set <your personality description>")
            return
        _save_persona("custom", custom)
        await update.message.reply_text(f"Custom persona active:\n\n\"{custom[:200]}\"")
        return

    if subcmd == "show":
        name, prompt = _load_persona()
        await reply(update.message, f"Persona: {name}\n\nSystem prompt:\n{prompt}")
        return

    # Switch to a preset
    if subcmd in PERSONAS:
        _save_persona(subcmd)
        preview = PERSONAS[subcmd].split(". ")[0] + "."
        await update.message.reply_text(f"Persona switched to: {subcmd}\n\n{preview}")
    else:
        available = ", ".join(PERSONAS.keys())
        await update.message.reply_text(
            f"Unknown persona: '{subcmd}'\n\nAvailable: {available}\n"
            f"Or use: /persona set <your custom prompt>"
        )


# ── System commands ──────────────────────────────────────────────

_PLAN_SYSTEM_PROMPT = """You are a command planner for MIMIR, an agent running on {host_name}.

ENVIRONMENT:
{environment}
- Ollama LLM reachable at {ollama_url}

AVAILABLE DIRECTIVES (put each on its own line):
- SCREENSHOT — takes a screenshot and sends it (waits for page to load)
- SENDFILE:<path> — sends a file to the user (not SENDERFILE)
- OPENDEV — auto-detects the dev server port, opens it in browser, waits for load

PLATFORM RULES:
{shell_rules}

PATH SEARCHING:
- If the user gives a project name, ALWAYS search for it first.
  Users say "prithvi ai" but folder might be "PRITHVI-AI" or "prithvi_ai".
  Use: {search_example}
  Then use the ACTUAL path found. Abort with exit 1 if not found.
- NEVER guess folder names. Search first, use what you find.

RULES:
- Output ONLY a bash script. No explanations, no markdown fences.
- The script runs as a SINGLE bash script, so cd and env vars persist.
- For dev servers that run forever, start them backgrounded:
  {dev_server_example}
- NEVER EVER hardcode a port number (no localhost:3000, no localhost:8080, nothing).
  ALWAYS use OPENDEV — it auto-detects which port the dev server actually started on.
- NEVER open a URL yourself. OPENDEV handles opening the browser.
- After OPENDEV, use SCREENSHOT to capture the page.
- CRITICAL: OPENDEV and SCREENSHOT MUST be at the TOP LEVEL of the script.
  NEVER put them inside if/else, for/done, or while/done blocks.
  If the path search fails, use exit 1 to abort. Example:
    project_path=$(SEARCH_COMMAND_HERE)
    [ -z "$project_path" ] && echo "Project not found" && exit 1
    START_DEV_SERVER_HERE
    sleep 15
    OPENDEV
    SCREENSHOT
- Keep it minimal — only the commands needed.
"""


def _plan_prompt() -> str:
    """The planner prompt, filled in for whichever host MIMIR is on.

    Previously this described WSL2 unconditionally and told the model to shell
    out to powershell.exe. On a Mac that produced plans that were syntactically
    fine and completely unrunnable, so the description of the machine is bound
    to core.platform rather than hardcoded.
    """
    return _PLAN_SYSTEM_PROMPT.format(
        host_name=host.NAME,
        environment=host.ENVIRONMENT,
        ollama_url=Config.OLLAMA_URL,
        shell_rules=host.SHELL_RULES,
        search_example=host.SEARCH_EXAMPLE,
        dev_server_example=host.DEV_SERVER_EXAMPLE,
    )


def _extract_url_from_output(text: str) -> str | None:
    """Extract a local dev server URL from captured process output.
    Looks for 'Local: http://...' or 'http://localhost:...' patterns,
    ignoring github/npm/external URLs."""
    _IGNORE = ("github.com", "npmjs.com", "npm.community", "nodejs.org", "vitejs.dev")
    # Strip ANSI escape codes
    clean = re.sub(r"\x1b\[[0-9;]*m", "", text)
    # First try to find "Local:" line (Vite/Next.js format)
    local_match = re.search(r"Local:\s*(https?://[^\s]+)", clean)
    if local_match:
        return local_match.group(1).strip()
    # Then try any localhost URL
    for m in re.finditer(r"https?://localhost[:\d]*/?\S*", clean):
        url = m.group(0).rstrip(".,;)")
        if not any(h in url for h in _IGNORE):
            return url
    # Then any 127.0.0.1 URL
    for m in re.finditer(r"https?://127\.0\.0\.1[:\d]*/?\S*", clean):
        return m.group(0).rstrip(".,;)")
    return None


async def _scan_open_ports() -> set[int]:
    """Dev server ports already listening, for before/after comparison."""
    return await host.scan_open_ports(Config.WORKSPACE)


async def _find_dev_server_by_scan(exclude_ports: set[int] | None = None) -> str | None:
    """Find a port serving HTML, optionally ignoring pre-existing ones."""
    return await host.find_dev_server(Config.WORKSPACE, exclude_ports)


async def _execute_task(bot, chat_id, task_text: str, context):
    """Use LLM to plan steps, get approval, execute, report results."""
    # Step 1: Ask LLM to create a plan
    planning_msg = await bot.send_message(chat_id, "Planning task steps...")

    system = _plan_prompt()
    plan = await llm.ask(task_text, system_prompt=system)

    if not plan or plan.startswith("Cannot connect") or plan.startswith("Ollama error"):
        await planning_msg.edit_text(f"Failed to plan: {plan}")
        return

    # Clean up: remove markdown fences if LLM wrapped them
    clean_plan = plan.strip()
    if clean_plan.startswith("```"):
        lines = clean_plan.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        clean_plan = "\n".join(lines)

    await planning_msg.edit_text(f"PLAN:\n\n{clean_plan}")

    # Step 2: Get approval
    approved = await guard.request_permission(
        bot, chat_id,
        "run_script",
        "Execute multi-step task",
        clean_plan[:500],
    )
    if not approved:
        return

    # Step 3: Split plan into bash segments and special directives
    # Everything between SCREENSHOT/SENDFILE lines is one bash segment
    status_msg = await bot.send_message(chat_id, "Executing...")

    segments = []  # ("bash"|"screenshot"|"sendfile"|"opendev", data)
    current_bash = []

    for line in clean_plan.strip().split("\n"):
        stripped = line.strip()
        if stripped == "SCREENSHOT":
            if current_bash:
                segments.append(("bash", "\n".join(current_bash)))
                current_bash = []
            segments.append(("screenshot", ""))
        elif stripped == "OPENDEV":
            if current_bash:
                segments.append(("bash", "\n".join(current_bash)))
                current_bash = []
            segments.append(("opendev", ""))
        elif stripped.startswith("SENDFILE:") or stripped.startswith("SENDERFILE:"):
            if current_bash:
                segments.append(("bash", "\n".join(current_bash)))
                current_bash = []
            path = stripped.split(":", 1)[1].strip()
            segments.append(("sendfile", path))
        else:
            current_bash.append(line)

    if current_bash:
        segments.append(("bash", "\n".join(current_bash)))

    output_log = ""
    bg_procs = []
    had_error = False
    detected_url = None
    bg_captured_output = ""  # output from background processes for URL extraction
    ports_before: set[int] = set()  # snapshot of open ports before starting server

    for seg_type, seg_data in segments:
        if seg_type == "screenshot":
            try:
                await status_msg.edit_text("Taking screenshot...")
            except Exception:
                pass
            import asyncio as _aio
            await _aio.sleep(8)  # let page fully render
            path, error = await screenshot.take_screenshot()
            if error:
                output_log += f"\nScreenshot error: {error}"
            elif path:
                with open(path, "rb") as f:
                    await bot.send_photo(chat_id, photo=f, caption="Screenshot")
                output_log += "\nScreenshot sent."

        elif seg_type == "sendfile":
            real_path, error = file_ops.get_file_for_send(seg_data)
            if error:
                output_log += f"\nSend file error ({seg_data}): {error}"
            elif real_path:
                with open(real_path, "rb") as f:
                    await bot.send_document(chat_id, document=f, filename=Path(real_path).name)
                output_log += f"\nSent: {seg_data}"

        elif seg_type == "opendev":
            try:
                await status_msg.edit_text("Detecting dev server URL...")
            except Exception:
                pass

            # 1. Try to extract URL from background process output
            found_url = _extract_url_from_output(bg_captured_output)
            if found_url:
                output_log += f"\nURL from server output: {found_url}"

            # 2. Fallback: scan ports, excluding pre-existing ones
            if not found_url:
                try:
                    await status_msg.edit_text("Scanning for NEW dev server ports...")
                except Exception:
                    pass
                found_url = await _find_dev_server_by_scan(exclude_ports=ports_before)

            if found_url:
                detected_url = found_url
                output_log += f"\nDev server found: {detected_url}"
                await process_ops.open_url(detected_url, timeout=10)
                output_log += f"\nOpened {detected_url} in browser"
                import asyncio as _aio
                await _aio.sleep(12)  # let page fully load and render
            else:
                output_log += "\nCould not find a running dev server."
                await bot.send_message(chat_id, "Could not detect dev server. Check if it started correctly.")

        elif seg_type == "bash":
            try:
                await status_msg.edit_text(f"Running bash segment...\n{seg_data[:200]}")
            except Exception:
                pass

            has_bg = any(l.strip().endswith("&") for l in seg_data.split("\n"))

            if has_bg:
                # Snapshot open ports BEFORE starting dev server
                ports_before = await _scan_open_ports()
                # Run as background process
                proc, captured = await process_ops.run_background_command(
                    f"bash -c {_shell_quote(seg_data)}",
                    wait_for="http",
                    wait_timeout=25,
                )
                bg_procs.append(proc)
                bg_captured_output += captured
                output_log += f"\n{captured}"
                # Check if background process had errors
                _err_signs = ("No such file", "Cannot find path", "ENOENT", "Missing script", "error", "ERR!")
                if any(e in captured for e in _err_signs) and "http" not in captured.lower():
                    output_log += "\nWARNING: Dev server may have failed to start."
                    had_error = True
                    break
            else:
                # Run as a single bash script
                returncode, stdout, stderr = await process_ops.run_command(
                    f"bash -c {_shell_quote(seg_data)}",
                    timeout=120,
                )
                if stdout.strip():
                    output_log += f"\n{stdout.strip()[:1000]}"
                if returncode != 0:
                    output_log += f"\nERROR (exit {returncode}): {stderr.strip()[:500]}"
                    had_error = True
                    break

    # Step 4: Report results
    result_header = "TASK COMPLETED" if not had_error else "TASK FAILED"
    result_text = f"{result_header}\n\n{output_log[-3000:]}"
    try:
        await status_msg.edit_text(result_text)
    except Exception:
        await bot.send_message(chat_id, result_text)

    # Clean up background processes
    for proc in bg_procs:
        try:
            proc.terminate()
        except Exception:
            pass


def _shell_quote(script: str) -> str:
    """Quote a multi-line script for bash -c '...'"""
    import shlex
    return shlex.quote(script)


@authorized
async def cmd_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute a complex multi-step task."""
    task = " ".join(context.args) if context.args else ""
    if not task:
        await update.message.reply_text("Usage: /do <describe what you want done>")
        return
    await _execute_task(context.bot, update.effective_chat.id, task, context)


# ── Commerce (agentic-commerce sidecar) ───────────────────────────

@authorized
async def cmd_providers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List shopping providers and their connection state. Usage: /providers"""
    await typing(update)
    await reply(update.message, await commerce.providers(update.effective_chat.id), use_html=True)


@authorized
async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log in to a shopping provider. Usage: /connect <provider_id>"""
    if not context.args:
        await update.message.reply_text(
            "Usage: /connect <provider_id>\nSee /providers for the list."
        )
        return
    chat_id = update.effective_chat.id
    provider = context.args[0].strip()
    msg = await update.message.reply_text(f"\U0001F510 <i>Connecting to {provider}\u2026</i>", parse_mode="HTML")
    # Selecting as well as connecting: a provider you just logged into is
    # almost certainly the one you want to shop from, and needing two commands
    # to get there is a papercut.
    await commerce.select(chat_id, provider)
    result = await commerce.connect(chat_id, provider)
    await msg.edit_text(result, parse_mode="HTML")


@authorized
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search the store. Usage: /search <product>"""
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /search <product>\nExample: /search milk")
        return
    await typing(update)
    msg = await update.message.reply_text(f"\U0001F50E <i>Searching for {query}\u2026</i>", parse_mode="HTML")
    chat_id = update.effective_chat.id
    result = await commerce.search(chat_id, query)
    if _looks_forgotten_text(result) and await _restore_provider(chat_id):
        result = await commerce.search(chat_id, query)
    await msg.delete()
    await reply(update.message, result, use_html=True)


@authorized
async def cmd_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the shopping cart. Usage: /cart"""
    await typing(update)
    chat_id = update.effective_chat.id
    result = await commerce.cart(chat_id)
    # Same self-heal as the conversational path: a restart of the commerce
    # service should not make /cart tell you to go and connect something you
    # already connected.
    if _looks_forgotten_text(result) and await _restore_provider(chat_id):
        result = await commerce.cart(chat_id)
    await reply(update.message, result, use_html=True)


@authorized
async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shop in natural language. Usage: /shop order 2 litres of milk"""
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text(
            "Usage: /shop <what you want>\n"
            "Example: /shop find the cheapest brown bread and add one to my cart\n\n"
            "Plain messages work too - this command just makes it discoverable."
        )
        return
    await _run_agent_turn(update, context, text)


# The three Swiggy services, in the order they are offered. Ids match
# providers.yaml; the bot never invents a provider id of its own.
SERVICES = (
    ("swiggy",         "\U0001F6D2", "Instamart",   "Groceries in minutes"),
    ("swiggy_food",    "\U0001F35B", "Food",        "Restaurant delivery"),
    ("swiggy_dineout", "\U0001F37D",  "Dineout",     "Book a table"),
)
_SERVICE_BY_ID = {s[0]: s for s in SERVICES}


def _service_keyboard(current: str | None) -> InlineKeyboardMarkup:
    rows = []
    for pid, icon, name, blurb in SERVICES:
        mark = "\u2705 " if pid == current else ""
        rows.append([InlineKeyboardButton(f"{mark}{icon}  {name} \u2014 {blurb}",
                                          callback_data=f"svc:{pid}")])
    return InlineKeyboardMarkup(rows)


_SERVICE_PROMPTS = {
    "swiggy": "Tell me what you need \u2014 <i>\u201cmilk and bread\u201d</i>, "
              "<i>\u201c2 packs of bourbon biscuits\u201d</i>.",
    "swiggy_food": "What are you hungry for? <i>\u201cbutter chicken near me\u201d</i>, "
                   "<i>\u201cpizza under 300\u201d</i>.",
    "swiggy_dineout": "Where and when? <i>\u201ctable for 2 in Koramangala "
                      "Saturday 8pm\u201d</i>.",
}


def _service_prompt(provider_id: str | None) -> str:
    return _SERVICE_PROMPTS.get(provider_id or "", "What are you after?")


def _service_label(provider_id: str | None) -> str:
    entry = _SERVICE_BY_ID.get(provider_id or "")
    return f"{entry[1]} {entry[2]}" if entry else "not chosen yet"


@authorized
async def cmd_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Choose or change what you are shopping for. Usage: /service [name]"""
    chat_id = update.effective_chat.id
    current = session.get_service(chat_id)

    # Accept "/service food" as well as the buttons -- typing is faster once you
    # know what you want, and the buttons are for the first time.
    if context.args:
        wanted = " ".join(context.args).strip().lower()
        match = next(
            (pid for pid, _, name, _ in SERVICES
             if wanted in (pid, name.lower(), pid.replace("swiggy_", ""))),
            None,
        )
        if not match:
            await update.message.reply_text(
                f"I don't know \u201c{wanted}\u201d. Pick one:",
                reply_markup=_service_keyboard(current),
            )
            return
        await _apply_service(update.effective_chat.id, match, context)
        await update.message.reply_text(
            f"\u2705 Switched to <b>{_service_label(match)}</b>.", parse_mode="HTML"
        )
        return

    await update.message.reply_text(
        "\U0001F944 <b>What are we doing?</b>\n"
        f"<i>Currently: {_service_label(current)}</i>\n\n"
        "You can switch any time with /service.",
        parse_mode="HTML",
        reply_markup=_service_keyboard(current),
    )


async def _apply_service(chat_id: int, provider_id: str, context) -> None:
    """Persist the choice locally, select it on the commerce side, and connect.

    The first two are deliberate: the local copy survives a restart of the BFF,
    the BFF's selection is what the agent loop reads, and storing only one
    leaves them disagreeing after either process restarts -- the visible symptom
    being groceries searched when the user asked for a table.

    The connect is what makes switching work at all. Connection is tracked per
    provider id, and Swiggy's three services are three ids, so `/connect swiggy`
    left `swiggy_food` unconnected: choosing Food then failed with "Provider
    'swiggy_food' is not connected", and the bot's advice for that error is
    "try /connect swiggy" -- which the user had already done, and which would
    not have helped if they did it again.

    They all share one OAuth token, so this is a cached-token read rather than a
    fresh login, and connect() is idempotent.
    """
    session.set_service(chat_id, provider_id)
    await commerce.select(chat_id, provider_id)
    await commerce.connect(chat_id, provider_id)


# What each method actually costs the user at pay time. The third column is the
# whole point of the picker: only one of these is genuinely hands-free, and a
# user turning on auto-approve should be told which.
PAYMENT_CHOICES = (
    ("Cash", "", "\U0001F4B5", "Cash on delivery", "no payment step \u2014 fully automatic"),
    ("UPI", "", "\U0001F4F1", "UPI app", "one approval in your UPI app, every order"),
)


@authorized
async def cmd_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Choose how orders get paid for. Usage: /pay [cash|upi]"""
    chat_id = update.effective_chat.id
    pref = session.get_payment_pref(chat_id)

    if context.args:
        wanted = context.args[0].strip().lower()
        method = {"cash": "Cash", "cod": "Cash", "upi": "UPI"}.get(wanted)
        if method:
            session.set_payment_pref(chat_id, method)
            await update.message.reply_text(_payment_summary(chat_id), parse_mode="HTML")
            return

    await update.message.reply_text(
        _payment_summary(chat_id),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"{'\u2705 ' if pref['method'] == m else ''}{icon}  {label}",
                callback_data=f"pay:{m}")]
            for m, _, icon, label, _note in PAYMENT_CHOICES
        ]),
    )


def _payment_summary(chat_id: int) -> str:
    pref = session.get_payment_pref(chat_id)
    current = next((c for c in PAYMENT_CHOICES if c[0] == pref["method"]), None)

    head = (
        f"\U0001F4B3 <b>Paying with: {current[3]}</b>\n<i>{current[4]}</i>"
        if current else
        "\U0001F4B3 <b>How should I pay?</b>\n<i>I'll ask each time until you pick one.</i>"
    )
    return (
        f"{head}\n\n"
        "<b>What Swiggy actually offers</b>\n"
        "  \U0001F4B5  <b>Cash on delivery</b> \u2014 nothing to authorise\n"
        "  \U0001F4F1  <b>UPI app</b> \u2014 I open it, you approve with your PIN\n"
        "  \U0001F5A5  <b>Scan QR</b> \u2014 for desktop, same PIN step\n\n"
        "<i>There is no card and no wallet on Swiggy's MCP \u2014 "
        "<code>get_payment_options</code> returns UPI, QR and cash, nothing else. "
        "And UPI cannot be authorised by a server: RBI requires two factors, one "
        "of them dynamic, on every transaction. So cash on delivery is the only "
        "combination that needs nothing from you at pay time.</i>"
    )


@authorized
async def cmd_autopay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Turn auto-approve on or off. Usage: /autopay [amount|off]"""
    chat_id = update.effective_chat.id

    if not Config.COMMERCE_AUTOPAY_ENABLED:
        await update.message.reply_text(
            "\U0001F512 <b>Auto-approve is switched off on this machine.</b>\n\n"
            "Set <code>COMMERCE_AUTOPAY_ENABLED=true</code> in <code>.env</code> "
            "to allow it, then come back.",
            parse_mode="HTML",
        )
        return

    state = session.get_autopay(chat_id)
    ceiling = Config.COMMERCE_AUTOPAY_MAX_RUPEES

    if context.args:
        raw = context.args[0].strip().lower()
        if raw in ("off", "no", "stop", "disable"):
            session.set_autopay(chat_id, enabled=False, limit_rupees=0)
            await update.message.reply_text(
                "\U0001F512 Auto-approve is <b>off</b>. I'll ask before every order.",
                parse_mode="HTML",
            )
            return
        digits = "".join(c for c in raw if c.isdigit())
        if digits:
            wanted = int(digits)
            limit = min(wanted, ceiling)
            session.set_autopay(chat_id, enabled=True, limit_rupees=limit)
            clamp = (
                f"\n\n<i>You asked for \u20b9{wanted:,} \u2014 capped to \u20b9{limit:,}, "
                f"the ceiling set in .env.</i>" if wanted > limit else ""
            )
            await update.message.reply_text(
                _autopay_summary(chat_id) + clamp, parse_mode="HTML"
            )
            return

    await update.message.reply_text(
        _autopay_summary(chat_id),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"\u20b9{n}", callback_data=f"autopay:{n}")
             for n in (100, 250, 500) if n <= ceiling],
            [InlineKeyboardButton("\U0001F512  Turn off", callback_data="autopay:0")],
        ]),
    )


def _autopay_summary(chat_id: int) -> str:
    state = session.get_autopay(chat_id)
    daily = Config.COMMERCE_AUTOPAY_DAILY_RUPEES
    left = session.autopay_remaining_today(chat_id, daily)

    if not state["enabled"] or state["limit_rupees"] <= 0:
        return (
            "\U0001F512 <b>Auto-approve: off</b>\n"
            "I ask before every order, showing the items and the total.\n\n"
            "Pick a per-order limit to skip that tap for small ones:"
        )
    return (
        "\u26A1 <b>Auto-approve: on</b>\n"
        f"  \u2022 Up to <b>\u20b9{state['limit_rupees']:,}</b> per order\n"
        f"  \u2022 <b>\u20b9{left:,}</b> of today's \u20b9{daily:,} left\n"
        f"  \u2022 Anything larger still needs your tap\n\n"
        "<i>This automates the approval, not the payment. Swiggy has no wallet "
        "on MCP \u2014 UPI still opens your UPI app, and cash on delivery still "
        "means cash at the door.</i>\n\n"
        "<code>/autopay off</code> to stop."
    )


def _autopay_decision(chat_id: int, total_minor: int | None) -> tuple[bool, str]:
    """Should this order skip the tap? Returns (auto_approve, message).

    Fails closed at every uncertainty. In particular an unknown total is NOT
    auto-approved: the manifests cannot yet confirm every provider's total
    field, so a None here means "we could not read the price", and the one thing
    that must never be automatic is spending an amount nobody could state.
    """
    if not Config.COMMERCE_AUTOPAY_ENABLED:
        return False, ""

    state = session.get_autopay(chat_id)
    if not state["enabled"] or state["limit_rupees"] <= 0:
        return False, ""

    if total_minor is None:
        return False, (
            "\U0001F914 I could not read a total for this order, so I am asking "
            "even though auto-approve is on."
        )

    rupees = int(total_minor) // 100
    if rupees > state["limit_rupees"]:
        return False, (
            f"\u20b9{rupees:,} is over your \u20b9{state['limit_rupees']:,} "
            "auto-approve limit, so this one needs your tap."
        )

    left = session.autopay_remaining_today(chat_id, Config.COMMERCE_AUTOPAY_DAILY_RUPEES)
    if rupees > left:
        return False, (
            f"That would take today's auto-approved spend past \u20b9"
            f"{Config.COMMERCE_AUTOPAY_DAILY_RUPEES:,} (\u20b9{left:,} left), "
            "so this one needs your tap."
        )

    return True, (
        "Payment itself is not automated: UPI still opens your UPI app, and "
        "cash on delivery still means cash at the door."
    )


@authorized
async def cmd_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Capturing screenshot...")
    path, error = await screenshot.take_screenshot()
    if error:
        await msg.edit_text(f"Screenshot error: {error}")
        return
    try:
        await msg.delete()
    except Exception:
        pass
    with open(path, "rb") as f:
        await update.message.reply_photo(photo=f, caption="Screenshot")


@authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await system_info.get_status()
    await update.message.reply_text(status)


@authorized
async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    models = await system_info.get_models()
    await update.message.reply_text(models)


@authorized
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_path = Config.WORKSPACE / Config.LOG_FILE
    if not log_path.exists():
        await update.message.reply_text("No logs yet.")
        return
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        tail = content[-2000:] if len(content) > 2000 else content
        await update.message.reply_text(f"Recent Logs:\n\n{tail}")
    except Exception as e:
        await update.message.reply_text(f"Error reading logs: {e}")


@authorized
async def cmd_apply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply a pending LLM-suggested fix."""
    pending = context.chat_data.get("pending_fix")
    if not pending:
        await update.message.reply_text("No pending fix to apply.")
        return

    script = pending["script"]
    fix_code = pending["fix"]

    # Extract code from markdown code blocks if present
    code_blocks = re.findall(r"```(?:python)?\n?(.*?)```", fix_code, re.DOTALL)
    if code_blocks:
        fix_code = code_blocks[0].strip()

    approved = await guard.request_permission(
        context.bot, update.effective_chat.id,
        "apply_fix",
        f"Apply LLM fix to {script}",
        fix_code[:300],
    )
    if approved:
        result = file_ops.write_file(script, fix_code)
        await update.message.reply_text(f"{result}\n\nUse /run {script} to test.")
        context.chat_data.pop("pending_fix", None)


# ── Free text handler ─────────────────────────────────────────────

@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text

    # Both of the branches below belong to the developer surface, and both
    # swallow a plain message before it reaches the shopping agent -- the file
    # picker eats any bare number, which in a shopping chat is far more likely
    # to mean "2 packs" than "the second file". Their commands are unregistered
    # in shopping mode, so neither state can be set; skipping them outright
    # keeps that a design decision rather than a coincidence.
    if not Config.SHOPPING_ONLY:
        handled = await _handle_system_text_modes(update, context, text)
        if handled:
            return

    await _run_agent_turn(update, context, text)


async def _handle_system_text_modes(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> bool:
    """Consume free text owed to a pending /find, /send or /nb prompt."""
    # Check if user is selecting from a numbered file list
    pending = context.chat_data.get("pending_files")
    if pending and text.strip().isdigit():
        idx = int(text.strip()) - 1  # 1-based to 0-based
        if 0 <= idx < len(pending):
            filepath = pending[idx]
            context.chat_data.pop("pending_files", None)
            fpath, error = file_ops.get_file_for_send(filepath)
            if error:
                await update.message.reply_text(f"Can't send: {error}")
            else:
                with open(fpath, "rb") as f:
                    await update.message.reply_document(
                        document=f, filename=Path(fpath).name
                    )
            return True
        else:
            await update.message.reply_text(f"Pick a number between 1 and {len(pending)}.")
            return True

    # Clear pending selection on any non-number message
    context.chat_data.pop("pending_files", None)

    # Check if we're in notebook edit mode
    nb_edit_cell = context.chat_data.get("nb_edit_cell")
    if nb_edit_cell is not None:
        cell_idx = nb_edit_cell
        context.chat_data.pop("nb_edit_cell", None)
        result = notebook_mgr.edit_cell(cell_idx, text)
        await update.message.reply_text(result)
        return True

    return False


# What Mimir says to the commerce agent on the user's behalf after they tap
# Approve. Phrased as the explicit confirmation the provider's tool docs
# require ("wait for clear confirmation") rather than a bare "yes", so the
# model is in no doubt that the gate has been cleared.
_CONFIRM_TEXT = (
    "Yes, I confirm. Place the order now with the payment method we discussed. "
    "If a payment link or QR is returned, show it to me exactly as given."
)


async def _run_commerce_turn(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Hand the turn to the commerce agent. Returns True if it handled it.

    That agent drives the provider's raw MCP tools, so it can do the things the
    canonical wrapper cannot: offer a list of biscuits to choose from, suggest
    what goes into a samosa, read your usual items, track an order. Mimir stays
    responsible for the one thing the loop must not decide alone -- whether to
    spend money.
    """
    chat_id = update.effective_chat.id

    # Ask once, before the first real turn. "Order me something" means three
    # different things across groceries, restaurant delivery and a table
    # booking, and guessing produces a confidently wrong first answer -- the
    # worst kind, because the user has no reason yet to distrust it.
    if session.get_service(chat_id) is None:
        await update.message.reply_text(
            "\U0001F44B <b>First things first \u2014 what are we doing?</b>\n\n"
            "<i>I'll remember, and you can switch any time with /service.</i>",
            parse_mode="HTML",
            reply_markup=_service_keyboard(None),
        )
        return True

    await typing(update)
    thinking = await update.message.reply_text("\U0001F50E <i>Looking\u2026</i>", parse_mode="HTML")

    data = await commerce.assistant(chat_id, text)

    # The commerce service keeps sessions in memory, so a restart of it wipes
    # the selected provider and the connected set -- while the bot still has
    # both on disk. That asymmetry surfaced as "No provider selected. POST
    # /providers/{id}/select first." in the middle of a conversation, telling
    # someone who had already connected to go and connect.
    #
    # Restore from what we already know and retry once, silently. A user who
    # connected once should not have to do it again because a process on the
    # same machine bounced; that is our bookkeeping, not their problem.
    if _session_was_forgotten(data):
        restored = await _restore_provider(chat_id)
        if restored:
            logger.info(f"Commerce session lost; restored {restored} and retrying")
            data = await commerce.assistant(chat_id, text)

    if not data.get("ok"):
        try:
            await thinking.delete()
        except Exception:
            pass
        error = data.get("error", "")
        if _session_was_forgotten(data):
            # The silent restore above already tried and failed, so this is a
            # real problem rather than lost bookkeeping. Name the service the
            # user actually chose -- the old text always said "/connect
            # swiggy", which is wrong for Food or Dineout and reads as nonsense
            # to someone who just logged in successfully.
            selected = session.get_service(chat_id) or "swiggy"
            logger.error(f"Commerce session could not be restored: {error}")
            await update.message.reply_text(
                "\U0001F510 <b>I lost my connection to the store and couldn't "
                "pick it back up.</b>\n"
                "Your cart is safe.\n\n"
                f"Try <code>/connect {selected}</code>.",
                parse_mode="HTML",
            )
            return True

        # Everything else: say so, and STOP.
        #
        # This used to `return False`, handing the turn to Mimir's own tool loop
        # as a "graceful" fallback. It was the opposite of graceful. That loop
        # has no share of the commerce agent's conversation, so it re-ran
        # commerce_providers and commerce_search from scratch every turn and
        # asked "which product?" to a user who had just named one -- 225 times
        # in one session. When its own cascade then ran out (Groq is 8000
        # tokens/minute), it invented a cart, quoted "$1.99" and "Cart total:
        # $6.97" for rupee-priced Indian groceries, and when challenged
        # explained that dollars were "the store's default display" and would
        # "be converted at checkout". None of that was true.
        #
        # A shopping assistant that cannot reach the store must say so. Falling
        # back to a model that will confidently make up prices is worse than
        # returning nothing, because the user cannot tell the difference.
        logger.error(f"Commerce agent unavailable, not falling back: {error}")
        await update.message.reply_text(_friendly_outage(error), parse_mode="HTML")
        return True

    if not data.get("blocked_on_confirmation"):
        try:
            await thinking.delete()
        except Exception:
            pass
        await reply_smart(update.message, data.get("reply") or "No response.")
        return True

    # The loop wanted to place the order. Show the real cart and ask.
    try:
        await thinking.delete()
    except Exception:
        pass

    if blocked := await commerce.live_spending_blocked(chat_id):
        await update.message.reply_text(blocked)
        return True

    # A commitment with no cart behind it -- Dineout's book_table -- cannot go
    # through checkout_intent, which reads a cart that does not exist. Without
    # this branch the loop blocks the call and nothing can ever approve it: safe,
    # but the feature simply does not work.
    action = data.get("blocked_action") or {}
    if action.get("name") and action["name"] not in ("checkout", "place_food_order"):
        return await _confirm_action(update, context, action)

    intent = await commerce.checkout_intent(chat_id)
    if not intent.get("ok"):
        await update.message.reply_text(f"Cannot check out: {intent.get('error')}")
        return True

    if over := await commerce.over_spending_cap(intent.get("total_minor")):
        await update.message.reply_text(over)
        return True

    dry = "\n\n\u26A0\uFE0F <i>Dry run \u2014 nothing will actually be ordered.</i>" if intent.get("dry_run") else ""
    summary = intent.get("summary", "")
    # The provider's own docs require the delivery address be stated before
    # confirmation, and it is the detail a user is most likely to get wrong.
    details = f"{summary}{dry}"

    auto_ok, auto_note = _autopay_decision(chat_id, intent.get("total_minor"))
    if auto_ok:
        # Auto-approved. The user still gets the full itemised bill -- an order
        # that appears without any record of what was in it is indistinguishable
        # from a bug, and they need to be able to spot a wrong one immediately.
        await update.message.reply_text(
            f"\u26A1 <b>Auto-approved</b> \u2014 under your \u20b9"
            f"{session.get_autopay(chat_id)['limit_rupees']:,} limit.\n\n"
            f"{details}\n\n<i>{auto_note}</i>\n"
            "<code>/autopay off</code> to go back to approving each order.",
            parse_mode="HTML",
        )
        approved = True
    else:
        if auto_note:
            await update.message.reply_text(auto_note, parse_mode="HTML")
        approved = await guard.request_permission(
            context.bot, chat_id, "commerce_checkout",
            f"place order for {intent['total_display']}",
            details,
        )
    if not approved:
        await update.message.reply_text(
            "\U0001F44D No problem \u2014 nothing ordered, nothing charged."
        )
        return True

    if auto_ok:
        # Recorded only once the approval actually stands, so a refused or
        # failed order does not eat the day's allowance.
        session.record_autopay_spend(chat_id, (intent.get("total_minor") or 0) // 100)

    # Re-run the turn with the money path armed, so the SAME agent that was
    # blocked completes the order. Routing the approval into the canonical
    # checkout() instead would place the order through a stub that hardcodes
    # {paymentMethod: "UPI", generateUPIQR: true} and never calls
    # get_payment_options -- which Swiggy's own tool documentation says it
    # rejects, and which is why no payment link came back.
    placing = await update.message.reply_text("\U0001F6F5 Placing your order\u2026")
    data = await commerce.assistant_confirmed(chat_id, _CONFIRM_TEXT)
    try:
        await placing.delete()
    except Exception:
        pass

    if not data.get("ok"):
        await update.message.reply_text(f"Order failed: {data.get('error')}")
        return True
    await reply_smart(update.message, data.get("reply") or "Order submitted.")
    return True


# What a shopper should read when the assistant cannot reach the store.
#
# The full error goes to the log, never to the chat. Dumping
# "ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model
# is currently experiencing high demand...'}}" into a checkout is worse than
# useless: it is unreadable, it is frightening next to a payment step, and
# "model" is a word about our infrastructure that means nothing to someone
# buying biscuits. Surfacing the raw error was right for the LOGIN scripts,
# where the reader is the operator. Here the reader is a customer.
_OUTAGE_MESSAGES = (
    (("429", "resource_exhausted", "quota", "503", "unavailable",
      "high demand", "overloaded", "timeout", "deadline"),
     "\U0001F634 <b>I'm a bit overloaded right now.</b>\n"
     "Nothing was charged and your cart is safe.\n\n"
     "Give me a few seconds and send that again."),

    # "No provider selected" contains no "not"; matching only "not selected"
    # missed the exact string the service actually returns.
    (("provider selected", "not selected", "/select first",
      "not connected", "is not connected"),
     "\U0001F510 <b>I lost my connection to the store.</b>\n"
     "Your cart is safe.\n\n"
     "Send /service to pick it up again."),

    (("connect", "connection", "unreachable", "refused",
      "not running", "timed out", "non-json"),
     "\U0001F50C <b>I can't reach the store right now.</b>\n"
     "Nothing was charged and your cart is safe.\n\n"
     "Try again in a moment."),
)

_OUTAGE_DEFAULT = (
    "\U0001F6D2 <b>Something went wrong on my side.</b>\n"
    "Nothing was charged and your cart is safe.\n\n"
    "Try again in a moment \u2014 /cart shows what's really in there."
)


def _friendly_outage(error: str) -> str:
    """Translate a provider or model failure into something a shopper can act on.

    Always says the same two things first: nothing was charged, and the cart is
    intact. During a checkout those are the only facts the person actually
    needs, and an unexplained failure at the payment step otherwise reads as
    "did that go through?"
    """
    lowered = (error or "").lower()
    for markers, message in _OUTAGE_MESSAGES:
        if any(m in lowered for m in markers):
            return message
    return _OUTAGE_DEFAULT


# The commerce service's way of saying it has no memory of this session.
_FORGOTTEN_MARKERS = (
    "no provider selected",
    "not selected",
    "/select first",
    "is not connected",
    "not connected",
)


def _looks_forgotten_text(result: str) -> bool:
    """Same check, for helpers that return a rendered string rather than a dict."""
    return any(m in str(result).lower() for m in _FORGOTTEN_MARKERS)


def _session_was_forgotten(data: dict) -> bool:
    if data.get("ok"):
        return False
    return any(m in str(data.get("error", "")).lower() for m in _FORGOTTEN_MARKERS)


async def _restore_provider(chat_id: int) -> str | None:
    """Re-select and re-connect the service this chat already chose.

    Returns the provider id on success, None if there is nothing to restore or
    the reconnect failed. Connect is a cached-token read for an account that is
    already authorised, so this costs a round trip rather than a fresh login.
    """
    provider_id = session.get_service(chat_id)
    if not provider_id:
        return None
    try:
        await commerce.select(chat_id, provider_id)
        result = await commerce.connect(chat_id, provider_id)
    except Exception as exc:
        logger.warning(f"Could not restore commerce session: {exc}")
        return None
    if isinstance(result, str) and "error" in result.lower():
        logger.warning(f"Reconnect reported a problem: {result[:200]}")
        return None
    return provider_id


async def _confirm_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: dict) -> bool:
    """Approve one exact non-cart commitment, then let the agent complete it."""
    chat_id = update.effective_chat.id
    intent = await commerce.action_intent(chat_id, action["name"], action.get("args") or {})
    if not intent.get("ok"):
        await update.message.reply_text(
            f"I can't confirm that: {intent.get('error')}"
        )
        return True

    dry = (
        "\n\n\u26A0\uFE0F <i>Dry run \u2014 nothing will actually be booked.</i>"
        if intent.get("dry_run") else ""
    )
    # Deliberately NOT eligible for auto-approve. Auto-approve is bounded by a
    # rupee limit, and a free booking has no amount to measure -- every ceiling
    # that makes the feature safe reads zero here, so it would wave through an
    # unlimited number of reservations. A booking is also uncancellable through
    # the MCP, which is the opposite of the low-stakes case autopay is for.
    approved = await guard.request_permission(
        context.bot, chat_id, "commerce_booking",
        _booking_headline(action),
        f"{intent.get('summary', '')}{dry}",
    )
    if not approved:
        await update.message.reply_text(
            "\U0001F44D No problem \u2014 nothing booked."
        )
        return True

    placing = await update.message.reply_text("\U0001F37D <i>Booking\u2026</i>", parse_mode="HTML")
    result = await commerce.assistant_confirmed(chat_id, _CONFIRM_TEXT)
    try:
        await placing.delete()
    except Exception:
        pass

    if not result.get("ok"):
        await update.message.reply_text(f"Booking failed: {result.get('error')}")
        return True
    await reply_smart(update.message, result.get("reply") or "Booking submitted.")
    return True


def _booking_headline(action: dict) -> str:
    args = action.get("args") or {}
    if action["name"] == "book_table":
        who = args.get("partySize") or args.get("guests") or args.get("covers")
        when = args.get("slotTime") or args.get("time") or ""
        where = args.get("restaurantName") or args.get("restaurant") or "the restaurant"
        bits = [f"book a table at {where}"]
        if when:
            bits.append(f"at {when}")
        if who:
            bits.append(f"for {who}")
        return " ".join(bits)
    return f"run {action['name']}"


async def _run_agent_turn(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Drive one turn: the commerce agent first, then Mimir's own tool loop.

    Shared by the plain-message handler and /shop so both take exactly the same
    path -- including the tool set, the permission gate and the regex fallback.
    """
    if await _run_commerce_turn(update, context, text):
        return

    await typing(update)
    thinking = await update.message.reply_text("Working...")

    async def deliver(path: str):
        """Send a file the agent produced back to the user."""
        with open(path, "rb") as handle:
            await update.message.reply_document(
                document=handle, filename=Path(path).name,
            )

    try:
        answer = await agent.run(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            text=text,
            llm=llm,
            guard=guard,
            on_file=deliver,
        )
    except Exception as exc:
        # Every provider is down. Fall back to the regex classifier so basic
        # navigation still works with no LLM at all.
        logger.warning(f"Agent loop unavailable ({exc}); falling back to regex.")
        try:
            await thinking.delete()
        except Exception:
            pass
        await _handle_message_regex(update, context, text)
        return

    try:
        await thinking.delete()
    except Exception:
        pass
    await reply_smart(update.message, answer or "No response.")


async def _handle_message_regex(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Last-resort classifier, used only when every LLM provider is down."""
    intent, args = brain.classify(text)

    if intent == "multi_step":
        await _execute_task(context.bot, update.effective_chat.id, text, context)

    elif intent == "ask_llm":
        await typing(update)
        thinking = await update.message.reply_text("Processing...")
        response = await llm.ask(text, system_prompt=get_persona())
        try:
            await thinking.delete()
        except Exception:
            pass
        await reply_smart(update.message, response or "No response.")

    elif intent == "screenshot":
        await cmd_screenshot(update, context)
    elif intent == "status":
        await cmd_status(update, context)
    elif intent == "models":
        await cmd_models(update, context)
    elif intent == "logs":
        await cmd_logs(update, context)

    elif intent == "read_file":
        context.chat_data["last_file_path"] = args
        content = file_ops.read_file(args)
        await reply(update.message, f"{args}:\n\n{content}")

    elif intent == "list_dir":
        content = file_ops.list_dir(args)
        await reply(update.message, f"{args}:\n\n{content}")

    elif intent == "run_script":
        approved = await guard.request_permission(
            context.bot, update.effective_chat.id,
            "run_script",
            f"Run: {args}",
        )
        if approved:
            await monitor.run_and_report(context.bot, update.effective_chat.id, args)

    elif intent == "send_file":
        await _handle_send_file(update, args, context)

    elif intent in ("write_file", "delete_file"):
        cmd = intent.split("_")[0]
        await update.message.reply_text(
            f"Use /{cmd} command for this. Example:\n/{cmd} <path>"
        )

    elif intent == "fix_error":
        last_err = monitor.get_last_error(update.effective_chat.id)
        if last_err:
            thinking = await update.message.reply_text("Analyzing error with LLM...")
            prompt = (
                f"The following Python script failed:\n"
                f"Script: {last_err['script']}\n"
                f"Error:\n{last_err['error']}\n"
                f"Output:\n{last_err['stdout']}\n\n"
                f"Diagnose the problem and suggest a specific fix."
            )
            response = await llm.ask_code(prompt)
            try:
                await thinking.delete()
            except Exception:
                pass
            await reply_smart(update.message, f"**Diagnosis:**\n\n{response}")
        else:
            await update.message.reply_text("No recent errors to diagnose.")

    else:
        await typing(update)
        thinking = await update.message.reply_text("Processing...")
        response = await llm.ask(text, system_prompt=get_persona())
        try:
            await thinking.delete()
        except Exception:
            pass
        await reply_smart(update.message, response or "No response.")


# ── Voice handler (speak your order) ─────────────────────────────

@authorized
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transcribe a voice note and run it as if it had been typed.

    The transcript is echoed back before anything acts on it. Speech recognition
    is confident even when it is wrong, and on a shopping bot a misheard word is
    a wrong item in a real cart -- the user has to be able to see what Mimir
    thinks it heard, especially since Saaras auto-detects the language.

    Note what this function does NOT do: it never inspects the text for consent.
    A spoken "yes, order it" arrives here as ordinary text and goes to the same
    agent turn a typed message would, where the money gate still waits for a tap
    on an inline button. Approval is a callback_query -- a different Telegram
    update type that neither a model nor a transcriber can produce -- and that is
    precisely why a sentence cannot stand in for the tap.
    """
    note = update.message.voice
    if not note:
        return

    if not voice.available():
        await update.message.reply_text(
            "\U0001F399 I can't hear voice notes yet \u2014 no SARVAM_API_KEY is set.\n"
            "Type it instead and I'll get straight on it."
        )
        return

    if voice.too_long(note.duration):
        await update.message.reply_text(
            f"\U0001F399 That's {note.duration}s \u2014 I can only hear up to "
            f"{voice.MAX_SECONDS}s at a time.\n"
            "Send a shorter note, or type it."
        )
        return

    await typing(update)
    thinking = await update.message.reply_text(
        "\U0001F399 <i>Listening\u2026</i>", parse_mode="HTML"
    )

    try:
        tg_file = await context.bot.get_file(note.file_id)
        audio = bytes(await tg_file.download_as_bytearray())
    except Exception as exc:
        logger.exception("voice download failed")
        await thinking.edit_text(f"Couldn't fetch that voice note: {type(exc).__name__}")
        return

    result = await voice.transcribe(
        audio,
        filename="speech.ogg",
        content_type=note.mime_type or "audio/ogg",
    )

    if not result.ok:
        await thinking.edit_text(
            f"\U0001F399 I couldn't make that out \u2014 {result.error}.\n"
            "Try again, or type it."
        )
        return

    heard = f"\U0001F399 <i>Heard:</i> \u201c{html.escape(result.text)}\u201d"
    if result.language:
        heard += f"  <code>{html.escape(result.language)}</code>"
    await thinking.edit_text(heard, parse_mode="HTML")

    await _run_agent_turn(update, context, result.text)


# ── Document handler (receive files from user) ───────────────────

@authorized
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save files sent by user to workspace (with permission)."""
    doc = update.message.document
    if not doc:
        return

    filename = doc.file_name or "received_file"
    filepath = Config.WORKSPACE / filename

    approved = await guard.request_permission(
        context.bot, update.effective_chat.id,
        "save_file",
        f"Save received file: {filename}",
        f"Size: {doc.file_size} bytes -> {filepath}",
    )
    if approved:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(str(filepath))
        await update.message.reply_text(f"Saved to {filepath}")


# ── Callback handler (inline buttons) ────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    # Auth check
    if update.effective_chat.id != Config.TELEGRAM_CHAT_ID:
        return

    # Permission callbacks
    if query.data.startswith("perm:"):
        await guard.handle_callback(update, context)
        return

    # Service picker
    if query.data.startswith("svc:"):
        provider_id = query.data.split(":", 1)[1]
        if provider_id not in _SERVICE_BY_ID:
            await query.answer("Unknown service.")
            return
        await query.answer()
        await _apply_service(update.effective_chat.id, provider_id, context)
        _, icon, name, blurb = _SERVICE_BY_ID[provider_id]
        await query.edit_message_text(
            f"{icon} <b>{name}</b> \u2014 <i>{blurb}</i>\n\n"
            f"{_service_prompt(provider_id)}\n\n"
            "<i>Switch any time with /service.</i>",
            parse_mode="HTML",
        )
        return

    # Payment method picker
    if query.data.startswith("pay:"):
        method = query.data.split(":", 1)[1]
        if method not in {m for m, *_ in PAYMENT_CHOICES}:
            await query.answer("Unknown method.")
            return
        session.set_payment_pref(update.effective_chat.id, method)
        await query.answer("Saved.")
        await query.edit_message_text(
            _payment_summary(update.effective_chat.id), parse_mode="HTML"
        )
        return

    # Auto-approve picker
    if query.data.startswith("autopay:"):
        if not Config.COMMERCE_AUTOPAY_ENABLED:
            await query.answer("Auto-approve is disabled on this machine.", show_alert=True)
            return
        try:
            amount = int(query.data.split(":", 1)[1])
        except ValueError:
            await query.answer("Bad amount.")
            return
        chat_id = update.effective_chat.id
        amount = min(amount, Config.COMMERCE_AUTOPAY_MAX_RUPEES)
        session.set_autopay(chat_id, enabled=amount > 0, limit_rupees=amount)
        await query.answer("Saved.")
        await query.edit_message_text(_autopay_summary(chat_id), parse_mode="HTML")
        return

    # Fix callbacks
    if query.data.startswith("fix:"):
        parts = query.data.split(":")
        fix_type = parts[1] if len(parts) > 1 else ""
        chat_id = int(parts[2]) if len(parts) > 2 else update.effective_chat.id

        if fix_type == "llm":
            await query.answer("Analyzing with LLM...")
            await query.edit_message_reply_markup(None)

            last_err = monitor.get_last_error(chat_id)
            if not last_err:
                await context.bot.send_message(chat_id, "No recent error found.")
                return

            msg = await context.bot.send_message(chat_id, "LLM is analyzing the error...")

            script_content = file_ops.read_file(last_err["script"], max_chars=5000)
            prompt = (
                f"Fix this Python script error.\n\n"
                f"Script ({last_err['script']}):\n{script_content}\n\n"
                f"Error:\n{last_err['error']}\n\n"
                f"Output:\n{last_err['stdout']}\n\n"
                f"Provide the corrected full script code. Wrap it in ```python``` markers."
            )
            fix = await llm.ask_code(prompt)

            try:
                await msg.edit_text(
                    f"Suggested fix for {last_err['script']}:\n\n{fix}\n\n"
                    f"Send /apply to apply this fix."
                )
            except Exception:
                await context.bot.send_message(
                    chat_id,
                    f"Suggested fix for {last_err['script']}:\n\n{fix}\n\n"
                    f"Send /apply to apply this fix.",
                )

            context.chat_data["pending_fix"] = {
                "script": last_err["script"],
                "fix": fix,
            }
            monitor.clear_error(chat_id)

        elif fix_type == "manual":
            await query.answer("OK, waiting for your fix.")
            await query.edit_message_reply_markup(None)
            await context.bot.send_message(
                chat_id,
                "Send me the fix as text, or use /write to update the file directly.",
            )


# ── Bot initialization ────────────────────────────────────────────

# The shopping surface: what Mimir is for, plus the few meta commands a
# Telegram bot needs to be operable at all. /status earns its place the same
# way the status *tool* does in tools_registry._SHOPPING_EXTRAS -- "are you
# alive?" is a fair question mid-order -- and every persona in PERSONAS is a
# shopping persona, so /persona is a tone dial on this surface, not a foreign
# feature.
_COMMERCE_COMMANDS = [
    BotCommand("shop", "Shop in plain English"),
    BotCommand("search", "Search the store"),
    BotCommand("cart", "Show your cart"),
    BotCommand("service", "Groceries, food or a table"),
    BotCommand("autopay", "Auto-approve small orders"),
    BotCommand("pay", "Cash or UPI"),
    BotCommand("providers", "Which stores are set up"),
    BotCommand("connect", "Log in to a store"),
    BotCommand("start", "Start over"),
    BotCommand("help", "What I can do"),
    BotCommand("status", "Am I working?"),
    BotCommand("persona", "Switch tone"),
    BotCommand("stop", "Shut me down"),
]

# Developer tooling. Registered only under BOT_MODE=full -- see Config.BOT_MODE
# for why these are gated rather than deleted.
_SYSTEM_COMMANDS = [
    BotCommand("ask", "Ask LLM"),
    BotCommand("code", "Ask code model"),
    BotCommand("run", "Run a Python script"),
    BotCommand("read", "Read a file"),
    BotCommand("write", "Write to a file"),
    BotCommand("delete", "Delete a file"),
    BotCommand("send", "Send a file to you"),
    BotCommand("ls", "List directory"),
    BotCommand("find", "Search for files"),
    BotCommand("tree", "Show directory tree"),
    BotCommand("recent", "Recently modified files"),
    BotCommand("watch", "Run script with live output"),
    BotCommand("cmd", "Run shell command"),
    BotCommand("tail", "Monitor a log file"),
    BotCommand("procs", "Show background processes"),
    BotCommand("kill", "Kill background process"),
    BotCommand("history", "Command history"),
    BotCommand("nb", "Notebook commands"),
    BotCommand("commit", "Git commit"),
    BotCommand("diff", "Show git diff"),
    BotCommand("gitlog", "Recent git commits"),
    BotCommand("undo", "Undo last commit"),
    BotCommand("screenshot", "Capture screen"),
    BotCommand("models", "List Ollama models"),
    BotCommand("logs", "View agent logs"),
    BotCommand("apply", "Apply LLM-suggested fix"),
    BotCommand("do", "Execute a complex task"),
    BotCommand("open", f"Reveal in {host.FILE_MANAGER}"),
    BotCommand("cd", "Change current folder"),
    BotCommand("pwd", "Show current folder"),
    BotCommand("clip", "Copy to clipboard"),
    BotCommand("bookmark", "Save a bookmark"),
    BotCommand("go", "Go to bookmark"),
    BotCommand("gpu", "GPU status"),
    BotCommand("livestream", "Stream desktop to Telegram"),
    BotCommand("remote", "Remote desktop access link"),
    BotCommand("remind", "Set a reminder"),
]


async def post_init(application: Application):
    """Register the command menu and send the startup notification."""
    commands = list(_COMMERCE_COMMANDS)
    if not Config.SHOPPING_ONLY:
        commands += _SYSTEM_COMMANDS
    await application.bot.set_my_commands(commands)

    # ── Startup Dashboard ──
    try:
        import shutil
        import datetime

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        health = await llm.health()

        # Was: "ON"/"OFF". A rate-limited provider is not off, and a banner
        # saying so had the reader hunting for a broken key when the fix was to
        # wait a minute -- and it is a boot snapshot that never refreshes, so
        # that wrong verdict sits there all session.
        _LABELS = {
            "ok": "ON", "limited": "LIMITED", "bad_key": "BAD KEY",
            "no_key": "no key", "off": "OFF",
        }

        def _dot(state): return _LABELS.get(state, str(state))

        try:
            disk = shutil.disk_usage(str(Config.WORKSPACE))
            disk_free = f"{disk.free // (1024**3)}GB free"
        except Exception:
            disk_free = "N/A"

        # GPU check
        gpu_line = "N/A"
        try:
            gpu_line = await host.gpu_summary() or "N/A"
        except Exception:
            pass

        dashboard = (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "       M I M I R  v2.0\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "\n"
            f"  Time:    {now}\n"
            f"  Groq:    {_dot(health.get('Groq'))}\n"
            f"  Gemini:  {_dot(health.get('Gemini'))}\n"
            f"  Ollama:  {_dot(health.get('Ollama'))}  (optional)\n"
            f"  Disk:    {disk_free}\n"
            f"  GPU:     {gpu_line}\n"
            "\n"
            "  All systems online, sir.\n"
            "  Send /help for commands.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        await application.bot.send_message(Config.TELEGRAM_CHAT_ID, dashboard)
    except Exception as e:
        logger.error(f"Failed to send startup dashboard: {e}")
        try:
            await application.bot.send_message(
                Config.TELEGRAM_CHAT_ID,
                "MIMIR online. Send /help for commands.",
            )
        except Exception:
            pass

    logger.info("Bot initialized and commands registered.")


def create_bot() -> Application:
    """Create and configure the Telegram bot."""
    # MIMIR starts on a cold WSL boot, where the NAT interface often needs a few
    # seconds before it can carry traffic. PTB's 5s default connect timeout is
    # short enough that the very first getMe() call can time out and — combined
    # with bootstrap_retries=0 in main.py — kill the process outright.
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=10.0,
    )
    # Long polling holds the connection open, so read_timeout must exceed it.
    get_updates_request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=40.0,
        write_timeout=30.0,
        pool_timeout=10.0,
    )

    app = (
        Application.builder()
        .token(Config.TELEGRAM_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    # Command handlers — shopping
    app.add_handler(CommandHandler("shop", cmd_shop))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("cart", cmd_cart))
    app.add_handler(CommandHandler("providers", cmd_providers))
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CommandHandler("service", cmd_service))
    app.add_handler(CommandHandler("autopay", cmd_autopay))
    app.add_handler(CommandHandler("pay", cmd_pay))

    # Command handlers — meta
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("persona", cmd_persona))
    app.add_handler(CommandHandler("stop", cmd_stop))

    if not Config.SHOPPING_ONLY:
        # Command handlers — LLM
        app.add_handler(CommandHandler("ask", cmd_ask))
        app.add_handler(CommandHandler("code", cmd_code))
        app.add_handler(CommandHandler("do", cmd_do))
        app.add_handler(CommandHandler("apply", cmd_apply))

        # Command handlers — files
        app.add_handler(CommandHandler("read", cmd_read))
        app.add_handler(CommandHandler("ls", cmd_ls))
        app.add_handler(CommandHandler("write", cmd_write))
        app.add_handler(CommandHandler("delete", cmd_delete))
        app.add_handler(CommandHandler("send", cmd_send))

        # Command handlers — file discovery
        app.add_handler(CommandHandler("find", cmd_find))
        app.add_handler(CommandHandler("tree", cmd_tree))
        app.add_handler(CommandHandler("recent", cmd_recent))

        # Command handlers — process management
        app.add_handler(CommandHandler("run", cmd_run))
        app.add_handler(CommandHandler("watch", cmd_watch))
        app.add_handler(CommandHandler("tail", cmd_tail))
        app.add_handler(CommandHandler("procs", cmd_procs))
        app.add_handler(CommandHandler("kill", cmd_kill))
        app.add_handler(CommandHandler("cmd", cmd_cmd))
        app.add_handler(CommandHandler("history", cmd_history))

        # Command handlers — notebook
        app.add_handler(CommandHandler("nb", cmd_nb))

        # Command handlers — git
        app.add_handler(CommandHandler("commit", cmd_commit))
        app.add_handler(CommandHandler("diff", cmd_diff))
        app.add_handler(CommandHandler("gitlog", cmd_gitlog))
        app.add_handler(CommandHandler("undo", cmd_undo))

        # Command handlers — host
        app.add_handler(CommandHandler("screenshot", cmd_screenshot))
        app.add_handler(CommandHandler("models", cmd_models))
        app.add_handler(CommandHandler("logs", cmd_logs))
        app.add_handler(CommandHandler("gpu", cmd_gpu))
        app.add_handler(CommandHandler("livestream", cmd_livestream))
        app.add_handler(CommandHandler("remote", cmd_remote))

        # Command handlers — quality of life
        app.add_handler(CommandHandler("open", cmd_open))
        app.add_handler(CommandHandler("cd", cmd_cd))
        app.add_handler(CommandHandler("pwd", cmd_pwd))
        app.add_handler(CommandHandler("clip", cmd_clip))
        app.add_handler(CommandHandler("bookmark", cmd_bookmark))
        app.add_handler(CommandHandler("go", cmd_go))
        app.add_handler(CommandHandler("remind", cmd_remind))

    # Inline button callback handler. Registered in both modes: the Approve /
    # Cancel tap on an order is a callback_query, and it is the one thing on
    # the money path that must always be able to land.
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Free text messages (must be after command handlers)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Voice notes. Registered in both modes: speaking your order is part of the
    # shopping surface, and the transcript re-enters through handle_message's
    # own entry point, so it inherits whatever tools that mode allows.
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # File/document receiver. Saving whatever arrives into the workspace is a
    # filesystem write with no part in shopping, so it goes with the rest of
    # the developer surface. Receipts still travel outward via the send_file
    # tool, which stays in _SHOPPING_EXTRAS.
    if not Config.SHOPPING_ONLY:
        app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    return app
