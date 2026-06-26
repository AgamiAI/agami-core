"""The server's OAuth 2.1 authorize/token/register endpoints — what claude.ai's connector completes.

The transport's discovery metadata points here; this module is the actual provider:
`/oauth/register` (minimal RFC 7591), `/oauth/authorize` (login → authorization code), `/oauth/token`
(code + PKCE verifier → a self-signed HS256 JWT). Credentials are checked by `user_store.authenticate`;
the issued JWT is what the transport will trust once a follow-up change wires the token validator in.

Security invariants enforced here: authorization codes are single-use + short-lived; PKCE (S256) is
required; redirect URIs are allow-listed (the claude.ai callback, the public base URL, or a URI the
client registered) so a code can't be sent to an attacker; the signing secret and tokens are never
logged or echoed in an error body. This module makes NO network call — it only mints/validates
locally and tells the browser where to redirect.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import jwt
import ui
from ports import Principal
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from store import Store
from user_store import (
    authenticate,
    bind_oidc_subject,
    create_user,
    get_user,
    get_user_by_email,
)

if TYPE_CHECKING:
    from oidc import Identity  # for type hints only — runtime imports oidc lazily (egress module)

_CODE_TTL = timedelta(minutes=10)  # authorization codes are short-lived
_JWT_TTL = timedelta(hours=1)
# Claude's fixed OAuth callback (the connector redirects here after authorize). The .com host is the
# announced future move; allow both so the connection survives the switch.
_CLAUDE_CALLBACKS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# HS256 forgeability scales with secret weakness; RFC 7518 §3.2 wants a key ≥ the hash output.
_MIN_SECRET_BYTES = 32


def _signing_secret() -> str:
    """The HS256 signing secret. Required when the OAuth provider is active — fail fast (like
    PUBLIC_BASE_URL) rather than mint a token under a weak/absent key."""
    secret = os.environ.get("AGAMI_SIGNING_SECRET", "")
    if not secret:
        raise RuntimeError(
            "AGAMI_SIGNING_SECRET must be set to issue OAuth tokens (the deploy generates it)."
        )
    if len(secret.encode()) < _MIN_SECRET_BYTES:
        raise RuntimeError(
            f"AGAMI_SIGNING_SECRET must be at least {_MIN_SECRET_BYTES} bytes (HS256 strength)."
        )
    return secret


def issue_jwt(subject: str) -> str:
    """A self-signed HS256 JWT for `subject` — the Bearer token the transport will accept."""
    from mcp_http import public_base_url  # lazy: mcp_http imports these handlers at module load

    now = _now()
    payload = {
        "sub": subject,
        "iss": public_base_url(),
        "iat": int(now.timestamp()),
        "exp": int((now + _JWT_TTL).timestamp()),
    }
    return jwt.encode(payload, _signing_secret(), algorithm="HS256")


class JwtAuthProvider:
    """Validates the self-signed HS256 JWTs `issue_jwt` mints — the transport's real token gate.

    Conforms structurally to the `ports.AuthProvider` protocol (validate_token → Principal | None).
    Pins the algorithm to HS256 (no `alg=none`/confusion), requires sub/exp/iss, and checks the
    issuer == PUBLIC_BASE_URL. Any bad/expired/forged token returns None (fail closed → 401)."""

    def validate_token(self, token: str) -> Principal | None:
        from mcp_http import public_base_url  # lazy: avoid the import cycle

        try:
            claims = jwt.decode(
                token,
                _signing_secret(),
                algorithms=["HS256"],
                issuer=public_base_url(),
                options={"require": ["exp", "sub", "iss"]},
            )
        except Exception:
            # Invalid signature, expired, wrong issuer, malformed, or no secret → reject.
            return None
        sub = claims.get("sub")
        # `Principal.subject` is a str — reject a non-string or blank sub rather than carry a
        # malformed identity forward.
        if not isinstance(sub, str) or not sub.strip():
            return None
        return Principal(subject=sub)


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _verify_pkce(code_verifier: str, code_challenge: str | None) -> bool:
    """PKCE S256: the challenge must equal base64url(sha256(verifier)), compared constant-time."""
    if not code_verifier or not code_challenge:
        return False
    expected = _b64url_nopad(hashlib.sha256(code_verifier.encode()).digest())
    return hmac.compare_digest(expected, code_challenge)


def _same_origin(a: str, b: str) -> bool:
    """Scheme + host(:port) equality. NOT a string prefix — `startswith` would accept
    `https://host.com.evil.com` for a base of `https://host.com` (an open redirect)."""
    pa, pb = urlsplit(a), urlsplit(b)
    return bool(pa.scheme) and pa.scheme == pb.scheme and pa.netloc == pb.netloc


def _redirect_allowed(redirect_uri: str, registered: str | None) -> bool:
    """Allow only the claude.ai callback, a same-origin URI under PUBLIC_BASE_URL, or one the client
    registered (exact match) — so an authorization code can never be redirected to an attacker host."""
    from mcp_http import public_base_url

    if not redirect_uri:
        return False
    if redirect_uri in _CLAUDE_CALLBACKS:
        return True
    if registered and redirect_uri in registered.split():
        return True
    return _same_origin(redirect_uri, public_base_url())


async def _form(request: Request) -> dict[str, str]:
    """Parse an application/x-www-form-urlencoded body without pulling in python-multipart."""
    body = (await request.body()).decode()
    return {k: v[0] for k, v in parse_qs(body).items()}


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    # OAuth-style error; never includes a token, secret, or the submitted password.
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


def _open_store() -> Store | None:
    return Store.from_env()


async def register(request: Request) -> Response:
    """Minimal RFC 7591 dynamic client registration — mint a client_id so the connector can proceed.
    Full DCR (client auth, rotation) is deferred."""
    store = _open_store()
    if store is None:
        return _oauth_error("server_error", "no datastore configured", status=500)
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        redirect_uris = body.get("redirect_uris") or []
        if not isinstance(redirect_uris, list) or not all(
            isinstance(u, str) for u in redirect_uris
        ):
            return _oauth_error("invalid_request", "redirect_uris must be a list of strings")
        client_id = uuid4().hex
        store.execute(
            "INSERT INTO oauth_client (client_id, redirect_uris, created) VALUES (?, ?, ?)",
            (client_id, " ".join(redirect_uris), _now().isoformat()),
        )
        store.commit()
        return JSONResponse(
            {
                "client_id": client_id,
                "redirect_uris": redirect_uris,
                "token_endpoint_auth_method": "none",
            },
            status_code=201,
        )
    finally:
        store.close()


_OAUTH_CONTEXT_KEYS = ("client_id", "redirect_uri", "code_challenge", "state")


def _login_form(
    params: dict[str, str], error: str = "", providers: tuple[str, ...] = ()
) -> HTMLResponse:
    """The branded sign-in page: optional 'Continue with <provider>' buttons + a username/password
    form, carrying the OAuth context forward as hidden fields. Every interpolated value is escaped
    (these are attacker-influenceable query/body params landing in HTML)."""
    return HTMLResponse(login_body_html(params, error=error, providers=providers, wrap=True))


def _client_label(redirect_uri: str) -> str | None:
    """A friendly name for the connecting client, derived from its callback. claude.ai/.com → 'Claude';
    otherwise None (we don't store a per-client name, so show a generic sign-in)."""
    if "claude.ai" in redirect_uri or "claude.com" in redirect_uri:
        return "Claude"
    return None


def login_body_html(
    params: dict[str, str], *, error: str = "", providers: tuple[str, ...] = (), wrap: bool = False
) -> str:
    """The sign-in page HTML (the inner body, or the full page when `wrap`). Split out so previews can
    render it with sample values without going through a request."""
    carried = {k: params.get(k, "") for k in _OAUTH_CONTEXT_KEYS}
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{ui.esc(params.get(k, ""))}">'
        for k in _OAUTH_CONTEXT_KEYS
    )
    # OIDC buttons carry the same OAuth context to /oauth/oidc/start so the flow resumes after the IdP.
    buttons = "".join(
        ui.provider_button(key, f"/oauth/oidc/start?{urlencode({**carried, 'provider': key})}")
        for key in providers
    )
    social = f'<div class="providers">{buttons}</div><div class="divider">or</div>' if buttons else ""
    alert = f'<div class="alert error">{ui.esc(error)}</div>' if error else ""
    client = _client_label(params.get("redirect_uri", ""))
    # Consent banner mirrors the web app: a quiet "Allow <client> to access your data". When the
    # client isn't a recognised AI assistant we just show the logo (no banner) — no filler text.
    consent = (
        f'<div class="consent"><p class="small">Allow</p>'
        f'<p class="who">{ui.esc(client)}</p>'
        f'<p class="small">to access your data</p></div>'
        if client
        else ""
    )
    body = f"""{consent}
{alert}{social}
<form method="post">{hidden}
<label for="u">Email</label>
<input id="u" name="username" type="email" autocomplete="email" placeholder="you@example.com">
<label for="p">Password</label>
<input id="p" name="password" type="password" autocomplete="current-password" placeholder="••••••••">
<button class="btn" type="submit" style="margin-top:22px">Sign in</button>
</form>"""
    return ui.auth_page("Sign in", body) if wrap else body


