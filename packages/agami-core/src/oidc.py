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

import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

_log = logging.getLogger(__name__)

# A short timeout: the IdP is a fast hop; we never want a hung login to wedge a worker.
_TIMEOUT = httpx.Timeout(10.0)

# Discovery URLs are fixed per provider (the HTTPS trust anchor); client creds come from env.
# Microsoft is pinned to one company tenant, set to `consumers` for personal accounts, or set to
# `common` for personal accounts plus work accounts from any tenant (see `_check_any_tenant`).
_PROVIDERS = ("google", "microsoft")
# Work accounts only, from any tenant — refused. `common` covers the same accounts with the checks
# below, so a second spelling would only be a second path to keep correct.
_REFUSED_MS_TENANTS = {"organizations"}
# The authority for personal Microsoft accounts only, and the tenant id those accounts carry in `tid`.
# Microsoft owns that tenant, so its addresses are treated as proven.
_MS_PERSONAL_AUTHORITY = "consumers"
_MS_PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
# The authority for personal accounts AND work accounts from any tenant. Its discovery document names
# a templated issuer, so each token's issuer is checked against its own `tid` instead.
_MS_ANY_AUTHORITY = "common"
_MS_TENANT_ISSUER = "https://login.microsoftonline.com/{tid}/v2.0"
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

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
    # Microsoft on `consumers` proves it with the personal-accounts tenant (see `verify_id_token`), and
    # on `common` with `_check_any_tenant`.
    require_email_verified: bool
    # False for Microsoft on `consumers` or `common`, which admit accounts from no single organization.
    # A caller that trusts claims beyond the email (roles, groups) should refuse them when this is False.
    pinned: bool = True
    # Microsoft on `common`: the tenants a token may come from (lower-case ids, the personal-accounts
    # tenant included when `consumers` is listed), or None for any tenant.
    allowed_tenants: frozenset[str] | None = None
    any_tenant: bool = False


@dataclass(frozen=True)
class Identity:
    """The verified identity from an ID token: the email (the lookup key) + the IdP's immutable
    subject (`sub`, used to bind the user to one provider account)."""

    email: str
    subject: str


def _discovery_url(key: str) -> str | None:
    """The provider's OpenID discovery URL. Google is fixed; Microsoft is tenant-specific: a pinned
    tenant id, `consumers` for personal accounts, or `common` for any account. `organizations` is
    refused."""
    if key == "google":
        return "https://accounts.google.com/.well-known/openid-configuration"
    if key == "microsoft":
        tenant = _ms_tenant()
        if not tenant or tenant.lower() in _REFUSED_MS_TENANTS:
            raise ValueError(
                "AGAMI_OIDC_MICROSOFT_TENANT must be a single tenant id, 'consumers' for personal "
                "Microsoft accounts, or 'common' for any Microsoft account (not 'organizations')."
            )
        return f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"
    return None


def _ms_tenant() -> str:
    return os.environ.get("AGAMI_OIDC_MICROSOFT_TENANT", "").strip()


