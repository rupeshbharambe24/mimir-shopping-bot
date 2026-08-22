"""Voice notes.

The assertion that matters most is the last one: a transcript is ordinary user
text. It re-enters through the same agent turn a typed message uses, and the
money gate still waits for a tap on an inline button. A spoken "yes, order it"
authorises nothing, because approval is a callback_query -- an update type no
transcriber can produce.

The rest guard the failure modes that would be worst on a bot holding a real
cart: inventing words out of noise, and uploading audio the API will refuse.
"""

import pytest

from config import Config
from tools import voice


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(Config, "SARVAM_API_KEY", "test-key")
    monkeypatch.setattr(Config, "SARVAM_MODEL", "saaras:v3")
    monkeypatch.setattr(Config, "SARVAM_LANGUAGE", "unknown")


# ── refusals that cost nothing ───────────────────────────────────────

async def test_no_key_reports_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(Config, "SARVAM_API_KEY", "")
    out = await voice.transcribe(b"audio")
    assert not out.ok
    assert "SARVAM_API_KEY" in out.error
    assert out.text == ""  # never invents an order from nothing


async def test_empty_audio_is_refused(key):
    out = await voice.transcribe(b"")
    assert not out.ok and "no audio" in out.error


def test_availability_follows_the_key(monkeypatch):
    monkeypatch.setattr(Config, "SARVAM_API_KEY", "")
    assert voice.available() is False
    monkeypatch.setattr(Config, "SARVAM_API_KEY", "k")
    assert voice.available() is True


@pytest.mark.parametrize(
    "duration,expected",
    [(None, False), (0, False), (5, False), (30, False), (31, True), (600, True)],
)
def test_long_audio_is_refused_before_the_upload(duration, expected):
    """Sarvam's sync endpoint takes 30s. Telegram gives us the duration up
    front, so a 10-minute note should never cost a round trip."""
    assert voice.too_long(duration) is expected


# ── transport failures come back as text, never as exceptions ────────

class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """Stands in for aiohttp.ClientSession. `post` may raise, to prove the
    caller still gets a Transcript rather than an exception."""

    def __init__(self, resp=None, raises=None):
        self._resp, self._raises = resp, raises

    def post(self, *a, **kw):
        if self._raises:
            raise self._raises
        return self._resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _patch_session(monkeypatch, session):
    monkeypatch.setattr(voice.aiohttp, "ClientSession", lambda *a, **kw: session)


@pytest.mark.parametrize(
    "status,body,fragment",
    [
        (401, "nope", "rejected the API key"),
        (403, "nope", "rejected the API key"),
        (429, "slow down", "rate limited"),
        (500, "boom", "HTTP 500"),
        (200, "not json at all", "non-JSON"),
        (200, '{"transcript": "   "}', "empty transcript"),
    ],
)
async def test_api_failures_are_reported_not_raised(monkeypatch, key, status, body, fragment):
    _patch_session(monkeypatch, _Session(resp=_Resp(status, body)))
    out = await voice.transcribe(b"audio")
    assert not out.ok
    assert fragment in out.error
    assert out.text == ""


async def test_transport_error_is_reported_not_raised(monkeypatch, key):
    _patch_session(monkeypatch, _Session(raises=voice.aiohttp.ClientError("down")))
    out = await voice.transcribe(b"audio")
    assert not out.ok and "transport error" in out.error


async def test_a_good_response_yields_text_and_language(monkeypatch, key):
    body = '{"transcript": "do we have milk", "language_code": "en-IN"}'
    _patch_session(monkeypatch, _Session(resp=_Resp(200, body)))
    out = await voice.transcribe(b"audio")
    assert out.ok
    assert out.text == "do we have milk"
    assert out.language == "en-IN"


async def test_devanagari_survives_intact(monkeypatch, key):
    """A transcript is passed through, never normalised.

    Python's \\w excludes Unicode combining marks, so the obvious punctuation
    filter [^\\w\\s-] silently strips every matra out of Devanagari -- turning
    "मुझे प्रोटीन" into "म झ प र ट न". The safe move is not to filter at all:
    the model reads the sentence better than a regex can clean it.
    """
    said = "मुझे प्रोटीन बार चाहिए ₹500 से कम।"
    _patch_session(
        monkeypatch,
        _Session(resp=_Resp(200, __import__("json").dumps({"transcript": said}))),
    )
    out = await voice.transcribe(b"audio")
    assert out.text == said


# ── the invariant ────────────────────────────────────────────────────

def test_voice_is_registered_but_grants_no_new_powers():
    """Voice rides the existing text path; it adds no tool and no bypass.

    handle_voice must end by calling _run_agent_turn -- the same entry point
    handle_message uses -- and must never touch the confirmation callback. If
    someone later wires speech straight into the confirm handler, a spoken
    "yes" would start authorising payments and this test is what catches it.
    """
    import inspect

    from core import bot

    src = inspect.getsource(bot.handle_voice)
    assert "_run_agent_turn" in src, "voice must re-enter through the normal agent turn"
    for bypass in ("handle_callback", "assistant_confirmed", "commerce.checkout", "_CONFIRM_TEXT"):
        assert bypass not in src, f"voice must not reach the money path directly ({bypass})"