async def authorize(request: Request) -> Response:
    """GET → the login form (carrying the OAuth params); POST → verify credentials and redirect to
    the client with a fresh authorization code, or re-render with an error."""
    import oidc

    providers = tuple(oidc.available_providers())
    if request.method == "GET":
        return _login_form(dict(request.query_params), providers=providers)

    form = await _form(request)
    store = _open_store()
    if store is None:
        return _oauth_error("server_error", "no datastore configured", status=500)
    try:
        redirect_uri = form.get("redirect_uri", "")
        client_id = form.get("client_id", "")
        client = store.query(
            "SELECT redirect_uris FROM oauth_client WHERE client_id = ?", (client_id,)
        )
        registered = client[0]["redirect_uris"] if client else None
        # Validate the redirect target BEFORE authenticating — never send a code to an unvetted URL.
        if not _redirect_allowed(redirect_uri, registered):
            return _oauth_error("invalid_request", "redirect_uri not allowed")
        # PKCE is mandatory: reject a missing challenge now rather than persist a code that could
        # never be redeemed (the token step requires a verifier that matches it).
        code_challenge = form.get("code_challenge", "")
        if not code_challenge:
            return _oauth_error("invalid_request", "code_challenge is required (PKCE)")

        principal = authenticate(store, form.get("username", ""), form.get("password", ""))
        if principal is None:
            return _login_form(form, error="Invalid username or password.", providers=providers)

        return _issue_authorization_code(
            store,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            username=principal.subject,
            client_state=form.get("state", ""),
        )
    finally:
        store.close()


