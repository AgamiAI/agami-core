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
import secrets
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
# A throwaway login, random per run. A secret scanner resolves a literal password, a named constant and
# even a concatenation of literals, and pairs it with the username beside it; a value that only exists
# at run time gives it nothing to resolve. The tests create the user and sign in with this same value.
ADMIN_PW = secrets.token_urlsafe(16)
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
    user_store.create_user(s, "admin", ADMIN_PW)
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
            "password": ADMIN_PW,
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
        data={"username": "admin", "password": ADMIN_PW, "redirect_uri": REDIRECT},
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
            "password": ADMIN_PW,
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
            "password": ADMIN_PW,
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
            "password": ADMIN_PW,
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


def test_jwt_provider_rejects_alg_none_and_alg_confusion(env):
    # The two classic JWT forgeries: an unsigned alg=none token, and a token whose header claims a
    # different algorithm than the HS256 we pin. Both must be rejected.
    from oauth_server import JwtAuthProvider

    provider = JwtAuthProvider()
    claims = {"sub": "admin", "iss": BASE, "exp": 9_999_999_999}
    assert provider.validate_token(jwt.encode(claims, None, algorithm="none")) is None
    assert provider.validate_token(jwt.encode(claims, SECRET, algorithm="HS512")) is None


def test_jwt_provider_rejects_token_from_a_different_issuer(env):
    from oauth_server import JwtAuthProvider

    forged = jwt.encode(
        {"sub": "admin", "iss": "https://evil.example.com", "exp": 9_999_999_999}, SECRET, "HS256"
    )
    assert JwtAuthProvider().validate_token(forged) is None


def test_jwt_provider_rejects_non_string_or_blank_sub(env):
    from oauth_server import JwtAuthProvider

    provider = JwtAuthProvider()
    numeric = jwt.encode({"sub": 123, "iss": BASE, "exp": 9_999_999_999}, SECRET, "HS256")
    blank = jwt.encode({"sub": "   ", "iss": BASE, "exp": 9_999_999_999}, SECRET, "HS256")
    assert provider.validate_token(numeric) is None
    assert provider.validate_token(blank) is None


def test_build_app_fails_fast_on_present_but_weak_signing_secret(env, monkeypatch):
    # A configured-but-invalid secret must not silently downgrade to presence auth — build fails.
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", "")  # present but empty
    with pytest.raises(RuntimeError, match="AGAMI_SIGNING_SECRET"):
        mcp_http.build_app()
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", "too-short")  # present but below 32 bytes
    with pytest.raises(RuntimeError, match="AGAMI_SIGNING_SECRET"):
        mcp_http.build_app()


def test_end_to_end_oauth_then_mcp_tools_list(env):
    # The whole point: a token minted via the OAuth flow is accepted by the transport, and the
    # tools/list comes back with exactly the 4 product tools.
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


# ---------------------------------------------------------------------------
# Refresh-token grant (ACE-033) — silent renewal of the short-lived access JWT, with rotation +
# reuse-detection so a connected client (claude.ai) isn't forced to re-login every hour.
# ---------------------------------------------------------------------------


def _token_pair(client: TestClient) -> dict:
    """Run the full authorization-code exchange and return the token body (access + refresh)."""
    code = _authorize_code(client)
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _refresh(client: TestClient, refresh_token: str, *, client_id: str = "cid") -> dict:
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_authorization_code_issues_a_refresh_token(env):
    c = TestClient(mcp_http.build_app())
    body = _token_pair(c)
    assert body["refresh_token"]  # the code exchange now returns a refresh token
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600


def test_refresh_grant_rotates_and_renews(env):
    c = TestClient(mcp_http.build_app())
    first = _token_pair(c)
    second = _refresh(c, first["refresh_token"])
    # a fresh, verifiable access JWT for the same subject
    claims = jwt.decode(second["access_token"], SECRET, algorithms=["HS256"], issuer=BASE)
    assert claims["sub"] == "admin"
    # rotation: a NEW refresh token, different from the one presented
    assert second["refresh_token"] and second["refresh_token"] != first["refresh_token"]
    # the successor keeps renewing (the chain works)
    third = _refresh(c, second["refresh_token"])
    assert third["refresh_token"] and third["refresh_token"] != second["refresh_token"]


