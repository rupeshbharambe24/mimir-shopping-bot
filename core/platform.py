"""The single place that knows whether MIMIR is running on macOS or in WSL2.

MIMIR was built for a two-machine arrangement: the agent ran inside a WSL2
Ubuntu VM while the screen, clipboard, file manager and GPU all belonged to the
Windows host next door. Everything visual therefore had to be shelled out to
``powershell.exe``, and every path had to be translated across the /mnt bridge.

macOS has no such split -- the agent, the desktop, the GPU and Ollama are all
the same machine -- so on a Mac these operations collapse into native BSD
commands. Rather than fork the project, every host-specific operation lives
here behind one name, and callers stay platform-agnostic.

Imports nothing from the rest of the project, so any module may import it
without risking a cycle.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _detect_wsl() -> bool:
    """True when running inside a WSL distro (not plain Linux)."""
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


IS_MAC = sys.platform == "darwin"
IS_WSL = _detect_wsl()
IS_LINUX = sys.platform == "linux" and not IS_WSL

NAME = "macOS" if IS_MAC else "WSL2" if IS_WSL else "Linux"


# ── low-level exec ────────────────────────────────────────────────
# Deliberately argv-only: no shell is ever involved, so a path or filename
# containing shell metacharacters cannot inject a command.

async def _exec(argv: list[str], timeout: int = 15,
                env: dict | None = None,
                stdin_data: bytes | None = None) -> tuple[int, str, str]:
    """Run a program by argv. Returns (returncode, stdout, stderr)."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_data), timeout=timeout
        )
        return (
            proc.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        return -1, "", f"Timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", f"{argv[0]} not found on this system ({NAME})"
    except Exception as exc:
        return -1, "", str(exc)


def _wslenv_with(**pairs: str) -> dict:
    """os.environ plus `pairs`, registered in WSLENV so they cross into Win32
    processes. Without this, vars set in WSL are invisible to powershell.exe."""
    env = dict(os.environ)
    env.update(pairs)
    existing = env.get("WSLENV", "")
    additions = ":".join(pairs)
    env["WSLENV"] = f"{existing}:{additions}" if existing else additions
    return env


# ── screen capture ────────────────────────────────────────────────

_PS_SCREENSHOT = """
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$screens = [System.Windows.Forms.Screen]::AllScreens
$top = ($screens | ForEach-Object { $_.Bounds.Top } | Measure-Object -Minimum).Minimum
$left = ($screens | ForEach-Object { $_.Bounds.Left } | Measure-Object -Minimum).Minimum
$width = ($screens | ForEach-Object { $_.Bounds.Right } | Measure-Object -Maximum).Maximum
$height = ($screens | ForEach-Object { $_.Bounds.Bottom } | Measure-Object -Maximum).Maximum
$bounds = [System.Drawing.Rectangle]::FromLTRB($left, $top, $width, $height)
$bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bmp.Save($env:MIMIR_SHOT_PATH)
$g.Dispose()
$bmp.Dispose()
"""


async def capture_screen(dest: str, timeout: int = 20) -> tuple[int, str, str]:
    """Capture the desktop to `dest` (a native path for this host)."""
    if IS_MAC:
        # -x silences the shutter sound; -t png is explicit about the format.
        # Captures the main display: screencapture writes one file per display
        # and the bot has a single file to send.
        return await _exec(
            ["screencapture", "-x", "-t", "png", dest], timeout=timeout
        )

    # WSL: the destination travels as an env var and PowerShell reads it as a
    # literal string, so a path with PowerShell metacharacters cannot inject.
    env = _wslenv_with(MIMIR_SHOT_PATH=dest)
    return await _exec(
        ["powershell.exe", "-NoProfile", "-Command", _PS_SCREENSHOT],
        timeout=timeout,
        env=env,
    )


def screenshot_permission_hint() -> str:
    """Actionable next step when a capture comes back blank or fails."""
    if IS_MAC:
        return (
            "macOS requires Screen Recording permission. Grant it to the app "
            "that launched MIMIR (Terminal, iTerm, or the launchd agent) under "
            "System Settings > Privacy & Security > Screen Recording, then "
            "restart MIMIR. Without it screencapture succeeds but returns only "
            "the wallpaper."
        )
    return "powershell.exe not reachable - is this really a WSL distro?"


# ── open a path in the desktop file manager ───────────────────────

async def open_path(native_path: str, timeout: int = 15) -> tuple[int, str, str]:
    """Reveal a file or folder on the user's desktop."""
    if IS_MAC:
        # `open` takes the path as a plain argv element; nothing parses it.
        return await _exec(["open", native_path], timeout=timeout)

    env = _wslenv_with(MIMIR_OPEN_PATH=native_path)
    return await _exec(
        ["powershell.exe", "-NoProfile", "-Command",
         "Start-Process -FilePath $env:MIMIR_OPEN_PATH"],
        timeout=timeout,
        env=env,
    )


async def open_url(url: str, timeout: int = 15) -> tuple[int, str, str]:
    """Open a URL in the user's default browser."""
    if IS_MAC:
        return await _exec(["open", url], timeout=timeout)
    env = _wslenv_with(MIMIR_OPEN_URL=url)
    return await _exec(
        ["powershell.exe", "-NoProfile", "-Command",
         "Start-Process -FilePath $env:MIMIR_OPEN_URL"],
        timeout=timeout,
        env=env,
    )


# ── clipboard ─────────────────────────────────────────────────────

async def copy_to_clipboard(text: str, timeout: int = 10) -> tuple[int, str, str]:
    """Put `text` on the desktop clipboard. Text goes over stdin, never a shell."""
    argv = ["pbcopy"] if IS_MAC else ["clip.exe"]
    return await _exec(argv, timeout=timeout, stdin_data=text.encode("utf-8"))


# ── dev-server discovery ──────────────────────────────────────────
# On macOS the agent and the dev server share one loopback interface, so a
# plain TCP connect is enough. Under WSL2 they do not: the VM has its own
# network namespace and 127.0.0.1 means the VM, not the Windows host -- which
# is why the WSL branch has to ask PowerShell to do the probing.

DEV_PORTS = [3000, 3001, 5173, 5174, 4200, 8000, 8001,
             5000, 5500, 4000, 1234, 8080, 8081, 8888]


async def _tcp_open(port: int, timeout: float = 1.0) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _ps_port_script(lines: list[str], workspace: Path,
                          stem: str, timeout: int) -> str:
    """Run a generated PowerShell snippet from a temp .ps1 and return stdout."""
    from core import paths  # local import: paths is WSL-only helper code

    script_path = workspace / f"{stem}.ps1"
    script_path.write_text("\n".join(lines), encoding="utf-8")
    try:
        rc, stdout, _ = await _exec(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", paths.to_windows(str(script_path))],
            timeout=timeout,
        )
        return stdout if rc == 0 or stdout else ""
    finally:
        try:
            script_path.unlink()
        except Exception:
            pass


async def scan_open_ports(workspace: Path) -> set[int]:
    """Which dev ports are already listening (for before/after comparison)."""
    if IS_MAC:
        results = await asyncio.gather(*(_tcp_open(p) for p in DEV_PORTS))
        return {p for p, is_open in zip(DEV_PORTS, results) if is_open}

    lines = (
        ["foreach ($p in @(" + ",".join(str(p) for p in DEV_PORTS) + ")) {",
         "  try {",
         "    $tcp = New-Object System.Net.Sockets.TcpClient",
         "    $tcp.Connect('127.0.0.1', $p)",
         "    $tcp.Close()",
         "    Write-Output $p",
         "  } catch {}",
         "}"]
    )
    out = await _ps_port_script(lines, workspace, "_portscan_snap", 15)
    return {int(p.strip()) for p in out.split("\n") if p.strip().isdigit()}


async def find_dev_server(workspace: Path,
                          exclude_ports: set[int] | None = None) -> str | None:
    """Find a port serving HTML (a dev server, not a database or bare API)."""
    ports = [p for p in DEV_PORTS if not exclude_ports or p not in exclude_ports]
    if not ports:
        return None

    if IS_MAC:
        import aiohttp

        async def _serves_html(session, port: int) -> bool:
            try:
                async with session.get(
                    f"http://127.0.0.1:{port}",
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as resp:
                    if resp.status >= 400:
                        return False
                    ctype = resp.headers.get("Content-Type", "").lower()
                    if "text/html" in ctype:
                        return True
                    body = (await resp.content.read(2048)).decode(
                        "utf-8", errors="replace"
                    ).lower()
                    return any(m in body for m in
                               ("<html", "<!doctype", "<div", "<script"))
            except Exception:
                return False

        async with aiohttp.ClientSession() as session:
            checks = await asyncio.gather(
                *(_serves_html(session, p) for p in ports)
            )
        # Preserve DEV_PORTS priority order rather than whichever answered first.
        for port, ok in zip(ports, checks):
            if ok:
                return f"http://localhost:{port}"
        return None

    lines = [
        "foreach ($p in @(" + ",".join(str(p) for p in ports) + ")) {",
        "  try {",
        "    $r = Invoke-WebRequest -Uri \"http://127.0.0.1:$p\" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop",
        "    $ct = $r.Headers['Content-Type']",
        "    if ($r.StatusCode -lt 400 -and ($r.Content -match '<html|<!doctype|<div|<script|text/html' -or $ct -match 'text/html')) {",
        "      Write-Output $p; exit",
        "    }",
        "  } catch {}",
        "}",
    ]
    out = await _ps_port_script(lines, workspace, "_portscan", 30)
    port = out.strip()
    return f"http://localhost:{port}" if port.isdigit() else None


# ── GPU ───────────────────────────────────────────────────────────

async def gpu_summary() -> str | None:
    """One-line GPU description, or None when it cannot be determined."""
    if IS_MAC:
        # Apple Silicon shares one pool of memory between CPU and GPU, so there
        # is no discrete VRAM figure to report. Per-process GPU utilisation
        # needs powermetrics, which needs sudo -- deliberately not attempted.
        rc, out, _ = await _exec(
            ["system_profiler", "SPDisplaysDataType", "-json"], timeout=15
        )
        name, cores = None, None
        if rc == 0 and out.strip():
            try:
                import json
                for gpu in json.loads(out).get("SPDisplaysDataType", []):
                    name = gpu.get("sppci_model") or gpu.get("_name")
                    cores = gpu.get("sppci_cores")
                    if name:
                        break
            except Exception:
                pass
        rc_mem, mem_out, _ = await _exec(["sysctl", "-n", "hw.memsize"], timeout=5)
        unified = ""
        if rc_mem == 0 and mem_out.strip().isdigit():
            unified = f", {int(mem_out.strip()) // (1024 ** 3)}GB unified memory"
        if name:
            core_str = f" ({cores} cores)" if cores else ""
            return f"{name}{core_str}{unified}"
        return f"Apple GPU{unified}" if unified else None

    rc, out, _ = await _exec(
        ["powershell.exe", "-NoProfile", "-Command",
         "nvidia-smi --query-gpu=name,memory.used,memory.total "
         "--format=csv,noheader,nounits"],
        timeout=10,
    )
    if rc != 0 or not out.strip():
        return None
    parts = [p.strip() for p in out.strip().split("\n")[0].split(",")]
    if len(parts) >= 3:
        return f"{parts[0]} ({parts[1]}/{parts[2]} MB)"
    return out.strip().split("\n")[0]


# ── prompt text ───────────────────────────────────────────────────
# The model was previously told, in several prompts, that it lives in WSL2 and
# should shell out to powershell.exe for anything Windows-side. Left unchanged
# on a Mac it keeps emitting powershell.exe, so the description of the host is
# selected here alongside the code that actually implements it.

_MAC_ENV = """- Running natively on macOS (Apple Silicon), a Unix system
- The user's files live under /Users, external volumes under /Volumes
- The agent, the desktop and the GPU are all the SAME machine -- there is no
  VM boundary and nothing needs to be bridged
- Use ordinary POSIX tools: ls, find, grep, open, pbcopy, screencapture
- Homebrew is at /opt/homebrew/bin -- prefer `brew` for installing software"""

_WSL_ENV = """- Running in WSL2 on Ubuntu, Windows drives at /mnt/c/ and /mnt/d/
- Can call powershell.exe for Windows tasks (open browser, etc.)
- The agent is in a Linux VM; the desktop and GPU belong to the Windows host"""

_MAC_RULES = """- Node projects run natively: cd into the directory and run npm directly.
- Virtualenvs use bin/, not Scripts/:  source .venv/bin/activate
  or call the interpreter directly:    /path/to/venv/bin/python script.py
- Paths are case-insensitive but case-preserving on the default APFS volume;
  match the real casing when you can.
- BSD userland, not GNU. `sed -i` needs an argument (`sed -i ''`), and `find`
  has no -printf. Prefer portable flags.
- /bin/bash is 3.2. Avoid bash 4+ syntax (mapfile, associative arrays, ${x^^}).
- Spotlight beats find for locating files:
  mdfind -name "prithvi" -onlyin ~/Projects | head -1
- Ports 5000 and 7000 are taken by the system AirPlay Receiver. If a server
  cannot bind to 5000, that is why -- pick another port or turn AirPlay
  Receiver off in System Settings > General > AirDrop & Handoff."""

_WSL_RULES = """- Projects with node_modules installed on Windows CANNOT run npm/node from WSL.
  ALWAYS use powershell.exe for Node.js projects:
  powershell.exe -Command "cd 'D:\\\\projects\\\\myapp'; npm run dev"
- When passing WSL paths to powershell.exe, convert /mnt/X/... to X:\\\\...
- Windows virtualenvs use Scripts/python.exe, not bin/python"""

ENVIRONMENT = _MAC_ENV if IS_MAC else _WSL_ENV
SHELL_RULES = _MAC_RULES if IS_MAC else _WSL_RULES

# How the agent refers to the user's machine in conversation.
HOST_DESCRIPTION = (
    "your Mac" if IS_MAC else "your Windows machine"
)
FILE_MANAGER = "Finder" if IS_MAC else "Windows Explorer"
PATH_STYLE = (
    "macOS paths (/Users/you/Projects)" if IS_MAC
    else "Windows paths (D:\\\\Projects)"
)

# Concrete examples the planner copies from. Getting these wrong is the most
# common way a plan fails on the wrong host: the model happily emits a
# Windows-shaped command on a Mac if the example it was shown looked like one.
SEARCH_EXAMPLE = (
    'mdfind -name "prithvi" -onlyin ~/Projects | head -1\n'
    '  (or: find ~/Projects -maxdepth 3 -iname "*prithvi*" -type d 2>/dev/null | head -1)'
    if IS_MAC else
    'find /mnt/d/projects -maxdepth 2 -iname "*prithvi*" -type d 2>/dev/null | head -1'
)

DEV_SERVER_EXAMPLE = (
    'cd "$project_path" && npm run dev &\n  sleep 15'
    if IS_MAC else
    'powershell.exe -Command "cd \'D:\\\\path\'; npm run dev" &\n  sleep 15'
)
