"""OIDC social login — the generic client + the onboarded-only flow, with NO real egress.

Verification is exercised for real: we generate an RSA keypair in-test, sign an ID token with it, and
feed the public key in as the IdP's signing key — so `verify_id_token` runs the actual RS256 +
aud/iss/exp/nonce/email_verified checks. The network seams (discovery, token exchange, JWKS) are
monkeypatched, so the suite makes no outbound call.
"""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("jwt")
pytest.importorskip("argon2")
pytest.importorskip("httpx")
pytest.importorskip("cryptography")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import jwt  # noqa: E402
import mcp_http  # noqa: E402
import oidc  # noqa: E402
import user_store  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from oauth_server import _resolve_oidc_user  # noqa: E402
from oidc import Identity  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

MS_TENANT = "00000000-0000-0000-0000-000000000000"  # a fake pinned tenant guid

BASE = "https://your-host.example.com"
SECRET = "x" * 40  # throwaway HS256 server secret (>=32 bytes); not real
ISSUER = "https://idp.example.com"
CLIENT_ID = "test-client-id"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
META = {
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "jwks_uri": f"{ISSUER}/jwks",
    "issuer": ISSUER,
}

VERIFIER = "v" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)

_PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIV_PEM = _PRIV.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
_PUB = _PRIV.public_key()


def _id_token(**overrides) -> str:
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": 9_999_999_999,
        "sub": "google-sub-123",
        "email": "you@example.com",
        "email_verified": True,
        "nonce": "the-nonce",
        **overrides,
    }
    return jwt.encode(claims, _PRIV_PEM, algorithm="RS256", headers={"kid": "test"})


class _FakeJWKClient:
    def __init__(self, uri):  # noqa: D401 - signature parity with jwt.PyJWKClient
        pass

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": _PUB})()


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_url = "sqlite://" + str(tmp_path / "oidc.db")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGAMI_OIDC_GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AGAMI_OIDC_GOOGLE_CLIENT_SECRET", "test-client-secret")
    # Point the provider's discovery + JWKS at our in-test IdP, and never touch the network.
    monkeypatch.setattr(oidc, "_discover", lambda p: META)
    monkeypatch.setattr(jwt, "PyJWKClient", _FakeJWKClient)
    oidc._jwks_clients.clear()  # the JWKS client is cached per process — reset between tests
    s = Store.connect(db_url)
    s.run_migrations()
    user_store.create_user(
        s, "alice", password=None, email="you@example.com", oidc_provider="google"
    )  # onboarded OIDC user, bound to Google
    s.close()
    return db_url


# --- the OIDC verifier in isolation (the security-critical core) -------------


def test_verify_id_token_accepts_a_valid_token(env):
    p = oidc.provider("google")
    identity = oidc.verify_id_token(p, _id_token(), nonce="the-nonce")
    assert identity.email == "you@example.com" and identity.subject == "google-sub-123"


@pytest.mark.parametrize(
    "bad",
    [
        {"aud": "someone-else"},  # wrong audience
        {"iss": "https://evil.example.com"},  # wrong issuer
        {"email_verified": False},  # unverified email
        {"exp": 1},  # expired
    ],
)
def test_verify_id_token_rejects_bad_claims(env, bad):
    p = oidc.provider("google")
    with pytest.raises(Exception):
        oidc.verify_id_token(p, _id_token(**bad), nonce="the-nonce")


def test_verify_id_token_rejects_nonce_mismatch(env):
    p = oidc.provider("google")
    with pytest.raises(Exception):
        oidc.verify_id_token(p, _id_token(nonce="other"), nonce="the-nonce")


def test_verify_id_token_rejects_a_token_signed_by_a_different_key(env):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "exp": 9_999_999_999,
            "nonce": "the-nonce",
            "email": "you@example.com",
            "email_verified": True,
        },
        other,
        algorithm="RS256",
    )
    with pytest.raises(Exception):
        oidc.verify_id_token(oidc.provider("google"), forged, nonce="the-nonce")


# --- the end-to-end flow over the transport ----------------------------------


def _client() -> TestClient:
    # https base_url so the Secure CSRF cookie round-trips in the test client.
    return TestClient(mcp_http.build_app(), base_url="https://testserver")