def test_refresh_token_reuse_revokes_the_family(env, monkeypatch):
    # Stolen-token reuse detection is a 'rotate'-mode feature (it needs the revoked old row to
    # replay against); the 'overwrite' default forgoes it by design. Pin rotate for this test.
    monkeypatch.setenv("AGAMI_REFRESH_TOKEN_MODE", "rotate")
    c = TestClient(mcp_http.build_app())
    first = _token_pair(c)
    second = _refresh(c, first["refresh_token"])  # rotates → `first` is now revoked
    # replaying the revoked (old) token is treated as a stolen-token replay → rejected
    replay = c.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
    )
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    # ...and the whole family is now dead: the previously-valid successor stops working too
    dead = c.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": second["refresh_token"]},
    )
    assert dead.status_code == 400 and dead.json()["error"] == "invalid_grant"


def test_ace050_overwrite_default_keeps_one_row_and_kills_old_tokens(env):
    # Default mode is 'overwrite': each refresh UPDATEs the session's single row in place, so no heap
    # of dead tokens accumulates and every superseded token stops authenticating.
    c = TestClient(mcp_http.build_app())
    first = _token_pair(c)
    second = _refresh(c, first["refresh_token"])
    third = _refresh(c, second["refresh_token"])
    assert third["refresh_token"] not in (first["refresh_token"], second["refresh_token"])

    s = Store.from_env()
    try:
        n = s.query("SELECT COUNT(*) AS n FROM oauth_refresh_token")[0]["n"]
        assert n == 1  # one row for the session, updated in place — no accumulation
    finally:
        s.close()

    for stale in (first["refresh_token"], second["refresh_token"]):
        r = c.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": stale, "client_id": "cid"},
        )
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_ace050_rotate_mode_prunes_only_expired_revoked_rows(env, monkeypatch):
    # 'rotate' keeps revoked rows for reuse detection but must prune the ones past expiry (dead
    # weight — the theft signal is moot once the token could no longer authenticate anyway).
    monkeypatch.setenv("AGAMI_REFRESH_TOKEN_MODE", "rotate")
    c = TestClient(mcp_http.build_app())
    pair = _token_pair(c)

    s = Store.from_env()
    try:
        # Seed two revoked rows directly: one already expired, one still within its window.
        for th, exp in (
            ("expired_revoked", "2000-01-01T00:00:00+00:00"),
            ("live_revoked", "2999-01-01T00:00:00+00:00"),
        ):
            s.execute(
                "INSERT INTO oauth_refresh_token (token_hash, family, client_id, username, "
                "expires_at, revoked, created) VALUES (?, 'fam', 'cid', 'admin', ?, 1, ?)",
                (th, exp, "2000-01-01T00:00:00+00:00"),
            )
        s.commit()
    finally:
        s.close()

    _refresh(c, pair["refresh_token"])  # a rotate refresh triggers the expired-revoked cleanup

    s = Store.from_env()
    try:
        hashes = {r["token_hash"] for r in s.query("SELECT token_hash FROM oauth_refresh_token")}
        assert "expired_revoked" not in hashes  # pruned
        assert "live_revoked" in hashes  # kept — still inside its validity window
    finally:
        s.close()


def test_ace050_refresh_mode_flag_default_and_validation(monkeypatch):
    import oauth_server

    monkeypatch.delenv("AGAMI_REFRESH_TOKEN_MODE", raising=False)
    assert oauth_server._refresh_token_mode() == "overwrite"  # default
    monkeypatch.setenv("AGAMI_REFRESH_TOKEN_MODE", "ROTATE")
    assert oauth_server._refresh_token_mode() == "rotate"  # case-insensitive
    monkeypatch.setenv("AGAMI_REFRESH_TOKEN_MODE", "garbage")
    assert oauth_server._refresh_token_mode() == "overwrite"  # invalid → default


