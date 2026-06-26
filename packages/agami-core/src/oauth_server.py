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
import html
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import jwt
from ports import Principal
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from store import Store
from user_store import authenticate, get_user_by_email

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
    """A minimal login form that carries the OAuth params forward as hidden fields, plus a
    "Sign in with <provider>" link per configured OIDC provider. Neutral placeholders only."""
    # Escape every interpolated value — these come from attacker-controllable query/body params and
    # land in HTML attributes on the password-entry page; unescaped, they're a reflected-XSS vector.
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(params.get(k, ""), quote=True)}">'
        for k in _OAUTH_CONTEXT_KEYS
    )
    msg = f'<p role="alert">{html.escape(error)}</p>' if error else ""
    # OIDC links carry the same OAuth context to /oauth/oidc/start so the flow can resume after the
    # IdP round-trip. urlencode escapes the values for the query string.
    carried = {k: params.get(k, "") for k in _OAUTH_CONTEXT_KEYS}
    oidc_links = "".join(
        f'<a href="/oauth/oidc/start?{urlencode({**carried, "provider": key})}">'
        f"Sign in with {html.escape(key.title())}</a>"
        for key in providers
    )
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><title>Sign in</title></head>
<body><h1>Sign in</h1>{msg}
<form method="post">{hidden}
<label>Username <input name="username" autocomplete="username" placeholder="admin"></label>
<label>Password <input name="password" type="password" autocomplete="current-password"></label>
<button type="submit">Sign in</button></form>
{oidc_links}</body></html>"""
    )


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
    p = oidc.provider(q.get("provider", ""))
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


async def oidc_callback(request: Request) -> Response:
    """Complete OIDC: verify state + CSRF, exchange the code, verify the ID token, resolve the user
    **onboarded-only**, then resume the OAuth code mint. Any verification failure is a generic
    403 — never an auto-created account."""
    import oidc

    q = request.query_params
    claims = _verify_oidc_state(q.get("state", ""), request.cookies.get(_CSRF_COOKIE))
    if claims is None:
        return _oauth_error("invalid_request", "invalid or expired state")
    p = oidc.provider(claims.get("provider", ""))
    if p is None:
        return _oauth_error("invalid_request", "unknown or unconfigured provider")
    if not q.get("code"):
        return _oauth_error("invalid_request", "missing code")
    try:
        id_token = oidc.exchange_code(p, code=q.get("code", ""), redirect_uri=_oidc_callback_uri())
        email = oidc.verify_id_token(p, id_token, nonce=claims.get("nonce", ""))
    except Exception:
        # Bad signature/aud/iss/nonce/unverified-email/exchange error — all collapse to one verdict.
        return _oauth_error("access_denied", "OIDC verification failed", status=403)

    store = _open_store()
    if store is None:
        return _oauth_error("server_error", "no datastore configured", status=500)
    try:
        user = get_user_by_email(store, email)
        # Onboarded-only: the verified email must already be an active user (admin-created). An
        # unknown email is rejected, never auto-provisioned.
        #
        # LOAD-BEARING: resolution is by email alone, which is safe ONLY while a single IdP is
        # enabled. Before enabling a second provider, bind identity to (provider, subject) — else an
        # attacker with the same email at another IdP would resolve to this user (IdP confusion).
        if user is None or user["status"] != "active":
            return _oauth_error("access_denied", "this account is not authorized", status=403)
        resp = _issue_authorization_code(
            store,
            client_id=claims.get("client_id", ""),
            redirect_uri=claims.get("redirect_uri", ""),
            code_challenge=claims.get("code_challenge", ""),
            username=user["username"],
            client_state=claims.get("client_state", ""),
        )
    finally:
        store.close()
    resp.delete_cookie(_CSRF_COOKIE)
    return resp
