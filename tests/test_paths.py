import pytest

from core import paths

CWD = "/mnt/c/Users/rupes/ai-agent"


@pytest.mark.parametrize("raw,expected", [
    # Windows absolute, both slash directions
    (r"D:\Projects", "/mnt/d/Projects"),
    ("D:/Projects", "/mnt/d/Projects"),
    (r"d:\projects\raven", "/mnt/d/projects/raven"),
    # Bare drive
    ("D:", "/mnt/d"),
    ("D:\\", "/mnt/d"),
    # Spoken drive
    ("d drive", "/mnt/d"),
    ("D drive", "/mnt/d"),
    ("d drive projects", "/mnt/d/projects"),
    ("d drive Projects/raven", "/mnt/d/Projects/raven"),
    # Already WSL
    ("/mnt/d/Projects", "/mnt/d/Projects"),
    ("/home/rupesh-wsl", "/home/rupesh-wsl"),
    # Relative to cwd
    ("core", f"{CWD}/core"),
    ("./core", f"{CWD}/core"),
    # Quoted
    ('"D:\\Projects"', "/mnt/d/Projects"),
    # Traversal collapses, does not escape silently
    ("/mnt/d/Projects/..", "/mnt/d"),
])
def test_normalize(raw, expected):
    assert paths.normalize(raw, CWD) == expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_normalize_rejects_empty(raw):
    assert paths.normalize(raw, CWD) is None


@pytest.mark.parametrize("wsl,win", [
    ("/mnt/d/Projects", r"D:\Projects"),
    ("/mnt/c/Users/rupes", r"C:\Users\rupes"),
    ("/mnt/d", "D:\\"),
    # No Windows equivalent -- returned unchanged
    ("/home/rupesh-wsl", "/home/rupesh-wsl"),
])
def test_to_windows(wsl, win):
    assert paths.to_windows(wsl) == win


def test_normalize_is_idempotent():
    once = paths.normalize(r"D:\Projects", CWD)
    assert paths.normalize(once, CWD) == once
