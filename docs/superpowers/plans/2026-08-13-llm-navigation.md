# LLM-Driven Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace MIMIR's brittle regex intent classifier with an LLM tool-calling loop, and make every path-taking command understand Windows paths and a persistent current directory.

**Architecture:** A new `core/paths.py` is the single authority on interpreting paths (`D:\Projects`, `d drive projects`, `/mnt/d/Projects` all normalize to one WSL path). `core/session.py` holds a per-chat current directory persisted to JSON. `core/tools_registry.py` declares the tool schemas and executes them, routing destructive tools through the existing `Guard`. `core/agent.py` runs the model→tool→model loop. `core/llm_cascade.py` gains `ask_with_tools()` which normalizes Groq, Gemini, and Ollama tool-call formats into one shape.

**Tech Stack:** Python 3.10, python-telegram-bot 22.7, aiohttp, pytest (new).

## Global Constraints

- Python 3.10 — use `str | None`, not `Optional[str]`. No `match` statements beyond 3.10 syntax.
- The agent runs in WSL2 Ubuntu; all internal paths are WSL paths (`/mnt/d/...`). All paths shown to the user are Windows paths (`D:\...`) via `paths.to_windows`.
- `core/paths.py` must import only stdlib and `config`. It must never import from `tools/` — that would create an import cycle.
- Tool results returned to the model are always strings. Failures return an error string; they never raise.
- Gated tools are exactly: `write_file`, `delete_file`, `run_script`, `shell_command`, `kill_process`. All others execute without prompting.
- The agent loop is capped at 5 iterations.
- Existing `Guard` behaviour (Approve/Deny inline keyboard) is unchanged — reuse `guard.request_permission`, do not reimplement.
- Run all tests inside WSL with the project venv:
  `wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pytest ..."`

---

## File Structure

| File | Responsibility |
|---|---|
| `core/paths.py` (new) | Interpret any path spelling → WSL path; render WSL → Windows |
| `core/session.py` (new) | Per-chat current directory, persisted |
| `core/tools_registry.py` (new) | Tool JSON schemas + dispatch to `tools/*` |
| `core/agent.py` (new) | model → tool → model loop, permission gating |
| `core/llm_cascade.py` (modify) | Add `ask_with_tools()` across three providers |
| `tools/file_ops.py` (modify) | `validate_path` delegates to `paths.normalize` |
| `core/bot.py` (modify) | Route free text to agent; path-aware commands; `/cd`, `/pwd` |
| `tests/` (new) | pytest suite |

---

### Task 1: Path normalization

**Files:**
- Create: `core/paths.py`
- Create: `tests/test_paths.py`
- Create: `pytest.ini`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `config.Config` (for nothing yet — kept importable).
- Produces:
  - `normalize(text: str | None, cwd: str) -> str | None`
  - `to_windows(path: str) -> str`

- [ ] **Step 1: Add pytest to requirements and create pytest.ini**

Append to `requirements.txt`:

```
pytest>=8.0
pytest-asyncio>=0.23
```

Create `pytest.ini`:

```ini
[pytest]
testpaths = tests
asyncio_mode = auto
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_paths.py`:

```python
import pytest

from core import paths

CWD = "/mnt/c/Users/rupes/ai-agent"


@pytest.mark.parametrize("raw,expected", [
    # Windows absolute, both slash directions
    (r"D:\Projects", "/mnt/d/Projects"),
    ("D:/Projects", "/mnt/d/Projects"),
    (r"d:\projects\mimir", "/mnt/d/projects/mimir"),
    # Bare drive
    ("D:", "/mnt/d"),
    (r"D:\", "/mnt/d"),
    # Spoken drive
    ("d drive", "/mnt/d"),
    ("D drive", "/mnt/d"),
    ("d drive projects", "/mnt/d/projects"),
    ("d drive Projects/mimir", "/mnt/d/Projects/mimir"),
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
    # No Windows equivalent — returned unchanged
    ("/home/rupesh-wsl", "/home/rupesh-wsl"),
])
def test_to_windows(wsl, win):
    assert paths.to_windows(wsl) == win


def test_normalize_is_idempotent():
    once = paths.normalize(r"D:\Projects", CWD)
    assert paths.normalize(once, CWD) == once
```

- [ ] **Step 3: Run test to verify it fails**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pytest tests/test_paths.py -v"
```
Expected: FAIL — `ModuleNotFoundError: No module named 'core.paths'`

- [ ] **Step 4: Write the implementation**

Create `core/paths.py`:

```python
"""Single authority on interpreting and rendering filesystem paths.

MIMIR runs in WSL but the user works on Windows, so every path the user types
may arrive in any of these spellings — all meaning the same directory:

    D:\\Projects   D:/Projects   /mnt/d/Projects   "d drive projects"

Everything internal uses WSL paths. Everything shown to the user uses Windows
paths. This module is the only place that knows how to convert between them.

Imports stdlib only (plus config) — importing from tools/ would create a cycle.
"""

import os
import re

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
    # normpath turns "/mnt/d/" into "/mnt/d" but leaves "//x" alone on posix
    if collapsed.startswith("//"):
        collapsed = collapsed[1:]
    return collapsed


