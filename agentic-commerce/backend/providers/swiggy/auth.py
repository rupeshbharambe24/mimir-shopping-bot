"""Swiggy OAuth 2.1 + PKCE + Dynamic Client Registration (RFC 7591).

The handshake itself now lives in `backend.providers.oauth_pkce`, shared with
Zepto, which advertises an identical flow in its authorization-server metadata.
This module is the Swiggy configuration for it plus the module-level names the
rest of the codebase already imports.

Every public name here keeps its original signature: `mcp_compat` imports
`SWIGGY_MCP_URL`, the adapter calls `get_access_token()`, and `login-swiggy.sh`
calls `token_file()`. Those all still work unchanged.
"""

from __future__ import annotations

from backend.providers import oauth_pkce
from backend.providers.oauth_pkce import make_pkce_pair  # re-exported

SWIGGY_AUTH_BASE = "https://mcp.swiggy.com/auth"
SWIGGY_MCP_URL = "https://mcp.swiggy.com/im"  # Instamart MCP endpoint

REDIRECT_PORT = 8934
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPES = "mcp:tools mcp:resources"

CONFIG = oauth_pkce.OAuthConfig(
    provider_id="swiggy",
    display_name="Swiggy",
    auth_base=SWIGGY_AUTH_BASE,
    scopes=SCOPES,
    redirect_port=REDIRECT_PORT,
    token_path_env="SWIGGY_TOKENS_PATH",
    client_path_env="SWIGGY_CLIENT_PATH",
    token_filename=".swiggy_tokens.json",
    client_filename=".swiggy_client.json",
)


def token_file() -> str:
    return oauth_pkce.token_file(CONFIG)


def client_file() -> str:
    return oauth_pkce.client_file(CONFIG)


def get_client_id() -> str:
    return oauth_pkce.get_client_id(CONFIG)


def run_authorize_flow() -> dict:
    return oauth_pkce.run_authorize_flow(CONFIG)


def exchange_code_for_token(code: str, verifier: str, client_id: str) -> dict:
    return oauth_pkce.exchange_code_for_token(CONFIG, code, verifier, client_id)


def refresh_token(refresh_tok: str, client_id: str) -> dict:
    return oauth_pkce.refresh_token(CONFIG, refresh_tok, client_id)


def get_access_token(force: bool = False) -> str:
    return oauth_pkce.get_access_token(CONFIG, force=force)