def _start_and_capture(c: TestClient):
    """Run /oauth/oidc/start, return (state_jwt, nonce) parsed from the IdP redirect."""
    r = c.get(
        "/oauth/oidc/start",
        params={
            "provider": "google",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "code_challenge": CHALLENGE,
            "state": "client-xyz",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    assert f"{ISSUER}/authorize" == f"{loc.scheme}://{loc.netloc}{loc.path}"
    q = parse_qs(loc.query)
    return q["state"][0], q["nonce"][0]


def test_full_oidc_flow_resumes_ace005_and_issues_a_jwt(env, monkeypatch):
    c = _client()
    state, nonce = _start_and_capture(c)
    # The IdP "redirects back": exchange returns an id_token carrying the nonce we minted.
    monkeypatch.setattr(
        oidc, "exchange_code", lambda p, *, code, redirect_uri: _id_token(nonce=nonce)
    )
    cb = c.get(
        "/oauth/oidc/callback",
        params={"code": "idp-code", "state": state},
        follow_redirects=False,
    )
    assert cb.status_code == 302
    back = urlparse(cb.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT
    code = parse_qs(back.query)["code"][0]
    assert parse_qs(back.query)["state"][0] == "client-xyz"
    # The minted OAuth code exchanges (with the matching PKCE verifier) for a JWT whose subject is
    # the resolved onboarded user — proving the OIDC leg resumed the OAuth flow end to end.
    tok = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT,
        },
    )
    assert tok.status_code == 200
    claims = jwt.decode(tok.json()["access_token"], SECRET, algorithms=["HS256"], issuer=BASE)
    assert claims["sub"] == "alice"


def test_callback_rejects_unknown_email_onboarded_only(env, monkeypatch):
    c = _client()
    state, nonce = _start_and_capture(c)
    monkeypatch.setattr(
        oidc,
        "exchange_code",
        lambda p, *, code, redirect_uri: _id_token(nonce=nonce, email="stranger@example.com"),
    )
    cb = c.get(
        "/oauth/oidc/callback",
        params={"code": "idp-code", "state": state},
        follow_redirects=False,
    )
    assert cb.status_code == 403  # not onboarded → rejected, never auto-created


def test_callback_rejects_tampered_state_or_missing_cookie(env, monkeypatch):
    c = _client()
    state, nonce = _start_and_capture(c)
    monkeypatch.setattr(
        oidc, "exchange_code", lambda p, *, code, redirect_uri: _id_token(nonce=nonce)
    )
    # Drop the CSRF cookie → the state can't be bound to this caller.
    c.cookies.clear()
    cb = c.get(
        "/oauth/oidc/callback",
        params={"code": "idp-code", "state": state},
        follow_redirects=False,
    )
    assert cb.status_code == 400


# --- admin social login (OIDC → admin SESSION, provider-pinned) --------------

ADMIN_EMAIL = "admin@example.com"


def _seed_admin(db_url, monkeypatch, *, provider="google"):
    """Make the configured admin a provider-bound user (username == email), as the seed would."""
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", ADMIN_EMAIL)
    monkeypatch.setenv("AGAMI_ADMIN_PROVIDER", provider)
    s = Store.connect(db_url)
    user_store.create_user(
        s, username=ADMIN_EMAIL, password=None, email=ADMIN_EMAIL, oidc_provider=provider
    )
    s.close()


def _admin_start(c: TestClient, provider: str = "google"):
    r = c.get("/admin/oidc/start", params={"provider": provider}, follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    return q["state"][0], q["nonce"][0]


def test_admin_oidc_login_mints_a_session(env, monkeypatch):
    _seed_admin(env, monkeypatch)
    c = _client()
    state, nonce = _admin_start(c)
    monkeypatch.setattr(
        oidc,
        "exchange_code",
        lambda p, *, code, redirect_uri: _id_token(nonce=nonce, email=ADMIN_EMAIL, sub="admin-sub"),
    )
    cb = c.get("/oauth/oidc/callback", params={"code": "x", "state": state}, follow_redirects=False)
    # An admin session (not a bearer code): 302 straight to /admin + a hardened session cookie.
    assert cb.status_code == 302 and cb.headers["location"] == "/admin"
    sc = cb.headers["set-cookie"]
    assert "agami_admin_session=" in sc and "HttpOnly" in sc and "Secure" in sc


def test_admin_oidc_refuses_a_non_admin_identity(env, monkeypatch):
    _seed_admin(env, monkeypatch)
    c = _client()
    state, nonce = _admin_start(c)
    # alice (you@example.com) is an onboarded NON-admin OIDC user (from the env fixture).
    monkeypatch.setattr(
        oidc,
        "exchange_code",
        lambda p, *, code, redirect_uri: _id_token(nonce=nonce, email="you@example.com"),
    )
    cb = c.get("/oauth/oidc/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert cb.status_code == 403
    assert "agami_admin_session" not in cb.headers.get("set-cookie", "")  # no session minted


def test_admin_oidc_refuses_an_unknown_identity(env, monkeypatch):
    _seed_admin(env, monkeypatch)
    c = _client()
    state, nonce = _admin_start(c)
    monkeypatch.setattr(
        oidc,
        "exchange_code",
        lambda p, *, code, redirect_uri: _id_token(nonce=nonce, email="stranger@example.com"),
    )
    cb = c.get("/oauth/oidc/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert cb.status_code == 403


def test_admin_oidc_idp_confusion_is_closed_by_the_pin(env, monkeypatch):
    # Admin pinned to Google. Configure Microsoft too; an admin-login attempt for the admin email via
    # Microsoft (an attacker controlling that email at another IdP) must be refused — the pin holds.
    _seed_admin(env, monkeypatch, provider="google")
    monkeypatch.setenv("AGAMI_OIDC_MICROSOFT_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AGAMI_OIDC_MICROSOFT_CLIENT_SECRET", "ms-secret")
    monkeypatch.setenv("AGAMI_OIDC_MICROSOFT_TENANT", MS_TENANT)
    c = _client()
    state, nonce = _admin_start(c, provider="microsoft")
    monkeypatch.setattr(
        oidc,
        "exchange_code",
        lambda p, *, code, redirect_uri: _id_token(nonce=nonce, email=ADMIN_EMAIL, sub="ms-sub"),
    )
    cb = c.get("/oauth/oidc/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert cb.status_code == 403  # bound to google ⇒ a microsoft identity for the email is not the admin


def test_admin_login_page_shows_only_the_pinned_provider(env, monkeypatch):
    _seed_admin(env, monkeypatch)  # pinned google; microsoft not configured
    html = _client().get("/admin/login").text
    assert "Continue with Google" in html and "/admin/oidc/start?provider=google" in html
    assert "Continue with Microsoft" not in html


def test_admin_oidc_start_rejects_an_unconfigured_provider(env, monkeypatch):
    _seed_admin(env, monkeypatch)
    r = _client().get("/admin/oidc/start", params={"provider": "microsoft"}, follow_redirects=False)
    assert r.status_code == 400  # microsoft isn't configured → clean 400, no redirect


def test_admin_oidc_never_self_provisions_even_with_public_signup(env, monkeypatch):
    # An admin sign-in attempt by an unknown email must NOT create a user, even on a public-signup
    # demo instance — the admin route is strictly onboarded-only.
    _seed_admin(env, monkeypatch)
    monkeypatch.setenv("AGAMI_PUBLIC_SIGNUP", "1")
    c = _client()
    state, nonce = _admin_start(c)
    monkeypatch.setattr(
        oidc,
        "exchange_code",
        lambda p, *, code, redirect_uri: _id_token(nonce=nonce, email="stranger@example.com", sub="x"),
    )
    cb = c.get("/oauth/oidc/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert cb.status_code == 403
    s = Store.connect(env)
    assert user_store.get_user_by_email(s, "stranger@example.com") is None  # no row created
    s.close()


def test_admin_login_keeps_the_provider_button_after_a_bad_password(env, monkeypatch):
    _seed_admin(env, monkeypatch)  # pinned google
    html = _client().post("/admin/login", data={"username": ADMIN_EMAIL, "password": "wrong"}).text
    assert "Continue with Google" in html  # a failed password attempt doesn't hide the social option


def test_provider_option_hidden_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    monkeypatch.delenv("AGAMI_OIDC_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("AGAMI_OIDC_GOOGLE_CLIENT_SECRET", raising=False)
    r = TestClient(mcp_http.build_app()).get("/oauth/authorize", params={"redirect_uri": REDIRECT})
    assert r.status_code == 200
    assert "Sign in with Google" not in r.text  # hidden when unconfigured
    assert "<form" in r.text  # password login still present


# --- Microsoft (tenant-pinned) + provider-binding + demo signup --------------


def test_microsoft_requires_a_pinned_tenant(monkeypatch):
    monkeypatch.setenv("AGAMI_OIDC_MICROSOFT_CLIENT_ID", "ms-client")
    monkeypatch.setenv("AGAMI_OIDC_MICROSOFT_CLIENT_SECRET", "ms-secret")
    for bad in ("", "common", "organizations", "consumers"):
        monkeypatch.setenv("AGAMI_OIDC_MICROSOFT_TENANT", bad)
        with pytest.raises(ValueError):
            oidc.provider("microsoft")
    monkeypatch.setenv("AGAMI_OIDC_MICROSOFT_TENANT", MS_TENANT)
    p = oidc.provider("microsoft")
    assert p is not None and p.require_email_verified is False  # tenant pin is the trust


def test_microsoft_token_without_email_verified_is_accepted(env, monkeypatch):
    # MS v2.0 tokens often omit email_verified; with a pinned tenant that's fine.
    monkeypatch.setenv("AGAMI_OIDC_MICROSOFT_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AGAMI_OIDC_MICROSOFT_CLIENT_SECRET", "ms-secret")
    monkeypatch.setenv("AGAMI_OIDC_MICROSOFT_TENANT", MS_TENANT)
    p = oidc.provider("microsoft")
    tok = jwt.encode(
        {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "exp": 9_999_999_999,
            "sub": "ms-sub",
            "email": "you@example.com",
            "nonce": "n",
        },  # no email_verified claim at all
        _PRIV_PEM,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    identity = oidc.verify_id_token(p, tok, nonce="n")
    assert identity.email == "you@example.com" and identity.subject == "ms-sub"


def test_google_token_without_email_verified_is_rejected(env):
    # Google (consumer IdP) MUST prove email_verified.
    p = oidc.provider("google")
    tok = jwt.encode(
        {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "exp": 9_999_999_999,
            "sub": "s",
            "email": "you@example.com",
            "nonce": "the-nonce",
        },  # no email_verified
        _PRIV_PEM,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    with pytest.raises(Exception):
        oidc.verify_id_token(p, tok, nonce="the-nonce")


def test_provider_mismatch_is_rejected_idp_confusion(env):
    # alice is bound to Google; a Microsoft identity with her email must NOT resolve to her.
    s = Store.from_env()
    assert _resolve_oidc_user(s, "microsoft", Identity("you@example.com", "ms-sub")) is None
    s.close()


def test_subject_tofu_binds_then_enforces(env):
    s = Store.from_env()
    # first Google login binds the subject; a different subject at Google is then refused
    assert _resolve_oidc_user(s, "google", Identity("you@example.com", "sub-1")) == "alice"
    assert _resolve_oidc_user(s, "google", Identity("you@example.com", "sub-2")) is None
    assert _resolve_oidc_user(s, "google", Identity("you@example.com", "sub-1")) == "alice"  # bound
    s.close()


def test_demo_signup_off_rejects_unknown_email(env):
    s = Store.from_env()  # AGAMI_PUBLIC_SIGNUP unset → fail-closed
    assert _resolve_oidc_user(s, "google", Identity("stranger@example.com", "s")) is None
    s.close()


def test_demo_signup_on_creates_a_demo_user(env, monkeypatch):
    monkeypatch.setenv("AGAMI_PUBLIC_SIGNUP", "true")
    s = Store.from_env()
    uname = _resolve_oidc_user(s, "google", Identity("newbie@example.com", "ns"))
    assert uname == "newbie@example.com"
    row = user_store.get_user_by_email(s, "newbie@example.com")
    assert row["status"] == "demo"
    assert row["oidc_provider"] == "google" and row["oidc_subject"] == "ns"
    s.close()


def test_disabled_user_cannot_oidc_login(env):
    s = Store.from_env()
    user_store.set_status(s, "alice", "disabled")
    assert _resolve_oidc_user(s, "google", Identity("you@example.com", "sub-1")) is None
    s.close()


def test_misconfigured_microsoft_tenant_is_a_clean_400_not_500(env, monkeypatch):
    # An unpinned MS tenant makes oidc.provider() raise; the handler must answer a clean 400
    # (a stale/bookmarked ?provider=microsoft link must not 500).
    monkeypatch.setenv("AGAMI_OIDC_MICROSOFT_CLIENT_ID", "ms-client")
    monkeypatch.setenv("AGAMI_OIDC_MICROSOFT_CLIENT_SECRET", "ms-secret")
    monkeypatch.setenv("AGAMI_OIDC_MICROSOFT_TENANT", "common")  # unpinned → provider() raises
    c = _client()
    r = c.get(
        "/oauth/oidc/start",
        params={
            "provider": "microsoft",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "code_challenge": CHALLENGE,
            "state": "s",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_verify_id_token_rejects_blank_sub(env):
    # `sub` is the binding key — a present-but-blank sub must be rejected (require only checks presence).
    p = oidc.provider("google")
    with pytest.raises(Exception):
        oidc.verify_id_token(p, _id_token(sub="   "), nonce="the-nonce")