def normalize(text: str | None, cwd: str) -> str | None:
    """Interpret any path spelling as an absolute WSL path.

    Relative input resolves against `cwd`. Returns None only when there is no
    input at all — an unparseable string is treated as a relative name, which
    validate_path will then reject if it escapes ALLOWED_PATHS.
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pip install -q pytest pytest-asyncio && python -m pytest tests/test_paths.py -v"
```
Expected: PASS — all parametrized cases green.

- [ ] **Step 6: Commit**

```bash
git add core/paths.py tests/test_paths.py pytest.ini requirements.txt
git commit -m "feat: add path normalization for Windows and spoken drive names"
```

---

### Task 2: Harden validate_path

**Files:**
- Modify: `tools/file_ops.py:9-18`
- Create: `tests/test_file_ops_paths.py`

**Interfaces:**
- Consumes: `paths.normalize` from Task 1.
- Produces: `validate_path(filepath: str, cwd: str | None = None) -> tuple[bool, Path]` — the added `cwd` parameter defaults to `Config.WORKSPACE`, so existing single-argument callers keep working.

- [ ] **Step 1: Write the failing test**

Create `tests/test_file_ops_paths.py`:

```python
from pathlib import Path

from config import Config
from tools import file_ops


def test_windows_path_resolves_to_wsl_mount():
    ok, target = file_ops.validate_path(r"D:\Projects")
    assert ok is True
    assert target == Path("/mnt/d/Projects")


def test_windows_path_is_not_treated_as_workspace_relative():
    """The old bug: D:\\Projects silently became WORKSPACE/D:\\Projects."""
    _, target = file_ops.validate_path(r"D:\Projects")
    assert "ai-agent" not in str(target)
    assert str(target).startswith("/mnt/d")


def test_spoken_drive_resolves():
    ok, target = file_ops.validate_path("d drive projects")
    assert ok is True
    assert target == Path("/mnt/d/projects")


def test_relative_path_resolves_against_supplied_cwd():
    ok, target = file_ops.validate_path("core", cwd="/mnt/d/Projects/mimir")
    assert ok is True
    assert target == Path("/mnt/d/Projects/mimir/core")


def test_empty_path_rejected():
    ok, _ = file_ops.validate_path("")
    assert ok is False


def test_path_outside_allowed_roots_rejected():
    ok, _ = file_ops.validate_path("/etc/shadow")
    assert ok is False
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pytest tests/test_file_ops_paths.py -v"
```
Expected: FAIL — `test_windows_path_resolves_to_wsl_mount` asserts `/mnt/d/Projects` but gets `/mnt/c/Users/rupes/ai-agent/D:\Projects`.

- [ ] **Step 3: Replace validate_path**

In `tools/file_ops.py`, add the import below the existing ones:

```python
from core import paths
```

Replace lines 9-18 entirely:

```python
def validate_path(filepath: str, cwd: str | None = None) -> tuple[bool, Path]:
    """Interpret a user-supplied path and confirm it is inside ALLOWED_PATHS.

    Normalization happens first, so Windows spellings (D:\\Projects) land where
    the user meant. Previously these fell through to cwd-relative resolution
    and silently pointed inside the workspace.
    """
    normalized = paths.normalize(filepath, cwd or str(Config.WORKSPACE))
    if normalized is None:
        return False, Path(filepath or ".")

    target = Path(normalized)
    for allowed in Config.ALLOWED_PATHS:
        try:
            target.relative_to(allowed.resolve())
            return True, target
        except ValueError:
            continue
    return False, target
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pytest tests/ -v"
```
Expected: PASS — both test files green.

- [ ] **Step 5: Commit**

```bash
git add tools/file_ops.py tests/test_file_ops_paths.py
git commit -m "fix: validate_path no longer resolves Windows paths as workspace-relative"
```

---

### Task 3: Per-chat session state

**Files:**
- Create: `core/session.py`
- Create: `tests/test_session.py`

**Interfaces:**
- Produces:
  - `get_cwd(chat_id: int) -> str`
  - `set_cwd(chat_id: int, path: str) -> None`
  - `reset(chat_id: int) -> None`
  - `SESSION_FILE: Path` — module-level, monkeypatchable in tests.

- [ ] **Step 1: Write the failing test**

Create `tests/test_session.py`:

```python
import json

import pytest

from core import session


@pytest.fixture(autouse=True)
def temp_session_file(tmp_path, monkeypatch):
    """Point the module at a throwaway file and clear its cache each test."""
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(session, "_cache", None)
    yield


def test_defaults_to_workspace():
    from config import Config
    assert session.get_cwd(123) == str(Config.WORKSPACE)


def test_set_then_get():
    session.set_cwd(123, "/mnt/d/Projects")
    assert session.get_cwd(123) == "/mnt/d/Projects"


def test_chats_are_isolated():
    session.set_cwd(1, "/mnt/d/one")
    session.set_cwd(2, "/mnt/d/two")
    assert session.get_cwd(1) == "/mnt/d/one"
    assert session.get_cwd(2) == "/mnt/d/two"


def test_survives_reload(monkeypatch):
    session.set_cwd(123, "/mnt/d/Projects")
    monkeypatch.setattr(session, "_cache", None)  # simulate restart
    assert session.get_cwd(123) == "/mnt/d/Projects"


def test_corrupt_file_degrades_to_default(monkeypatch):
    session.SESSION_FILE.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(session, "_cache", None)
    from config import Config
    assert session.get_cwd(123) == str(Config.WORKSPACE)


def test_reset_restores_default():
    session.set_cwd(123, "/mnt/d/Projects")
    session.reset(123)
    from config import Config
    assert session.get_cwd(123) == str(Config.WORKSPACE)


def test_write_is_atomic_no_temp_left_behind():
    session.set_cwd(123, "/mnt/d/Projects")
    leftovers = list(session.SESSION_FILE.parent.glob("*.tmp"))
    assert leftovers == []


def test_file_contents_are_readable_json():
    session.set_cwd(123, "/mnt/d/Projects")
    data = json.loads(session.SESSION_FILE.read_text(encoding="utf-8"))
    assert data["123"]["cwd"] == "/mnt/d/Projects"
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pytest tests/test_session.py -v"
```
Expected: FAIL — `ModuleNotFoundError: No module named 'core.session'`

- [ ] **Step 3: Write the implementation**

Create `core/session.py`:

```python
"""Per-chat conversation state, persisted across restarts.

