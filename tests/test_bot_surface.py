"""The Telegram menu must describe exactly what the bot will answer.

Before BOT_MODE gated registration, all 47 commands were registered and all 47
were in the menu regardless of mode -- a shopping assistant advertising /commit
and /screenshot. The failure mode this file guards is subtler than that: a menu
and a handler list that drift apart. An entry in the menu with no handler is a
command Telegram offers and the bot ignores; a handler with no menu entry is a
capability that survives a mode switch unadvertised. Both are the menu lying.

create_bot() is called for real here rather than stubbed: registration is the
behaviour under test, so a fake Application would test nothing.
"""

import importlib
import sys

import pytest

_PREFIXES = ("config", "core", "tools")


def _purge():
    for name in [m for m in sys.modules if m.startswith(_PREFIXES)]:
        del sys.modules[name]


def _reload(monkeypatch, mode):
    """Re-import the bot under a given BOT_MODE.

    Config reads the env once at class-definition time and both the menu and
    the handler wiring branch on it, so the modules have to come back fresh.
    """
    monkeypatch.setenv("BOT_MODE", mode)
    _purge()
    import config  # noqa: F401
    return importlib.import_module("core.bot"), importlib.import_module("core.tools_registry")


def _registered(app):
    return {
        cmd
        for group in app.handlers.values()
        for h in group
        if getattr(h, "commands", None)
        for cmd in h.commands
    }


def _menu(bot, mode):
    commands = list(bot._COMMERCE_COMMANDS)
    if mode == "full":
        commands += bot._SYSTEM_COMMANDS
    return {c.command for c in commands}


@pytest.fixture(autouse=True)
def _restore():
    """Put the original module objects back, not merely clear the cache.

    Other test modules bind `Config`, `session` and friends at import time and
    then monkeypatch those exact objects. Leaving a purged sys.modules behind
    makes the code under test import a *second* copy, so those patches land on
    a class nobody reads -- which failed seven unrelated tests, none of them
    about the bot surface, the first time this file existed.
    """
    saved = {m: sys.modules[m] for m in list(sys.modules) if m.startswith(_PREFIXES)}
    try:
        yield
    finally:
        _purge()
        sys.modules.update(saved)


@pytest.mark.parametrize("mode", ["shopping", "full"])
def test_menu_matches_registered_handlers(monkeypatch, mode):
    bot, _ = _reload(monkeypatch, mode)
    app = bot.create_bot()

    menu, registered = _menu(bot, mode), _registered(app)

    assert not menu - registered, f"in the menu but unregistered: {sorted(menu - registered)}"
    assert not registered - menu, f"registered but hidden from the menu: {sorted(registered - menu)}"


def test_shopping_mode_drops_the_developer_commands(monkeypatch):
    bot, _ = _reload(monkeypatch, "shopping")
    registered = _registered(bot.create_bot())

    assert registered == {
        "shop", "search", "cart", "providers", "connect", "service", "autopay",
        "pay", "start", "help", "status", "persona", "stop",
    }
    # A representative from each developer group: shell, files, git, host.
    for cmd in ("cmd", "write", "delete", "commit", "screenshot", "nb", "run"):
        assert cmd not in registered


def test_full_mode_restores_everything(monkeypatch):
    bot, _ = _reload(monkeypatch, "full")
    registered = _registered(bot.create_bot())

    assert len(registered) == 50
    for cmd in ("cmd", "write", "commit", "screenshot", "nb", "run", "shop", "connect"):
        assert cmd in registered


def test_approval_button_survives_both_modes(monkeypatch):
    """The Approve/Cancel tap is a callback_query, not a message.

    It is the only thing standing between the commerce agent and real money, so
    it must land in either mode. Gating it would strand a pending order.
    """
    from telegram.ext import CallbackQueryHandler

    for mode in ("shopping", "full"):
        bot, _ = _reload(monkeypatch, mode)
        handlers = [h for g in bot.create_bot().handlers.values() for h in g]
        assert any(isinstance(h, CallbackQueryHandler) for h in handlers), mode


def test_shopping_mode_hides_the_document_receiver(monkeypatch):
    """Saving an arbitrary upload into the workspace is a filesystem write."""
    from telegram.ext import MessageHandler

    def doc_handlers(bot):
        return [
            h for g in bot.create_bot().handlers.values() for h in g
            if isinstance(h, MessageHandler) and "Document" in str(h.filters)
        ]

    bot, _ = _reload(monkeypatch, "shopping")
    assert not doc_handlers(bot)

    bot, _ = _reload(monkeypatch, "full")
    assert doc_handlers(bot)


def test_one_switch_drives_menu_and_tool_set(monkeypatch):
    """Config.SHOPPING_ONLY is the single source; nothing reads BOT_MODE twice."""
    bot, registry = _reload(monkeypatch, "shopping")
    from config import Config

    assert Config.SHOPPING_ONLY is True
    tools = {s["function"]["name"] for s in registry.TOOL_SCHEMAS}
    assert not {t for t in tools if t.startswith(("shell", "write", "delete", "run_"))}

    bot, registry = _reload(monkeypatch, "full")
    from config import Config as FullConfig

    assert FullConfig.SHOPPING_ONLY is False
    tools = {s["function"]["name"] for s in registry.TOOL_SCHEMAS}
    assert "shell_command" in tools and "commerce_cart" in tools
