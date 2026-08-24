#!/usr/bin/env python3
"""AI Agent - Telegram-controlled local LLM agent with permission system."""

import logging
import sys
from pathlib import Path

# --- interpreter check, before anything imports telegram --------------------
#
# Mimir and the commerce sidecar have SEPARATE virtualenvs on purpose: the
# sidecar pins mcp==2.0.0 exactly, and merging those pins into the bot would let
# a provider-SDK bump take the bot down. Nothing enforced that, so activating
# the sidecar's venv and running `python3 main.py` produced a bare
# ModuleNotFoundError naming `telegram` -- which reads as a missing dependency
# rather than the wrong interpreter.
#
# The obvious next step makes it worse. `pip install telegram` installs an
# unrelated 879-byte package that squats the name on PyPI; the real one is
# `python-telegram-bot`. The squatter imports fine and then fails with
# "cannot import name 'Update' from 'telegram'", which looks like a version
# problem and is not one.
def _check_interpreter() -> None:
    try:
        import telegram
    except ImportError:
        _wrong_venv("python-telegram-bot is not installed in this interpreter")
        return
    if not hasattr(telegram, "Update"):
        _wrong_venv(
            f"the installed 'telegram' package is not python-telegram-bot\n"
            f"     (found {getattr(telegram, '__file__', '?')})"
        )


def _wrong_venv(reason: str) -> None:
    venv = Path.home() / ".venvs" / "ai-agent"
    here = Path(__file__).resolve().parent
    in_sidecar = "agentic-commerce" in sys.prefix

    print(f"\n[!!] Cannot start: {reason}.\n", file=sys.stderr)
    if in_sidecar:
        print(
            "     You are in the agentic-commerce venv. That one is for the\n"
            "     commerce sidecar and deliberately has no telegram library --\n"
            "     the two are kept separate so a provider-SDK bump cannot take\n"
            "     the bot down.\n",
            file=sys.stderr,
        )
    print(
        f"     Run the bot with its own interpreter instead:\n\n"
        f"       {venv}/bin/python {here}/main.py\n\n"
        f"     or just:  ./start-mimir.sh\n\n"
        f"     If that venv does not exist yet, run ./setup.sh once.\n"
        f"     Do NOT `pip install telegram` -- that is a different package.\n"
        f"     The real one is python-telegram-bot.\n",
        file=sys.stderr,
    )
    raise SystemExit(1)


_check_interpreter()

from config import Config  # noqa: E402
from core.bot import create_bot  # noqa: E402

# Ensure log directory exists
log_dir = (Config.WORKSPACE / Config.LOG_FILE).parent
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(str(Config.WORKSPACE / Config.LOG_FILE)),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def main():
    logger.info("Starting AI Agent...")
    logger.info(f"Ollama URL: {Config.OLLAMA_URL}")
    logger.info(f"Workspace: {Config.WORKSPACE}")
    logger.info(f"Models: {Config.DEFAULT_MODEL}, {Config.CODE_MODEL}")

    bot = create_bot()
    logger.info("Bot created. Starting Telegram polling...")
    # bootstrap_retries=-1 means retry forever. The default of 0 makes a single
    # transient connect timeout on the first getMe() call abort startup, which
    # is exactly what happens when WSL's network isn't up yet at cold boot.
    bot.run_polling(drop_pending_updates=True, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
