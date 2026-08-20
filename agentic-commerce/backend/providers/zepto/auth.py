"""Zepto OAuth 2.1 + PKCE + Dynamic Client Registration.

Values below are taken from Zepto's own discovery documents, not guessed:

    https://mcp.zepto.co.in/.well-known/oauth-protected-resource
      -> authorization_servers: ["https://auth.zepto.co.in"]
      -> scopes_supported:      ["tools:read", "tools:write"]

    https://auth.zepto.co.in/.well-known/oauth-authorization-server
      -> authorization_endpoint: /authorize
      -> token_endpoint:         /token
      -> registration_endpoint:  /register
      -> code_challenge_methods_supported:      ["S256"]
      -> token_endpoint_auth_methods_supported: ["none"]

which is the same public-client PKCE handshake Swiggy uses, so the flow is
shared via backend.providers.oauth_pkce.

Note the auth host differs from the MCP host here (auth.zepto.co.in vs
mcp.zepto.co.in), unlike Swiggy where both live under mcp.swiggy.com. That is
why oauth_pkce takes an explicit auth_base rather than deriving one.

The redirect port is deliberately NOT Swiggy's 8934: each provider registers its
own redirect_uri with its own authorization server, and sharing a port would
collide if two logins ever overlapped.
"""

from __future__ import annotations

from backend.providers import oauth_pkce

ZEPTO_AUTH_BASE = "https://auth.zepto.co.in"
ZEPTO_MCP_URL = "https://mcp.zepto.co.in/mcp"

REDIRECT_PORT = 8935
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPES = "tools:read tools:write"

CONFIG = oauth_pkce.OAuthConfig(
    provider_id="zepto",
    display_name="Zepto",
    auth_base=ZEPTO_AUTH_BASE,
    scopes=SCOPES,
    redirect_port=REDIRECT_PORT,
    token_path_env="ZEPTO_TOKENS_PATH",
    client_path_env="ZEPTO_CLIENT_PATH",
    token_filename=".zepto_tokens.json",
    client_filename=".zepto_client.json",
)


def token_file() -> str:
    return oauth_pkce.token_file(CONFIG)


def client_file() -> str:
    return oauth_pkce.client_file(CONFIG)


def get_client_id() -> str:
    return oauth_pkce.get_client_id(CONFIG)


def get_access_token(force: bool = False) -> str:
    return oauth_pkce.get_access_token(CONFIG, force=force)
