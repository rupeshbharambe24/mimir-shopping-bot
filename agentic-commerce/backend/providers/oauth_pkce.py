"""OAuth 2.1 + PKCE + Dynamic Client Registration (RFC 7591), provider-agnostic.

Extracted from `swiggy/auth.py` when Zepto was added. The two providers advertise
the same handshake in their authorization-server metadata -- `/register`,
`/authorize`, `/token`, `code_challenge_methods_supported: ["S256"]` and
`token_endpoint_auth_methods_supported: ["none"]` -- so the flow is shared and
only the endpoints, scopes and cache paths differ.

Behaviour is unchanged from the Swiggy implementation this came from. Two things
deliberately kept per-provider rather than derived:

* The authorization base URL. Swiggy serves auth from the MCP host
  (mcp.swiggy.com/auth) while Zepto uses a separate one (auth.zepto.co.in), so
  guessing it from the MCP URL would be wrong for one of them.
* The redirect port. Each provider registers its own redirect_uri with its own
  authorization server, and two providers sharing a port would collide if a
  login were ever attempted concurrently.

The localhost redirect catcher is for local/dev use. A deployed web app would
swap it for a real HTTPS callback route.
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
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass


@dataclass(frozen=True)
class OAuthConfig:
    """Everything that differs between providers using this same flow."""

    provider_id: str
    display_name: str          # shown in the "Opening your browser..." message
    auth_base: str             # e.g. https://auth.zepto.co.in (no trailing slash)
    scopes: str
    redirect_port: int
    token_path_env: str        # env var overriding the token cache location
    client_path_env: str       # env var overriding the client-registration cache
    token_filename: str        # default cache filename, used when the env var is unset
    client_filename: str
    client_name: str = "agentic-commerce-platform"

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.redirect_port}/callback"


def _default_path(filename: str) -> str:
    # Alongside the provider packages, matching where the Swiggy files have
    # always lived so an existing cache keeps working.
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def token_file(cfg: OAuthConfig) -> str:
    return os.environ.get(cfg.token_path_env) or _default_path(cfg.token_filename)


def client_file(cfg: OAuthConfig) -> str:
    return os.environ.get(cfg.client_path_env) or _default_path(cfg.client_filename)


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
# HTTP
# --------------------------------------------------------------------------

class AuthServerError(RuntimeError):
    """The provider's authorization server refused or failed.

    Carries `status` and the server's own `body`, because that body is where
    the actual reason lives and urllib throws it away by default: a bare
    HTTPError renders as "HTTP Error 500: Internal Server Error" while the
    response said, in full, `{"error":"server_error","message":"OOM command not
    allowed when used memory > 'maxmemory'."}`. One of those tells you the
    provider's Redis is full and there is nothing to fix on your side; the
    other costs an afternoon.
    """

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body

    @property
    def is_provider_fault(self) -> bool:
        """5xx: their server broke. 4xx means our request was wrong."""
        return self.status is not None and 500 <= self.status < 600


def _post_json(url: str, payload: dict, *, what: str, timeout: float = 30.0) -> dict:
    """POST JSON and return the parsed response, or raise AuthServerError.

    Every OAuth call here goes through this so a failure reports what the
    server actually said. The timeout is not optional either: these run
    interactively behind a 180s browser wait, and urlopen's default of None
    means a hung endpoint blocks the login forever with no output at all.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace").strip()
        except Exception:
            pass
        detail = ""
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                detail = str(parsed.get("message") or parsed.get("error_description")
                             or parsed.get("error") or "").strip()
        except ValueError:
            detail = body[:300]
        raise AuthServerError(
            f"{what} failed: HTTP {exc.code}"
            + (f" -- {detail}" if detail else ""),
            status=exc.code,
            body=body,
        ) from None
    except urllib.error.URLError as exc:
        raise AuthServerError(f"{what} failed: cannot reach {url} ({exc.reason})") from None
    except TimeoutError:
        raise AuthServerError(f"{what} failed: {url} timed out after {timeout:.0f}s") from None

    try:
        return json.loads(raw)
    except ValueError:
        raise AuthServerError(
            f"{what} failed: server returned a non-JSON body",
            body=raw.decode("utf-8", "replace")[:300],
        ) from None


# --------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591) -- run once, cache client_id
# --------------------------------------------------------------------------

def get_client_id(cfg: OAuthConfig) -> str:
    path = client_file(cfg)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)["client_id"]

    data = _post_json(
        f"{cfg.auth_base}/register",
        {
            "client_name": cfg.client_name,
            "redirect_uris": [cfg.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",  # public client, PKCE-only
        },
        what=f"{cfg.display_name} client registration",
    )

    if "client_id" not in data:
        raise AuthServerError(
            f"{cfg.display_name} registration returned no client_id",
            body=json.dumps(data)[:300],
        )
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


def run_authorize_flow(cfg: OAuthConfig) -> dict:
    """Full OAuth 2.1 + PKCE authorization_code flow. Returns token response dict."""
    client_id = get_client_id(cfg)
    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(16)

    _CallbackHandler.result = {}
    _CallbackHandler.done.clear()
    server = http.server.HTTPServer(("localhost", cfg.redirect_port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    auth_url = f"{cfg.auth_base}/authorize?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": cfg.redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": cfg.scopes,
    })

    print(f"\nOpening your browser to log in to {cfg.display_name} "
          f"(phone + OTP handled by {cfg.display_name} directly)...")
    print(f"If it doesn't open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    if not _CallbackHandler.done.wait(timeout=180):
        server.server_close()
        raise RuntimeError(f"Timed out waiting for {cfg.display_name} login. Try again.")
    thread.join(timeout=5)

    result = _CallbackHandler.result
    if result.get("error"):
        raise RuntimeError(f"{cfg.display_name} login failed: {result['error']}")
    if result.get("state") != state:
        raise RuntimeError("State mismatch on OAuth callback -- possible CSRF, aborting.")

    return exchange_code_for_token(cfg, result["code"], verifier, client_id)


def exchange_code_for_token(cfg: OAuthConfig, code: str, verifier: str, client_id: str) -> dict:
    token = _post_json(
        f"{cfg.auth_base}/token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": cfg.redirect_uri,
            "client_id": client_id,
        },
        what=f"{cfg.display_name} token exchange",
    )
    token["obtained_at"] = int(time.time())
    return token


def refresh_token(cfg: OAuthConfig, refresh_tok: str, client_id: str) -> dict:
    token = _post_json(
        f"{cfg.auth_base}/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
            "client_id": client_id,
        },
        what=f"{cfg.display_name} token refresh",
    )
    token["obtained_at"] = int(time.time())
    return token


# --------------------------------------------------------------------------
# Token cache / lifecycle
# --------------------------------------------------------------------------

def get_access_token(cfg: OAuthConfig, force: bool = False) -> str:
    client_id = get_client_id(cfg)
    path = token_file(cfg)

    if not force and os.path.exists(path):
        with open(path) as f:
            token = json.load(f)

        expires_at = token.get("obtained_at", 0) + token.get("expires_in", 0)
        if time.time() < expires_at - 60:
            return token["access_token"]

        if token.get("refresh_token"):
            try:
                new_token = refresh_token(cfg, token["refresh_token"], client_id)
                with open(path, "w") as f:
                    json.dump(new_token, f)
                return new_token["access_token"]
            except Exception as e:
                print(f"(refresh failed, re-authenticating: {e})")

    token = run_authorize_flow(cfg)
    with open(path, "w") as f:
        json.dump(token, f)
    return token["access_token"]
