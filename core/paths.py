"""Single authority on interpreting and rendering filesystem paths.

Two hosts, two conventions:

WSL2  -- MIMIR runs in a Linux VM but the user works on Windows, so the same
         directory arrives spelled any of these ways, and all must resolve to
         the same place:

             D:\\Projects   D:/Projects   /mnt/d/Projects   "d drive projects"

         Internals use WSL paths; anything shown to the user is rendered back
         into Windows form.

macOS -- there is no VM boundary and no drive letters. Paths are already
         native, so interpretation is nearly the identity function; the work
         is expanding ~ and rendering long /Users/... paths compactly enough
         to read on a phone.

Imports stdlib and core.platform only -- importing from tools/ would cycle.
"""

import os
import re

from core import platform as host

# "d drive", "D drive projects", "d drive Projects/mimir"
_DRIVE_WORD = re.compile(r"^([a-zA-Z])\s+drive\b\s*(.*)$", re.IGNORECASE)
# "D:\..." or "D:/..."
_WIN_ABS = re.compile(r"^([a-zA-Z]):[\\/](.*)$")
# "D:" or "D:\" or "D:/"
_WIN_DRIVE_ONLY = re.compile(r"^([a-zA-Z]):[\\/]?$")


def _collapse(path: str) -> str:
    """Normalize separators and resolve . and .. lexically.

    Deliberately lexical (normpath, not resolve): it must not touch the
    filesystem or follow symlinks. Callers pass the result to
    file_ops.validate_path, which is what enforces ALLOWED_PATHS.
    """
    collapsed = os.path.normpath(path.replace("\\", "/"))
    if collapsed.startswith("//"):
        collapsed = collapsed[1:]
    return collapsed


def normalize(text: str | None, cwd: str) -> str | None:
    """Interpret any path spelling as an absolute native path.

    Relative input resolves against `cwd`. Returns None only when there is no
    input at all -- an unparseable string is treated as a relative name, which
    validate_path then rejects if it escapes ALLOWED_PATHS.
    """
    if text is None:
        return None
    raw = text.strip().strip('"').strip("'").strip()
    if not raw:
        return None

    # ~ and ~user. Barely mattered under WSL, where the user's files lived on
    # /mnt rather than in the Linux home; on macOS everything the user owns is
    # under /Users/<name>, so "~/Projects" is a spelling they will actually
    # type. Without this it resolved to "<cwd>/~/Projects" and was rejected.
    if raw == "~" or raw.startswith("~/") or (raw.startswith("~") and "/" in raw):
        expanded = os.path.expanduser(raw)
        if not expanded.startswith("~"):  # expanduser leaves unknown users be
            return _collapse(expanded)

    # Already a POSIX absolute path
    if raw.startswith("/"):
        return _collapse(raw)

    # Windows drive spellings only mean something where drives are mounted.
    # On macOS "D:\Projects" is not a path, and quietly inventing /mnt/d for it
    # would send the user a confusing "not found" for a place that never
    # existed. Falling through treats it as a relative name instead.
    if not host.IS_MAC:
        # D:\... or D:/...
        match = _WIN_ABS.match(raw)
        if match:
            drive, rest = match.group(1).lower(), match.group(2)
            return _collapse(f"/mnt/{drive}/{rest}")

        # D:  /  D:\  /  D:/
        match = _WIN_DRIVE_ONLY.match(raw)
        if match:
            return f"/mnt/{match.group(1).lower()}"

        # "d drive" optionally followed by a subpath
        match = _DRIVE_WORD.match(raw)
        if match:
            drive, rest = match.group(1).lower(), match.group(2).strip()
            rest = rest.replace("\\", "/").strip("/")
            if not rest:
                return f"/mnt/{drive}"
            return _collapse(f"/mnt/{drive}/{rest}")

    # Anything else: relative to the current directory
    return _collapse(f"{cwd.rstrip('/')}/{raw}")


def to_windows(path: str) -> str:
    """Render a WSL path in Windows form, for handing to a Win32 program.

    Paths outside /mnt have no Windows equivalent and are returned unchanged.
    """
    if not path.startswith("/mnt/"):
        return path
    remainder = path[len("/mnt/"):]
    if not remainder:
        return path
    parts = remainder.split("/", 1)
    drive = parts[0].upper()
    if len(drive) != 1 or not drive.isalpha():
        return path
    rest = parts[1] if len(parts) > 1 else ""
    return f"{drive}:\\{rest}".replace("/", "\\")


def to_native(path: str) -> str:
    """Render a path the way *this host's* own programs expect it.

    On WSL that means Windows form, because the program on the other end is a
    Win32 one. On macOS the path is already native.
    """
    return path if host.IS_MAC else to_windows(path)


def display(path: str) -> str:
    """Render a path the way the user thinks about it, for Telegram.

    On WSL the user thinks in Windows drive letters. On macOS they think in
    ordinary paths -- but the home prefix is pure noise on a phone screen, so
    it collapses to ~.
    """
    if not host.IS_MAC:
        return to_windows(path)
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path