def _issue_authorization_code(
    store: Store,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    username: str,
    client_state: str,
) -> RedirectResponse:
    """Mint a fresh authorization code for an authenticated user and 302 back to the client. Shared
    by the password path and the OIDC callback so both resume the same OAuth token exchange."""
    code = secrets.token_urlsafe(32)
    store.execute(
        "INSERT INTO oauth_state (code, client_id, redirect_uri, code_challenge, username, "
        "expires_at, used, created) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (
            code,
            client_id,
            redirect_uri,
            code_challenge,
            username,
            (_now() + _CODE_TTL).isoformat(),
            _now().isoformat(),
        ),
    )
    store.commit()
    query = urlencode({"code": code, "state": client_state})
    return RedirectResponse(f"{redirect_uri}?{query}", status_code=302)


async def token(request: Request) -> Response:
    """Exchange an authorization code + PKCE verifier for a self-signed JWT. Enforces single-use,
    expiry, redirect match, and PKCE — any failure is a 400 with no token in the body."""
    form = await _form(request)
    if form.get("grant_type") != "authorization_code":
        return _oauth_error("unsupported_grant_type", "only authorization_code is supported")

    store = _open_store()
    if store is None:
        return _oauth_error("server_error", "no datastore configured", status=500)
    try:
        rows = store.query("SELECT * FROM oauth_state WHERE code = ?", (form.get("code", ""),))
        row = rows[0] if rows else None
        if row is None or row["used"]:
            return _oauth_error("invalid_grant", "code is invalid or already used")
        if _now().isoformat() > row["expires_at"]:
            return _oauth_error("invalid_grant", "code has expired")
        if form.get("redirect_uri", "") != (row["redirect_uri"] or ""):
            return _oauth_error("invalid_grant", "redirect_uri mismatch")
        if not _verify_pkce(form.get("code_verifier", ""), row["code_challenge"]):
            return _oauth_error("invalid_grant", "PKCE verification failed")
        # Confirm we can sign (secret present AND strong enough) BEFORE burning the code, so a
        # misconfigured server doesn't consume the code on a request it can't fulfil.
        try:
            _signing_secret()
        except RuntimeError:
            return _oauth_error("server_error", "token signing is not configured", status=500)

        # Single-use, atomically: only the request that flips used 0→1 may issue a token. The
        # conditional UPDATE + rowcount check closes the read-then-write race two concurrent
        # exchanges would otherwise win together (double-issued JWTs for one code).
        burned = store.execute(
            "UPDATE oauth_state SET used = 1 WHERE code = ? AND used = 0", (row["code"],)
        )
        store.commit()
        if burned.rowcount != 1:
            return _oauth_error("invalid_grant", "code is invalid or already used")
        access_token = issue_jwt(row["username"])
        return JSONResponse(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": int(_JWT_TTL.total_seconds()),
            }
        )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# OIDC social login ("Sign in with Google/Microsoft")
