#!/usr/bin/env bash
# One-time Swiggy login. Run this from a terminal ON the Mac, before starting
# the commerce service.
#
# Why not just use /connect from Telegram: auth.get_access_token() is
# synchronous and waits up to 180s for the browser callback on localhost:8934.
# Called from inside the running service it blocks the whole event loop for
# those three minutes, and you still have to be at the Mac to finish the login.
# Doing it once here writes the token file; after that /connect is instant and
# refreshes itself.

set -e
COMMERCE_DIR="${COMMERCE_DIR:-$(cd "$(dirname "$0")" && pwd)/agentic-commerce}"

if [ ! -x "$COMMERCE_DIR/.venv/bin/python" ]; then
    echo "[!!] Commerce venv missing. Run ./start-commerce.sh once first."
    exit 1
fi

cd "$COMMERCE_DIR"
echo "[..] Starting Swiggy OAuth. A browser window will open."
echo "     Log in with your phone number + OTP. Swiggy handles that directly;"
echo "     the token lands on this machine and never leaves it."
echo ""

.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv()
from backend.providers.swiggy import auth
tok = auth.get_access_token()
print()
print('[OK] Logged in. Token cached at:', auth.token_file())
print('     Access token length:', len(tok))
"
