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
AUD = f"{BASE}/mcp"  # the canonical resource: every access token is minted for, and checked against, it
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
    claims = jwt.decode(body["access_token"], SECRET, algorithms=["HS256"], audience=AUD)
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
    forged = jwt.encode(
        {"sub": "admin", "iss": BASE, "aud": AUD, "exp": 9_999_999_999}, "wrong", "HS256"
    )
    assert provider.validate_token(forged) is None


def test_jwt_provider_rejects_alg_none_and_alg_confusion(env):
    # The two classic JWT forgeries: an unsigned alg=none token, and a token whose header claims a
    # different algorithm than the HS256 we pin. Both must be rejected.
    from oauth_server import JwtAuthProvider

    provider = JwtAuthProvider()
    claims = {"sub": "admin", "iss": BASE, "aud": AUD, "exp": 9_999_999_999}
    assert provider.validate_token(jwt.encode(claims, None, algorithm="none")) is None
    assert provider.validate_token(jwt.encode(claims, SECRET, algorithm="HS512")) is None


def test_jwt_provider_rejects_token_from_a_different_issuer(env):
    from oauth_server import JwtAuthProvider

    forged = jwt.encode(
        {"sub": "admin", "iss": "https://evil.example.com", "aud": AUD, "exp": 9_999_999_999},
        SECRET,
        "HS256",
    )
    assert JwtAuthProvider().validate_token(forged) is None


def test_jwt_provider_rejects_non_string_or_blank_sub(env):
    from oauth_server import JwtAuthProvider

    provider = JwtAuthProvider()
    numeric = jwt.encode(
        {"sub": 123, "iss": BASE, "aud": AUD, "exp": 9_999_999_999}, SECRET, "HS256"
    )
    blank = jwt.encode(
        {"sub": "   ", "iss": BASE, "aud": AUD, "exp": 9_999_999_999}, SECRET, "HS256"
    )
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
    claims = jwt.decode(
        second["access_token"], SECRET, algorithms=["HS256"], issuer=BASE, audience=AUD
    )
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
    return jwt.decode(access_token, SECRET, algorithms=["HS256"], issuer=BASE, audience=AUD).get(
        "sid"
    )


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
    assert "sid" not in jwt.decode(token, SECRET, algorithms=["HS256"], issuer=BASE, audience=AUD)
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
                "aud": AUD,
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
        claims = jwt.decode(
            issue_jwt("admin", sid=bad), SECRET, algorithms=["HS256"], issuer=BASE, audience=AUD
        )
        assert "sid" not in claims, bad
    assert _sid(issue_jwt("admin", sid="real")) == "real"


# --- audience and resource: a token is for this server's /mcp and nothing else -------------------------


def test_metadata_advertises_cimd_iss_and_none_auth(env):
    c = TestClient(mcp_http.build_app())
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["client_id_metadata_document_supported"] is True
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    assert meta["authorization_response_iss_parameter_supported"] is True
    # The protected-resource document and the token audience name the same resource.
    assert c.get("/.well-known/oauth-protected-resource").json()["resource"] == AUD


def test_an_access_token_is_minted_for_the_canonical_resource(env):
    c = TestClient(mcp_http.build_app())
    token = _token_pair(c)["access_token"]
    assert jwt.decode(token, SECRET, algorithms=["HS256"], audience=AUD)["aud"] == AUD


def test_jwt_provider_rejects_a_token_without_or_with_the_wrong_audience(env):
    from oauth_server import JwtAuthProvider

    provider = JwtAuthProvider()
    claims = {"sub": "admin", "iss": BASE, "exp": 9_999_999_999}
    assert provider.validate_token(jwt.encode(claims, SECRET, "HS256")) is None
    for wrong in (f"{BASE}/other", "https://other.example.com/mcp", [f"{BASE}/other"]):
        token = jwt.encode({**claims, "aud": wrong}, SECRET, "HS256")
        assert provider.validate_token(token) is None, wrong
    assert provider.validate_token(jwt.encode({**claims, "aud": AUD}, SECRET, "HS256")) is not None


def _code_exchange(client: TestClient, **extra: str):
    code = _authorize_code(client)
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT,
            **extra,
        },
    )


