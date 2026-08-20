# LLM-Driven Navigation for MIMIR

**Date:** 2026-08-13
**Status:** Approved

## Problem

MIMIR classifies free-text messages with a regex table in `core/brain.py`. It
misfires on ordinary phrasing, and there is no concept of "where I am", so
navigation is effectively unusable from a phone.

Measured against the real classifier:

| Message | Current result |
|---|---|
| `go to d drive and projects folder and send me list of all folders in it` | `send_file` searching `/mnt/d/it` — the word "it" became a folder name |
| `show me all folders in d drive projects` | `list_dir /mnt/d` — drops `projects` |
| `list all folders in D:/Projects` | `list_dir /Projects` — drops the drive |
| `open d drive projects in explorer` | `list_dir /mnt/d` — ignores "explorer" |
| `what is inside my projects folder on d drive` | `ask_llm` — chats instead of navigating |

Three independent defects underlie this:

1. **No Windows→WSL path translation.** `/ls D:\Projects` resolves relative to
   the process working directory, producing
   `/mnt/c/Users/rupes/ai-agent/D:\Projects`, then reports "Directory not
   found". Only `/mnt/d/Projects` works. The user works on Windows and types
   Windows paths.

2. **No current directory.** `cmd_ls` and `cmd_tree` default to
   `Config.WORKSPACE` whenever no argument is given, so bare `/ls` always lists
   `/mnt/c/Users/rupes/ai-agent`. "Go to D drive, then list" cannot be
   expressed because nothing holds the "go to".

3. **`validate_path` accepts unresolvable paths.** `Path("D:\\Projects").resolve()`
   silently becomes workspace-relative, so `validate_path` returns
   `allowed=True` for a path the user never meant. Bad input becomes a wrong
   location rather than an error.

`/open` compounds this: with no argument it opens `WORKSPACE`, and
`_wsl_to_win` only rewrites strings starting with `/mnt/`, passing anything
else through unchanged.

## Approach

Replace regex intent classification with an **LLM tool-calling loop**. The model
receives real tools and chooses and chains them itself. Regex is retained only
as a last-resort fallback when every LLM provider is unavailable.

Rejected alternatives:

- **Single-shot LLM router** (message → one structured action). Simpler, but one
  message equals one action, so chained requests need multiple messages. The
  user's own example is inherently multi-step.
- **Repair the regex only.** No latency or cost, works offline, but stays
  brittle for unanticipated phrasing — which is the complaint being raised.

## Components

### `core/paths.py` (new)

Single authority on path interpretation.

- `normalize(text: str, cwd: str) -> str | None`
  Accepts `D:\Projects`, `D:/Projects`, `/mnt/d/Projects`, `d drive projects`,
  and relative names. Returns an absolute WSL path, or `None` when the input
  cannot be interpreted as a path. Relative input resolves against `cwd`.
- `to_windows(path: str) -> str`
  `/mnt/d/Projects` → `D:\Projects`. Non-`/mnt` paths (e.g. `/home/user`) are
  returned as WSL paths, since they have no Windows equivalent.

`tools/file_ops.validate_path` is changed to reject input that `normalize`
cannot resolve, closing defect 3. It must not fall back to
working-directory-relative resolution.

### `core/session.py` (new)

Per-chat state, persisted as JSON under `Config.WORKSPACE` so it survives
restarts and the `/stop` command.

- `get_cwd(chat_id) -> str` — defaults to `Config.WORKSPACE` on first use.
- `set_cwd(chat_id, path) -> None`

Writes are atomic (write temp, then replace) so a crash mid-write cannot leave
a corrupt state file. A corrupt or unreadable file degrades to defaults rather
than crashing the bot.

### `core/agent.py` (new)

The tool-calling loop.

**Auto-executed** (no prompt): `cd`, `list_dir`, `tree`, `find_files`,
`read_file`, `send_file`, `open_explorer`, `recent_files`, `status`,
`screenshot`.

