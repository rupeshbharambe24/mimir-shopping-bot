import shutil
import logging

from config import Config
from core.llm_cascade import SmartLLM
from core import platform as host

logger = logging.getLogger(__name__)


async def get_status() -> str:
    llm = SmartLLM()
    health = await llm.health()

    try:
        disk = shutil.disk_usage(str(Config.WORKSPACE))
        disk_line = f"Disk: {disk.free // (1024 ** 3)}GB free / {disk.total // (1024 ** 3)}GB total"
    except Exception:
        disk_line = "Disk: unable to check"

    # Each state gets its own wording, because each needs a different reaction:
    # wait, fix a key, install something, or investigate.
    _BADGES = {
        "ok":      "OK",
        "limited": "RATE-LIMITED (working, just throttled)",
        "bad_key": "KEY REJECTED",
        "no_key":  "no key set",
        "off":     "unreachable",
    }

    def _badge(state: str) -> str:
        return _BADGES.get(state, state)

    ollama_note = "  - optional, only used if both cloud providers fail"

    lines = [
        "SYSTEM STATUS",
        "",
        f"Agent: Running on {host.NAME}",
        f"LLM cascade: {llm.get_status() or 'idle'}",
        f"  Groq:   {_badge(health['Groq'])}  ({Config.GROQ_MODEL})",
        f"  Gemini: {_badge(health['Gemini'])}  ({Config.GEMINI_MODEL})",
        f"  Ollama: {_badge(health['Ollama'])}  ({Config.OLLAMA_URL})",
        ollama_note if health["Ollama"] != "ok" else "",
        f"Default model: {Config.DEFAULT_MODEL}",
        f"Code model: {Config.CODE_MODEL}",
        disk_line,
        f"Workspace: {Config.WORKSPACE}",
    ]
    return "\n".join(lines)


async def get_models() -> str:
    llm = SmartLLM()
    models = await llm.list_models()
    if models is None:
        return "Cannot connect to Ollama."
    if not models:
        return "No models installed."

    lines = ["AVAILABLE MODELS", ""]
    for m in models:
        name = m.get("name", "unknown")
        size_bytes = m.get("size", 0)
        size_gb = size_bytes / (1024 ** 3)
        lines.append(f"  {name}  ({size_gb:.1f}GB)")
    return "\n".join(lines)