@pytest.mark.parametrize("resource", [AUD, AUD + "/"])
def test_the_canonical_resource_is_served_at_the_token_endpoint(env, resource):
    c = TestClient(mcp_http.build_app())
    r = _code_exchange(c, resource=resource)
    assert r.status_code == 200, r.text
    refreshed = c.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": r.json()["refresh_token"],
            "resource": resource,
        },
    )
    assert refreshed.status_code == 200, refreshed.text


@pytest.mark.parametrize(
    "resource", [f"{BASE}/other", "https://other.example.com/mcp", AUD + "//", BASE]
)
def test_a_wrong_resource_is_invalid_target_on_both_grants(env, resource):
    c = TestClient(mcp_http.build_app())
    wrong = _code_exchange(c, resource=resource)
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_target"
    pair = _token_pair(c)
    refreshed = c.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": pair["refresh_token"],
            "resource": resource,
        },
    )
    assert refreshed.status_code == 400 and refreshed.json()["error"] == "invalid_target"
    # The refusal happens before the grant runs, so the refresh token was not spent on it.
    assert _refresh(c, pair["refresh_token"])["access_token"]


def test_a_refresh_token_issued_before_audiences_renews_into_a_token_with_one(env):
    # A deployment upgrading in place has refresh rows minted before tokens carried `aud`. Those rows
    # hold no claims at all, so they must keep renewing, and what they renew into carries the audience.
    import oauth_server

    presented = "pre-audience-refresh-token"
    s = Store.from_env()
    try:
        s.execute(
            "INSERT INTO oauth_refresh_token (token_hash, family, client_id, username, expires_at, "
            "revoked, created) VALUES (?, 'old-family', 'cid', 'admin', ?, 0, ?)",
            (
                oauth_server._hash_token(presented),
                "2999-01-01T00:00:00+00:00",
                "2000-01-01T00:00:00+00:00",
            ),
        )
        s.commit()
    finally:
        s.close()
    c = TestClient(mcp_http.build_app())
    renewed = _refresh(c, presented)
    claims = jwt.decode(renewed["access_token"], SECRET, algorithms=["HS256"], audience=AUD)
    assert claims["aud"] == AUD and claims["sub"] == "admin"


# --- clients identified by a metadata-document URL --------------------------------------------------

CLIENT_DOC_URL = "https://app.example.com/oauth/client.json"
DOC_REDIRECT = "https://app.example.com/callback"
LOOPBACK_REDIRECT = "http://127.0.0.1/callback"


@pytest.fixture
def client_doc(monkeypatch):
    """Serve a client metadata document with no network: DNS answers a global address nobody connects
    to, and the transport is mocked. Returns the list of requests the transport saw, and the document."""
    httpx = pytest.importorskip("httpx")
    import client_metadata

    doc = {
        "client_id": CLIENT_DOC_URL,
        "client_name": "Claude",  # what an impostor would write; the page must not show it
        "redirect_uris": [DOC_REDIRECT, LOOPBACK_REDIRECT],
    }
    requests: list = []

    async def body():
        yield httpx.Response(200, json=doc).content

    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=body())

    async def resolve(host, port):
        return ["11.0.0.1"]

    client_metadata._cache.clear()
    monkeypatch.setattr(client_metadata, "_resolve", resolve)
    monkeypatch.setattr(
        client_metadata, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    yield requests, doc
    client_metadata._cache.clear()


def _authorize_post(client: TestClient, **fields: str):
    data = {
        "username": "admin",
        "password": "s3cret-pw",
        "redirect_uri": REDIRECT,
        "client_id": "cid",
        "code_challenge": _challenge(VERIFIER),
        "state": "xyz",
        **fields,
    }
    return client.post("/oauth/authorize", data=data, follow_redirects=False)


def _oauth_client_count() -> int:
    s = Store.from_env()
    try:
        return s.query("SELECT COUNT(*) AS n FROM oauth_client")[0]["n"]
    finally:
        s.close()


def test_metadata_document_client_signs_in_without_registering(env, client_doc):
    requests, _ = client_doc
    c = TestClient(mcp_http.build_app())
    before = _oauth_client_count()
    r = _authorize_post(c, client_id=CLIENT_DOC_URL, redirect_uri=DOC_REDIRECT)
    assert r.status_code == 302, r.text
    loc = urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == DOC_REDIRECT
    qs = parse_qs(loc.query)
    assert qs["state"] == ["xyz"] and qs["iss"] == [BASE]
    tok = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": qs["code"][0],
            "code_verifier": VERIFIER,
            "redirect_uri": DOC_REDIRECT,
            "client_id": CLIENT_DOC_URL,
            "resource": AUD,
        },
    )
    assert tok.status_code == 200, tok.text
    claims = jwt.decode(tok.json()["access_token"], SECRET, algorithms=["HS256"], audience=AUD)
    assert claims["sub"] == "admin"
    assert _oauth_client_count() == before  # no registration row: that growth is what this replaces
    assert len(requests) == 1


