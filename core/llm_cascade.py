"""
Smart LLM cascade: Groq -> Gemini -> local Ollama.

Drop-in replacement for OllamaClient. Tries cloud providers first
for speed, falls back to local Ollama if they fail.
"""

import asyncio
import json
import logging
import re

import aiohttp

from config import Config

logger = logging.getLogger(__name__)


_GROQ_MAX_RETRY_WAIT_S = 12.0


def _retry_after_seconds(resp, body: str) -> float | None:
    """How long Groq says to wait, or None if it did not say / it is too long."""
    raw = resp.headers.get("retry-after")
    wait = None
    if raw:
        try:
            wait = float(raw)
        except ValueError:
            wait = None
    if wait is None:
        # Groq puts it in prose too: "Please try again in 4.65s".
        m = re.search(r"try again in ([0-9.]+)s", body)
        if m:
            wait = float(m.group(1))
    if wait is None or wait > _GROQ_MAX_RETRY_WAIT_S:
        return None
    return wait + 0.25  # small cushion; the window is measured server-side


# Provider names for logging / status
GROQ = "Groq"
GEMINI = "Gemini"
OLLAMA = "Ollama"

_TIMEOUT = aiohttp.ClientTimeout(total=30)


class SmartLLM:
    """Cascading LLM client: Groq -> Gemini -> local Ollama."""

    def __init__(self):
        self._last_provider: str = "none"

    # ── public interface (same as OllamaClient) ─────────────────────

    async def ask(
        self,
        prompt: str,
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """Send a prompt through the cascade and return the first successful response."""
        providers = [
            (GROQ, self._ask_groq),
            (GEMINI, self._ask_gemini),
            (OLLAMA, self._ask_ollama),
        ]

        for name, fn in providers:
            try:
                logger.info(f"LLM cascade: trying {name}")
                result = await fn(prompt, system_prompt)
                self._last_provider = name
                logger.info(f"LLM cascade: {name} succeeded")
                return result
            except Exception as exc:
                logger.warning(f"LLM cascade: {name} failed — {type(exc).__name__}: {exc}")
                continue

        self._last_provider = "none"
        return "All LLM providers failed. Check logs for details."

    async def ask_code(self, prompt: str) -> str:
        """Code-focused request (uses same cascade)."""
        system = (
            "You are an expert programmer. Provide clean, correct code. "
            "Wrap code in markdown code blocks with the language tag."
        )
        return await self.ask(prompt, system_prompt=system)

    async def list_models(self) -> list | None:
        """List locally-installed Ollama models (for /models command)."""
        url = f"{Config.OLLAMA_URL}/api/tags"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    return data.get("models", [])
        except Exception as exc:
            logger.error(f"Error listing Ollama models: {exc}")
            return None

    async def is_alive(self) -> dict:
        """Check connectivity for all three providers. Returns a status dict."""
        groq_ok, gemini_ok, ollama_ok = await asyncio.gather(
            self._ping_groq(),
            self._ping_gemini(),
            self._ping_ollama(),
        )
        return {
            GROQ: groq_ok,
            GEMINI: gemini_ok,
            OLLAMA: ollama_ok,
        }

    def get_status(self) -> str:
        """Return which provider handled the last request."""
        return self._last_provider

    # ── Groq (OpenAI-compatible) ────────────────────────────────────

    async def _ask_groq(self, prompt: str, system_prompt: str | None) -> str:
        if not Config.GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY not configured")

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {Config.GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": Config.GROQ_MODEL,
            "messages": messages,
        }

        async with aiohttp.ClientSession() as session:
            for attempt in (1, 2):
                async with session.post(url, json=payload, headers=headers, timeout=_TIMEOUT) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"]
                    body = await resp.text()
                    if resp.status == 429 and attempt == 1:
                        wait = _retry_after_seconds(resp, body)
                        if wait is not None:
                            logger.info(f"Groq rate-limited; waiting {wait:.1f}s and retrying once")
                            await asyncio.sleep(wait)
                            continue
                    raise RuntimeError(f"Groq HTTP {resp.status}: {body[:300]}")
            raise RuntimeError("Groq rate-limited twice in a row")

    async def _ping_groq(self) -> bool:
        if not Config.GROQ_API_KEY:
            return False
        url = "https://api.groq.com/openai/v1/models"
        headers = {"Authorization": f"Bearer {Config.GROQ_API_KEY}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return resp.status == 200
        except Exception:
            return False

    # ── Gemini (REST API) ───────────────────────────────────────────

    async def _ask_gemini(self, prompt: str, system_prompt: str | None) -> str:
        if not Config.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY not configured")

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"models/{Config.GEMINI_MODEL}:generateContent"
            f"?key={Config.GEMINI_API_KEY}"
        )
        headers = {"Content-Type": "application/json"}

        body: dict = {
            "contents": [{"parts": [{"text": prompt}]}],
        }
        if system_prompt:
            body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=headers, timeout=_TIMEOUT) as resp:
                if resp.status == 429:
                    text = await resp.text()
                    raise RuntimeError(f"Gemini rate-limited (429): {text[:200]}")
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Gemini HTTP {resp.status}: {text[:300]}")
                data = await resp.json()
                # Navigate Gemini's response structure
                try:
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError) as exc:
                    raise RuntimeError(f"Unexpected Gemini response: {str(data)[:300]}") from exc

    async def _ping_gemini(self) -> bool:
        if not Config.GEMINI_API_KEY:
            return False
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models"
            f"?key={Config.GEMINI_API_KEY}"
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return resp.status == 200
        except Exception:
            return False

    # ── Ollama (local) ──────────────────────────────────────────────

    async def _ask_ollama(self, prompt: str, system_prompt: str | None) -> str:
        url = f"{Config.OLLAMA_URL}/api/generate"
        payload: dict = {
            "model": Config.DEFAULT_MODEL,
            "prompt": prompt,
            "stream": False,
        }
        if system_prompt:
            payload["system"] = system_prompt

        timeout = aiohttp.ClientTimeout(total=120)  # Ollama is local, allow more time
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    raise RuntimeError(f"Ollama HTTP {resp.status}: {error[:300]}")
                data = await resp.json()
                text = data.get("response", "")
                if not text:
                    raise RuntimeError("Ollama returned empty response")
                return text

    async def _ping_ollama(self) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(Config.OLLAMA_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return resp.status == 200
        except Exception:
            return False

    # ── Tool calling ────────────────────────────────────────────────
    #
    # Each provider speaks a different dialect for function calling. These
    # parsers flatten all three into one shape so agent.py never has to know
    # which provider answered:
    #
    #     {"text": str | None, "tool_calls": [{"id", "name", "args"}]}

    @staticmethod
    def _parse_groq(data: dict) -> dict:
        message = (data.get("choices") or [{}])[0].get("message", {})
        calls = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (ValueError, TypeError):
                # A model that emits malformed JSON should not kill the turn;
                # empty args let the tool report what it actually needs.
                args = {}
            calls.append({
                "id": raw.get("id") or fn.get("name", "call"),
                "name": fn.get("name", ""),
                "args": args if isinstance(args, dict) else {},
            })
        return {"text": message.get("content"), "tool_calls": calls}

    @staticmethod
    def _parse_gemini(data: dict) -> dict:
        parts = (
            (data.get("candidates") or [{}])[0]
            .get("content", {})
            .get("parts", [])
        )
        text_chunks, calls = [], []
        for index, part in enumerate(parts):
            if "functionCall" in part:
                fc = part["functionCall"]
                args = fc.get("args") or {}
                calls.append({
                    "id": f"{fc.get('name', 'call')}_{index}",
                    "name": fc.get("name", ""),
                    "args": args if isinstance(args, dict) else {},
                })
            elif "text" in part:
                text_chunks.append(part["text"])
        return {"text": "".join(text_chunks) or None, "tool_calls": calls}

    @staticmethod
    def _parse_ollama(data: dict) -> dict:
        message = data.get("message", {})
        calls = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            fn = raw.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (ValueError, TypeError):
                    args = {}
            calls.append({
                "id": f"{fn.get('name', 'call')}_{index}",
                "name": fn.get("name", ""),
                "args": args if isinstance(args, dict) else {},
            })
        return {"text": message.get("content") or None, "tool_calls": calls}

    async def ask_with_tools(self, messages: list[dict], tools: list[dict]) -> dict:
        """Ask the cascade for the next step, with tools available.

        Raises RuntimeError only when every provider fails, so callers can fall
        back to the regex classifier.
        """
        providers = [
            (GROQ, self._tools_groq),
            (GEMINI, self._tools_gemini),
            (OLLAMA, self._tools_ollama),
        ]
        errors = []
        for name, fn in providers:
            try:
                logger.info(f"Tool cascade: trying {name}")
                result = await fn(messages, tools)
                self._last_provider = name
                return result
            except Exception as exc:
                logger.warning(f"Tool cascade: {name} failed — {type(exc).__name__}: {exc}")
                errors.append(f"{name}: {exc}")
        self._last_provider = "none"
        raise RuntimeError("All providers failed: " + "; ".join(errors))

    async def _tools_groq(self, messages: list[dict], tools: list[dict]) -> dict:
        if not Config.GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY not configured")
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {Config.GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": Config.GROQ_MODEL,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        async with aiohttp.ClientSession() as sess:
            for attempt in (1, 2):
                async with sess.post(url, json=payload, headers=headers, timeout=_TIMEOUT) as resp:
                    if resp.status == 200:
                        return self._parse_groq(await resp.json())
                    body = await resp.text()
                    if resp.status == 429 and attempt == 1:
                        wait = _retry_after_seconds(resp, body)
                        if wait is not None:
                            logger.info(f"Groq rate-limited; waiting {wait:.1f}s and retrying once")
                            await asyncio.sleep(wait)
                            continue
                    raise RuntimeError(f"Groq HTTP {resp.status}: {body[:300]}")
            raise RuntimeError("Groq rate-limited twice in a row")

    async def _tools_gemini(self, messages: list[dict], tools: list[dict]) -> dict:
        if not Config.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY not configured")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"models/{Config.GEMINI_MODEL}:generateContent?key={Config.GEMINI_API_KEY}"
        )
        contents, system_text = [], None
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                system_text = msg.get("content")
                continue
            if role == "tool":
                contents.append({"role": "user", "parts": [
                    {"text": f"Result of {msg.get('name')}:\n{msg.get('content')}"},
                ]})
                continue
            contents.append({
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": msg.get("content") or ""}],
            })
        body = {
            "contents": contents,
            "tools": [{"functionDeclarations": [t["function"] for t in tools]}],
        }
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=body, timeout=_TIMEOUT) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Gemini HTTP {resp.status}: {(await resp.text())[:300]}")
                return self._parse_gemini(await resp.json())

    async def _tools_ollama(self, messages: list[dict], tools: list[dict]) -> dict:
        url = f"{Config.OLLAMA_URL}/api/chat"
        payload = {
            "model": Config.DEFAULT_MODEL,
            "messages": messages,
            "tools": tools,
            "stream": False,
        }
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Ollama HTTP {resp.status}: {(await resp.text())[:300]}")
                return self._parse_ollama(await resp.json())