#
# The OIDC leg nests inside the OAuth authorize flow: we send the user to the IdP, and on callback
# resume minting the OAuth authorization code for the original MCP client. The original authorize
# context (client_id, redirect_uri, code_challenge, the client's state) + a fresh nonce ride across
# the IdP round-trip in a short-lived **signed** `state` JWT, bound to an **HttpOnly CSRF cookie** so
# a forged callback can't complete the flow. User resolution is **onboarded-only** — no auto-create.
# ---------------------------------------------------------------------------

_OIDC_STATE_TTL = timedelta(minutes=10)
_CSRF_COOKIE = "agami_oidc_csrf"


def _oidc_callback_uri() -> str:
    from mcp_http import public_base_url

    return f"{public_base_url()}/oauth/oidc/callback"


def _safe_provider(key: str):
    """`oidc.provider`, but a misconfigured provider (e.g. an unpinned Microsoft tenant, which raises)
    resolves to None so the handler answers a clean 400 instead of an unhandled 500."""
    import oidc

    try:
        return oidc.provider(key)
    except ValueError:
        return None


def _mint_oidc_state(payload: dict, csrf: str) -> str:
    """Sign the carried authorize context + a CSRF binding into a short-lived HS256 state JWT."""
    claims = {
        **payload,
        "csrf": hashlib.sha256(csrf.encode()).hexdigest(),
        "iat": int(_now().timestamp()),
        "exp": int((_now() + _OIDC_STATE_TTL).timestamp()),
    }
    return jwt.encode(claims, _signing_secret(), algorithm="HS256")


