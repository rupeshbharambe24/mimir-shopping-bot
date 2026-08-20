import asyncio
import logging
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import Config

logger = logging.getLogger(__name__)


class Guard:
    """Permission gate: read ops auto-allowed, write/delete ops require Telegram approval."""

    READ_OPS = frozenset({
        "read_file", "list_dir", "status", "logs",
        "screenshot", "ask_llm", "models",
    })
    WRITE_OPS = frozenset({
        "write_file", "delete_file", "run_script",
        "apply_fix", "install_pkg", "save_file",
        # Spends real money. Listed here so needs_permission() returns True --
        # a tool absent from this set is auto-approved without any prompt.
        "commerce_checkout",
    })

    def __init__(self):
        self._pending: dict[str, asyncio.Future] = {}
        self._descriptions: dict[str, str] = {}

    def needs_permission(self, operation: str) -> bool:
        return operation in self.WRITE_OPS

    async def request_permission(
        self, bot, chat_id: int, operation: str, description: str, details: str = ""
    ) -> bool:
        if not self.needs_permission(operation):
            return True

        action_id = uuid.uuid4().hex[:8]
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[action_id] = future
        self._descriptions[action_id] = description

        # Money asks get a different frame from ordinary write asks: the user is
        # being shown a basket and a number, not a filesystem action.
        is_purchase = operation == "commerce_checkout"
        if is_purchase:
            text = "\U0001F512 <b>Confirm your order</b>\n\n"
            if details:
                text += f"{details[:900]}\n\n"
            text += (
                "<i>Tap Place order and I'll go ahead.\n"
                "Nothing is charged until you do.</i>"
            )
            yes_label, no_label = "\u2705 Place order", "\u2716 Cancel"
        else:
            text = f"\u26A0\uFE0F <b>Permission needed</b>\n\n{description}"
            if details:
                text += f"\n\n<code>{details[:500]}</code>"
            yes_label, no_label = "\u2705 Allow", "\u2716 Deny"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(yes_label, callback_data=f"perm:y:{action_id}"),
            InlineKeyboardButton(no_label, callback_data=f"perm:n:{action_id}"),
        ]])

        await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML")
        logger.info(f"Permission requested: {description} [{action_id}]")

        try:
            result = await asyncio.wait_for(future, timeout=Config.PERMISSION_TIMEOUT)
            return result
        except asyncio.TimeoutError:
            self._cleanup(action_id)
            await bot.send_message(
                chat_id,
                "\u23F1 That request timed out, so I left it alone.\n"
                "<i>Ask again whenever you're ready.</i>",
                parse_mode="HTML",
            )
            logger.warning(f"Permission timeout: {description} [{action_id}]")
            return False

    async def handle_callback(self, update, context):
        query = update.callback_query
        if not query.data or not query.data.startswith("perm:"):
            return

        parts = query.data.split(":", 2)
        if len(parts) != 3:
            return

        _, decision, action_id = parts

        if action_id not in self._pending:
            try:
                await query.answer("This request has expired.")
            except Exception:
                pass
            return

        approved = decision == "y"
        future = self._pending.pop(action_id)
        desc = self._descriptions.pop(action_id, "Unknown action")

        if not future.done():
            future.set_result(approved)

        status = "APPROVED" if approved else "DENIED"
        try:
            await query.answer("Done" if approved else "Cancelled")
            mark = "\u2705" if approved else "\u2716"
            verb = "Confirmed" if approved else "Cancelled"
            await query.edit_message_text(f"{mark} <b>{verb}</b>\n<i>{desc}</i>",
                                          parse_mode="HTML")
        except Exception:
            pass
        logger.info(f"Permission {status.lower()}: {desc} [{action_id}]")

    def _cleanup(self, action_id: str):
        self._pending.pop(action_id, None)
        self._descriptions.pop(action_id, None)
