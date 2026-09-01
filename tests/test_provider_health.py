"""Provider health has four states, not two.

"Gemini, Groq and Ollama are all coming as OFF" — while Groq and Gemini were
answering HTTP 200 the whole time. The check reported a single boolean, so four
different situations printed the same word:

    unreachable          -> genuinely off
    429 rate-limited     -> up, key fine, sending too fast
    401/403              -> up, key wrong
    no key configured    -> nothing to check

During the Groq rate-limit storm every provider read OFF, which says "your
setup is broken" when the truth was "wait a minute", "fix a key" and "not
installed" respectively.
"""

import pytest

from core import llm_cascade
from core.llm_cascade import BAD_KEY, LIMITED, NO_KEY, OFF, OK, SmartLLM


class FakeResp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, status=None, raises=None):
        self._status, self._raises = status, raises

    def get(self, *a, **kw):
        if self._raises:
            raise self._raises
        return FakeResp(self._status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _session(monkeypatch, **kw):
    monkeypatch.setattr(
        llm_cascade.aiohttp, "ClientSession", lambda *a, **k: FakeSession(**kw)
    )


@pytest.mark.parametrize("status,expected", [
    (200, OK),
    (429, LIMITED),
    (401, BAD_KEY),
    (403, BAD_KEY),
    (500, OFF),
    (404, OFF),
])
async def test_each_http_status_maps_to_its_own_state(monkeypatch, status, expected):
    _session(monkeypatch, status=status)
    assert await SmartLLM._status_of("https://example.invalid") == expected


async def test_a_rate_limited_provider_is_not_reported_as_off(monkeypatch):
    """The one that caused the confusion. 429 means the provider is up and the
    key is good; reporting OFF sends people hunting for a broken key."""
    _session(monkeypatch, status=429)
    state = await SmartLLM._status_of("https://example.invalid")
    assert state == LIMITED
    assert state != OFF


async def test_an_unreachable_host_is_off(monkeypatch):
    _session(monkeypatch, raises=OSError("connection refused"))
    assert await SmartLLM._status_of("https://example.invalid") == OFF


async def test_a_missing_key_is_distinguished_from_a_dead_service(monkeypatch):
    """"no key" is a setup step; "off" is an outage. Same word for both sent
    the reader looking in the wrong place."""
    from config import Config

    monkeypatch.setattr(Config, "GROQ_API_KEY", "")
    assert await SmartLLM()._probe_once(llm_cascade.GROQ) == NO_KEY

    monkeypatch.setattr(Config, "GEMINI_API_KEY", "")
    assert await SmartLLM()._probe_once(llm_cascade.GEMINI) == NO_KEY


async def test_a_transient_failure_is_retried_before_being_called_off(monkeypatch):
    """The boot banner is a snapshot people keep looking at, and a cold start
    can beat the network up. One retry stops a two-second blip becoming a
    verdict that sits there all session."""
    calls = []

    async def flaky(self, provider):
        calls.append(provider)
        return OFF if len(calls) == 1 else OK

    monkeypatch.setattr(SmartLLM, "_probe_once", flaky)
    monkeypatch.setattr(llm_cascade.asyncio, "sleep", lambda *_: _noop())

    async def _noop():
        return None

    assert await SmartLLM()._probe(llm_cascade.GROQ) == OK
    assert len(calls) == 2


async def test_a_healthy_provider_is_not_probed_twice(monkeypatch):
    calls = []

    async def always_ok(self, provider):
        calls.append(provider)
        return OK

    monkeypatch.setattr(SmartLLM, "_probe_once", always_ok)
    assert await SmartLLM()._probe(llm_cascade.GROQ) == OK
    assert len(calls) == 1


async def test_is_alive_still_returns_booleans(monkeypatch):
    """Back-compatible: the old callers expect a bool per provider."""
    async def health(self):
        return {llm_cascade.GROQ: OK, llm_cascade.GEMINI: LIMITED,
                llm_cascade.OLLAMA: OFF}

    monkeypatch.setattr(SmartLLM, "health", health)
    out = await SmartLLM().is_alive()
    assert out == {"Groq": True, "Gemini": False, "Ollama": False}
    assert all(isinstance(v, bool) for v in out.values())