def test_ace050_authorize_clears_used_and_expired_codes(env):
    # One-time codes are single-use, short-lived tickets; the authorize chokepoint prunes used or
    # expired ones (spent tickets), but never a valid, unused code.
    c = TestClient(mcp_http.build_app())
    s = Store.from_env()
    try:
        seeded = [
            ("used_code", "2999-01-01T00:00:00+00:00", 1),  # used → prune
            ("expired_code", "2000-01-01T00:00:00+00:00", 0),  # expired → prune
            ("valid_code", "2999-01-01T00:00:00+00:00", 0),  # valid + unused → keep
        ]
        for code, exp, used in seeded:
            s.execute(
                "INSERT INTO oauth_state (code, client_id, redirect_uri, code_challenge, username, "
                "expires_at, used, created) VALUES (?, 'cid', ?, 'ch', 'admin', ?, ?, ?)",
                (code, REDIRECT, exp, used, "2000-01-01T00:00:00+00:00"),
            )
        s.commit()
    finally:
        s.close()

    _authorize_code(c)  # a fresh authorize runs the spent-code cleanup

    s = Store.from_env()
    try:
        codes = {r["code"] for r in s.query("SELECT code FROM oauth_state")}
        assert "used_code" not in codes and "expired_code" not in codes  # spent tickets pruned
        assert "valid_code" in codes  # valid, unused code preserved
    finally:
        s.close()


def test_ace050_query_executions_ts_index_exists(env):
    # The retained query log is never pruned, so it must stay fast to read newest-first — index ts
    # (migration 011). The test DB is sqlite, so check its catalog.
    s = Store.from_env()
    try:
        idx = s.query(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_query_executions_ts'"
        )
        assert idx, "idx_query_executions_ts should be created by migration 011"
    finally:
        s.close()


def test_ace050_hygiene_never_deletes_query_or_activity_logs(env):
    # The headline guarantee: the hygiene paths (refresh rotation, authorize code cleanup) must NEVER
    # touch the user-visible query/activity history. Seed both logs, run the hygiene chokepoints, and
    # assert every seeded row survives.
    c = TestClient(mcp_http.build_app())
    s = Store.from_env()
    try:
        s.execute(
            "INSERT INTO query_executions (id, ts, datasource, question, sql, row_count, source) "
            "VALUES ('q1', ?, 'acme', 'q', 'SELECT 1', 1, 'mcp_server')",
            ("2020-01-01T00:00:00+00:00",),
        )
        s.execute(
            "INSERT INTO tool_calls (id, ts, tool_name, success) VALUES ('t1', ?, 'execute_sql', 1)",
            ("2020-01-01T00:00:00+00:00",),
        )
        s.commit()
    finally:
        s.close()

    pair = _token_pair(c)  # authorize (runs code cleanup) + issue
    _refresh(c, pair["refresh_token"])  # refresh (runs token hygiene)

    s = Store.from_env()
    try:
        assert s.query("SELECT id FROM query_executions WHERE id = 'q1'"), (
            "query log must be retained"
        )
        assert s.query("SELECT id FROM tool_calls WHERE id = 't1'"), "activity log must be retained"
    finally:
        s.close()


def test_refresh_rejects_missing_unknown_and_wrong_client(env):
    c = TestClient(mcp_http.build_app())
    # missing / unknown token
    assert c.post("/oauth/token", data={"grant_type": "refresh_token"}).status_code == 400
    assert (
        c.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "nope"}
        ).status_code
        == 400
    )
    first = _token_pair(c)
    # a wrong client_id (when one is supplied) is rejected — and does NOT rotate the token
    wrong = c.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": "someone-else",
        },
    )
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    # the token still works with the right client (the failed attempt didn't burn it)
    assert _refresh(c, first["refresh_token"])["access_token"]


def test_refresh_with_client_id_when_the_token_has_no_client(env):
    # An authorize can complete without a client_id, so a refresh token's stored client_id may be
    # blank. A later refresh that DOES send a client_id must not spuriously fail the bind check —
    # binding is only enforced when BOTH sides present one. (We blank the stored client_id directly
    # rather than re-run authorize, to keep the test focused on the bind logic.)
    c = TestClient(mcp_http.build_app())
    pair = _token_pair(c)
    s = Store.from_env()
    try:
        s.execute("UPDATE oauth_refresh_token SET client_id = ? WHERE revoked = 0", ("",))
        s.commit()
    finally:
        s.close()
    # refresh WITH a client_id succeeds (blank stored client_id → binding skipped, not a mismatch)
    assert _refresh(c, pair["refresh_token"], client_id="cid")["access_token"]


def test_refresh_token_expiry_is_enforced(env):
    c = TestClient(mcp_http.build_app())
    first = _token_pair(c)
    s = Store.from_env()
    try:
        s.execute(
            "UPDATE oauth_refresh_token SET expires_at = ? WHERE revoked = 0",
            ("2000-01-01T00:00:00+00:00",),
        )
        s.commit()
    finally:
        s.close()
    expired = c.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": "cid",
        },
    )
    assert expired.status_code == 400 and expired.json()["error"] == "invalid_grant"