Currently just the working directory, but this is the seam for anything else
that needs to survive `/stop` or a systemd restart.
"""

import json
import logging
import os
from pathlib import Path

from config import Config

logger = logging.getLogger(__name__)

SESSION_FILE = Config.WORKSPACE / ".sessions.json"

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        _cache = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        if not isinstance(_cache, dict):
            raise ValueError("session file is not an object")
    except FileNotFoundError:
        _cache = {}
    except Exception as exc:
        # A corrupt state file must never stop the bot from starting.
        logger.warning(f"Session file unreadable ({exc}); starting fresh.")
        _cache = {}
    return _cache


def _save() -> None:
    data = _load()
    tmp = SESSION_FILE.with_suffix(".tmp")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, SESSION_FILE)  # atomic; no torn file on crash
    except Exception as exc:
        logger.error(f"Failed to persist session state: {exc}")
        try:
            tmp.unlink()
        except OSError:
            pass


def get_cwd(chat_id: int) -> str:
    entry = _load().get(str(chat_id))
    if isinstance(entry, dict) and isinstance(entry.get("cwd"), str):
        return entry["cwd"]
    return str(Config.WORKSPACE)


def set_cwd(chat_id: int, path: str) -> None:
    data = _load()
    data.setdefault(str(chat_id), {})["cwd"] = path
    _save()


def reset(chat_id: int) -> None:
    _load().pop(str(chat_id), None)
    _save()
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pytest tests/test_session.py -v"
```
Expected: PASS

- [ ] **Step 5: Add the session file to .gitignore and commit**

Append to `.gitignore` under `# Agent runtime`:

```
.sessions.json
```

```bash
git add core/session.py tests/test_session.py .gitignore
git commit -m "feat: add persistent per-chat working directory"
```

---

### Task 4: Tool registry

**Files:**
- Create: `core/tools_registry.py`
- Create: `tests/test_tools_registry.py`

**Interfaces:**
- Consumes: `paths.normalize`, `paths.to_windows`, `session.get_cwd/set_cwd`, and the existing `tools.file_ops`, `tools.process_ops`, `tools.screenshot`, `tools.system_info`.
- Produces:
  - `TOOL_SCHEMAS: list[dict]` — OpenAI-style function schemas.
  - `GATED_TOOLS: frozenset[str]`
  - `async execute(name: str, args: dict, chat_id: int) -> str` — never raises.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tools_registry.py`:

```python
import pytest

from core import session, tools_registry


@pytest.fixture(autouse=True)
def temp_session(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / "s.json")
    monkeypatch.setattr(session, "_cache", None)
    yield


def test_schemas_are_wellformed():
    names = set()
    for schema in tools_registry.TOOL_SCHEMAS:
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"
        names.add(fn["name"])
    # Every gated tool must actually be declared
    assert tools_registry.GATED_TOOLS <= names


def test_gated_set_is_exactly_the_destructive_tools():
    assert tools_registry.GATED_TOOLS == frozenset({
        "write_file", "delete_file", "run_script",
        "shell_command", "kill_process",
    })


async def test_cd_updates_session_and_reports_windows_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools_registry.file_ops, "validate_path",
        lambda p, cwd=None: (True, __import__("pathlib").Path("/mnt/d/Projects")),
    )
    monkeypatch.setattr(
        tools_registry, "_is_dir", lambda p: True,
    )
    out = await tools_registry.execute("cd", {"path": r"D:\Projects"}, chat_id=7)
    assert session.get_cwd(7) == "/mnt/d/Projects"
    assert r"D:\Projects" in out


async def test_cd_rejects_missing_directory(monkeypatch):
    monkeypatch.setattr(
        tools_registry.file_ops, "validate_path",
        lambda p, cwd=None: (True, __import__("pathlib").Path("/mnt/d/nope")),
    )
    monkeypatch.setattr(tools_registry, "_is_dir", lambda p: False)
    out = await tools_registry.execute("cd", {"path": "nope"}, chat_id=7)
    assert "not a directory" in out.lower()
    assert session.get_cwd(7) != "/mnt/d/nope"


async def test_list_dir_defaults_to_session_cwd(monkeypatch):
    session.set_cwd(7, "/mnt/d/Projects")
    seen = {}

    def fake_list_dir(path, **kw):
        seen["path"] = path
        return "(empty directory)"

    monkeypatch.setattr(tools_registry.file_ops, "list_dir", fake_list_dir)
    await tools_registry.execute("list_dir", {}, chat_id=7)
    assert seen["path"] == "/mnt/d/Projects"


async def test_unknown_tool_returns_error_string_not_raise():
    out = await tools_registry.execute("no_such_tool", {}, chat_id=7)
    assert "unknown tool" in out.lower()


async def test_tool_exception_is_caught_and_returned(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(tools_registry.file_ops, "list_dir", boom)
    out = await tools_registry.execute("list_dir", {"path": "/mnt/d"}, chat_id=7)
    assert "disk on fire" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pytest tests/test_tools_registry.py -v"
```
Expected: FAIL — `ModuleNotFoundError: No module named 'core.tools_registry'`

- [ ] **Step 3: Write the implementation**

Create `core/tools_registry.py`:

```python
"""Tool schemas the LLM can call, and the dispatch that runs them.

Kept separate from agent.py so the loop logic and the tool surface can be read
and tested independently. Every executor returns a string — the loop feeds it
straight back to the model as a tool result, so failures must come back as text
rather than exceptions or the model cannot recover from them.
"""

import logging
from pathlib import Path

from core import paths, session
from tools import file_ops, process_ops, screenshot, system_info

logger = logging.getLogger(__name__)

GATED_TOOLS = frozenset({
    "write_file", "delete_file", "run_script", "shell_command", "kill_process",
})


def _is_dir(path: Path) -> bool:
    """Wrapped for monkeypatching in tests."""
    return path.is_dir()


def _path_arg(args: dict, chat_id: int, key: str = "path") -> tuple[bool, Path | None, str]:
    """Resolve a path argument against the chat's cwd. Returns (ok, path, error)."""
    cwd = session.get_cwd(chat_id)
    raw = args.get(key)
    if raw is None or str(raw).strip() == "":
        return True, Path(cwd), ""
    ok, target = file_ops.validate_path(str(raw), cwd=cwd)
    if not ok:
        return False, None, f"Access denied or unreadable path: {raw}"
    return True, target, ""


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "cd",
            "description": (
                "Change the current working directory. Accepts Windows paths "
                "(D:\\Projects), WSL paths (/mnt/d/Projects), spoken drives "
                "('d drive projects'), or a name relative to the current folder."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Target directory."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and folders. Omit path to list the current folder.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory to list."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tree",
            "description": "Show a nested directory tree. Omit path for the current folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_depth": {"type": "integer", "description": "Default 3."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file's contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Search all allowed drives for files whose name matches a query.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": "Send a file to the user over Telegram.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_explorer",
            "description": (
                "Open a folder or file in Windows Explorer on the user's desktop. "
                "Omit path to open the current folder."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_files",
            "description": "List recently modified files. Omit path for the current folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "hours": {"type": "integer", "description": "Default 24."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "status",
            "description": "Report system status: CPU, memory, disk, LLM providers.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Capture the user's Windows desktop and send it.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text to a file. Requires the user's approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file. Requires the user's approval.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": "Run a Python script. Requires the user's approval.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell_command",
            "description": "Run a bash command in WSL. Requires the user's approval.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_process",
            "description": "Kill a tracked background process. Requires the user's approval.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": [],
            },
        },
    },
]


