"""The OAuth provider — authorize + token + register, end to end over the Starlette app.

SQLite-backed (the portable backend the gate runs on). Proves the full authorization-code + PKCE
round trip yields a verifiable JWT, and that every guard fires: wrong PKCE verifier, reused code,
expired code, redirect mismatch, bad credentials, missing signing secret. Also that the /oauth/*
endpoints are reachable without a bearer while /mcp still 401s.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("jwt")
pytest.importorskip("argon2")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import jwt  # noqa: E402
import mcp_http  # noqa: E402
import user_store  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

BASE = "https://your-host.example.com"
SECRET = "x" * 40  # a throwaway HS256 key for tests (≥32 bytes); obviously not a real secret
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "a" * 64  # a fixed PKCE code_verifier


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_url = "sqlite://" + str(tmp_path / "oauth.db")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    # Seed the schema + a user the authorize step can authenticate.
    s = Store.connect(db_url)
    s.run_migrations()
    user_store.create_user(s, "admin", "s3cret-pw")
    s.close()
    return db_url


def _authorize_code(
    client: TestClient, *, redirect: str = REDIRECT, challenge: str | None = None
) -> str:
    """Run the authorize POST and return the issued authorization code from the redirect."""
    resp = client.post(
        "/oauth/authorize",
        data={
            "username": "admin",
            "password": "s3cret-pw",
            "redirect_uri": redirect,
            "client_id": "cid",
            "code_challenge": challenge if challenge is not None else _challenge(VERIFIER),
            "state": "xyz",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith(redirect)
    qs = parse_qs(urlparse(loc).query)
    assert qs["state"] == ["xyz"]
    return qs["code"][0]


def test_register_mints_a_client_id(env):
    c = TestClient(mcp_http.build_app())
    r = c.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    assert r.status_code == 201
    assert r.json()["client_id"]


def test_authorize_get_renders_a_login_form(env):
    c = TestClient(mcp_http.build_app())
    r = c.get("/oauth/authorize", params={"redirect_uri": REDIRECT, "state": "xyz"})
    assert r.status_code == 200 and "<form" in r.text and 'name="password"' in r.text


def test_full_pkce_flow_yields_a_verifiable_jwt(env):
    c = TestClient(mcp_http.build_app())
    code = _authorize_code(c)
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600
    claims = jwt.decode(body["access_token"], SECRET, algorithms=["HS256"])
    assert claims["sub"] == "admin" and claims["iss"] == BASE


def test_bad_credentials_re_render_form_without_a_code(env):
    c = TestClient(mcp_http.build_app())
    r = c.post(
        "/oauth/authorize",
        data={
            "username": "admin",
            "password": "wrong",
            "redirect_uri": REDIRECT,
            "code_challenge": _challenge(VERIFIER),  # valid PKCE so we reach the credential check
        },
        follow_redirects=False,
    )
    assert r.status_code == 200 and "Invalid username or password" in r.text


def test_authorize_requires_pkce_challenge(env):
    # PKCE is mandatory — a submission without a code_challenge is rejected before any code is minted.
    c = TestClient(mcp_http.build_app())
    r = c.post(
        "/oauth/authorize",
        data={"username": "admin", "password": "s3cret-pw", "redirect_uri": REDIRECT},
        follow_redirects=False,
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_register_rejects_malformed_redirect_uris(env):
    c = TestClient(mcp_http.build_app())
    r = c.post("/oauth/register", json={"redirect_uris": "not-a-list"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_wrong_pkce_verifier_is_rejected(env):
    c = TestClient(mcp_http.build_app())
    code = _authorize_code(c)
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": "b" * 64,  # wrong verifier
            "redirect_uri": REDIRECT,
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_code_is_single_use(env):
    c = TestClient(mcp_http.build_app())
    code = _authorize_code(c)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": VERIFIER,
        "redirect_uri": REDIRECT,
    }
    assert c.post("/oauth/token", data=data).status_code == 200
    assert c.post("/oauth/token", data=data).status_code == 400  # reuse rejected


def test_expired_code_is_rejected(env, monkeypatch):
    c = TestClient(mcp_http.build_app())
    code = _authorize_code(c)
    # Force the stored code to be in the past.
    s = Store.from_env()
    s.execute(
        "UPDATE oauth_state SET expires_at = ? WHERE code = ?",
        ("2000-01-01T00:00:00+00:00", code),
    )
    s.commit()
    s.close()
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT,
        },
    )
    assert r.status_code == 400 and "expired" in r.json()["error_description"]


def test_redirect_uri_mismatch_is_rejected_at_token(env):
    c = TestClient(mcp_http.build_app())
    code = _authorize_code(c)
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": BASE + "/different",  # not what the code was issued for
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_disallowed_redirect_is_rejected_at_authorize(env):
    c = TestClient(mcp_http.build_app())
    r = c.post(
        "/oauth/authorize",
        data={
            "username": "admin",
            "password": "s3cret-pw",
            "redirect_uri": "https://evil.example.com/steal",
            "client_id": "cid",
            "code_challenge": _challenge(VERIFIER),
        },
        follow_redirects=False,
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_missing_signing_secret_fails_cleanly_without_burning_the_code(env, monkeypatch):
    c = TestClient(mcp_http.build_app())
    code = _authorize_code(c)
    monkeypatch.delenv("AGAMI_SIGNING_SECRET", raising=False)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": VERIFIER,
        "redirect_uri": REDIRECT,
    }
    r = c.post("/oauth/token", data=data)
    assert r.status_code == 500 and r.json()["error"] == "server_error"
    # the code was NOT consumed — it works once the secret is restored
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    assert c.post("/oauth/token", data=data).status_code == 200


def test_prefix_confusion_redirect_is_rejected(env):
    # Open-redirect guard: a host that merely *prefixes* PUBLIC_BASE_URL is a different origin and
    # must be rejected (startswith would have wrongly allowed it).
    c = TestClient(mcp_http.build_app())
    r = c.post(
        "/oauth/authorize",
        data={
            "username": "admin",
            "password": "s3cret-pw",
            "redirect_uri": BASE + ".evil.example.com/cb",  # same prefix, different origin
            "code_challenge": _challenge(VERIFIER),
        },
        follow_redirects=False,
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_same_origin_redirect_under_public_base_url_is_allowed(env):
    c = TestClient(mcp_http.build_app())
    r = c.post(
        "/oauth/authorize",
        data={
            "username": "admin",
            "password": "s3cret-pw",
            "redirect_uri": BASE + "/callback",  # same scheme+host as PUBLIC_BASE_URL
            "code_challenge": _challenge(VERIFIER),
            "state": "xyz",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["location"].startswith(BASE + "/callback")


def test_login_form_escapes_params_no_reflected_xss(env):
    # The carried OAuth params land in HTML attributes on the password page; they must be escaped.
    c = TestClient(mcp_http.build_app())
    r = c.get("/oauth/authorize", params={"state": '"><script>alert(1)</script>'})
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text  # not reflected raw
    assert "&lt;script&gt;" in r.text  # escaped instead


def test_oauth_endpoints_are_public_but_mcp_still_requires_auth(env):
    c = TestClient(mcp_http.build_app())
    assert c.get("/oauth/authorize", params={"redirect_uri": REDIRECT}).status_code == 200
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401  # the MCP endpoint still demands a bearer
    # The OAuth skip is EXACT-match: a sibling under /oauth/* that isn't a real route doesn't get a
    # free pass — it still hits auth (401) rather than being treated as public.
    assert c.post("/oauth/token/extra").status_code == 401


# --- the JWT-validating provider + end-to-end (the issued token gates /mcp) ---


def _mint_jwt(c: TestClient) -> str:
    """Run the full authorize→token flow and return the issued access token (a JWT)."""
    code = _authorize_code(c)
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT,
        },
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def test_jwt_provider_accepts_issued_token_and_rejects_junk(env):
    from oauth_server import JwtAuthProvider, issue_jwt

    provider = JwtAuthProvider()
    principal = provider.validate_token(issue_jwt("admin"))
    assert principal is not None and principal.subject == "admin"
    assert provider.validate_token("not-a-jwt") is None
    # a token signed with the WRONG secret must not validate
    forged = jwt.encode({"sub": "admin", "iss": BASE, "exp": 9_999_999_999}, "wrong", "HS256")
    assert provider.validate_token(forged) is None


def test_end_to_end_oauth_then_mcp_tools_list(env):
    # The whole point: a token minted via the OAuth flow is accepted by the transport, and the
    # tools/list comes back with exactly the 5 product tools.
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(mcp_http.build_app()) as c:
        bearer = {"Authorization": f"Bearer {_mint_jwt(c)}", **headers}
        init = c.post(
            "/mcp",
            headers=bearer,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
        assert init.status_code == 200
        sid = init.headers.get("mcp-session-id")
        h2 = {**bearer, **({"mcp-session-id": sid} if sid else {})}
        c.post("/mcp", headers=h2, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        tl = c.post("/mcp", headers=h2, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert tl.status_code == 200
        payload = json.loads(re.search(r"\{.*\}", tl.text, re.DOTALL).group(0))
        names = {t["name"] for t in payload["result"]["tools"]}
    assert names == {
        "list_datasources",
        "get_datasource_schema",
        "get_prompt_examples",
        "execute_sql",
        "log_feedback",
    }


def test_non_jwt_bearer_is_rejected_when_signing_secret_set(env):
    # With a signing secret configured the transport uses the JWT provider, so a bare presence token
    # ("Bearer present") that worked in the no-secret fallback is now rejected.
    c = TestClient(mcp_http.build_app())
    r = c.post(
        "/mcp",
        headers={"Authorization": "Bearer present"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 401
