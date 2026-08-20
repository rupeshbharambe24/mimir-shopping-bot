"""Shared test fixtures.

MIMIR targets two hosts whose path conventions are precisely what a lot of
these tests are about. Rather than assert WSL behaviour unconditionally -- which
made the whole suite fail on a Mac for reasons that had nothing to do with the
code being wrong -- the host-specific spellings live here and tests ask for
them by role.
"""

from pathlib import Path

import pytest

from core import platform as host

wsl_only = pytest.mark.skipif(not host.IS_WSL, reason="WSL-only path translation")
mac_only = pytest.mark.skipif(not host.IS_MAC, reason="macOS-only path handling")

# A directory that exists, sits inside ALLOWED_PATHS, and is NOT the workspace.
# Tests use it to tell "resolved against the supplied cwd" apart from "silently
# fell back to the workspace" -- a distinction that was a real bug once.
ALLOWED_ROOT = str(Path.home()) if host.IS_MAC else "/mnt/d"