def test_refresh_token_is_stored_hashed_not_plaintext(env):
    import oauth_server

    c = TestClient(mcp_http.build_app())
    rt = _token_pair(c)["refresh_token"]
    s = Store.from_env()
    try:
        stored = [r["token_hash"] for r in s.query("SELECT token_hash FROM oauth_refresh_token")]
    finally:
        s.close()
    assert rt not in stored  # never the plaintext
    assert oauth_server._hash_token(rt) in stored  # the sha256 hash is what's persisted


def test_metadata_advertises_refresh_grant(env):
    c = TestClient(mcp_http.build_app())
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["grant_types_supported"] == ["authorization_code", "refresh_token"]


def test_token_ttls_are_env_configurable_and_fail_safe(env, monkeypatch):
    from datetime import timedelta

    import oauth_server

    monkeypatch.delenv("AGAMI_ACCESS_TOKEN_TTL", raising=False)
    monkeypatch.delenv("AGAMI_REFRESH_TOKEN_TTL", raising=False)
    assert oauth_server._access_ttl() == timedelta(hours=1)  # defaults
    assert oauth_server._refresh_ttl() == timedelta(days=30)

    monkeypatch.setenv("AGAMI_ACCESS_TOKEN_TTL", "1800")  # explicit override
    assert oauth_server._access_ttl() == timedelta(seconds=1800)
    # garbage / non-positive / out-of-range (would overflow timedelta) → default (fail-safe, no crash)
    for bad in ("not-a-number", "0", "-5", "", "999999999999999"):
        monkeypatch.setenv("AGAMI_ACCESS_TOKEN_TTL", bad)
        assert oauth_server._access_ttl() == timedelta(hours=1)

    monkeypatch.setenv("AGAMI_ACCESS_TOKEN_TTL", "1800")  # and the override reaches the wire
    assert _token_pair(TestClient(mcp_http.build_app()))["expires_in"] == 1800


# --- `sid`: telling two clients of one login apart -----------------------------


def _sid(access_token: str) -> str | None:
    return jwt.decode(access_token, SECRET, algorithms=["HS256"], issuer=BASE).get("sid")


def test_access_token_carries_the_hashed_refresh_family_as_sid(env):
    """The claim is derived from the family, never the family itself: that value is what reuse detection
    burns on (`WHERE family = ?`), so it must not leave the server."""
    import hashlib

    c = TestClient(mcp_http.build_app())
    body = _token_pair(c)
    s = Store.from_env()
    families = [r["family"] for r in s.query("SELECT family FROM oauth_refresh_token")]
    s.close()
    assert len(families) == 1
    expected = hashlib.sha256(families[0].encode()).hexdigest()[:16]
    assert _sid(body["access_token"]) == expected
    assert families[0] not in body["access_token"]  # the raw lineage key never ships


def test_sid_survives_a_rotation(env, monkeypatch):
    """THE property that makes the family the right key and a per-token id the wrong one: a `jti` would
    change on every refresh, so anything keyed on it would silently reset each hour."""
    monkeypatch.setenv("AGAMI_REFRESH_TOKEN_MODE", "rotate")
    c = TestClient(mcp_http.build_app())
    first = _token_pair(c)
    second = _refresh(c, first["refresh_token"])
    third = _refresh(c, second["refresh_token"])
    assert _sid(first["access_token"])  # present
    assert _sid(second["access_token"]) == _sid(first["access_token"])
    assert _sid(third["access_token"]) == _sid(first["access_token"])
    # ...even though the refresh token itself changed every time.
    assert first["refresh_token"] != second["refresh_token"] != third["refresh_token"]


def test_sid_survives_an_overwrite_renewal(env, monkeypatch):
    # 'overwrite' is the default and takes a different code path (UPDATE in place, no new row), so the
    # continuity has to be asserted separately from rotate's.
    monkeypatch.setenv("AGAMI_REFRESH_TOKEN_MODE", "overwrite")
    c = TestClient(mcp_http.build_app())
    first = _token_pair(c)
    second = _refresh(c, first["refresh_token"])
    assert _sid(second["access_token"]) == _sid(first["access_token"])


