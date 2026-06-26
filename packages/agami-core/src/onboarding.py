"""Teammate self-onboarding — the **setup link** path for a password deployment.

When a deployment has no OIDC configured, a teammate the admin created is a *pending* user (no
password, no provider) and there's no email to send an invite to. The admin copies a **setup link**
from the console (a signed, time-boxed token — no new table) and shares it out-of-band; the teammate
opens it and sets their own password. The token is single-use by construction: claiming flips the user
out of the *pending* state, so the guarded `claim_pending_password` UPDATE no-ops any replay. (OIDC
deployments don't use this — teammates bind on first OIDC login at the connector; see `oauth_server`.)

This is a public surface (the teammate has no session yet), so it self-checks: a bad/expired/used
token, or an already-claimed user, yields a generic page — never a credential-overwrite of a claimed
account, and no email-enumeration (the token is opaque; we don't echo who it was for).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import ui
import user_store
from oauth_server import _open_store, _signing_secret
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

# A generous TTL — the admin shares the link out-of-band and the teammate may take a while. The token
# is still single-use (the pending guard), so the window only bounds an unused link, not a claimed one.
_SETUP_TTL = timedelta(days=14)
_SETUP_PURPOSE = "setup"  # marks this token apart from the OAuth bearer + the admin session JWT
_MIN_PASSWORD_LEN = 8


def mint_setup_token(username: str) -> str:
    """A signed, time-boxed setup token for `username` — the body of the admin's copy-able setup link."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": username,
            "purpose": _SETUP_PURPOSE,
            "iat": int(now.timestamp()),
            "exp": int((now + _SETUP_TTL).timestamp()),
        },
        _signing_secret(),
        algorithm="HS256",
    )


def verify_setup_token(token: str) -> str | None:
    """The username a valid setup token names, or None (bad signature / expired / wrong purpose)."""
    try:
        claims = jwt.decode(
            token, _signing_secret(), algorithms=["HS256"], options={"require": ["exp", "sub"]}
        )
    except Exception:
        return None
    if claims.get("purpose") != _SETUP_PURPOSE:
        return None
    sub = claims.get("sub")
    return sub if isinstance(sub, str) and sub else None


def is_pending(user: dict[str, Any]) -> bool:
    """A user who hasn't claimed a credential yet: no password AND no OIDC provider. Works for both row
    shapes — `get_user` (carries `password_hash`) and `list_users` (carries the derived `has_password`)."""
    has_password = user.get("password_hash") is not None or bool(user.get("has_password"))
    return not has_password and user.get("oidc_provider") is None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def setup_page_html(token: str, error: str = "") -> str:
    """The set-your-password page reached from a valid setup link."""
    alert = f'<div class="alert error">{ui.esc(error)}</div>' if error else ""
    body = f"""<div class="consent"><p class="who">Set up your account</p>
<p class="small">Choose a password to finish setting up.</p></div>
{alert}
<form method="post" action="/claim">
<input type="hidden" name="token" value="{ui.esc(token)}">
<label for="p">Password</label>
<input id="p" name="password" type="password" autocomplete="new-password" placeholder="••••••••">
<button class="btn" type="submit" style="margin-top:22px">Set password</button>
</form>"""
    return ui.auth_page("Set up your account", body)


def setup_done_html(base_url: str) -> str:
    body = f"""<div class="consent"><p class="who">You're all set</p>
<p class="small">Your password is set. Add this server to Claude as a custom connector:</p></div>
<p><span class="code">{ui.esc(base_url)}/mcp</span></p>"""
    return ui.auth_page("All set", body)


def setup_invalid_html() -> str:
    body = """<div class="consent"><p class="who">This link isn't valid</p>
<p class="small">It may have expired or already been used. Ask your administrator for a new setup
link.</p></div>"""
    return ui.auth_page("Invalid link", body)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _form(request: Request) -> dict[str, str]:
    data = await request.form()
    return {k: (v if isinstance(v, str) else "") for k, v in data.items()}


def _pending_user(username: str) -> dict[str, Any] | None:
    """The pending user a token names, or None (unknown / already claimed / no store)."""
    store = _open_store()
    if store is None:
        return None
    try:
        user = user_store.get_user(store, username)
    finally:
        store.close()
    return user if (user is not None and is_pending(user)) else None


async def claim(request: Request) -> Response:
    """GET → the set-password page for a valid link to a still-pending user; POST → set the password.
    Every failure (bad token, already-claimed, weak password, lost race) is a generic page — no
    credential overwrite of a claimed account, no enumeration."""
    if request.method == "GET":
        username = verify_setup_token(request.query_params.get("token", ""))
        if username is None or _pending_user(username) is None:
            return HTMLResponse(setup_invalid_html(), status_code=400)
        return HTMLResponse(setup_page_html(request.query_params.get("token", "")))

    form = await _form(request)
    token = form.get("token", "")
    username = verify_setup_token(token)
    if username is None or _pending_user(username) is None:
        return HTMLResponse(setup_invalid_html(), status_code=400)
    password = form.get("password", "")
    if len(password) < _MIN_PASSWORD_LEN:
        return HTMLResponse(
            setup_page_html(token, error=f"Use at least {_MIN_PASSWORD_LEN} characters."),
            status_code=400,
        )
    store = _open_store()
    if store is None:
        return HTMLResponse(setup_invalid_html(), status_code=400)
    try:
        # Guarded: the UPDATE only fires while still pending — a concurrent claim makes it a no-op.
        changed = user_store.claim_pending_password(store, username, password)
    finally:
        store.close()
    if not changed:
        return HTMLResponse(setup_invalid_html(), status_code=400)
    from mcp_http import public_base_url

    return HTMLResponse(setup_done_html(public_base_url()))


def routes() -> list:
    from starlette.routing import Route

    return [Route("/claim", claim, methods=["GET", "POST"])]


PUBLIC_PATHS = ("/claim",)
