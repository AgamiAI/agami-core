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
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

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
    s = Store.connect(db_url)
    s.run_migrations()
    user_store.create_user(
        s, "alice", password=None, email="you@example.com"
    )  # onboarded OIDC user
    s.close()
    return db_url


# --- the OIDC verifier in isolation (the security-critical core) -------------


def test_verify_id_token_accepts_a_valid_token(env):
    p = oidc.provider("google")
    assert oidc.verify_id_token(p, _id_token(), nonce="the-nonce") == "you@example.com"


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


def test_provider_option_hidden_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    monkeypatch.delenv("AGAMI_OIDC_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("AGAMI_OIDC_GOOGLE_CLIENT_SECRET", raising=False)
    r = TestClient(mcp_http.build_app()).get("/oauth/authorize", params={"redirect_uri": REDIRECT})
    assert r.status_code == 200
    assert "Sign in with Google" not in r.text  # hidden when unconfigured
    assert "<form" in r.text  # password login still present
