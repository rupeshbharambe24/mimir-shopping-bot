import os
from pathlib import Path
from dotenv import load_dotenv

from core import platform as host

load_dotenv(Path(__file__).parent / ".env")


def _env(name: str, default: str = "") -> str:
    """os.getenv, but a blank value counts as unset.

    A key present-but-empty in .env (WORKSPACE=) is how people disable a
    setting, yet os.getenv returns "" rather than the default -- which turned
    WORKSPACE into Path("") == Path(".") and pointed the whole agent at the
    process's cwd.
    """
    return (os.getenv(name) or default).strip()


class Config:
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

    # Where Ollama lives. On macOS and plain Linux it runs on this very
    # machine, so localhost is right. Only WSL2 needs the /etc/resolv.conf
    # trick: there the agent sits in a VM and Ollama belongs to the Windows
    # host, whose address turns up as the VM's nameserver.
    #
    # The IS_WSL guard is load-bearing. macOS has /etc/resolv.conf too, so the
    # ungated version silently resolved OLLAMA_URL to the router (e.g.
    # http://30.30.30.1:11434) and every local-model call hung until timeout
    # instead of failing loudly.
    OLLAMA_URL = _env("OLLAMA_URL")
    if not OLLAMA_URL and host.IS_WSL:
        try:
            with open("/etc/resolv.conf") as f:
                for line in f:
                    if line.strip().startswith("nameserver"):
                        OLLAMA_URL = f"http://{line.split()[-1]}:11434"
                        break
        except OSError:
            pass
    if not OLLAMA_URL:
        OLLAMA_URL = "http://localhost:11434"

    DEFAULT_MODEL = _env("DEFAULT_MODEL", "qwen2.5:7b")
    CODE_MODEL = _env("CODE_MODEL", "deepseek-coder:6.7b")

    # Cloud LLM cascade (Groq -> Gemini -> local Ollama)
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    # Env-configurable and NOT pinned to a dated build, for the same reason
    # GEMINI_MODEL isn't: llama-3.3-70b-versatile was retired from Groq and
    # started returning 404 "model_not_found", silently killing the primary
    # provider. A hardcoded model name is a time bomb on a hosted API.
    GROQ_MODEL = _env("GROQ_MODEL", "openai/gpt-oss-120b")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    # "-latest" rather than a pinned version: gemini-2.0-flash was retired and
    # started returning 404, silently killing the fallback provider.
    GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-flash-latest")

    # ── Agentic-commerce sidecar ─────────────────────────────────
    # The commerce BFF runs as its own process with its own pinned deps
    # (mcp==2.0.0 et al). Keeping it out-of-process means a provider SDK bump
    # cannot break the bot, and the bot's aiohttp/telegram stack cannot break
    # the provider transport.
    COMMERCE_BFF_URL = _env("COMMERCE_BFF_URL", "http://127.0.0.1:8011").rstrip("/")

    # Second, independent switch on the money path. The BFF has its own DRY_RUN;
    # a real order requires BOTH that to be off AND this to be on. One flag
    # flipped by accident -- in either repo -- therefore cannot spend money.
    COMMERCE_ALLOW_LIVE = _env("COMMERCE_ALLOW_LIVE", "false").lower() in ("1", "true", "yes")

    # Mimir's own spending ceiling in rupees, checked before the approval
    # prompt is even shown. This is a self-imposed limit, not a provider one:
    # Swiggy explicitly declares no cart-value ceiling. 0 disables the check.
    COMMERCE_MAX_ORDER_RUPEES = int(_env("COMMERCE_MAX_ORDER_RUPEES", "5000") or 0)

    # ── Voice notes (Sarvam speech-to-text) ──────────────────────
    # Optional. Unset simply means voice notes are declined with a note
    # saying so, rather than silently ignored -- a voice message that
    # vanishes reads as a broken bot.
    SARVAM_API_KEY = _env("SARVAM_API_KEY")

    # Env-configurable and NOT pinned to a dated build, for exactly the reason
    # GROQ_MODEL and GEMINI_MODEL are not: hosted models get retired. Sarvam
    # has already moved the default from saarika:v2.5 to saaras:v3 and marked
    # saarika legacy, so a hardcoded name here is the same time bomb a third
    # time over.
    SARVAM_MODEL = _env("SARVAM_MODEL", "saaras:v3")

    # "unknown" lets Saaras auto-detect across the 22 Indian languages rather
    # than forcing English onto someone who spoke Hindi or Kannada.
    SARVAM_LANGUAGE = _env("SARVAM_LANGUAGE", "unknown")

    # ── Auto-approve ("autopay") ─────────────────────────────────
    # Lets a user skip the tap for small orders. OFF for everyone unless they
    # turn it on, and bounded even then.
    #
    # These are the ceilings the USER cannot raise from chat. The per-order
    # limit they pick is clamped to the first; the second is a rolling daily
    # total, because a per-order limit alone bounds nothing -- an agent looping
    # on checkout places many small orders, not one big one.
    COMMERCE_AUTOPAY_MAX_RUPEES = int(_env("COMMERCE_AUTOPAY_MAX_RUPEES", "500") or 0)
    COMMERCE_AUTOPAY_DAILY_RUPEES = int(_env("COMMERCE_AUTOPAY_DAILY_RUPEES", "1500") or 0)

    # A master switch for the operator. With this false, no chat can enable
    # auto-approve at all, whatever their saved preference says -- so a stored
    # setting cannot outlive the operator's decision to allow the feature.
    COMMERCE_AUTOPAY_ENABLED = _env("COMMERCE_AUTOPAY_ENABLED", "false").lower() in ("1", "true", "yes")

    # ── Bot surface ──────────────────────────────────────────────
    # Mimir ships as a shopping assistant, so the default surface is the
    # commerce one: only those slash commands are registered, and only the
    # commerce tools reach the model. The developer tooling -- files, shell,
    # git, notebooks, screenshots -- stays in the tree and comes back whole
    # with BOT_MODE=full.
    #
    # Registration is gated rather than the menu alone, because a command
    # hidden from the menu but still live when typed is a menu that lies about
    # what the bot can do. One switch, one answer.
    BOT_MODE = _env("BOT_MODE", "shopping").lower()
    SHOPPING_ONLY = BOT_MODE != "full"

    WORKSPACE = Path(_env("WORKSPACE", str(Path(__file__).parent)))
    LOG_FILE = Path(_env("LOG_FILE", "logs/agent.log"))
    PERMISSION_TIMEOUT = int(_env("PERMISSION_TIMEOUT", "300"))

    # Security: restrict file operations to these base directories
    # Configurable via ALLOWED_PATHS env var (comma-separated), or defaults to
    # common locations for the host we are actually running on.
    _extra_paths = _env("ALLOWED_PATHS")
    ALLOWED_PATHS = [WORKSPACE]
    if _extra_paths:
        ALLOWED_PATHS.extend(Path(p.strip()) for p in _extra_paths.split(",") if p.strip())
    else:
        home = Path.home()
        if home.exists():
            ALLOWED_PATHS.append(home)
        if host.IS_MAC:
            # External disks, DMGs and network shares mount under /Volumes.
            # The boot volume appears here as a symlink back to /, which would
            # widen the allowlist to the entire filesystem -- so resolve each
            # entry and skip anything that lands on /.
            volumes = Path("/Volumes")
            if volumes.is_dir():
                for volume in volumes.iterdir():
                    try:
                        if volume.is_dir() and volume.resolve() != Path("/"):
                            ALLOWED_PATHS.append(volume)
                    except OSError:
                        continue
        else:
            # WSL: Windows drives are mounted as single letters under /mnt.
            mnt = Path("/mnt")
            if mnt.is_dir():
                for drive in mnt.iterdir():
                    if drive.is_dir() and len(drive.name) == 1:
                        ALLOWED_PATHS.append(drive)
