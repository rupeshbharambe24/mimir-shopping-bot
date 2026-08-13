#!/usr/bin/env bash
# RAVEN launcher. Called from "Start RAVEN.bat" on the Windows side.
#
# Deliberately uses NO sudo. When the ai-agent unit is installed and enabled,
# systemd starts it automatically as soon as the WSL distro boots, so this
# script only has to wait for it and report. If the unit isn't installed, it
# falls back to running the agent in the foreground.

PROJECT_DIR="/mnt/c/Users/rupes/ai-agent"
VENV_DIR="$HOME/.venvs/ai-agent"
UNIT="ai-agent.service"

run_directly() {
    echo "[..] Starting RAVEN in the foreground (Ctrl+C to stop)..."
    cd "$PROJECT_DIR" || { echo "[!!] Cannot cd to $PROJECT_DIR"; exit 1; }
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate" || { echo "[!!] venv missing at $VENV_DIR — run ./setup.sh"; exit 1; }
    exec python3 main.py
}

# Already up? Nothing to do.
if systemctl is-active --quiet "$UNIT" 2>/dev/null; then
    echo "[OK] RAVEN service is already running."
    echo "     Logs:  wsl -d Ubuntu -- journalctl -u ai-agent -f"
    exit 0
fi

# Unit not installed at all -> foreground fallback.
if ! systemctl cat "$UNIT" >/dev/null 2>&1; then
    echo "[i]  Service not installed — see README for the one-time install commands."
    run_directly
fi

# Unit exists. If it's enabled, systemd is bringing it up as the distro boots;
# give it a few seconds rather than racing it.
echo "[..] Waiting for the RAVEN service to come up..."
for _ in $(seq 1 20); do
    if systemctl is-active --quiet "$UNIT"; then
        echo "[OK] RAVEN service started."
        echo "     Logs:  wsl -d Ubuntu -- journalctl -u ai-agent -f"
        exit 0
    fi
    sleep 1
done

echo "[!]  Service did not become active in 20s. Last few log lines:"
journalctl -u "$UNIT" -n 15 --no-pager 2>/dev/null || echo "     (no journal access)"
echo
run_directly
