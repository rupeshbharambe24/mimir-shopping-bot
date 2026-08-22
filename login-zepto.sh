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
from backend.providers import oauth_pkce
from backend.providers.zepto import auth

try:
    tok = auth.get_access_token()
except oauth_pkce.AuthServerError as exc:
    # A traceback here is noise: nothing in it is our code's fault when the
    # provider's own server is the thing that broke. Print what they said.
    print()
    print('[!!]', exc)
    if exc.is_provider_fault:
        print()
        print('     That is a fault on Zepto\'s side, not a problem with this')
        print('     machine or your account -- their auth server accepted the')
        print('     request and failed to store the result. Nothing here can')
        print('     fix it; try again later.')
        if 'OOM' in (exc.body or '') or 'maxmemory' in (exc.body or ''):
            print()
            print('     (Their registration store is out of memory. Every new')
            print('      client registration is failing, not just yours.)')
    sys.exit(1)
except KeyboardInterrupt:
    print()
    print('[..] Cancelled. Nothing was saved.')
    sys.exit(130)

print()
print('[OK] Logged in. Token cached at:', auth.token_file())
print('     Access token length:', len(tok))
"