async def execute(name: str, args: dict, chat_id: int) -> str:
    """Run a tool and return a string result. Never raises."""
    try:
        return await _dispatch(name, args or {}, chat_id)
    except Exception as exc:
        logger.exception(f"Tool {name} failed")
        return f"Tool '{name}' failed: {type(exc).__name__}: {exc}"


async def _dispatch(name: str, args: dict, chat_id: int) -> str:
    if name == "cd":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        if not _is_dir(target):
            return f"Not a directory: {paths.to_windows(str(target))}"
        session.set_cwd(chat_id, str(target))
        return f"Current folder is now {paths.to_windows(str(target))}"

    if name == "list_dir":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        listing = file_ops.list_dir(str(target))
        return f"{paths.to_windows(str(target))}\n\n{listing}"

    if name == "tree":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        depth = int(args.get("max_depth") or 3)
        return file_ops.tree(str(target), max_depth=depth)

    if name == "read_file":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        return file_ops.read_file(str(target))

    if name == "find_files":
        query = str(args.get("query") or "").strip()
        if not query:
            return "find_files needs a query."
        hits = file_ops.find_files(query)
        if not hits:
            return f"No files matching '{query}'."
        return "\n".join(paths.to_windows(str(h)) for h in hits)

    if name == "recent_files":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        hours = int(args.get("hours") or 24)
        return file_ops.recent_files(str(target), hours=hours)

    if name == "status":
        return await system_info.get_status()

    if name == "open_explorer":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        win = paths.to_windows(str(target))
        cmd = f"powershell.exe -NoProfile -Command \"Start-Process '{win}'\""
        rc, _, stderr = await process_ops.run_command(cmd, timeout=15)
        if rc != 0:
            return f"Could not open {win}: {stderr.strip()[:200]}"
        return f"Opened {win} in Explorer."

    # send_file and screenshot need the Telegram Bot object, so agent.py
    # intercepts them before dispatch and they never reach here.
    if name in ("send_file", "screenshot"):
        return f"Tool '{name}' must be handled by the agent loop."

    if name == "write_file":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        return file_ops.write_file(str(target), str(args.get("content") or ""))

    if name == "delete_file":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        return file_ops.delete_file(str(target))

    if name == "run_script":
        ok, target, err = _path_arg(args, chat_id)
        if not ok:
            return err
        rc, out, errout = await process_ops.run_script(str(target))
        return f"exit={rc}\n{out}\n{errout}".strip()

    if name == "shell_command":
        command = str(args.get("command") or "").strip()
        if not command:
            return "shell_command needs a command."
        cwd = session.get_cwd(chat_id)
        rc, out, errout = await process_ops.run_command(command, timeout=120, cwd=cwd)
        process_ops.add_to_history(command, rc, out or errout)
        return f"exit={rc}\n{out}\n{errout}".strip()

    if name == "kill_process":
        return await process_ops.kill_process(args.get("name"))

    return f"Unknown tool: {name}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pytest tests/test_tools_registry.py -v"
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/tools_registry.py tests/test_tools_registry.py
git commit -m "feat: add LLM tool schemas and dispatch"
```

---

### Task 5: Tool-calling across the provider cascade

**Files:**
- Modify: `core/llm_cascade.py`
- Create: `tests/test_llm_cascade_tools.py`

**Interfaces:**
- Produces on `SmartLLM`:
  - `async ask_with_tools(messages: list[dict], tools: list[dict]) -> dict`
    returning `{"text": str | None, "tool_calls": [{"id": str, "name": str, "args": dict}]}`
  - Static parsers `_parse_groq(data)`, `_parse_gemini(data)`, `_parse_ollama(data)` returning the same shape.

- [ ] **Step 1: Write the failing test**

Create `tests/test_llm_cascade_tools.py`:

```python
import json

from core.llm_cascade import SmartLLM


def test_parse_groq_tool_call():
    data = {"choices": [{"message": {
        "content": None,
        "tool_calls": [{
            "id": "call_1",
            "function": {"name": "cd", "arguments": json.dumps({"path": "D:/Projects"})},
        }],
    }}]}
    out = SmartLLM._parse_groq(data)
    assert out["tool_calls"] == [{"id": "call_1", "name": "cd", "args": {"path": "D:/Projects"}}]
    assert out["text"] is None


def test_parse_groq_plain_text():
    data = {"choices": [{"message": {"content": "all done"}}]}
    out = SmartLLM._parse_groq(data)
    assert out["text"] == "all done"
    assert out["tool_calls"] == []


def test_parse_groq_malformed_arguments_degrade_to_empty():
    data = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c", "function": {"name": "cd", "arguments": "{not json"}},
    ]}}]}
    out = SmartLLM._parse_groq(data)
    assert out["tool_calls"][0]["args"] == {}


def test_parse_gemini_function_call():
    data = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "list_dir", "args": {"path": "/mnt/d"}}},
    ]}}]}
    out = SmartLLM._parse_gemini(data)
    assert out["tool_calls"][0]["name"] == "list_dir"
    assert out["tool_calls"][0]["args"] == {"path": "/mnt/d"}


def test_parse_gemini_text():
    data = {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}
    out = SmartLLM._parse_gemini(data)
    assert out["text"] == "hello"
    assert out["tool_calls"] == []


def test_parse_ollama_tool_call():
    data = {"message": {"content": "", "tool_calls": [
        {"function": {"name": "cd", "arguments": {"path": "/mnt/d"}}},
    ]}}
    out = SmartLLM._parse_ollama(data)
    assert out["tool_calls"][0]["name"] == "cd"
    assert out["tool_calls"][0]["args"] == {"path": "/mnt/d"}