def _verify_oidc_state(state: str, csrf_cookie: str | None) -> dict | None:
    """Validate the state JWT (signature + expiry) AND that it's bound to the caller's CSRF cookie.
    Returns the claims, or None on any failure (treated identically to avoid an oracle)."""
    try:
        claims = jwt.decode(
            state, _signing_secret(), algorithms=["HS256"], options={"require": ["exp"]}
        )
    except Exception:
        return None
    expected = claims.get("csrf")
    if not csrf_cookie or not expected:
        return None
    if not hmac.compare_digest(hashlib.sha256(csrf_cookie.encode()).hexdigest(), expected):
        return None
    return claims


async def oidc_start(request: Request) -> Response:
    """Begin OIDC: re-apply the password path's gates (redirect allow-list + PKCE) on the carried
    OAuth params, then redirect to the IdP with a signed state (context + nonce) + a CSRF cookie."""
    import oidc

    q = request.query_params
    p = _safe_provider(q.get("provider", ""))
    if p is None:
        return _oauth_error("invalid_request", "unknown or unconfigured provider")
    redirect_uri = q.get("redirect_uri", "")
    code_challenge = q.get("code_challenge", "")
    store = _open_store()
    if store is None:
        return _oauth_error("server_error", "no datastore configured", status=500)
    try:
        client = store.query(
            "SELECT redirect_uris FROM oauth_client WHERE client_id = ?", (q.get("client_id", ""),)
        )
        registered = client[0]["redirect_uris"] if client else None
    finally:
        store.close()
    if not _redirect_allowed(redirect_uri, registered):
        return _oauth_error("invalid_request", "redirect_uri not allowed")
    if not code_challenge:
        return _oauth_error("invalid_request", "code_challenge is required (PKCE)")

    nonce = secrets.token_urlsafe(16)
    csrf = secrets.token_urlsafe(16)
    state = _mint_oidc_state(
        {
            "provider": p.key,
            "client_id": q.get("client_id", ""),
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "client_state": q.get("state", ""),
            "nonce": nonce,
        },
        csrf,
    )
    url = oidc.authorize_url(p, state=state, nonce=nonce, redirect_uri=_oidc_callback_uri())
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        _CSRF_COOKIE,
        csrf,
        max_age=int(_OIDC_STATE_TTL.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return resp


async def admin_oidc_start(request: Request) -> Response:
    """Begin an OIDC flow for **admin** login: redirect to the IdP with a signed state marked
    `purpose=admin_login` so the shared callback mints an admin **session** (not a bearer code).
    Carries no OAuth client context (this isn't a connector flow) and reuses the one registered
    callback URI, so a deployer registers a single redirect URI for both flows."""
    import oidc  # noqa: F401  (lazy: the egress module)

    p = _safe_provider(request.query_params.get("provider", ""))
    if p is None:
        return _oauth_error("invalid_request", "unknown or unconfigured provider")
    nonce = secrets.token_urlsafe(16)
    csrf = secrets.token_urlsafe(16)
    state = _mint_oidc_state({"purpose": "admin_login", "provider": p.key, "nonce": nonce}, csrf)
    url = oidc.authorize_url(p, state=state, nonce=nonce, redirect_uri=_oidc_callback_uri())
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        _CSRF_COOKIE,
        csrf,
        max_age=int(_OIDC_STATE_TTL.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return resp


async def oidc_callback(request: Request) -> Response:
    """Complete OIDC: verify state + CSRF, exchange the code, verify the ID token, resolve the user
    **onboarded-only**, then either mint an **admin session** (when the state's `purpose=admin_login`)
    or resume the OAuth code mint. Any verification failure is a generic 403 — never an auto-created
    account. The `purpose` marker keeps the two flows from crossing: an admin-login state can only mint
    a session, a connector state can only mint a bearer code."""
    import oidc

    q = request.query_params
    claims = _verify_oidc_state(q.get("state", ""), request.cookies.get(_CSRF_COOKIE))
    if claims is None:
        return _oauth_error("invalid_request", "invalid or expired state")
    p = _safe_provider(claims.get("provider", ""))
    if p is None:
        return _oauth_error("invalid_request", "unknown or unconfigured provider")
    if not q.get("code"):
        return _oauth_error("invalid_request", "missing code")
    try:
        id_token = oidc.exchange_code(p, code=q.get("code", ""), redirect_uri=_oidc_callback_uri())
        identity = oidc.verify_id_token(p, id_token, nonce=claims.get("nonce", ""))
    except Exception:
        # Bad signature/aud/iss/sub/nonce/unverified-email/exchange error — all collapse to one verdict.
        return _oauth_error("access_denied", "OIDC verification failed", status=403)

    store = _open_store()
    if store is None:
        return _oauth_error("server_error", "no datastore configured", status=500)
    try:
        username = _resolve_oidc_user(store, p.key, identity)
        if claims.get("purpose") == "admin_login":
            # Admin login: a verified identity that resolves to THE configured admin gets a session;
            # anyone else (unresolved, or a non-admin user) is refused. The provider-pin + subject bind
            # are already enforced by `_resolve_oidc_user`, so this only adds the "is it the admin?" gate.
            import admin

            resp = admin.complete_admin_oidc_login(username)
        elif username is None:
            return _oauth_error("access_denied", "this account is not authorized", status=403)
        else:
            resp = _issue_authorization_code(
                store,
                client_id=claims.get("client_id", ""),
                redirect_uri=claims.get("redirect_uri", ""),
                code_challenge=claims.get("code_challenge", ""),
                username=username,
                client_state=claims.get("client_state", ""),
            )
    finally:
        store.close()
    resp.delete_cookie(_CSRF_COOKIE)
    return resp


# Statuses that may complete a login. `demo` is admitted so a public-demo instance works; `disabled`
# (or anything else) is refused.
_LOGIN_STATUSES = {"active", "demo"}


def _resolve_oidc_user(store: Store, provider_key: str, identity: Identity) -> str | None:
    """Map a verified OIDC identity to a username, or None to reject. Onboarded-only by default; with
    public signup enabled an unknown email self-provisions a demo user.

    Provider-binding closes IdP confusion: an existing user must be bound to THIS provider, and (once
    set) THIS subject — so an attacker with the same email at another IdP, or a different account at
    the same IdP, is refused rather than resolved to the victim."""
    import oidc

    user = get_user_by_email(store, identity.email)
    if user is not None:
        if user["status"] not in _LOGIN_STATUSES:
            return None
        if user["oidc_provider"] != provider_key:
            return None  # bound to a different IdP (or password-only) → not an OIDC login for this provider
        # Bind the subject on first login (a no-clobber UPDATE that only sets it when NULL), then
        # **re-read and require the stored subject is ours**. The re-read is what closes the
        # concurrent first-login race: if another subject bound first, our guarded UPDATE is a no-op,
        # the stored subject won't match, and we reject — rather than logging in against someone
        # else's binding. It also covers the steady state (an already-bound, mismatched subject).
        bind_oidc_subject(store, user["username"], identity.subject)
        bound = get_user(store, user["username"])
        if bound is None or bound["oidc_subject"] != identity.subject:
            return None
        return user["username"]

    # Unknown email: only a public-demo instance may self-provision (fail-closed default).
    if not oidc.public_signup_enabled():
        return None
    try:
        create_user(
            store,
            username=identity.email,
            password=None,
            email=identity.email,
            status="demo",
            oidc_provider=provider_key,
            oidc_subject=identity.subject,
        )
    except Exception as exc:
        # A UNIQUE collision (concurrent signup, or the email already used as a username) is not an
        # authorization → reject cleanly. But a real failure (datastore down, unexpected SQL error)
        # must surface, not masquerade as a 403 — so only swallow integrity errors. The MRO check is
        # portable across backends (sqlite3.IntegrityError / psycopg2.errors.UniqueViolation) without
        # importing psycopg2 here.
        if any(cls.__name__ == "IntegrityError" for cls in type(exc).__mro__):
            return None
        raise
    return identity.email
