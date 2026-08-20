import logging
from pathlib import Path

from config import Config
from core import paths, platform as host

logger = logging.getLogger(__name__)


async def take_screenshot() -> tuple[str | None, str | None]:
    """Capture the user's desktop. Returns (local_path, error).

    The returned path is always a local path the agent can open and upload;
    only the path handed to the capturing program is host-native.
    """
    local_path = str(Config.WORKSPACE / "screenshot.png")

    # Stale file from a previous run would otherwise be re-sent as if it were
    # fresh when the capture silently produces nothing.
    try:
        Path(local_path).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(f"Could not clear previous screenshot: {exc}")

    returncode, _, stderr = await host.capture_screen(
        paths.to_native(local_path), timeout=20
    )

    if returncode != 0:
        detail = stderr.strip()[:300] or "no error output"
        return None, f"Screenshot failed: {detail}\n\n{host.screenshot_permission_hint()}"

    if not Path(local_path).exists():
        # screencapture exits 0 without writing a file when the process has no
        # Screen Recording grant, so a clean exit is not proof of success.
        return None, f"Screenshot file not created.\n\n{host.screenshot_permission_hint()}"

    return local_path, None