def test_parse_ollama_text():
    data = {"message": {"content": "done"}}
    out = SmartLLM._parse_ollama(data)
    assert out["text"] == "done"
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pytest tests/test_llm_cascade_tools.py -v"
```
Expected: FAIL — `AttributeError: type object 'SmartLLM' has no attribute '_parse_groq'`

- [ ] **Step 3: Add the parsers and ask_with_tools**

Append these methods inside the `SmartLLM` class in `core/llm_cascade.py`:

```python
    # ── Tool calling ────────────────────────────────────────────────

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

        Returns {"text": str | None, "tool_calls": [...]}. Raises RuntimeError
        only when every provider fails, so callers can fall back to regex.
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
            async with sess.post(url, json=payload, headers=headers, timeout=_TIMEOUT) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Groq HTTP {resp.status}: {(await resp.text())[:300]}")
                return self._parse_groq(await resp.json())

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
```

Add `import json` to the imports at the top of `core/llm_cascade.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pytest tests/test_llm_cascade_tools.py -v"
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/llm_cascade.py tests/test_llm_cascade_tools.py
git commit -m "feat: add tool-calling across Groq, Gemini and Ollama"
```

---

### Task 6: The agent loop

**Files:**
- Create: `core/agent.py`
- Create: `tests/test_agent.py`

**Interfaces:**
- Consumes: `llm.ask_with_tools`, `tools_registry.TOOL_SCHEMAS/GATED_TOOLS/execute`, `guard.request_permission`, `session.get_cwd`.
- Produces: `async run(bot, chat_id, text, llm, guard, on_file=None) -> str` — returns the final text to show the user. `on_file` is an optional `async (path: str) -> None` used for `send_file` and `screenshot`.
- `MAX_ITERATIONS: int = 5`

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent.py`:

```python
import json

import pytest

from core import agent, session


class FakeLLM:
    """Returns canned responses in order, recording what it was asked."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def ask_with_tools(self, messages, tools):
        self.calls.append(messages)
        if not self._responses:
            return {"text": "done", "tool_calls": []}
        return self._responses.pop(0)


class FakeGuard:
    def __init__(self, approve=True):
        self.approve = approve
        self.requests = []

    def needs_permission(self, operation):
        return True

    async def request_permission(self, bot, chat_id, operation, description, details=""):
        self.requests.append((operation, description))
        return self.approve


@pytest.fixture(autouse=True)
def temp_session(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / "s.json")
    monkeypatch.setattr(session, "_cache", None)
    yield


async def test_single_tool_then_answer(monkeypatch):
    executed = []

    async def fake_execute(name, args, chat_id):
        executed.append((name, args))
        return "Folder listing here"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    llm = FakeLLM([
        {"text": None, "tool_calls": [{"id": "1", "name": "list_dir", "args": {"path": "/mnt/d"}}]},
        {"text": "Here are your folders.", "tool_calls": []},
    ])
    out = await agent.run(bot=None, chat_id=7, text="list d drive", llm=llm, guard=FakeGuard())

    assert executed == [("list_dir", {"path": "/mnt/d"})]
    assert out == "Here are your folders."


async def test_chained_tools(monkeypatch):
    executed = []

    async def fake_execute(name, args, chat_id):
        executed.append(name)
        return "ok"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    llm = FakeLLM([
        {"text": None, "tool_calls": [{"id": "1", "name": "cd", "args": {"path": "D:/Projects"}}]},
        {"text": None, "tool_calls": [{"id": "2", "name": "list_dir", "args": {}}]},
        {"text": "Done.", "tool_calls": []},
    ])
    await agent.run(bot=None, chat_id=7, text="go to d drive projects and list", llm=llm, guard=FakeGuard())
    assert executed == ["cd", "list_dir"]


async def test_gated_tool_requires_approval(monkeypatch):
    executed = []

    async def fake_execute(name, args, chat_id):
        executed.append(name)
        return "deleted"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)
    guard = FakeGuard(approve=False)

    llm = FakeLLM([
        {"text": None, "tool_calls": [{"id": "1", "name": "delete_file", "args": {"path": "/mnt/d/x.txt"}}]},
        {"text": "I did not delete it.", "tool_calls": []},
    ])
    await agent.run(bot=None, chat_id=7, text="delete x", llm=llm, guard=guard)

    assert guard.requests, "guard should have been consulted"
    assert executed == [], "denied tool must never execute"


async def test_approved_gated_tool_executes(monkeypatch):
    executed = []

    async def fake_execute(name, args, chat_id):
        executed.append(name)
        return "deleted"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    llm = FakeLLM([
        {"text": None, "tool_calls": [{"id": "1", "name": "delete_file", "args": {"path": "/mnt/d/x.txt"}}]},
        {"text": "Deleted.", "tool_calls": []},
    ])
    await agent.run(bot=None, chat_id=7, text="delete x", llm=llm, guard=FakeGuard(approve=True))
    assert executed == ["delete_file"]


async def test_denied_tool_is_not_retried(monkeypatch):
    async def fake_execute(name, args, chat_id):
        return "ok"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)
    guard = FakeGuard(approve=False)

    call = {"id": "1", "name": "delete_file", "args": {"path": "/mnt/d/x.txt"}}
    llm = FakeLLM([
        {"text": None, "tool_calls": [call]},
        {"text": None, "tool_calls": [dict(call)]},  # model stubbornly retries
        {"text": "Fine.", "tool_calls": []},
    ])
    await agent.run(bot=None, chat_id=7, text="delete x", llm=llm, guard=guard)

    assert len(guard.requests) == 1, "must not re-prompt for an identical denied call"


async def test_iteration_cap(monkeypatch):
    async def fake_execute(name, args, chat_id):
        return "again"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    # Always asks for another tool, never answers
    class Looping:
        async def ask_with_tools(self, messages, tools):
            return {"text": None, "tool_calls": [{"id": "x", "name": "list_dir", "args": {}}]}

    out = await agent.run(bot=None, chat_id=7, text="loop", llm=Looping(), guard=FakeGuard())
    assert "could not finish" in out.lower()


async def test_all_providers_down_raises(monkeypatch):
    class Dead:
        async def ask_with_tools(self, messages, tools):
            raise RuntimeError("All providers failed")

    with pytest.raises(RuntimeError):
        await agent.run(bot=None, chat_id=7, text="hi", llm=Dead(), guard=FakeGuard())


async def test_cwd_is_in_system_prompt(monkeypatch):
    async def fake_execute(name, args, chat_id):
        return "ok"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)
    session.set_cwd(7, "/mnt/d/Projects")

    llm = FakeLLM([{"text": "hi", "tool_calls": []}])
    await agent.run(bot=None, chat_id=7, text="where am i", llm=llm, guard=FakeGuard())

    system = llm.calls[0][0]["content"]
    assert r"D:\Projects" in system
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pytest tests/test_agent.py -v"
```
Expected: FAIL — `ModuleNotFoundError: No module named 'core.agent'`

