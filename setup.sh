#!/usr/bin/env bash
set -e

VENV_DIR="$HOME/.venvs/ai-agent"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

case "$(uname -s)" in
    Darwin) HOST_KIND="macos" ;;
    Linux)
        if grep -qi microsoft /proc/version 2>/dev/null; then
            HOST_KIND="wsl"
        else
            HOST_KIND="linux"
        fi
        ;;
    *) HOST_KIND="unknown" ;;
esac

echo "==============================="
echo "  MIMIR Setup  ($HOST_KIND)"
echo "==============================="
echo ""

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python 3 not found."
    if [ "$HOST_KIND" = "macos" ]; then
        echo "  Install with: brew install python@3.13"
    else
        echo "  Install with: sudo apt update && sudo apt install python3 python3-pip python3-venv"
    fi
    exit 1
fi
echo "[OK] Python 3 found: $(python3 --version)"

# Create the venv outside the project. On WSL this matters because a venv on an
# NTFS /mnt/c mount breaks; keeping the same layout on macOS means one set of
# instructions works on both.
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "[SETUP] Creating virtual environment at $VENV_DIR ..."
    mkdir -p "$(dirname "$VENV_DIR")"
    rm -rf "$VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi
echo "[OK] Virtual environment ready at $VENV_DIR"

# Activate and install dependencies
echo "[SETUP] Installing dependencies..."
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -r "$PROJECT_DIR/requirements.txt" -q
echo "[OK] Dependencies installed"

mkdir -p "$PROJECT_DIR/logs"

# Check .env
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "[ERROR] .env file not found! Copy .env.example to .env and fill it in."
    exit 1
fi
echo "[OK] .env file found"

# Test cloud LLM keys
echo ""
echo "[TEST] Checking LLM API keys..."
GROQ_KEY=$(python3 -c "import sys; sys.path.insert(0,'$PROJECT_DIR'); from config import Config; print(Config.GROQ_API_KEY[:8] + '...' if Config.GROQ_API_KEY else 'NOT SET')")
GEMINI_KEY=$(python3 -c "import sys; sys.path.insert(0,'$PROJECT_DIR'); from config import Config; print(Config.GEMINI_API_KEY[:8] + '...' if Config.GEMINI_API_KEY else 'NOT SET')")
echo "  Groq API key:   $GROQ_KEY"
echo "  Gemini API key: $GEMINI_KEY"
if [ "$GROQ_KEY" = "NOT SET" ] || [ "$GEMINI_KEY" = "NOT SET" ]; then
    echo "  [WARN] Set GROQ_API_KEY and GEMINI_API_KEY in .env for best performance"
fi

# Check Jupyter kernel
echo ""
echo "[TEST] Checking Jupyter..."
if python3 -c "import jupyter_client" 2>/dev/null; then
    echo "[OK] jupyter_client available"
else
    echo "[WARN] jupyter_client not found - /nb commands won't work until setup reruns"
fi

# Test Ollama connectivity. Optional: the Groq -> Gemini cascade runs without it.
echo ""
echo "[TEST] Checking Ollama connection..."
OLLAMA_URL=$(python3 -c "import sys; sys.path.insert(0,'$PROJECT_DIR'); from config import Config; print(Config.OLLAMA_URL)")
echo "  Detected Ollama URL: $OLLAMA_URL"

if curl -s --max-time 5 "$OLLAMA_URL" > /dev/null 2>&1; then
    echo "[OK] Ollama reachable at $OLLAMA_URL"
    echo ""
    echo "  Available models:"
    curl -s "$OLLAMA_URL/api/tags" 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
for m in data.get('models', []):
    name = m.get('name', '?')
    size = m.get('size', 0) / (1024**3)
    print(f'    - {name} ({size:.1f}GB)')
" 2>/dev/null || echo "    (could not list models)"
else
    echo "[INFO] Cannot reach Ollama at $OLLAMA_URL (optional - cloud cascade still works)"
    echo ""
    if [ "$HOST_KIND" = "macos" ]; then
        echo "  To enable the local fallback:"
        echo "    brew install ollama"
        echo "    brew services start ollama"
        echo "    ollama pull qwen2.5:7b && ollama pull deepseek-coder:6.7b"
        echo "  Ollama runs natively on Apple Silicon (Metal); no host bridging needed."
    else
        echo "  Troubleshooting:"
        echo "  1. Make sure Ollama is running on Windows"
        echo "  2. On Windows, set environment variable: OLLAMA_HOST=0.0.0.0"
        echo "     (System Settings > Environment Variables > User variables)"
        echo "  3. Restart Ollama after setting OLLAMA_HOST"
        echo "  4. Or set OLLAMA_URL manually in .env"
    fi
fi

# Clean up old files
if [ -f "$PROJECT_DIR/tools/actions.py" ]; then
    echo ""
    echo "[CLEANUP] Removing old tools/actions.py (replaced by new modules)"
    rm -f "$PROJECT_DIR/tools/actions.py"
    rm -rf "$PROJECT_DIR/tools/__pycache__" "$PROJECT_DIR/core/__pycache__"
fi

if [ -d "$PROJECT_DIR/venv" ]; then
    echo "[CLEANUP] Removing stray venv from project directory"
    rm -rf "$PROJECT_DIR/venv"
fi

# ── Service definition for whichever init system this host uses ──
echo ""
if [ "$HOST_KIND" = "macos" ]; then
    BREW_PREFIX="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
    PLIST="$PROJECT_DIR/com.mimir.agent.plist"
    if [ -f "$PROJECT_DIR/com.mimir.agent.plist.template" ]; then
        sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
            -e "s|__VENV_DIR__|$VENV_DIR|g" \
            -e "s|__BREW_PREFIX__|$BREW_PREFIX|g" \
            -e "s|__HOME__|$HOME|g" \
            "$PROJECT_DIR/com.mimir.agent.plist.template" > "$PLIST"
        plutil -lint "$PLIST" >/dev/null && echo "[OK] LaunchAgent plist generated"
    fi
else
    if [ -f "$PROJECT_DIR/ai-agent.service.template" ]; then
        sed -e "s|__WSL_USER__|$(whoami)|g" \
            -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
            -e "s|__VENV_DIR__|$VENV_DIR|g" \
            "$PROJECT_DIR/ai-agent.service.template" > "$PROJECT_DIR/ai-agent.service"
        echo "[OK] Systemd service file generated"
    fi
fi

echo ""
echo "==============================="
echo "  Setup complete!"
echo "==============================="
echo ""
echo "To run the agent:"
echo "  source $VENV_DIR/bin/activate"
echo "  cd $PROJECT_DIR"
echo "  python3 main.py"
echo ""
if [ "$HOST_KIND" = "macos" ]; then
    echo "Or install it as a LaunchAgent (starts at login):"
    echo "  cp $PROJECT_DIR/com.mimir.agent.plist ~/Library/LaunchAgents/"
    echo "  launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.mimir.agent.plist"
    echo "  launchctl kickstart -k gui/\$(id -u)/com.mimir.agent"
    echo ""
    echo "IMPORTANT - grant Screen Recording for /screenshot to work:"
    echo "  System Settings > Privacy & Security > Screen Recording"
    echo "  Add the app that launches MIMIR (Terminal/iTerm), then restart it."
    echo "  Without it screencapture exits cleanly but captures nothing."
else
    echo "Or use the systemd service:"
    echo "  sudo cp $PROJECT_DIR/ai-agent.service /etc/systemd/system/"
    echo "  sudo systemctl enable ai-agent"
    echo "  sudo systemctl start ai-agent"
fi
echo ""
