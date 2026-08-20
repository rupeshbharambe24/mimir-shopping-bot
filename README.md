# MIMIR - Telegram AI Agent

> Control your development machine from anywhere. Send a mimir.

A personal AI agent that runs on **macOS or WSL2**, connects to cloud + local LLMs, and gives you full remote control of your dev environment through Telegram — complete with a permission system so it can't do anything destructive without your explicit approval.

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20WSL2-lightgrey)
![Telegram](https://img.shields.io/badge/Telegram-Bot%20API-blue)
![LLMs](https://img.shields.io/badge/LLMs-Groq%20%7C%20Gemini%20%7C%20Ollama-purple)
![License](https://img.shields.io/badge/License-MIT-green)

## What It Does

**Send a message. Your machine does the work.**

- Ask LLM questions from your phone (Groq/Gemini/Ollama cascade)
- Run Python scripts and see output in real-time
- Execute Jupyter notebooks cell-by-cell with live output
- Start dev servers, auto-detect the port, open browser, take screenshots
- Find files, read code, manage git — all from Telegram
- Get GPU status, set reminders, monitor running processes
- **Every write/delete operation requires your tap-to-approve**

### Demo Flow

```
You:   "open my react project, run npm dev and screenshot it"

MIMIR: [Plans the steps, asks for approval]
You:   [Tap Approve]

MIMIR: [Starts the dev server]
       [Auto-detects port 5173]
       [Opens browser, waits for load]
       [Sends screenshot to your phone]
```

### Startup Dashboard

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
       R A V E N  v2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Time:    2026-04-18 18:30
  Groq:    ON
  Gemini:  ON
  Ollama:  ON
  Disk:    45GB free
  GPU:     Apple M5 (8 cores), 16GB unified memory

  All systems online.
  Send /help for commands.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## Architecture

**macOS** — everything is one machine:

```
Your Phone (Telegram)
        |
   Telegram Bot API
        |
     MIMIR Agent  ──── Ollama (Metal, unified memory)
        |
   macOS itself
   (files, GPU, Finder, browser, dev servers)
```

**WSL2** — the agent is in a VM, the desktop is not:

```
Your Phone (Telegram)
        |
   Telegram Bot API
        |
     MIMIR Agent (WSL2)
    /       |        \
Groq     Gemini    Ollama
(70B)    (Flash)   (7B fallback)
    \       |        /
     Windows Machine
     (files, GPU, browser, dev servers)
```

Everything host-specific — screen capture, clipboard, file manager, port
scanning, GPU, and the wording of the LLM prompts — lives in
[`core/platform.py`](core/platform.py). The rest of the codebase never
branches on the operating system.

**LLM Cascade**: Tries Groq first (fast, 70B model), falls back to Gemini Flash, then local Ollama. Automatic failover on rate limits or errors.

## Commands (40 total)

### LLM
| Command | Description | Approval |
|---------|-------------|----------|
| `/ask <prompt>` | Ask the LLM anything | Auto |
| `/code <prompt>` | Code-focused query | Auto |

### Files
| Command | Description | Approval |
|---------|-------------|----------|
| `/read <path>` | Read file contents | Auto |
| `/ls <path>` | List directory | Auto |
| `/send <path>` | Send file to you | Auto |
| `/find <name>` | Search for files across drives | Auto |
| `/tree <path>` | Directory tree view | Auto |
| `/recent` | Recently modified files | Auto |
| `/write <path>` | Write/create file | **Approval** |
| `/delete <path>` | Delete file | **Approval** |

### Process Management
| Command | Description | Approval |
|---------|-------------|----------|
| `/run <script.py>` | Run Python script | **Approval** |
| `/watch <script>` | Run with live streaming output | **Approval** |
| `/cmd <command>` | Run any shell command | **Approval** |
| `/tail <logfile>` | Monitor log file live | Auto |
| `/procs` | Show background processes | Auto |
| `/kill <name>` | Kill a background process | **Approval** |
| `/history` | Command history | Auto |

### Jupyter Notebooks
| Command | Description | Approval |
|---------|-------------|----------|
| `/nb open <path>` | Load a notebook | Auto |
| `/nb run` | Run all cells sequentially | **Approval** |
| `/nb run 5` | Run specific cell | **Approval** |
| `/nb run 5+` | Run from cell 5 onward | **Approval** |
| `/nb cell 5` | Show cell source code | Auto |
| `/nb edit 5` | Edit a cell | **Approval** |
| `/nb out 5` | Show cell output | Auto |
| `/nb env <path>` | Set Python environment | Auto |
| `/nb status` | Kernel status | Auto |
| `/nb stop` | Shutdown kernel | Auto |

### Git (local only, never pushes)
| Command | Description | Approval |
|---------|-------------|----------|
| `/commit <msg>` | Commit changes | **Approval** |
| `/diff` | Show uncommitted changes | Auto |
| `/gitlog` | Recent commits | Auto |
| `/undo` | Soft reset last commit | **Approval** |

### System
| Command | Description | Approval |
|---------|-------------|----------|
| `/do <task>` | Multi-step task (natural language) | **Approval** |
| `/screenshot` | Capture screen | Auto |
| `/status` | System + LLM provider status | Auto |
| `/gpu` | NVIDIA GPU usage + VRAM + temp | Auto |
| `/models` | List Ollama models | Auto |
| `/logs` | Agent logs | Auto |
| `/remind <min> <msg>` | Set a reminder | Auto |

### Utility
| Command | Description |
|---------|-------------|
| `/open <path>` | Reveal in Finder / Windows Explorer |
| `/clip <text>` | Copy to the system clipboard |
| `/bookmark <name> <path>` | Save a path shortcut |
| `/go <name>` | Navigate to bookmark |
| `/persona` | Switch AI personality |
| `/livestream start [N]` | Stream desktop screenshots every N seconds |
| `/livestream stop` | Stop livestream |
| `/remote` | Chrome Remote Desktop setup guide |
| `/stop` | Shutdown agent cleanly |

### Personality System

MIMIR comes with switchable AI personalities:

```
/persona              — show current + all presets
/persona mimir        — sharp, direct, darkly witty (default)
/persona formal       — professional, no personality
/persona mentor       — patient, teaches as it goes
/persona brutal       — extremely blunt, no sugar coating
/persona pirate       — nautical terms, fun but helpful
/persona zen          — calm, philosophical, minimalist
/persona set <prompt> — fully custom personality
/persona reset        — back to default
```

Persists across restarts. Create your own with `/persona set You are a sarcastic cat who judges every command`.

### Live Desktop Monitoring

Two ways to see your desktop remotely:

**Livestream (built-in)** — screenshots streamed directly to your Telegram chat:
```
/livestream start      — every 5 seconds
/livestream start 3    — every 3 seconds
/livestream stop       — end stream
```
Send commands and watch results in the same chat. Previous screenshot auto-deletes to keep the chat clean.

**Chrome Remote Desktop (recommended for full control)** — full interactive desktop access from your phone. Run `/remote` for setup instructions.

### Natural Language

You don't always need commands. Just type naturally:

- *"send me the training notebook from D drive"*
- *"find the config file"*
- *"run test.py"*
- *"take a screenshot"*
- *"what's running in the background?"*

### Shopping / Transactions

Backed by the [agentic-commerce](#shopping-agentic-commerce) sidecar.

| Command | Description | Approval |
|---------|-------------|----------|
| `/shop <request>` | Order things in plain English | Checkout only |
| `/compare <item>` | Compare prices across providers | Auto |
| `/cart` | Show the cart and total | Auto |
| `/providers` | List providers + connection state | Auto |
| `/connect <id>` | Log in to a provider | Auto |

## Setup

### Prerequisites

**macOS**
- macOS 12+ (Apple Silicon or Intel)
- Python 3.10+ (`brew install python`)
- [Ollama](https://ollama.com) — optional, the cloud cascade works without it

**Windows**
- Windows 10/11 with WSL2 (Ubuntu)
- Python 3.10+ in WSL
- [Ollama](https://ollama.com) installed on Windows

**Both**
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Your Telegram chat ID (from [@userinfobot](https://t.me/userinfobot))

### Quick Start

```bash
# Clone
git clone https://github.com/rupeshbharambe24/mimir-telegram-agent.git
cd mimir-telegram-agent

# Configure
cp .env.example .env
# Edit .env with your tokens and API keys

# WSL2 only — make Ollama on the Windows host reachable from the VM.
# Run in Windows PowerShell, then restart Ollama:
#   [System.Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")
# On macOS this step does not exist: Ollama is on localhost already.

# Setup (creates the venv, installs deps, tests connections,
# generates a launchd plist on macOS or a systemd unit on WSL)
bash setup.sh

# Run
source ~/.venvs/ai-agent/bin/activate
python3 main.py
```

### macOS: grant Screen Recording

`/screenshot` needs it, and this is the one thing that fails *silently* without
it — `screencapture` exits 0 and writes nothing, or captures only the wallpaper.

**System Settings → Privacy & Security → Screen Recording** → add the app that
launches MIMIR (Terminal, iTerm, or the launchd agent), then restart it.

### Get Free API Keys

| Provider | Model | Free Tier | Link |
|----------|-------|-----------|------|
| **Groq** | Llama 3.3 70B | 14K req/day | [console.groq.com](https://console.groq.com) |
| **Gemini** | Flash | 1500 req/day | [aistudio.google.com](https://aistudio.google.com/apikey) |
| **Ollama** | qwen, deepseek, etc. | Unlimited (local) | [ollama.com](https://ollama.com) |

### Run as Service

<details open>
<summary><b>macOS — launchd</b></summary>

`setup.sh` generates `com.mimir.agent.plist` with your paths filled in.

```bash
cp com.mimir.agent.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mimir.agent.plist
launchctl kickstart -k gui/$(id -u)/com.mimir.agent
launchctl print gui/$(id -u)/com.mimir.agent | head -20   # status
```

It is a **LaunchAgent**, not a LaunchDaemon, and that distinction matters: a
Daemon starts at boot but runs outside any login session with no access to the
window server, so `/screenshot` and `/open` would fail every time. An Agent
starts at login and has a GUI session.

The plist sets `PATH` explicitly. launchd hands a job a bare
`/usr/bin:/bin:/usr/sbin:/sbin`, so without it Homebrew, `node`, `npm` and
`ollama` are all invisible to `shell_command` — the most common reason a job
that works in Terminal fails under launchd.

To stop or remove:

```bash
launchctl kickstart -k gui/$(id -u)/com.mimir.agent   # restart
launchctl bootout gui/$(id -u)/com.mimir.agent        # stop + unload
```

Logs: `tail -f logs/agent.log` (and `logs/launchd.err.log` if it won't start).

</details>

<details>
<summary><b>WSL2 — systemd</b></summary>

One-time install. `chmod 644` matters: the file is copied off a Windows drive,
where everything is mode 777, and systemd refuses world-writable unit files.

```bash
sudo cp ai-agent.service /etc/systemd/system/ai-agent.service
sudo chmod 644 /etc/systemd/system/ai-agent.service
sudo systemctl daemon-reload
sudo systemctl enable ai-agent    # auto-start when the WSL distro boots
sudo systemctl start ai-agent
systemctl status ai-agent --no-pager
```

Once enabled, systemd starts MIMIR as soon as the distro boots, so neither
`Start MIMIR.bat` nor the login VBS needs `sudo`. Both call `start-mimir.sh`,
which waits for the service and falls back to a foreground run if it isn't
installed.

> **Never put `sudo` in the startup VBS.** It runs hidden, so sudo has no
> terminal to prompt on and blocks forever — MIMIR then silently never starts.

Logs: `journalctl -u ai-agent -f`

</details>

## Shopping (agentic-commerce)

MIMIR can search, compare, cart and transact across commerce providers (Swiggy
Instamart, plus a mock provider for offline testing) through the
**agentic-commerce** platform, which speaks each provider's MCP server behind one
canonical interface.

### Architecture

```
Telegram
   |
MIMIR agent loop  (Groq -> Gemini -> Ollama)
   |  commerce_* tools
   |  HTTP, loopback only
agentic-commerce BFF  :8011      <- separate process, separate venv
   |  ProviderAdapter
Swiggy Instamart MCP  /  Mock provider
```

The sidecar is a **separate process on purpose**. Its `requirements.txt` pins
`mcp` exactly because an unpinned bump silently broke its transport once;
keeping those pins out of MIMIR's environment means a provider-SDK problem
cannot take the bot down with it.

### Running it

```bash
# One-off:
bash start-commerce.sh              # creates its venv on first run

# Or as a LaunchAgent beside MIMIR:
cp com.mimir.commerce.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mimir.commerce.plist
```

Set `COMMERCE_DIR` if agentic-commerce is not at
`~/Projects/Experiments/agentic-commerce`.

### The money path

Checkout is the one commerce operation that requires your tap. Cart edits do
not — gating every "add milk" would make ordering unusable, so the friction sits
exactly at the point money moves.

Approving a bare "checkout" would tell you nothing about what you are buying, so
the prompt is built from a real cart snapshot:

```
PERMISSION REQUIRED

Action: place order on swiggy for ₹136.00
Details:
2 x Toned Milk @ ₹33.00
1 x Whole Wheat Bread @ ₹45.00
Total: ₹136.00

[ Approve ]  [ Deny ]
```

Five independent layers stand between the model and your money:

| # | Layer | What it stops |
|---|-------|---------------|
| 1 | `COMMERCE_ALLOW_LIVE` + the BFF's `DRY_RUN` | Both must agree before anything is real |
| 2 | `GATED_TOOLS` + `Guard` | No order without a tap on your phone |
| 3 | Cart-bound confirmation token | Single-use, expires in 300s, **void if the cart changes after you approve** |
| 4 | `CapabilityFlags.max_order_value` | Provider-declared ceiling (Swiggy: ₹1000) |
| 5 | `GuardedSwiggySession` | In-transport block; the last line even if a caller is confused |

Layer 3 is the subtle one. A plain "checkout is armed" boolean authorizes
*whatever the cart contains when the call lands* — so an agent that adds an item
between you reading the total and tapping Approve would spend against an
approval you never gave. Binding the token to a fingerprint of the exact cart you
were shown closes that window: if the basket moves, the token is dead and you are
asked again with the new total.

> **Start with `DRY_RUN=true`** in agentic-commerce's `.env` and leave
> `COMMERCE_ALLOW_LIVE=false` in MIMIR's until you have watched the whole flow
> against the mock provider.

## Permission System

MIMIR uses a **read-auto, write-approve** model:

- **Read operations** (read, ls, find, screenshot, status) execute immediately
- **Write operations** (write, delete, run, commit) send an inline keyboard:

```
PERMISSION REQUIRED
Action: Run script: train.py
[Approve] [Deny]
```

Tap to approve or deny. 5-minute timeout auto-denies.

## Project Structure

```
mimir-telegram-agent/
├── main.py                 # Entry point
├── config.py               # Configuration from .env
├── .env.example            # Template for secrets
├── requirements.txt        # Python dependencies
├── setup.sh                # One-command setup (macOS + WSL2)
├── start-mimir.sh          # Launcher (launchd / systemd aware)
├── com.mimir.agent.plist.template   # macOS LaunchAgent
├── ai-agent.service.template        # WSL2 systemd unit
├── core/
│   ├── platform.py         # Host adapter — the ONLY OS-aware module
│   ├── paths.py            # Path interpretation + display
│   ├── bot.py              # Telegram handlers (36 commands)
│   ├── agent.py            # LLM tool-calling loop
│   ├── tools_registry.py   # Tool schemas + dispatch
│   ├── llm_cascade.py      # Groq -> Gemini -> Ollama
│   ├── guard.py            # Permission system
│   ├── brain.py            # Natural language intent classifier
│   ├── monitor.py          # Script execution + error recovery
│   └── notebook.py         # Jupyter kernel manager
└── tools/
    ├── file_ops.py         # File operations + search
    ├── process_ops.py      # Process management + streaming
    ├── git_ops.py          # Git operations (local only)
    ├── screenshot.py       # Screen capture
    └── system_info.py      # System status
```

### Porting to another host

Everything OS-specific is behind `core/platform.py`: screen capture, opening
paths and URLs, the clipboard, port scanning, GPU reporting, and the prompt
text that tells the model what kind of machine it is on. Adding a host means
adding a branch there — no other module checks the platform.

The test suite reflects this. `tests/conftest.py` exposes `wsl_only` and
`mac_only` markers plus an `ALLOWED_ROOT` fixture, so host-specific assertions
skip cleanly instead of failing on the wrong machine.

## Contributing

Contributions welcome. Some ideas:

- **Docker support** — containerize for easier deployment
- **Voice messages** — TTS/STT via Telegram voice
- **More LLM providers** — Claude, local llama.cpp
- **Tests** — no test suite yet
- **Linux native** — plain Linux desktop support (`xdg-open`, `wl-copy`, `grim`)
- **Web dashboard** — optional web UI alongside Telegram
- **Multi-user** — support multiple authorized users

### How to Contribute

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/voice-messages`)
3. Make your changes
4. Test with your own bot
5. Submit a PR

## License

MIT License — see [LICENSE](LICENSE) for details.

---

Built by [@rupeshbharambe24](https://github.com/rupeshbharambe24)
