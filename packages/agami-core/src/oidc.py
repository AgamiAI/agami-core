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

# Discovery URLs are fixed per provider (the HTTPS trust anchor); client creds come from env.
# Microsoft is either pinned to one company tenant, or set to `consumers` for personal accounts.
_PROVIDERS = ("google", "microsoft")
# Microsoft authorities that admit work accounts from ANY tenant — refused. On those, any tenant's
# administrator can put any address on an account, and Microsoft documents no claim that proves it.
_UNPINNED_MS_TENANTS = {"common", "organizations"}
# The authority for personal Microsoft accounts only, and the tenant id those accounts carry in `tid`.
# Microsoft owns that tenant, so its addresses are treated as proven.
_MS_PERSONAL_AUTHORITY = "consumers"
_MS_PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"

_discovery_cache: dict[str, dict] = {}
_jwks_clients: dict[str, "jwt.PyJWKClient"] = {}


@dataclass(frozen=True)
class Provider:
    key: str
    discovery_url: str
    client_id: str
    client_secret: str
    # Whether the address must be proven. Google proves it with email_verified. Microsoft pinned to one
    # tenant does not need to: the org controls the addresses, and its tokens often omit the claim.
    # Microsoft on `consumers` proves it with the personal-accounts tenant (see `verify_id_token`).
    require_email_verified: bool
    # False only for Microsoft on `consumers`, which admits accounts from no single organization. A
    # caller that trusts claims beyond the email (roles, groups) should refuse them when this is False.
    pinned: bool = True


@dataclass(frozen=True)
class Identity:
    """The verified identity from an ID token: the email (the lookup key) + the IdP's immutable
    subject (`sub`, used to bind the user to one provider account)."""

    email: str
    subject: str


def _discovery_url(key: str) -> str | None:
    """The provider's OpenID discovery URL. Google is fixed; Microsoft is tenant-specific: a pinned
    tenant id, or `consumers` for personal accounts. An any-tenant authority is refused."""
    if key == "google":
        return "https://accounts.google.com/.well-known/openid-configuration"
    if key == "microsoft":
        tenant = _ms_tenant()
        if not tenant or tenant.lower() in _UNPINNED_MS_TENANTS:
            raise ValueError(
                "AGAMI_OIDC_MICROSOFT_TENANT must be a single tenant id, or 'consumers' for "
                "personal Microsoft accounts (not 'common'/'organizations')."
            )
        return f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"
    return None


def _ms_tenant() -> str:
    return os.environ.get("AGAMI_OIDC_MICROSOFT_TENANT", "").strip()


def provider(key: str) -> Provider | None:
    """The configured provider for `key`, or None when its client id/secret env is absent (so the
    option is simply hidden). Env: AGAMI_OIDC_<KEY>_CLIENT_ID / _CLIENT_SECRET (+ _TENANT for MS).
    Raises ValueError on a misconfigured Microsoft tenant (an any-tenant authority is refused)."""
    if key not in _PROVIDERS:
        return None
    client_id = os.environ.get(f"AGAMI_OIDC_{key.upper()}_CLIENT_ID", "").strip()
    client_secret = os.environ.get(f"AGAMI_OIDC_{key.upper()}_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    discovery_url = _discovery_url(key)  # may raise for an any-tenant MS authority
    if discovery_url is None:
        return None
    # The personal-accounts tenant id is an alias for `consumers` at Microsoft, so it is unpinned too.
    pinned = not (
        key == "microsoft" and _ms_tenant().lower() in {_MS_PERSONAL_AUTHORITY, _MS_PERSONAL_TENANT}
    )
    return Provider(
        key,
        discovery_url,
        client_id,
        client_secret,
        require_email_verified=key == "google" or not pinned,
        pinned=pinned,
    )


def available_providers() -> list[str]:
    """The provider keys that are configured (have client id/secret) — what the login page offers. A
    misconfigured Microsoft tenant is skipped here, so its button is simply hidden; if it's invoked
    directly the handler answers a clean 400 (via `_safe_provider`), not a 500."""
    out = []
    for key in _PROVIDERS:
        try:
            if provider(key) is not None:
                out.append(key)
        except ValueError:
            continue
    return out


def public_signup_enabled() -> bool:
    """Whether unknown verified emails may self-provision a demo user. Default OFF (fail-closed) —
    intended only for a dedicated demo instance whose datasource holds only demo data."""
    return os.environ.get("AGAMI_PUBLIC_SIGNUP", "").strip().lower() in {"1", "true", "yes", "on"}


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


def verify_id_token(p: Provider, id_token: str, *, nonce: str) -> Identity:
    """Verify the ID token and return the verified `Identity` (email + subject). Raises on any failure.

    Explicit and uniform: RS256 signature against the IdP JWKS, `aud` == our client_id, `iss` == the
    discovered issuer, `exp` enforced, `sub` present, and the `nonce` matches the one we sent (replay
    defense). The address must be proven where the provider requires it: Google by `email_verified`,
    Microsoft on `consumers` by `tid` being the personal-accounts tenant (Microsoft sends no
    `email_verified`, so that claim is not read there). For a tenant-pinned Microsoft the pin is the
    trust, so neither is needed. `preferred_username` is never used as identity."""
    meta = _discover(p)
    # Reuse the JWKS client per provider — it keeps its own key cache, so steady-state logins don't
    # re-fetch the IdP's keys on every request (and don't amplify into the IdP under load).
    jwks_uri = meta["jwks_uri"]
    if jwks_uri not in _jwks_clients:
        _jwks_clients[jwks_uri] = jwt.PyJWKClient(jwks_uri)
    signing_key = _jwks_clients[jwks_uri].get_signing_key_from_jwt(id_token).key
    claims = jwt.decode(
        id_token,
        signing_key,
        algorithms=["RS256"],
        audience=p.client_id,
        issuer=meta["issuer"],
        options={"require": ["exp", "aud", "iss", "sub", "nonce"]},
    )
    if claims.get("nonce") != nonce:
        raise ValueError("nonce mismatch")
    if p.key == "microsoft" and not p.pinned:
        if claims.get("tid") != _MS_PERSONAL_TENANT:
            raise ValueError("not a personal Microsoft account")
    elif p.require_email_verified and claims.get("email_verified") is not True:
        raise ValueError("email_verified is not true")
    email = claims.get("email")
    if not email:
        raise ValueError("id token has no email claim")
    subject = claims.get("sub")
    # sub is the long-term binding key — fail closed on a missing/blank/non-string value rather than
    # persist an invalid identity binding.
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("id token has no usable sub claim")
    return Identity(email=email, subject=subject)