def test_two_authorizations_get_different_sids(env):
    """The requirement itself: one login, two independent client authorizations, two identities. Without
    this the server cannot tell two concurrent windows apart at all."""
    c = TestClient(mcp_http.build_app())
    a, b = _token_pair(c), _token_pair(c)
    assert _sid(a["access_token"]) and _sid(b["access_token"])
    assert _sid(a["access_token"]) != _sid(b["access_token"])


def test_a_token_minted_without_a_sid_still_validates(env):
    """Tokens predating this claim, and any minted outside the OAuth flow (scripts, tests, an operator's
    hand-rolled bearer), must keep working — `sid` is deliberately not in the `require` list."""
    from oauth_server import JwtAuthProvider, issue_jwt

    token = issue_jwt("admin")
    assert "sid" not in jwt.decode(token, SECRET, algorithms=["HS256"], issuer=BASE)
    principal = JwtAuthProvider().validate_token(token)
    assert principal is not None and principal.subject == "admin"
    assert principal.session_id is None  # absent reads as "no session", not as an error


def test_validate_token_surfaces_the_sid_on_the_principal(env):
    from oauth_server import JwtAuthProvider, issue_jwt

    token = issue_jwt("admin", sid="abc123")
    principal = JwtAuthProvider().validate_token(token)
    assert principal is not None and principal.session_id == "abc123"


def test_a_malformed_sid_degrades_to_none_rather_than_rejecting(env):
    # A blank/non-string claim must not cost an otherwise-valid caller their request.
    from oauth_server import JwtAuthProvider

    for bad in ("", "   ", 42):
        token = jwt.encode(
            {
                "sub": "admin",
                "iss": BASE,
                "iat": 1,
                "exp": 4102444800,  # 2100-01-01, comfortably unexpired
                "sid": bad,
            },
            SECRET,
            algorithm="HS256",
        )
        principal = JwtAuthProvider().validate_token(token)
        assert principal is not None and principal.session_id is None, bad


def test_a_blank_sid_is_never_minted(env):
    """Mint and validate must use the same rule. A whitespace-only `sid` would otherwise ship on the wire
    and then normalize away on the way back in — a claim that exists but can never reach a consumer."""
    from oauth_server import issue_jwt

    for bad in ("", "   ", None):
        claims = jwt.decode(issue_jwt("admin", sid=bad), SECRET, algorithms=["HS256"], issuer=BASE)
        assert "sid" not in claims, bad
    assert _sid(issue_jwt("admin", sid="real")) == "real"


# --- the redirect allow-list for dynamically registered clients ---
#
# `/oauth/register` is unauthenticated, so a URI a client registered says nothing about who owns it: a
# stranger could register their own page and send a victim a real sign-in link naming that client.
# Registration and every use of a registered URI therefore go through one allow-list.

CHATGPT_STABLE = "https://chatgpt.com/connector_platform_oauth_redirect"
CHATGPT_PER_CALLBACK = "https://chatgpt.com/connector/oauth/abc123_XY-z"
EXTRA_ENV = "AGAMI_OAUTH_REDIRECT_URIS"


def _register(c: TestClient, uris) -> object:
    return c.post("/oauth/register", json={"redirect_uris": uris})


@pytest.mark.parametrize(
    "uri",
    [
        REDIRECT,  # Claude's callback
        "https://claude.com/api/mcp/auth_callback",
        BASE + "/app/oauth/callback",  # same origin as PUBLIC_BASE_URL
        "http://127.0.0.1:33418/callback",  # RFC 8252 loopback, any port
        "http://127.0.0.1/callback",
        "http://[::1]:8080/cb",
        "http://localhost:6274/oauth/callback",
        CHATGPT_STABLE,
        CHATGPT_PER_CALLBACK,
    ],
)
def test_register_accepts_an_allow_listed_redirect(env, uri):
    r = _register(TestClient(mcp_http.build_app()), [uri])
    assert r.status_code == 201, r.text
    assert r.json()["redirect_uris"] == [uri]


