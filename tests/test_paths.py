import os

import pytest

from core import paths, platform as host
from conftest import mac_only, wsl_only

CWD = os.path.join(os.path.expanduser("~"), "ai-agent") if host.IS_MAC \
    else "/mnt/c/Users/rupes/ai-agent"


@pytest.mark.parametrize("raw,expected", [
    # Already absolute, on either host
    ("/tmp/x", "/tmp/x"),
    # Relative to cwd
    ("core", f"{CWD}/core"),
    ("./core", f"{CWD}/core"),
    # Traversal collapses lexically, does not escape silently
    ("/tmp/x/..", "/tmp"),
])
def test_normalize_everywhere(raw, expected):
    assert paths.normalize(raw, CWD) == expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_normalize_rejects_empty(raw):
    assert paths.normalize(raw, CWD) is None


def test_normalize_is_idempotent():
    once = paths.normalize("core", CWD)
    assert paths.normalize(once, CWD) == once


# ── ~ expansion: universal, but only load-bearing on macOS ────────
# Under WSL the user's files lived on /mnt, so ~ almost never came up. On macOS
# everything the user owns is under /Users/<name> and "~/Projects" is a
# spelling they will actually type; without expansion it became
# "<cwd>/~/Projects" and was rejected as outside the allowed roots.

def test_tilde_expands_to_home():
    assert paths.normalize("~", CWD) == os.path.expanduser("~")


def test_tilde_subpath_expands():
    expected = os.path.join(os.path.expanduser("~"), "Projects/mimir")
    assert paths.normalize("~/Projects/mimir", CWD) == expected


def test_unknown_user_tilde_is_not_silently_expanded():
    """~nosuchuser has no home; it must not become an absolute path."""
    out = paths.normalize("~nosuchuser/x", CWD)
    assert out.startswith(CWD)


# ── WSL: Windows spellings all mean the same directory ───────────

@wsl_only
@pytest.mark.parametrize("raw,expected", [
    (r"D:\Projects", "/mnt/d/Projects"),
    ("D:/Projects", "/mnt/d/Projects"),
    (r"d:\projects\mimir", "/mnt/d/projects/mimir"),
    ("D:", "/mnt/d"),
    ("D:\\", "/mnt/d"),
    ("d drive", "/mnt/d"),
    ("D drive", "/mnt/d"),
    ("d drive projects", "/mnt/d/projects"),
    ("d drive Projects/mimir", "/mnt/d/Projects/mimir"),
    ("/mnt/d/Projects", "/mnt/d/Projects"),
    ('"D:\\Projects"', "/mnt/d/Projects"),
])
def test_normalize_windows_spellings(raw, expected):
    assert paths.normalize(raw, CWD) == expected


@wsl_only
@pytest.mark.parametrize("wsl,win", [
    ("/mnt/d/Projects", r"D:\Projects"),
    ("/mnt/c/Users/rupes", r"C:\Users\rupes"),
    ("/mnt/d", "D:\\"),
    ("/home/rupesh-wsl", "/home/rupesh-wsl"),
])
def test_to_windows(wsl, win):
    assert paths.to_windows(wsl) == win


# ── macOS: no drive letters, and display collapses $HOME ─────────

@mac_only
@pytest.mark.parametrize("raw", [r"D:\Projects", "D:", "d drive projects"])
def test_drive_letters_are_not_invented_on_mac(raw):
    """A Mac has no /mnt/d. Quietly manufacturing one would send the user a
    'not found' for a location that never existed."""
    out = paths.normalize(raw, CWD)
    assert not out.startswith("/mnt/")


@mac_only
def test_display_collapses_home():
    home = os.path.expanduser("~")
    assert paths.display(f"{home}/Projects/mimir") == "~/Projects/mimir"
    assert paths.display(home) == "~"


@mac_only
def test_display_leaves_non_home_paths_alone():
    assert paths.display("/opt/homebrew/bin") == "/opt/homebrew/bin"
    assert paths.display("/Volumes/T7/data") == "/Volumes/T7/data"


@mac_only
def test_to_native_is_identity_on_mac():
    assert paths.to_native("/Users/x/y") == "/Users/x/y"
