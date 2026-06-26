"""Generic OIDC client — "Sign in with Google/Microsoft" behind the OAuth authorize page.

This is the server's ONE deliberate outbound egress: it calls the IdP's discovery, token, and JWKS
endpoints (httpx + PyJWT's JWKS fetch). It is server-only (the `[server]` extra) and excluded from
the zero-egress privacy contract — the local skill never imports it; users who want no egress run the
local `agami serve`.

Verification is **explicit and uniform** for every provider (a provider is just a discovery URL +
client id/secret): the ID token's RS256 signature is checked against the IdP JWKS, and
`aud`/`iss`/`exp`/`nonce`/`email_verified` are all asserted *here*, not left implicit to a provider
SDK. That is the whole reason this is a generic client rather than google-auth/msal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

# A short timeout: the IdP is a fast hop; we never want a hung login to wedge a worker.
_TIMEOUT = httpx.Timeout(10.0)

# Discovery URLs are fixed per provider (the HTTPS trust anchor); client creds come from env. Google
# ships first; Microsoft (with tenant pinning) follows.
_DISCOVERY = {
    "google": "https://accounts.google.com/.well-known/openid-configuration",
}

_discovery_cache: dict[str, dict] = {}


@dataclass(frozen=True)
class Provider:
    key: str
    discovery_url: str
    client_id: str
    client_secret: str


def provider(key: str) -> Provider | None:
    """The configured provider for `key`, or None when its client id/secret env is absent (so the
    option is simply hidden). Env: AGAMI_OIDC_<KEY>_CLIENT_ID / _CLIENT_SECRET."""
    discovery_url = _DISCOVERY.get(key)
    if discovery_url is None:
        return None
    client_id = os.environ.get(f"AGAMI_OIDC_{key.upper()}_CLIENT_ID", "").strip()
    client_secret = os.environ.get(f"AGAMI_OIDC_{key.upper()}_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    return Provider(key, discovery_url, client_id, client_secret)


def available_providers() -> list[str]:
    """The provider keys that are configured (have client id/secret) — what the login page offers."""
    return [k for k in _DISCOVERY if provider(k) is not None]


def _discover(p: Provider) -> dict:
    """The IdP's OpenID discovery document (authorization/token/jwks URIs + issuer), cached."""
    if p.discovery_url not in _discovery_cache:
        resp = httpx.get(p.discovery_url, timeout=_TIMEOUT)
        resp.raise_for_status()
        _discovery_cache[p.discovery_url] = resp.json()
    return _discovery_cache[p.discovery_url]


def authorize_url(p: Provider, *, state: str, nonce: str, redirect_uri: str) -> str:
    """The IdP authorize URL to send the user to (authorization-code flow, `openid email`)."""
    meta = _discover(p)
    params = {
        "client_id": p.client_id,
        "response_type": "code",
        "scope": "openid email",
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": nonce,
    }
    return f"{meta['authorization_endpoint']}?{urlencode(params)}"


def exchange_code(p: Provider, *, code: str, redirect_uri: str) -> str:
    """Exchange the authorization code for the IdP's `id_token` (raises on any failure)."""
    meta = _discover(p)
    resp = httpx.post(
        meta["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": p.client_id,
            "client_secret": p.client_secret,
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    id_token = resp.json().get("id_token")
    if not id_token:
        raise ValueError("token response missing id_token")
    return id_token


def verify_id_token(p: Provider, id_token: str, *, nonce: str) -> str:
    """Verify the ID token and return the **verified** email. Raises on any verification failure.

    Explicit and uniform: RS256 signature against the IdP JWKS, `aud` == our client_id, `iss` == the
    discovered issuer, `exp` enforced, the `nonce` matches the one we sent (replay defense), and
    `email_verified` is true (never trust an unverified email as identity)."""
    meta = _discover(p)
    signing_key = jwt.PyJWKClient(meta["jwks_uri"]).get_signing_key_from_jwt(id_token).key
    claims = jwt.decode(
        id_token,
        signing_key,
        algorithms=["RS256"],
        audience=p.client_id,
        issuer=meta["issuer"],
        options={"require": ["exp", "aud", "iss", "nonce"]},
    )
    if claims.get("nonce") != nonce:
        raise ValueError("nonce mismatch")
    if claims.get("email_verified") is not True:
        raise ValueError("email_verified is not true")
    email = claims.get("email")
    if not email:
        raise ValueError("id token has no email claim")
    return email