@pytest.mark.parametrize(
    "uri",
    [
        "https://attacker.example/cb",
        "http://example.com/cb",  # non-loopback http
        "https://your-host.example.com.evil.example/cb",  # prefixes PUBLIC_BASE_URL, other origin
        "http://your-host.example.com/cb",  # PUBLIC_BASE_URL's host, downgraded to http
        "https://user@your-host.example.com/cb",  # userinfo
        BASE + "/cb#frag",  # RFC 6749 §3.1.2: a redirection endpoint MUST NOT carry a fragment
        "https://claude.ai/api/mcp/auth_callback/../../steal",
        "https://chatgpt.com/connector/oauth/",  # the per-callback form needs an id
        "https://chatgpt.com/connector/oauth/a/b",
        "https://chatgpt.com/connector/oauth/abc?next=https://attacker.example",
        "http://127.0.0.1.attacker.example/cb",  # a host that merely starts with a loopback address
        "https://127.0.0.1:443/cb",  # loopback is the http form only (RFC 8252 §7.3)
        "http://[::1/cb",  # unparseable
        BASE + "/a b",  # whitespace would split into two URIs in the stored list
        "",
    ],
)
def test_register_refuses_a_redirect_off_the_allow_list(env, uri):
    r = _register(TestClient(mcp_http.build_app()), [uri])
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_register_refuses_the_whole_request_when_one_redirect_is_off_the_list(env, tmp_path):
    r = _register(TestClient(mcp_http.build_app()), [REDIRECT, "https://attacker.example/cb"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
    s = Store.connect(env)
    try:
        assert s.query("SELECT client_id FROM oauth_client") == []  # nothing was minted
    finally:
        s.close()


@pytest.mark.parametrize("body", [{}, {"redirect_uris": []}])
def test_register_refuses_a_client_with_no_redirect_uris(env, body):
    # RFC 7591 §2: a client of a redirect-based flow must register its redirect URIs, and the only flow
    # served here is the authorization code. A client without one could never be sent a code.
    r = TestClient(mcp_http.build_app()).post("/oauth/register", json=body)
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


def test_register_accepts_operator_configured_extras(env, monkeypatch):
    monkeypatch.setenv(
        EXTRA_ENV, "https://partner.example, https://tools.example/exact/cb  myapp://oauth/cb"
    )
    c = TestClient(mcp_http.build_app())
    for uri in (
        "https://partner.example/any/path",  # an https origin entry admits that origin
        "https://tools.example/exact/cb",  # an exact entry admits exactly itself
        "myapp://oauth/cb",
    ):
        assert _register(c, [uri]).status_code == 201, uri
    for uri in (
        "https://tools.example/exact/cb/more",  # exact means exact
        "https://tools.example/other",
        "https://partner.example.evil.example/cb",
        "http://partner.example/cb",  # the origin entry is https; http is a different origin
    ):
        assert _register(c, [uri]).status_code == 400, uri


def test_operator_extras_never_take_a_wildcard(env, monkeypatch):
    monkeypatch.setenv(EXTRA_ENV, "https://*.example, *, http://, https://bad.example/*")
    c = TestClient(mcp_http.build_app())
    for uri in ("https://a.example/cb", "https://attacker.example/cb", "https://bad.example/x"):
        assert _register(c, [uri]).status_code == 400, uri


def _seed_client(db_url: str, client_id: str, redirect_uris: str) -> None:
    """Insert a client row directly — the way a registration made before the allow-list existed sits
    in a live database."""
    s = Store.connect(db_url)
    try:
        s.execute(
            "INSERT INTO oauth_client (client_id, redirect_uris, created) VALUES (?, ?, ?)",
            (client_id, redirect_uris, "2026-01-01T00:00:00Z"),
        )
        s.commit()
    finally:
        s.close()


def test_authorize_refuses_a_pre_existing_client_registered_with_an_attacker_redirect(env):
    _seed_client(env, "old-client", "https://attacker.example/cb")
    r = TestClient(mcp_http.build_app()).post(
        "/oauth/authorize",
        data={
            "username": "admin",
            "password": ADMIN_PW,
            "redirect_uri": "https://attacker.example/cb",
            "client_id": "old-client",
            "code_challenge": _challenge(VERIFIER),
            "state": "xyz",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"
    s = Store.connect(env)
    try:
        assert s.query("SELECT code FROM oauth_state") == []  # no code was minted for it
    finally:
        s.close()


def test_redirect_allowed_no_longer_trusts_a_registered_uri_on_its_own(env, monkeypatch):
    from oauth_server import _redirect_allowed

    assert not _redirect_allowed("https://attacker.example/cb", "https://attacker.example/cb")
    # An allow-listed URI that is not a fixed callback still needs the client to have registered it…
    loopback = "http://127.0.0.1:5000/cb"
    assert not _redirect_allowed(loopback, None)
    assert not _redirect_allowed(loopback, "http://127.0.0.1:5001/cb")
    assert _redirect_allowed(loopback, f"{REDIRECT} {loopback}")
    assert _redirect_allowed(CHATGPT_PER_CALLBACK, CHATGPT_PER_CALLBACK)
    # …and Claude's callback and a same-origin URI pass without one, as they always have.
    assert _redirect_allowed(REDIRECT, None)
    assert _redirect_allowed(BASE + "/callback", None)
    # An operator extra that is later removed from the setting stops working for clients holding it.
    monkeypatch.setenv(EXTRA_ENV, "https://partner.example")
    assert _redirect_allowed("https://partner.example/cb", "https://partner.example/cb")
    monkeypatch.delenv(EXTRA_ENV)
    assert not _redirect_allowed("https://partner.example/cb", "https://partner.example/cb")


def test_a_registered_loopback_client_completes_the_flow(env):
    c = TestClient(mcp_http.build_app())
    loopback = "http://127.0.0.1:33418/callback"
    client_id = _register(c, [loopback]).json()["client_id"]
    r = c.post(
        "/oauth/authorize",
        data={
            "username": "admin",
            "password": ADMIN_PW,
            "redirect_uri": loopback,
            "client_id": client_id,
            "code_challenge": _challenge(VERIFIER),
            "state": "xyz",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["location"].startswith(loopback + "?")
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    t = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": loopback,
        },
    )
    assert t.status_code == 200 and t.json()["access_token"]


def test_issuing_a_code_rechecks_the_redirect_whoever_calls_it(env):
    # The one place a code is minted refuses a disallowed redirect itself, so a caller that skipped the
    # check (or carried a redirect vetted under an older allow-list) still cannot deliver a code.
    from oauth_server import _issue_authorization_code

    s = Store.connect(env)
    try:
        r = _issue_authorization_code(
            s,
            client_id="cid",
            redirect_uri="https://attacker.example/cb",
            code_challenge=_challenge(VERIFIER),
            username="admin",
            client_state="xyz",
        )
        assert r.status_code == 400 and "location" not in r.headers
        assert s.query("SELECT code FROM oauth_state") == []
    finally:
        s.close()


def test_a_malformed_operator_extra_is_ignored_and_the_valid_ones_still_work(
    env, monkeypatch, caplog
):
    # One unparseable entry in the setting must not take registration and sign-in down with a 500.
    import oauth_server

    oauth_server._parse_extra_redirects.cache_clear()  # warned once per value, per process
    monkeypatch.setenv(
        EXTRA_ENV, "https://[::1, https://partner.example, https://bad.example:99999"
    )
    c = TestClient(mcp_http.build_app())
    with caplog.at_level("WARNING", logger="oauth_server"):
        r = _register(c, ["https://partner.example/cb"])
        assert r.status_code == 201, r.text
        a = c.post(
            "/oauth/authorize",
            data={
                "username": "admin",
                "password": ADMIN_PW,
                "redirect_uri": "https://partner.example/cb",
                "client_id": r.json()["client_id"],
                "code_challenge": _challenge(VERIFIER),
                "state": "xyz",
            },
            follow_redirects=False,
        )
    assert a.status_code == 302 and a.headers["location"].startswith("https://partner.example/cb?")
    assert _register(c, ["https://bad.example:99999/cb"]).status_code == 400
    warnings = [rec.getMessage() for rec in caplog.records if EXTRA_ENV in rec.getMessage()]
    # Said once for this setting, however many requests read it, and never quoting the entry: an
    # operator may have pasted a URI with credentials in it.
    assert len(warnings) == 1
    assert "[::1" not in warnings[0] and "bad.example" not in warnings[0]


@pytest.mark.parametrize("uri", ["https://[::1/cb", "https://partner.example:99999/cb"])
def test_a_malformed_redirect_is_refused_never_a_500(env, monkeypatch, uri):
    # A malformed operator extra beside it must not change that either.
    monkeypatch.setenv(EXTRA_ENV, "https://[::1, https://partner.example")
    c = TestClient(mcp_http.build_app())
    r = _register(c, [uri])
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
    a = c.post(
        "/oauth/authorize",
        data={
            "username": "admin",
            "password": ADMIN_PW,
            "redirect_uri": uri,
            "client_id": "cid",
            "code_challenge": _challenge(VERIFIER),
            "state": "xyz",
        },
        follow_redirects=False,
    )
    assert a.status_code == 400 and a.json()["error"] == "invalid_request"