**Gated** through the existing `Guard` (Approve/Deny keyboard, unchanged):
`write_file`, `delete_file`, `run_script`, `shell_command`, `kill_process`.

Loop: user message plus tool schemas → model returns tool calls → execute →
append results → repeat until the model returns a final text answer. Capped at
**5 iterations**; on exhaustion, return what was gathered plus a note that the
task did not converge.

Tool failures are returned to the model as tool results, not raised. A "directory
not found" lets the model correct itself instead of dead-ending at the user.

A denied permission returns a result stating the user denied it. The model must
not retry the same gated call; the loop enforces this by refusing a gated tool
whose identical arguments were already denied in this run.

### `core/llm_cascade.py` (extended)

Add `ask_with_tools(messages, tools)`. Providers are tried in order:

1. **Groq** — OpenAI-compatible `tools` / `tool_calls`.
2. **Gemini** — `functionDeclarations` / `functionCall`.
3. **Ollama** — `/api/chat` with `tools`, model `qwen2.5:7b`.

Each provider's response normalizes to one internal shape:

```python
{"text": str | None, "tool_calls": [{"id": str, "name": str, "args": dict}]}
```

`agent.py` never learns which provider answered. The existing `ask()` cascade is
left as is.

### `core/bot.py` (modified)

- `handle_message` calls the agent loop instead of `brain.classify`. If every
  provider fails, fall back to `brain.classify` so basic navigation still works
  with no LLM at all.
- `/ls`, `/tree`, `/open`, `/read`, `/recent`, `/find` run input through
  `paths.normalize` and default to the **session current directory** rather than
  `WORKSPACE`.
- New `/cd` and `/pwd`.
- `/open` with no argument opens the current directory.
- `_wsl_to_win` is deleted; callers use `paths.to_windows`.

Replies display Windows-style paths, since the user operates on Windows.

## Data flow

```
Telegram message
  └─ handle_message
       └─ agent.run(chat_id, text)
            ├─ session.get_cwd(chat_id)
            ├─ llm_cascade.ask_with_tools(messages, TOOL_SCHEMAS)
            ├─ for each tool_call:
            │    ├─ gated?  → guard.request_permission → Approve/Deny
            │    └─ execute → paths.normalize → tools/* → result
            └─ loop until final text (max 5)
```

## Error handling

| Failure | Behaviour |
|---|---|
| Path uninterpretable | Tool returns an error result; model retries or explains |
| Path outside `ALLOWED_PATHS` | Tool returns "access denied"; never executed |
| Permission denied | Result records the denial; that gated call is not retried |
| All LLM providers down | Fall back to `brain.classify` |
| Loop hits 5 iterations | Return gathered output plus a non-convergence note |
| Corrupt session file | Degrade to defaults |

## Testing

`pytest` is added; the repo currently has no test framework.

- **`tests/test_paths.py`** — normalization table covering `D:\`, `D:/`,
  `d drive`, `d drive projects`, `/mnt/d/...`, relative-to-cwd, and
  non-`/mnt` paths. Includes the traversal cases that currently pass wrongly,
  asserting they are now rejected.
- **`tests/test_session.py`** — cwd persistence across reload; corrupt file
  degrades to defaults.
- **`tests/test_agent.py`** — a fake LLM returning canned tool calls. Asserts
  the correct tool and arguments are invoked, that gated tools cannot execute
  without approval, that a denied call is not retried, and that the iteration
  cap holds.
- **Regression** — the five messages in the Problem table become test cases
  asserting the correct tool sequence.

## Scope

`core/bot.py` is 2343 lines. Path and session helpers move out as described
because this feature needs them. No broader refactor is in scope.

## Accepted trade-offs

- Every plain-text message costs an LLM round trip (~1–2s via Groq). Slash
  commands stay instant and regex-free for when speed matters.
- Directory listings and file contents become tool results sent to Groq/Gemini.
  This is the same exposure as today's `/ask`, but it now occurs during ordinary
  navigation. Setting Ollama first in the cascade keeps navigation local at the
  cost of speed and reliability.