def test_the_metadata_fetch_takes_no_worker_thread(env, client_doc, monkeypatch):
    # The worker pool is shared with every query. A sign-in anyone can start, pointed at a slow URL, must
    # not be able to occupy its threads, so the fetch stays on the event loop.
    import oauth_server

    offloaded = []
    real = oauth_server.run_blocking

    async def recording(func, *args, **kwargs):
        offloaded.append(func)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(oauth_server, "run_blocking", recording)
    requests, _ = client_doc
    # A redirect the document does not list: refused at the gate, before any credential check offloads.
    r = _authorize_post(TestClient(mcp_http.build_app()), client_id=CLIENT_DOC_URL)
    assert r.status_code == 400 and len(requests) == 1
    assert offloaded == []


def test_an_invalid_metadata_document_is_invalid_client_with_no_redirect(env, client_doc):
    _, doc = client_doc
    doc["client_id"] = "https://other.example.com/oauth/client.json"  # vouches for someone else
    c = TestClient(mcp_http.build_app())
    r = _authorize_post(c, client_id=CLIENT_DOC_URL, redirect_uri=DOC_REDIRECT)
    assert r.status_code == 400 and r.json()["error"] == "invalid_client"
    assert "location" not in r.headers


def test_a_registered_client_with_no_datastore_is_a_server_error_not_a_redirect(env, monkeypatch):
    # The gate looks up a registered client itself, so it answers a missing datastore before any
    # handler opens one; the sign-in must stop there rather than send a code anywhere.
    monkeypatch.delenv("AGAMI_DB_URL")
    r = _authorize_post(TestClient(mcp_http.build_app()), client_id="cid", redirect_uri=REDIRECT)
    assert r.status_code == 500 and r.json()["error"] == "server_error"
    assert "location" not in r.headers


