"""Speech to text via Sarvam, for Telegram voice notes.

Two things about this module are deliberate.

**It never raises and never guesses.** A failed transcription comes back as a
Transcript carrying an error string, and the caller shows it. Inventing a
plausible-sounding order out of noise is the one behaviour that would make voice
genuinely dangerous on a bot that fills a real cart with real money.

**A transcript is ordinary user text, nothing more.** It goes back into the same
entry point a typed message uses, so it inherits the same tool set, the same
guards and the same tap-to-approve gate. A spoken "yes" is still only text, and
text cannot authorise a payment here: approval arrives as a Telegram
callback_query, which is a different update type that no model and no
transcriber can produce. Voice is a faster keyboard, not a way around the gate.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import aiohttp

from config import Config

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.sarvam.ai/speech-to-text"

# Sarvam's synchronous endpoint takes 30 seconds of audio. Longer needs the
# batch API, which is a different shape entirely -- so we refuse early and say
# why rather than uploading something that will be rejected.
MAX_SECONDS = 30


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str = ""
    language: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text.strip()) and not self.error


def available() -> bool:
    return bool(Config.SARVAM_API_KEY)


def too_long(duration_s: int | None) -> bool:
    """Telegram hands us the duration up front, so we can refuse before spending
    the round trip on audio Sarvam will not accept."""
    return bool(duration_s and duration_s > MAX_SECONDS)


async def transcribe(
    audio: bytes,
    *,
    filename: str = "speech.ogg",
    content_type: str = "audio/ogg",
    timeout_s: float = 30.0,
) -> Transcript:
    """Send audio to Sarvam. Returns a Transcript; never raises.

    Telegram voice notes are OGG/Opus, which Sarvam accepts directly -- so the
    bytes go up exactly as they arrived, with no ffmpeg step and no re-encoding
    to lose quality through.
    """
    if not Config.SARVAM_API_KEY:
        return Transcript(error="SARVAM_API_KEY is not set")
    if not audio:
        return Transcript(error="no audio received")

    form = aiohttp.FormData()
    form.add_field("file", audio, filename=filename, content_type=content_type)
    form.add_field("model", Config.SARVAM_MODEL)
    form.add_field("language_code", Config.SARVAM_LANGUAGE)
    # saaras:v3 uses `mode` to pick transcription over translation. We want the
    # words that were actually said, in the language they were said in: an order
    # silently translated to English is an order the user cannot check.
    if Config.SARVAM_MODEL.startswith("saaras"):
        form.add_field("mode", "transcribe")

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                ENDPOINT,
                data=form,
                headers={"api-subscription-key": Config.SARVAM_API_KEY},
            ) as resp:
                body = await resp.text()
                status = resp.status
    except TimeoutError:
        return Transcript(error="Sarvam timed out")
    except aiohttp.ClientError as exc:
        return Transcript(error=f"transport error: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as text
        logger.exception("sarvam request failed")
        return Transcript(error=f"{type(exc).__name__}: {exc}")

    if status in (401, 403):
        return Transcript(error="Sarvam rejected the API key")
    if status == 429:
        return Transcript(error="Sarvam rate limited; try again shortly")
    if status >= 400:
        return Transcript(error=f"Sarvam HTTP {status}: {body[:150]}")

    try:
        doc = json.loads(body)
    except ValueError:
        return Transcript(error="Sarvam returned a non-JSON body")

    text = str(doc.get("transcript") or doc.get("text") or "").strip()
    if not text:
        return Transcript(error="Sarvam returned an empty transcript")

    return Transcript(text=text, language=str(doc.get("language_code") or ""))
