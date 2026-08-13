"""Per-chat conversation state, persisted across restarts.

Currently just the working directory, but this is the seam for anything else
that needs to survive `/stop` or a systemd restart.
"""

import json
import logging
import os

from config import Config

logger = logging.getLogger(__name__)

SESSION_FILE = Config.WORKSPACE / ".sessions.json"

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        loaded = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("session file is not an object")
        _cache = loaded
    except FileNotFoundError:
        _cache = {}
    except Exception as exc:
        # A corrupt state file must never stop the bot from starting.
        logger.warning(f"Session file unreadable ({exc}); starting fresh.")
        _cache = {}
    return _cache


def _save() -> None:
    data = _load()
    tmp = SESSION_FILE.with_suffix(".tmp")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, SESSION_FILE)  # atomic; no torn file on crash
    except Exception as exc:
        logger.error(f"Failed to persist session state: {exc}")
        try:
            tmp.unlink()
        except OSError:
            pass


def get_cwd(chat_id: int) -> str:
    entry = _load().get(str(chat_id))
    if isinstance(entry, dict) and isinstance(entry.get("cwd"), str):
        return entry["cwd"]
    return str(Config.WORKSPACE)


def set_cwd(chat_id: int, path: str) -> None:
    data = _load()
    data.setdefault(str(chat_id), {})["cwd"] = path
    _save()


def reset(chat_id: int) -> None:
    _load().pop(str(chat_id), None)
    _save()
