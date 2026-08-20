"""Swiggy OAuth 2.1 + PKCE + Dynamic Client Registration (RFC 7591).

Ported VERBATIM (behavior unchanged) from the working `swiggy_agent_gemini.py`
terminal script — this has nothing to do with which LLM or transport is used, so
it carries over as-is. The only change is that the token/client cache file paths
are now configurable via env (`SWIGGY_TOKENS_PATH` / `SWIGGY_CLIENT_PATH`) so a
service can point them at an existing cache instead of a file next to the script.

The localhost redirect flow is retained for local/dev use. A deployed web app
would swap the localhost catcher for a real HTTPS callback route (flutter web
plan §3.2 / §6) — that is a step-5 concern and intentionally NOT done here.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

SWIGGY_AUTH_BASE = "https://mcp.swiggy.com/auth"
SWIGGY_MCP_URL = "https://mcp.swiggy.com/im"  # Instamart MCP endpoint

REDIRECT_PORT = 8934
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPES = "mcp:tools mcp:resources"


def _default_path(filename: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def token_file() -> str:
    return os.environ.get("SWIGGY_TOKENS_PATH") or _default_path(".swiggy_tokens.json")


def client_file() -> str:
    return os.environ.get("SWIGGY_CLIENT_PATH") or _default_path(".swiggy_client.json")


# --------------------------------------------------------------------------
# PKCE helpers
# --------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# --------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591) -- run once, cache client_id
# --------------------------------------------------------------------------

def get_client_id() -> str:
    path = client_file()
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)["client_id"]

    payload = json.dumps({
        "client_name": "agentic-commerce-platform",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",  # public client, PKCE-only
    }).encode()

    req = urllib.request.Request(
        f"{SWIGGY_AUTH_BASE}/register",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    client_id = data["client_id"]
    with open(path, "w") as f:
        json.dump({"client_id": client_id}, f)
    return client_id


# --------------------------------------------------------------------------
# Local redirect catcher
# --------------------------------------------------------------------------

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}
    done = threading.Event()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result["code"] = params.get("code", [None])[0]
        _CallbackHandler.result["state"] = params.get("state", [None])[0]
        _CallbackHandler.result["error"] = params.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if _CallbackHandler.result.get("error"):
            self.wfile.write(b"<h3>Login failed. You can close this tab.</h3>")
        else:
            self.wfile.write(b"<h3>Logged in. You can close this tab and return to the app.</h3>")
        _CallbackHandler.done.set()

    def log_message(self, *args):
        pass  # keep stdout clean


def run_authorize_flow() -> dict:
    """Full OAuth 2.1 + PKCE authorization_code flow. Returns token response dict."""
    client_id = get_client_id()
    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(16)

    _CallbackHandler.result = {}
    _CallbackHandler.done.clear()
    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    auth_url = f"{SWIGGY_AUTH_BASE}/authorize?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": SCOPES,
    })

    print("\nOpening your browser to log in to Swiggy (phone + OTP handled by Swiggy directly)...")
    print(f"If it doesn't open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    if not _CallbackHandler.done.wait(timeout=180):
        server.server_close()
        raise RuntimeError("Timed out waiting for Swiggy login. Try again.")
    thread.join(timeout=5)

    result = _CallbackHandler.result
    if result.get("error"):
        raise RuntimeError(f"Swiggy login failed: {result['error']}")
    if result.get("state") != state:
        raise RuntimeError("State mismatch on OAuth callback -- possible CSRF, aborting.")

    return exchange_code_for_token(result["code"], verifier, client_id)


def exchange_code_for_token(code: str, verifier: str, client_id: str) -> dict:
    payload = json.dumps({
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
    }).encode()
    req = urllib.request.Request(
        f"{SWIGGY_AUTH_BASE}/token",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        token = json.loads(resp.read())
    token["obtained_at"] = int(time.time())
    return token


def refresh_token(refresh_tok: str, client_id: str) -> dict:
    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": client_id,
    }).encode()
    req = urllib.request.Request(
        f"{SWIGGY_AUTH_BASE}/token",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        token = json.loads(resp.read())
    token["obtained_at"] = int(time.time())
    return token


# --------------------------------------------------------------------------
# Token cache / lifecycle
# --------------------------------------------------------------------------

def get_access_token(force: bool = False) -> str:
    client_id = get_client_id()
    path = token_file()

    if not force and os.path.exists(path):
        with open(path) as f:
            token = json.load(f)

        expires_at = token.get("obtained_at", 0) + token.get("expires_in", 0)
        if time.time() < expires_at - 60:
            return token["access_token"]

        if token.get("refresh_token"):
            try:
                new_token = refresh_token(token["refresh_token"], client_id)
                with open(path, "w") as f:
                    json.dump(new_token, f)
                return new_token["access_token"]
            except Exception as e:
                print(f"(refresh failed, re-authenticating: {e})")

    token = run_authorize_flow()
    with open(path, "w") as f:
        json.dump(token, f)
    return token["access_token"]
