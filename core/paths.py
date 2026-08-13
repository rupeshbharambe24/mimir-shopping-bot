"""Single authority on interpreting and rendering filesystem paths.

RAVEN runs in WSL but the user works on Windows, so every path the user types
may arrive in any of these spellings -- all meaning the same directory:

    D:\\Projects   D:/Projects   /mnt/d/Projects   "d drive projects"

Everything internal uses WSL paths. Everything shown to the user uses Windows
paths. This module is the only place that knows how to convert between them.

Imports stdlib only -- importing from tools/ would create a cycle.
"""

import os
import re

# "d drive", "D drive projects", "d drive Projects/raven"
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
    """Interpret any path spelling as an absolute WSL path.

    Relative input resolves against `cwd`. Returns None only when there is no
    input at all -- an unparseable string is treated as a relative name, which
    validate_path then rejects if it escapes ALLOWED_PATHS.
    """
    if text is None:
        return None
    raw = text.strip().strip('"').strip("'").strip()
    if not raw:
        return None

    # Already a POSIX absolute path
    if raw.startswith("/"):
        return _collapse(raw)

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
    """Render a WSL path the way the user thinks about it.

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
