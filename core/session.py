"""Per-chat conversation state, persisted across restarts.

The working directory, the chosen Swiggy service, and the auto-approve setting
-- anything that must survive `/stop` or a systemd restart.
"""

import datetime
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


def _get(chat_id: int, key: str, default=None):
    entry = _load().get(str(chat_id))
    return entry.get(key, default) if isinstance(entry, dict) else default


def _set(chat_id: int, key: str, value) -> None:
    _load().setdefault(str(chat_id), {})[key] = value
    _save()


# ── Which Swiggy service the user is shopping ────────────────────

def get_service(chat_id: int) -> str | None:
    """The provider id this chat is using, or None if never chosen.

    None is meaningful, not a missing default. Guessing that someone wants
    groceries when they might want a table booked produces a confidently wrong
    first answer, so the bot asks once instead.
    """
    value = _get(chat_id, "service")
    return value if isinstance(value, str) and value else None


def set_service(chat_id: int, provider_id: str) -> None:
    _set(chat_id, "service", provider_id)


# ── Preferred payment method ─────────────────────────────────────
#
# Swiggy's checkout takes paymentMethod ∈ {"UPI", "Cash"} and, for UPI, an
# intentApp id from get_payment_options. Remembering the choice removes the
# per-order question; it does NOT remove the payment step. For UPI the human
# still authorises in their UPI app, because RBI's Authentication Mechanisms
# Directions require two factors with at least one dynamic per transaction, and
# a server holding a token is neither.

def get_payment_pref(chat_id: int) -> dict:
    raw = _get(chat_id, "payment")
    if not isinstance(raw, dict):
        return {"method": "", "intent_app": ""}
    return {
        "method": str(raw.get("method") or ""),
        "intent_app": str(raw.get("intent_app") or ""),
    }


def set_payment_pref(chat_id: int, method: str, intent_app: str = "") -> None:
    _set(chat_id, "payment", {"method": method, "intent_app": intent_app})


# ── Auto-approve ─────────────────────────────────────────────────
#
# Off unless the user turns it on, and bounded on two axes even then: a
# per-order ceiling and a rolling daily total. One ceiling is not enough -- a
# ₹400 limit still permits an unbounded number of ₹400 orders, and the failure
# mode of an agent looping on checkout is precisely many small orders rather
# than one large one.

def get_autopay(chat_id: int) -> dict:
    raw = _get(chat_id, "autopay")
    if not isinstance(raw, dict):
        return {"enabled": False, "limit_rupees": 0, "spent_today": 0, "day": ""}
    return {
        "enabled": bool(raw.get("enabled")),
        "limit_rupees": int(raw.get("limit_rupees") or 0),
        "spent_today": int(raw.get("spent_today") or 0),
        "day": str(raw.get("day") or ""),
    }


def set_autopay(chat_id: int, *, enabled: bool, limit_rupees: int) -> None:
    current = get_autopay(chat_id)
    _set(chat_id, "autopay", {
        "enabled": enabled,
        "limit_rupees": max(0, int(limit_rupees)),
        # Turning it off and on again does NOT reset the day's running total.
        # Otherwise the daily cap is bypassed by toggling the switch, which is
        # the first thing an agent stuck in a retry loop would effectively do.
        "spent_today": current["spent_today"],
        "day": current["day"],
    })


def _today() -> str:
    return datetime.date.today().isoformat()


def autopay_remaining_today(chat_id: int, daily_cap_rupees: int) -> int:
    """Rupees still available under today's cap. Rolls over at local midnight."""
    state = get_autopay(chat_id)
    spent = state["spent_today"] if state["day"] == _today() else 0
    return max(0, int(daily_cap_rupees) - spent)


def record_autopay_spend(chat_id: int, rupees: int) -> None:
    state = get_autopay(chat_id)
    spent = state["spent_today"] if state["day"] == _today() else 0
    _set(chat_id, "autopay", {
        "enabled": state["enabled"],
        "limit_rupees": state["limit_rupees"],
        "spent_today": spent + max(0, int(rupees)),
        "day": _today(),
    })


def reset(chat_id: int) -> None:
    _load().pop(str(chat_id), None)
    _save()