- [ ] **Step 3: Write the implementation**

Create `core/agent.py`:

```python
"""The model → tool → model loop.

The LLM is given real tools and decides which to call and in what order, which
is what makes "go to d drive projects and list the folders" work as one
message. Destructive tools are routed through Guard first, so the model can
propose them but only the user can authorize them.
"""

import json
import logging

from core import paths, session, tools_registry

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 5

_SYSTEM = """You control the user's Windows machine through a WSL2 agent called MIMIR.

Use the provided tools to actually do what the user asks — never tell them to \
run a command themselves, and never claim you cannot navigate.

The user thinks in Windows paths (D:\\Projects). You may pass paths in any \
form: D:\\Projects, D:/Projects, /mnt/d/Projects, or "d drive projects". They \
all resolve correctly. Relative names resolve against the current folder.

The current folder is {cwd_win}.

Chain tools when a request needs more than one step. For example, "go to d \
drive projects and list the folders" is cd followed by list_dir.

When you have the answer, reply in plain text. Keep replies short — they are \
read on a phone. Show paths in Windows form."""


async def run(bot, chat_id: int, text: str, llm, guard, on_file=None) -> str:
    """Run the loop until the model answers. Returns the text to show the user.

    Raises RuntimeError when every LLM provider is unavailable, so the caller
    can fall back to the regex classifier.
    """
    cwd = session.get_cwd(chat_id)
    messages = [
        {"role": "system", "content": _SYSTEM.format(cwd_win=paths.to_windows(cwd))},
        {"role": "user", "content": text},
    ]

    denied: set[str] = set()
    collected: list[str] = []

    for iteration in range(MAX_ITERATIONS):
        response = await llm.ask_with_tools(messages, tools_registry.TOOL_SCHEMAS)
        calls = response.get("tool_calls") or []

        if not calls:
            answer = (response.get("text") or "").strip()
            if answer:
                return answer
            return "\n\n".join(collected) if collected else "No response."

        messages.append({
            "role": "assistant",
            "content": response.get("text") or "",
            "tool_calls": [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": json.dumps(c["args"])},
                }
                for c in calls
            ],
        })

        for call in calls:
            result = await _run_one(bot, chat_id, call, guard, denied, on_file)
            collected.append(result)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": result,
            })

        logger.info(f"Agent iteration {iteration + 1}: ran {[c['name'] for c in calls]}")

    return (
        "I could not finish that in "
        f"{MAX_ITERATIONS} steps. Here is what I gathered:\n\n"
        + "\n\n".join(collected[-3:])
    )


async def _run_one(bot, chat_id, call, guard, denied, on_file) -> str:
    name = call["name"]
    args = call.get("args") or {}

    if name in tools_registry.GATED_TOOLS:
        fingerprint = f"{name}:{json.dumps(args, sort_keys=True)}"
        if fingerprint in denied:
            # The model sometimes re-proposes a rejected action. Answer from
            # memory rather than prompting the user a second time.
            return f"The user already denied '{name}' with these arguments. Do not ask again."
        description = _describe(name, args)
        approved = await guard.request_permission(
            bot, chat_id, _guard_op(name), description, json.dumps(args)[:400],
        )
        if not approved:
            denied.add(fingerprint)
            return f"The user denied permission to {description}."

    # These two need the Telegram Bot object, so they are handled here rather
    # than in the registry, which is deliberately Telegram-agnostic.
    if name == "send_file":
        return await _send_file(chat_id, args, on_file)
    if name == "screenshot":
        return await _screenshot(on_file)

    return await tools_registry.execute(name, args, chat_id)


async def _send_file(chat_id, args, on_file) -> str:
    from tools import file_ops

    ok, target = file_ops.validate_path(
        str(args.get("path") or ""), cwd=session.get_cwd(chat_id),
    )
    if not ok:
        return f"Access denied: {args.get('path')}"
    path, error = file_ops.get_file_for_send(str(target))
    if error:
        return f"Cannot send {paths.to_windows(str(target))}: {error}"
    if on_file is None:
        return "No delivery channel available."
    await on_file(path)
    return f"Sent {paths.to_windows(path)} to the user."


async def _screenshot(on_file) -> str:
    from tools import screenshot as screenshot_tool

    path, error = await screenshot_tool.take_screenshot()
    if error or not path:
        return f"Screenshot failed: {error}"
    if on_file is None:
        return "No delivery channel available."
    await on_file(path)
    return "Screenshot sent to the user."


def _guard_op(name: str) -> str:
    """Map a tool name onto the operation names Guard already knows."""
    return {
        "write_file": "write_file",
        "delete_file": "delete_file",
        "run_script": "run_script",
        "shell_command": "run_script",
        "kill_process": "run_script",
    }.get(name, "run_script")


def _describe(name: str, args: dict) -> str:
    if name == "shell_command":
        return f"run: {args.get('command', '')}"
    if name == "kill_process":
        return f"kill process: {args.get('name', 'most recent')}"
    target = args.get("path", "")
    win = paths.to_windows(str(target)) if target else ""
    return {
        "write_file": f"write to {win}",
        "delete_file": f"delete {win}",
        "run_script": f"run script {win}",
    }.get(name, f"{name} {win}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pytest tests/test_agent.py -v"
```
Expected: PASS — all eight tests.

- [ ] **Step 5: Commit**

```bash
git add core/agent.py tests/test_agent.py
git commit -m "feat: add LLM tool-calling agent loop with permission gating"
```

---

### Task 7: Wire the agent into the bot

**Files:**
- Modify: `core/bot.py` — imports, `cmd_ls`, `cmd_tree`, `cmd_read`, `cmd_recent`, `cmd_open`, `handle_message`, `create_bot`; delete `_wsl_to_win`
- Modify: `tools/screenshot.py` — reuse `paths.to_windows`

**Interfaces:**
- Consumes: `agent.run`, `session.get_cwd/set_cwd`, `paths.normalize/to_windows` from Tasks 1, 3, 6.
- Produces: `/cd` and `/pwd` commands.

- [ ] **Step 1: Add the imports and helpers**

