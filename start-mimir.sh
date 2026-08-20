#!/usr/bin/env bash
# MIMIR launcher, for macOS (launchd) and WSL2 (systemd).
#
# Deliberately uses NO sudo. When the service is installed it is already
# starting itself -- at login on macOS, at distro boot on WSL -- so this script
# only has to wait for it and report. If it is not installed, MIMIR runs in the
# foreground instead.

# Repo-relative, so the script works wherever the project is checked out. It
# used to hardcode one machine's path, which is exactly what broke on a second
# machine.
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$HOME/.venvs/ai-agent"

run_directly() {
    echo "[..] Starting MIMIR in the foreground (Ctrl+C to stop)..."
    cd "$PROJECT_DIR" || { echo "[!!] Cannot cd to $PROJECT_DIR"; exit 1; }
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate" || { echo "[!!] venv missing at $VENV_DIR — run ./setup.sh"; exit 1; }
    exec python3 main.py
}

if [ "$(uname -s)" = "Darwin" ]; then
    LABEL="com.mimir.agent"
    TARGET="gui/$(id -u)/$LABEL"

    if launchctl print "$TARGET" >/dev/null 2>&1; then
        if launchctl print "$TARGET" 2>/dev/null | grep -qE '^\s*state = running'; then
            echo "[OK] MIMIR LaunchAgent is already running."
            echo "     Logs:  tail -f $PROJECT_DIR/logs/agent.log"
            exit 0
        fi
        echo "[..] Agent is loaded but not running — starting it..."
        launchctl kickstart -k "$TARGET" 2>/dev/null && {
            echo "[OK] MIMIR started."
            echo "     Logs:  tail -f $PROJECT_DIR/logs/agent.log"
            exit 0
        }
        echo "[!]  kickstart failed. Last few log lines:"
        tail -n 15 "$PROJECT_DIR/logs/launchd.err.log" 2>/dev/null || echo "     (no launchd log yet)"
        echo
    else
        echo "[i]  LaunchAgent not installed — see README for the one-time install commands."
    fi
    run_directly
fi

# ── WSL2 / Linux: systemd ────────────────────────────────────────
UNIT="ai-agent.service"

if systemctl is-active --quiet "$UNIT" 2>/dev/null; then
    echo "[OK] MIMIR service is already running."
    echo "     Logs:  journalctl -u ai-agent -f"
    exit 0
fi

if ! systemctl cat "$UNIT" >/dev/null 2>&1; then
    echo "[i]  Service not installed — see README for the one-time install commands."
    run_directly
fi

echo "[..] Waiting for the MIMIR service to come up..."
for _ in $(seq 1 20); do
    if systemctl is-active --quiet "$UNIT"; then
        echo "[OK] MIMIR service started."
        echo "     Logs:  journalctl -u ai-agent -f"
        exit 0
    fi
    sleep 1
done

echo "[!]  Service did not become active in 20s. Last few log lines:"
journalctl -u "$UNIT" -n 15 --no-pager 2>/dev/null || echo "     (no journal access)"
echo
run_directly
