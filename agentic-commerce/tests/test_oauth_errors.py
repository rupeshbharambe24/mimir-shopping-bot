"""An OAuth failure must say what the server said.

Zepto's registration endpoint returned, in full:

    {"error":"server_error","message":"OOM command not allowed when used
     memory > 'maxmemory'."}

which names the cause exactly -- their registration store is full and every new
client registration is failing. urllib discards that body, so what surfaced was
a 30-line traceback ending in "HTTP Error 500: Internal Server Error", and the
difference between those two messages is the difference between "wait, it is
their outage" and an afternoon spent checking your own config.
"""

import io
import json
import urllib.error

import pytest

from backend.providers import oauth_pkce
from backend.providers.oauth_pkce import AuthServerError


def _http_error(code, body, url="https://auth.example/register"):
    return urllib.error.HTTPError(
        url, code, "Internal Server Error", {},
        io.BytesIO(body.encode() if isinstance(body, str) else body),
    )


def test_the_servers_own_message_reaches_the_caller(monkeypatch):
    body = json.dumps(
        {"error": "server_error",
         "message": "OOM command not allowed when used memory > 'maxmemory'."}
    )
    monkeypatch.setattr(
        oauth_pkce.urllib.request, "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(_http_error(500, body)),
    )

    with pytest.raises(AuthServerError) as exc:
        oauth_pkce._post_json("https://auth.example/register", {}, what="registration")

    assert "OOM command not allowed" in str(exc.value)
    assert exc.value.status == 500
    assert exc.value.body == body


def test_5xx_is_flagged_as_the_providers_fault_and_4xx_is_not(monkeypatch):
    """The distinction that decides whether the user should change anything."""
    for code, theirs in ((500, True), (502, True), (400, False), (401, False)):
        monkeypatch.setattr(
            oauth_pkce.urllib.request, "urlopen",
            lambda *a, _c=code, **kw: (_ for _ in ()).throw(_http_error(_c, "{}")),
        )
        with pytest.raises(AuthServerError) as exc:
            oauth_pkce._post_json("https://auth.example/token", {}, what="x")
        assert exc.value.is_provider_fault is theirs, code


def test_an_oauth_error_description_is_preferred_over_the_raw_body(monkeypatch):
    body = json.dumps({"error": "invalid_grant",
                       "error_description": "authorization code has expired"})
    monkeypatch.setattr(
        oauth_pkce.urllib.request, "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(_http_error(400, body)),
    )
    with pytest.raises(AuthServerError) as exc:
        oauth_pkce._post_json("https://auth.example/token", {}, what="token exchange")
    assert "authorization code has expired" in str(exc.value)


def test_a_non_json_body_is_still_reported(monkeypatch):
    monkeypatch.setattr(
        oauth_pkce.urllib.request, "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(_http_error(503, "<html>gateway down</html>")),
    )
    with pytest.raises(AuthServerError) as exc:
        oauth_pkce._post_json("https://auth.example/register", {}, what="registration")
    assert "gateway down" in str(exc.value)


def test_an_unreachable_host_names_the_url(monkeypatch):
    monkeypatch.setattr(
        oauth_pkce.urllib.request, "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(urllib.error.URLError("nodename nor servname")),
    )
    with pytest.raises(AuthServerError) as exc:
        oauth_pkce._post_json("https://auth.example/register", {}, what="registration")
    assert "auth.example" in str(exc.value)


def test_a_registration_without_a_client_id_is_not_cached(tmp_path, monkeypatch):
    """A 200 carrying no client_id must fail loudly rather than KeyError, and
    must not write a cache file that would poison every later login."""
    cfg = oauth_pkce.OAuthConfig(
        provider_id="x", display_name="X", auth_base="https://auth.example",
        scopes="s", redirect_port=9999,
        token_path_env="X_TOKENS", client_path_env="X_CLIENT",
        token_filename=".x_tokens.json", client_filename=".x_client.json",
    )
    cache = tmp_path / "client.json"
    monkeypatch.setenv("X_CLIENT", str(cache))
    monkeypatch.setattr(oauth_pkce, "_post_json", lambda *a, **kw: {"not_a_client_id": 1})

    with pytest.raises(AuthServerError) as exc:
        oauth_pkce.get_client_id(cfg)
    assert "no client_id" in str(exc.value)
    assert not cache.exists(), "a failed registration must not leave a cache file"


def test_requests_carry_a_timeout(monkeypatch):
    """urlopen defaults to no timeout. These run behind a 180s browser wait, so
    a hung endpoint would block the login forever with nothing on screen."""
    seen = {}

    class _Resp:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake(req, *a, **kw):
        seen.update(kw)
        return _Resp()

    monkeypatch.setattr(oauth_pkce.urllib.request, "urlopen", _fake)
    oauth_pkce._post_json("https://auth.example/token", {}, what="x")
    assert seen.get("timeout"), "no timeout passed to urlopen"
