#!/usr/bin/env bash
# One-time Zepto login. Run from a terminal ON the Mac.
#
# Same reasoning as login-swiggy.sh: get_access_token() blocks up to 180s
# waiting for the browser callback on localhost:8935, so doing it inside the
# running service would freeze it for that whole window.

set -e
COMMERCE_DIR="${COMMERCE_DIR:-$(cd "$(dirname "$0")" && pwd)/agentic-commerce}"

if [ ! -x "$COMMERCE_DIR/.venv/bin/python" ]; then
    echo "[!!] Commerce venv missing. Run ./start-commerce.sh once first."
    exit 1
fi

cd "$COMMERCE_DIR"
echo "[..] Starting Zepto OAuth. A browser window will open."
echo "     Log in with your Indian mobile number + OTP. Zepto handles that"
echo "     directly; the token stays on this machine."
echo ""

.venv/bin/python -c "
import sys; sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv('.env')
from backend.providers.zepto import auth
tok = auth.get_access_token()
print()
print('[OK] Logged in. Token cached at:', auth.token_file())
print('     Access token length:', len(tok))
"