def _ms_allowed_tenants() -> frozenset[str] | None:
    """`AGAMI_OIDC_MICROSOFT_ALLOWED_TENANTS`: comma-separated tenant ids, plus `consumers` for personal
    accounts. None when unset. Raises ValueError on an entry that is not a tenant id, or when set
    without `common`, so a typo hides the button rather than silently admitting more than intended."""
    raw = os.environ.get("AGAMI_OIDC_MICROSOFT_ALLOWED_TENANTS", "").strip()
    if not raw:
        return None
    if _ms_tenant().lower() != _MS_ANY_AUTHORITY:
        raise ValueError(
            "AGAMI_OIDC_MICROSOFT_ALLOWED_TENANTS applies only with AGAMI_OIDC_MICROSOFT_TENANT=common."
        )
    allowed = set()
    for entry in (e.strip().lower() for e in raw.split(",")):
        if not entry:
            continue
        if entry == _MS_PERSONAL_AUTHORITY:
            entry = _MS_PERSONAL_TENANT
        if not _GUID.fullmatch(entry):
            raise ValueError(
                "AGAMI_OIDC_MICROSOFT_ALLOWED_TENANTS takes tenant ids and 'consumers' only."
            )
        allowed.add(entry)
    if not allowed:
        raise ValueError("AGAMI_OIDC_MICROSOFT_ALLOWED_TENANTS names no tenant.")
    return frozenset(allowed)


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
    discovery_url = _discovery_url(key)  # may raise for a refused MS authority
    if discovery_url is None:
        return None
    tenant = _ms_tenant().lower() if key == "microsoft" else ""
    any_tenant = tenant == _MS_ANY_AUTHORITY
    allowed = _ms_allowed_tenants() if key == "microsoft" else None  # may raise on a bad list
    # The personal-accounts tenant id is an alias for `consumers` at Microsoft, so it is unpinned too.
    pinned = not (any_tenant or tenant in {_MS_PERSONAL_AUTHORITY, _MS_PERSONAL_TENANT})
    return Provider(
        key,
        discovery_url,
        client_id,
        client_secret,
        # Unpinned Microsoft proves every address it admits (`verify_id_token`), allow-list or not.
        require_email_verified=key == "google" or not pinned,
        pinned=pinned,
        allowed_tenants=allowed,
        any_tenant=any_tenant,
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
        except ValueError as exc:
            # Logged, or a typo in a tenant setting hides the button with nothing to find it by. The
            # messages name the setting, never its value.
            _log.warning("sign-in provider %s is misconfigured and hidden: %s", key, exc)
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
    if p.key == "microsoft":
        # Otherwise Microsoft silently reuses whichever account the browser is signed in to, and a
        # person with a personal and a work account cannot pick the one they were invited as.
        params["prompt"] = "select_account"
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
    `email_verified`, so that claim is not read there), Microsoft on `common` by `_check_any_tenant`.
    For a tenant-pinned Microsoft the pin is the trust, so neither is needed. `preferred_username` is
    never used as identity."""
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
        # `common`'s discovery issuer is a template, so the issuer is checked per token, against its
        # own tenant, in `_check_any_tenant`. It is still required to be present.
        issuer=None if p.any_tenant else meta["issuer"],
        options={"require": ["exp", "aud", "iss", "sub", "nonce"]},
    )
    if claims.get("nonce") != nonce:
        raise ValueError("nonce mismatch")
    if p.key == "microsoft" and p.any_tenant:
        _check_any_tenant(p, claims)
    elif p.key == "microsoft" and not p.pinned:
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


def _check_any_tenant(p: Provider, claims: dict) -> None:
    """Microsoft on `common`: the token's issuer must be its own tenant's, the tenant must be allowed,
    and a work account's address must be proven. Raises ValueError naming which check failed.

    On `common` any tenant's administrator can put any address on an account, so a work account's
    address is taken as proven only when the token says the address's domain is verified by that
    tenant (`xms_edov`, an optional claim the app registration must request). The allow-list only
    narrows which tenants may sign in; it never stands in for that proof, because with two listed
    tenants either one's administrator could otherwise put the other's addresses on an account."""
    tid = claims.get("tid")
    if not isinstance(tid, str) or not _GUID.fullmatch(tid.lower()):
        raise ValueError("token has no usable tenant id")
    tid = tid.lower()
    # Microsoft signs for every tenant with the same keys, so the signature alone does not say which
    # tenant issued the token; the issuer must agree with the tenant the token claims.
    if claims.get("iss") != _MS_TENANT_ISSUER.format(tid=tid):
        raise ValueError("token issuer does not match its tenant")
    if p.allowed_tenants is not None and tid not in p.allowed_tenants:
        raise ValueError("tenant is not in AGAMI_OIDC_MICROSOFT_ALLOWED_TENANTS")
    if tid == _MS_PERSONAL_TENANT:
        return
    edov = claims.get("xms_edov")
    # `is True` rather than `in`, which would also take 1 and 1.0 because they equal True.
    if edov is not True and edov not in ("true", "True", "1"):
        raise ValueError("work account's email domain is not verified by its tenant (xms_edov)")