In `core/bot.py`, extend the core imports:

```python
from core import brain, agent, paths, session, tools_registry
```

(Keep the existing `from core import brain` line's other imports intact — merge rather than duplicate.)

Delete `_wsl_to_win` (lines 278-285) and replace every call to it with `paths.to_windows`.

Add this helper next to `_resolve_repo_path`:

```python
def _dir_arg(update, context) -> str:
    """Resolve a command's path argument against the chat's current folder."""
    chat_id = update.effective_chat.id
    cwd = session.get_cwd(chat_id)
    raw = " ".join(context.args) if context.args else ""
    return paths.normalize(raw, cwd) or cwd
```

- [ ] **Step 2: Make the path commands session-aware**

Replace `cmd_ls`:

```python
@authorized
async def cmd_ls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dirpath = _dir_arg(update, context)
    content = file_ops.list_dir(dirpath)
    await reply(update.message, f"{paths.to_windows(dirpath)}:\n\n{content}")
```

Replace `cmd_tree`:

```python
@authorized
async def cmd_tree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show directory tree. Usage: /tree <path> or /tree (current folder)"""
    dirpath = _dir_arg(update, context)
    result = file_ops.tree(dirpath)
    await reply(update.message, f"{paths.to_windows(dirpath)}:\n\n{result}")
```

Replace `cmd_recent`:

```python
@authorized
async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recently modified files. Usage: /recent or /recent <path>"""
    dirpath = _dir_arg(update, context)
    result = file_ops.recent_files(dirpath)
    await reply(update.message, result)
```

Replace `cmd_read` body's path handling:

```python
@authorized
async def cmd_read(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /read <filepath>")
        return
    filepath = _dir_arg(update, context)
    context.chat_data["last_file_path"] = filepath
    content = file_ops.read_file(filepath)
    await reply(update.message, f"{paths.to_windows(filepath)}:\n\n{content}")
```

Replace `cmd_open`:

```python
@authorized
async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open file/folder in Windows Explorer. Usage: /open <path> or /open (current folder)"""
    target = _dir_arg(update, context)
    win_path = paths.to_windows(target)
    cmd = f"powershell.exe -NoProfile -Command \"Start-Process '{win_path}'\""
    returncode, _, stderr = await process_ops.run_command(cmd, timeout=15)
    if returncode != 0:
        await update.message.reply_text(f"Failed to open {win_path}: {stderr.strip()[:200]}")
        return
    await update.message.reply_text(f"Opened: {win_path}")
```

- [ ] **Step 3: Add /cd and /pwd**

Add next to the other quality-of-life commands:

```python
@authorized
async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change the current folder. Usage: /cd <path>"""
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            f"Current folder: {paths.to_windows(session.get_cwd(chat_id))}"
        )
        return
    target = _dir_arg(update, context)
    ok, resolved = file_ops.validate_path(target, cwd=session.get_cwd(chat_id))
    if not ok:
        await update.message.reply_text(f"Access denied: {target}")
        return
    if not resolved.is_dir():
        await update.message.reply_text(f"Not a directory: {paths.to_windows(str(resolved))}")
        return
    session.set_cwd(chat_id, str(resolved))
    await update.message.reply_text(f"Now in: {paths.to_windows(str(resolved))}")


@authorized
async def cmd_pwd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the current folder."""
    cwd = session.get_cwd(update.effective_chat.id)
    await update.message.reply_text(paths.to_windows(cwd))
```

Register them in `create_bot`, next to the other quality-of-life handlers:

```python
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("pwd", cmd_pwd))
```

And add to the `post_init` command menu list, after the `open` entry:

```python
        BotCommand("cd", "Change current folder"),
        BotCommand("pwd", "Show current folder"),
```

- [ ] **Step 4: Route free text through the agent**

In `handle_message`, replace the block from `intent, args = brain.classify(text)` through the end of the function with:

```python
    await typing(update)
    thinking = await update.message.reply_text("Working...")

    async def deliver(path: str):
        """Send a file the agent produced back to the user."""
        with open(path, "rb") as handle:
            await update.message.reply_document(
                document=handle, filename=Path(path).name,
            )

    try:
        answer = await agent.run(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            text=text,
            llm=llm,
            guard=guard,
            on_file=deliver,
        )
    except Exception as exc:
        # Every provider is down. Fall back to the regex classifier so basic
        # navigation still works with no LLM at all.
        logger.warning(f"Agent loop unavailable ({exc}); falling back to regex.")
        try:
            await thinking.delete()
        except Exception:
            pass
        await _handle_message_regex(update, context, text)
        return

    try:
        await thinking.delete()
    except Exception:
        pass
    await reply_smart(update.message, answer or "No response.")
```

Rename the *existing* body (the `intent, args = brain.classify(text)` dispatch chain) into a new function directly above `handle_message`, so it survives as the fallback:

```python
async def _handle_message_regex(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Last-resort classifier used only when every LLM provider is down."""
    intent, args = brain.classify(text)
    # ... existing if/elif chain verbatim, unchanged ...
```

Keep the numbered-file-selection and notebook-edit-mode blocks at the top of `handle_message` exactly where they are — they run before the agent.

- [ ] **Step 5: Deduplicate screenshot's path converter**

In `tools/screenshot.py`, delete the local `_wsl_to_win` (lines 10-18) and import the shared one:

```python
from core.paths import to_windows as _wsl_to_win
```

- [ ] **Step 6: Verify the bot imports and starts**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -c 'from core.bot import create_bot; create_bot(); print(\"bot builds OK\")' && python -m pytest tests/ -v"
```
Expected: `bot builds OK` and the whole suite green.

- [ ] **Step 7: Commit**

```bash
git add core/bot.py tools/screenshot.py
git commit -m "feat: route free text through the agent loop, add /cd and /pwd"
```

---

### Task 8: Regression tests for the reported failures

**Files:**
- Create: `tests/test_regression_navigation.py`

**Interfaces:**
- Consumes: `agent.run`, `paths.normalize` from Tasks 1 and 6.

- [ ] **Step 1: Write the tests**

Create `tests/test_regression_navigation.py`:

```python
"""The exact messages that failed before LLM navigation existed.

Each asserts the tool sequence a correct model would produce is actually
executed, and that path arguments survive normalization intact.
"""

import pytest

from core import agent, paths, session

CWD = "/mnt/c/Users/rupes/ai-agent"


@pytest.fixture(autouse=True)
def temp_session(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / "s.json")
    monkeypatch.setattr(session, "_cache", None)
    yield


class ScriptedLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    async def ask_with_tools(self, messages, tools):
        return self._responses.pop(0)


class AlwaysApprove:
    async def request_permission(self, *a, **kw):
        return True


@pytest.mark.parametrize("raw,expected", [
    # "list all folders in D:/Projects" used to yield "/Projects"
    ("D:/Projects", "/mnt/d/Projects"),
    # "go to d drive and projects folder" used to yield "/mnt/d/it"
    ("d drive projects", "/mnt/d/projects"),
    # "show me all folders in d drive projects" used to drop "projects"
    (r"D:\Projects", "/mnt/d/Projects"),
])
def test_paths_that_used_to_be_mangled(raw, expected):
    assert paths.normalize(raw, CWD) == expected


async def test_go_to_d_drive_projects_and_list(monkeypatch):
    """The original failing message, end to end through the loop."""
    executed = []

    async def fake_execute(name, args, chat_id):
        executed.append((name, args))
        return "ok"

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    llm = ScriptedLLM([
        {"text": None, "tool_calls": [
            {"id": "1", "name": "cd", "args": {"path": r"D:\Projects"}},
        ]},
        {"text": None, "tool_calls": [
            {"id": "2", "name": "list_dir", "args": {}},
        ]},
        {"text": "Those are the folders in D:\\Projects.", "tool_calls": []},
    ])

    out = await agent.run(
        bot=None, chat_id=7,
        text="go to d drive and projects folder and send me list of all folders in it",
        llm=llm, guard=AlwaysApprove(),
    )

    assert [name for name, _ in executed] == ["cd", "list_dir"]
    assert "D:\\Projects" in out


async def test_open_in_explorer_is_not_a_directory_listing(monkeypatch):
    """'open d drive projects in explorer' used to classify as list_dir."""
    executed = []

    async def fake_execute(name, args, chat_id):
        executed.append(name)
        return "Opened D:\\Projects in Explorer."

    monkeypatch.setattr(agent.tools_registry, "execute", fake_execute)

    llm = ScriptedLLM([
        {"text": None, "tool_calls": [
            {"id": "1", "name": "open_explorer", "args": {"path": "d drive projects"}},
        ]},
        {"text": "Opened it.", "tool_calls": []},
    ])

    await agent.run(
        bot=None, chat_id=7,
        text="open d drive projects in explorer",
        llm=llm, guard=AlwaysApprove(),
    )
    assert executed == ["open_explorer"]
```

- [ ] **Step 2: Run the full suite**

Run:
```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/rupes/ai-agent && source ~/.venvs/ai-agent/bin/activate && python -m pytest tests/ -v"
```
Expected: PASS — every test across all six files.

- [ ] **Step 3: Commit**

```bash
git add tests/test_regression_navigation.py
git commit -m "test: add regression coverage for the reported navigation failures"
```

---

### Task 9: Live verification

**Files:** none — this is a manual check against the running bot.

- [ ] **Step 1: Sync to the deployed copy**

```bash
cd /d/Projects/mimir-telegram-agent
for f in core/paths.py core/session.py core/tools_registry.py core/agent.py \
         core/llm_cascade.py core/bot.py tools/file_ops.py tools/screenshot.py \
         requirements.txt pytest.ini .gitignore; do
  cp "$f" "/c/Users/rupes/ai-agent/$f"
done
cp -r tests /c/Users/rupes/ai-agent/
diff -rq --exclude=.git --exclude=__pycache__ --exclude=logs \
     --exclude=screenshot.png --exclude=.persona --exclude=.sessions.json \
     /c/Users/rupes/ai-agent /d/Projects/mimir-telegram-agent
```

- [ ] **Step 2: Restart and confirm startup**

```bash
wsl -d Ubuntu -- bash -c "sudo systemctl restart ai-agent && sleep 5 && systemctl is-active ai-agent"
```
Expected: `active`. If the unit is not installed yet, run
`timeout 30 python3 main.py` from the project directory instead and confirm it
reaches `Application started`.

- [ ] **Step 3: Exercise it from Telegram**

Send each of these and confirm the described behaviour:

| Message | Expected |
|---|---|
| `go to d drive and projects folder and send me list of all folders in it` | Lists folders in `D:\Projects` |
| `/pwd` | `D:\Projects` |
| `/ls` | Lists `D:\Projects`, **not** the ai-agent folder |
| `open it in explorer` | Explorer opens `D:\Projects` on the desktop |
| `what python files are in here` | Lists `.py` files under the current folder |
| `delete something.txt` | Approve/Deny keyboard appears before anything happens |

- [ ] **Step 4: Commit any fixes found**

```bash
git add -A
git commit -m "fix: address issues found in live verification"
```

---

## Self-Review

**Spec coverage:** `core/paths.py` → Task 1. `validate_path` hardening → Task 2. `core/session.py` → Task 3. Tool surface and gated set → Task 4. `ask_with_tools` across three providers → Task 5. Agent loop, iteration cap, denied-call handling → Task 6. Bot wiring, `/cd`, `/pwd`, path-aware commands, regex fallback → Task 7. Test plan → Tasks 1-3, 6, 8. Live check → Task 9. No spec section is unimplemented.

**Naming consistency:** The spec names the registry module `core/agent.py`'s collaborator without fixing a filename; this plan uses `core/tools_registry.py` throughout (Tasks 4, 6, 7, 8). `validate_path(filepath, cwd=None)` is defined in Task 2 and called with that signature in Tasks 4, 6, 7. `paths.normalize(text, cwd)` and `paths.to_windows(path)` are defined in Task 1 and used unchanged thereafter. `session.get_cwd/set_cwd/reset` and `SESSION_FILE` are defined in Task 3 and monkeypatched by that exact name in Tasks 4, 6, 8.

**Known deviation from the spec:** the spec's `normalize` returns `None` for uninterpretable input. In practice anything non-empty is treated as a relative name, and `validate_path`'s `ALLOWED_PATHS` check is what rejects bad targets. `None` is returned only for empty input. Task 1's tests encode this, and it is the safer behaviour — an unparseable string becomes a path that fails the allow-list rather than silently succeeding.
