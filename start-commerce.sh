#!/usr/bin/env bash
# Launch the agentic-commerce BFF, the sidecar MIMIR's commerce tools talk to.
#
# It runs as its own process with its own venv on purpose. Its requirements.txt
# pins `mcp` exactly because an unpinned bump once broke the transport silently;
# keeping those pins out of MIMIR's environment means a provider-SDK problem
# cannot take the Telegram bot down with it.

set -e

COMMERCE_DIR="${COMMERCE_DIR:-$HOME/Projects/Experiments/agentic-commerce}"
PORT="${COMMERCE_PORT:-8011}"

if [ ! -d "$COMMERCE_DIR" ]; then
    echo "[!!] agentic-commerce not found at $COMMERCE_DIR"
    echo "     Set COMMERCE_DIR to its location."
    exit 1
fi

VENV="$COMMERCE_DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo "[..] Creating the commerce venv (its pins are kept separate from MIMIR's)..."
    rm -rf "$VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip -q
    "$VENV/bin/pip" install -r "$COMMERCE_DIR/requirements.txt" -q
fi

# Already listening? Don't start a second one.
if curl -s --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "[OK] Commerce service already running on port $PORT."
    curl -s "http://127.0.0.1:$PORT/health"
    echo
    exit 0
fi

# providers.yaml is loaded relative to the working directory, so this must run
# from the project root, not from wherever the caller happens to be.
cd "$COMMERCE_DIR"

DRY=$(grep -E '^DRY_RUN=' .env 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
if [ "$DRY" != "true" ]; then
    echo "[!!] WARNING: DRY_RUN is '${DRY:-unset}' in $COMMERCE_DIR/.env"
    echo "     Real orders can be placed. MIMIR will still refuse to check out"
    echo "     unless COMMERCE_ALLOW_LIVE=true is also set in its own .env."
fi

echo "[..] Starting commerce BFF on port $PORT (cwd=$COMMERCE_DIR)..."
exec "$VENV/bin/python" -m uvicorn backend.app:app --host 127.0.0.1 --port "$PORT"