def test_an_http_metadata_url_is_refused_not_looked_up(env, client_doc):
    requests, _ = client_doc
    r = _authorize_post(
        TestClient(mcp_http.build_app()),
        client_id="http://app.example.com/oauth/client.json",
        redirect_uri=REDIRECT,
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_client"
    assert requests == []


def test_a_non_ascii_metadata_url_is_invalid_client_not_a_server_error(env, client_doc):
    requests, _ = client_doc
    r = _authorize_post(
        TestClient(mcp_http.build_app(), raise_server_exceptions=False),
        client_id="https://ünïcode.example.com/oauth/client.json",
        redirect_uri=DOC_REDIRECT,
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_client"
    assert requests == []


def test_a_metadata_client_may_not_use_a_redirect_its_document_does_not_list(env, client_doc):
    # The Claude callback and same-origin fallbacks are for registered clients only. A document names its
    # own redirect URIs, and anything else would let any URL claim a code bound for Claude.
    c = TestClient(mcp_http.build_app())
    for redirect in (REDIRECT, BASE + "/callback", "https://evil.example.com/steal"):
        r = _authorize_post(c, client_id=CLIENT_DOC_URL, redirect_uri=redirect)
        assert r.status_code == 400 and r.json()["error"] == "invalid_request", redirect
        assert "location" not in r.headers


def test_a_metadata_client_loopback_redirect_matches_on_any_port(env, client_doc):
    # A native client listens on whatever port the OS gives it, so its document cannot list the port.
    r = _authorize_post(
        TestClient(mcp_http.build_app()),
        client_id=CLIENT_DOC_URL,
        redirect_uri="http://127.0.0.1:53123/callback",
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("http://127.0.0.1:53123/callback?")


def test_a_registered_client_loopback_redirect_matches_on_any_port(env):
    c = TestClient(mcp_http.build_app())
    cid = c.post("/oauth/register", json={"redirect_uris": [LOOPBACK_REDIRECT]}).json()["client_id"]
    r = _authorize_post(c, client_id=cid, redirect_uri="http://localhost:53123/callback")
    assert r.status_code == 400  # a different loopback NAME is not the registered one
    r = _authorize_post(c, client_id=cid, redirect_uri="http://127.0.0.1:53123/callback")
    assert r.status_code == 302


@pytest.mark.parametrize(
    ("uri", "allowed", "expected"),
    [
        ("https://app.example.com/callback", "https://app.example.com/callback", True),
        ("http://127.0.0.1:53123/callback", "http://127.0.0.1/callback", True),
        ("http://localhost:1/callback?x=1", "http://localhost:2/callback?x=1", True),
        ("http://127.0.0.1:53123/other", "http://127.0.0.1/callback", False),
        ("http://127.0.0.1:53123/callback?x=2", "http://127.0.0.1/callback?x=1", False),
        ("http://localhost:53123/callback", "http://127.0.0.1/callback", False),
        ("https://127.0.0.1:53123/callback", "https://127.0.0.1/callback", False),
        ("http://127.0.0.1.example.com:1/callback", "http://127.0.0.1.example.com/callback", False),
        ("http://[::1]:53123/callback", "http://[::1]/callback", False),
        ("http://evil@127.0.0.1:53123/callback", "http://127.0.0.1/callback", False),
        ("https://app.example.com:8443/callback", "https://app.example.com/callback", False),
        ("http://[bad:53123/callback", "http://127.0.0.1/callback", False),  # unparseable
        # A "port" that runs on into a name: the hostname still parses as loopback, the port does not.
        ("http://127.0.0.1:53123.evil.example.com/callback", "http://127.0.0.1/callback", False),
    ],
)
def test_redirect_matches(uri, allowed, expected):
    from oauth_server import _redirect_matches

    assert _redirect_matches(uri, allowed) is expected


def test_the_sign_in_page_names_the_redirect_host_not_the_document(env, client_doc):
    requests, _ = client_doc
    r = TestClient(mcp_http.build_app()).get(
        "/oauth/authorize",
        params={"client_id": CLIENT_DOC_URL, "redirect_uri": DOC_REDIRECT, "state": "xyz"},
    )
    assert r.status_code == 200
    assert '<p class="who">app.example.com</p>' in r.text
    assert (
        '<p class="who">Claude</p>' not in r.text
    )  # the document's self-chosen name is never shown
    assert requests == []  # rendering the page fetches nothing


def test_the_sign_in_page_survives_an_unparseable_redirect(env, client_doc):
    r = TestClient(mcp_http.build_app()).get(
        "/oauth/authorize", params={"client_id": CLIENT_DOC_URL, "redirect_uri": "http://[bad"}
    )
    assert r.status_code == 200 and '<p class="who">' not in r.text


def test_the_resource_rides_the_form_to_the_post(env):
    r = TestClient(mcp_http.build_app()).get(
        "/oauth/authorize", params={"redirect_uri": REDIRECT, "resource": AUD}
    )
    assert f'name="resource" value="{AUD}"' in r.text


def test_iss_is_on_the_success_redirect(env):
    r = _authorize_post(TestClient(mcp_http.build_app()))
    assert parse_qs(urlparse(r.headers["location"]).query)["iss"] == [BASE]


def test_a_wrong_resource_at_authorize_redirects_invalid_target_with_iss(env):
    c = TestClient(mcp_http.build_app())
    r = _authorize_post(c, resource="https://other.example.com/mcp")
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == REDIRECT
    assert parse_qs(loc.query) == {"error": ["invalid_target"], "state": ["xyz"], "iss": [BASE]}
    # ...but only to a redirect URI that passed the allow-list: an unvetted one gets no redirect at all.
    bad = _authorize_post(
        c, resource="https://other.example.com/mcp", redirect_uri="https://evil.example.com/cb"
    )
    assert bad.status_code == 400 and "location" not in bad.headers


@pytest.mark.parametrize("resource", [AUD, AUD + "/"])
def test_the_canonical_resource_is_served_at_authorize(env, resource):
    r = _authorize_post(TestClient(mcp_http.build_app()), resource=resource)
    assert r.status_code == 302 and "code" in parse_qs(urlparse(r.headers["location"]).query)
