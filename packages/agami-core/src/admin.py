"""The admin web surface — onboard/enable/disable/list users, plus the friendly browser landings.

Two auth surfaces live in this server: the MCP bearer JWT (claude.ai) and — here — a browser
**session cookie** for the human admin. `/admin/*` is session-gated (the admin-gate = the
env-configured `AGAMI_ADMIN_USERNAME`); a non-admin, even with valid credentials, can't get in. This
module also renders the friendly landings a human sees if they point a browser at the server.

The page builders (`*_html`) are split from the request handlers so previews can render them with
sample values. Every interpolated value goes through `ui.esc` (these pages show emails/names).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import ui
import user_store

# Reuse the OAuth provider's shared HS256 secret accessor + store opener + the admin OIDC-start handler
# so the admin surface signs with the same key, reads the same datastore, and runs the same hardened
# OIDC flow (no second source of truth).
from oauth_server import _open_store, _signing_secret, admin_oidc_start
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response


def _full_name(user: dict[str, Any]) -> str:
    """A user's display name from first/last; falls back to the email's local part when unnamed."""
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p).strip()
    return name or (user.get("email") or "").split("@")[0]


# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------


def admin_login_body_html(error: str = "", provider: str | None = None) -> str:
    """The admin sign-in page: the admin's pinned social provider (when configured) above the password
    form — matching the MCP login's options. Only the admin's *one* pinned provider is offered, since
    that's the only one that can resolve to the admin (a second button would just say "not an admin").
    No banner copy — the logo and the form speak for themselves (this is the admin's own login)."""
    button = (
        ui.provider_button(provider, f"/admin/oidc/start?provider={provider}") if provider else ""
    )
    social = f'<div class="providers">{button}</div><div class="divider">or</div>' if button else ""
    alert = f'<div class="alert error">{ui.esc(error)}</div>' if error else ""
    body = f"""{alert}{social}
<form method="post">
<label for="u">Email</label>
<input id="u" name="username" type="email" autocomplete="email" placeholder="you@example.com">
<label for="p">Password</label>
<input id="p" name="password" type="password" autocomplete="current-password" placeholder="••••••••">
<button class="btn" type="submit" style="margin-top:22px">Sign in</button>
</form>"""
    return ui.auth_page("Admin sign in", body)


# ---------------------------------------------------------------------------
# Admin console — Users tab (+ Dashboard / Sessions placeholders)
# ---------------------------------------------------------------------------


def _status_pill(status: str) -> str:
    cls = "active" if status == "active" else "disabled"
    return f'<span class="pill {cls}">{ui.esc(status)}</span>'


def _row_action(user: dict[str, Any], csrf: str, admin_username: str) -> str:
    # The admin can't disable their own account (it would lock themselves out). Keyed on username —
    # the stable identity column the status update writes against (email is display-only).
    if user.get("username") == admin_username:
        return '<span class="muted">—</span>'
    active = user["status"] == "active"
    target, label, cls = ("disabled", "Disable", "danger") if active else ("active", "Enable", "secondary")
    return (
        '<form method="post" action="/admin/users/status" style="display:inline">'
        f'<input type="hidden" name="csrf" value="{ui.esc(csrf)}">'
        f'<input type="hidden" name="username" value="{ui.esc(user.get("username"))}">'
        f'<input type="hidden" name="status" value="{target}">'
        f'<button class="btn tiny {cls}" type="submit">{label}</button></form>'
    )


def _add_user_drawer(csrf: str) -> str:
    """A CSS-only right-side drawer (no JS) with a minimal 'Add user' form.

    Just email + first/last name: the teammate picks their own sign-in method (Google, Microsoft, or
    a password they set themselves) the first time they sign in — the admin never sets a password."""
    return f"""<input type="checkbox" id="add-user" class="drawer-toggle">
<div class="drawer-wrap">
<label for="add-user" class="drawer-backdrop"></label>
<aside class="drawer">
<div class="drawer-head"><h1 style="font-size:17px">Add user</h1>
<label for="add-user" class="drawer-x" aria-label="Close">&times;</label></div>
<p class="sub" style="margin-bottom:8px">They'll sign in with this email — through Google, Microsoft,
or a password they set the first time — and can then use this agami server from Claude.</p>
<form method="post" action="/admin/users">
<input type="hidden" name="csrf" value="{ui.esc(csrf)}">
<label for="d-email">Email</label>
<input id="d-email" name="email" type="email" placeholder="you@example.com">
<label for="d-first">First name</label>
<input id="d-first" name="first_name" type="text" placeholder="Jordan">
<label for="d-last">Last name</label>
<input id="d-last" name="last_name" type="text" placeholder="Lee">
<button class="btn" type="submit" style="margin-top:22px">Add user</button>
</form>
</aside>
</div>"""


def _signin_cell(user: dict[str, Any], setup_links: dict[str, str]) -> str:
    """The Sign-in column: the user's method, plus — for a *pending* user in a password deployment —
    a copy-able setup link the admin shares out-of-band (the page is session-gated, admin-only)."""
    sign_in = user.get("oidc_provider") or ("password" if user.get("has_password") else "not set yet")
    link = setup_links.get(user.get("username", ""))
    extra = (
        f'<details class="setup"><summary>Setup link</summary>'
        f'<input class="code" readonly value="{ui.esc(link)}" style="width:100%;margin-top:6px">'
        f"</details>"
        if link
        else ""
    )
    return f'<td class="muted">{ui.esc(sign_in)}{extra}</td>'


def users_tab_html(
    users: list[dict[str, Any]],
    csrf: str,
    *,
    admin_username: str = "",
    admin_email: str = "",
    admin_label: str = "",
    setup_links: dict[str, str] | None = None,
    error: str = "",
    ok: str = "",
) -> str:
    """The Users tab: a roster table + an 'Add user' button that opens the drawer. `setup_links`
    (username → URL) attaches a copy-able setup link to each pending row (password deployments)."""
    setup_links = setup_links or {}
    rows = ""
    for u in users:
        rows += (
            "<tr>"
            f'<td><strong>{ui.esc(_full_name(u))}</strong></td>'
            f'<td class="muted">{ui.esc(u.get("email") or "—")}</td>'
            f"{_signin_cell(u, setup_links)}"
            f"<td>{_status_pill(u['status'])}</td>"
            f'<td style="text-align:right">{_row_action(u, csrf, admin_username)}</td>'
            "</tr>"
        )
    alerts = (f'<div class="alert ok">{ui.esc(ok)}</div>' if ok else "") + (
        f'<div class="alert error">{ui.esc(error)}</div>' if error else ""
    )
    panel = f"""{alerts}
<div class="row" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
<p class="muted" style="margin:0">People who can use this agami server.</p>
<label for="add-user" class="btn tiny">+ Add user</label>
</div>
<div class="table-wrap"><table>
<thead><tr><th>Name</th><th>Email</th><th>Sign-in</th><th>Status</th><th></th></tr></thead>
<tbody>{rows}</tbody>
</table></div>"""
    return ui.admin_shell(
        "Users · agami admin",
        "users",
        panel,
        admin_label=admin_label or admin_username,
        admin_email=admin_email,
        extra=_add_user_drawer(csrf),
    )


def _coming_soon(tab: str, label: str, *, admin_label: str = "", admin_email: str = "") -> str:
    panel = f'<div class="empty"><strong>{ui.esc(label)}</strong><br>Coming soon.</div>'
    return ui.admin_shell(
        f"{label} · agami admin", tab, panel, admin_label=admin_label, admin_email=admin_email
    )


def dashboard_tab_html(*, admin_label: str = "", admin_email: str = "") -> str:
    return _coming_soon("dashboard", "Dashboard", admin_label=admin_label, admin_email=admin_email)


def sessions_tab_html(*, admin_label: str = "", admin_email: str = "") -> str:
    return _coming_soon("sessions", "Sessions", admin_label=admin_label, admin_email=admin_email)


# ---------------------------------------------------------------------------
# Friendly browser landings
# ---------------------------------------------------------------------------


def _connect_block(base_url: str) -> str:
    return (
        '<p class="small" style="margin-bottom:6px">Add this server to Claude as a custom connector:</p>'
        f'<p><span class="code">{ui.esc(base_url)}/mcp</span></p>'
    )


def landing_body_html(base_url: str) -> str:
    """The root page a human lands on if they open the server URL in a browser."""
    body = f"""<div class="consent"><p class="who">agami</p>
<p class="small">A governed, self-hosted data agent for Claude.</p></div>
{_connect_block(base_url)}
<p class="foot"><a href="/admin">Admin sign in →</a></p>"""
    return ui.auth_page("agami", body)


def not_admin_body_html(base_url: str) -> str:
    """Shown when a valid but non-admin user signs in at /admin/login."""
    body = f"""<div class="consent"><p class="who">You're signed in</p>
<p class="small">This account isn't an administrator.</p></div>
{_connect_block(base_url)}
<p class="foot muted">Only the administrator can manage users here.</p>"""
    return ui.auth_page("Signed in", body)


def not_authorized_body_html(email: str) -> str:
    """The branded "your identity is real but no admin has added you" page for an un-onboarded
    Google/Microsoft sign-in. Not yet wired into the OIDC rejection (which still returns a JSON OAuth
    error); rendered in previews and ready for the self-onboarding flow to adopt. No connector hint —
    they can't use it yet."""
    body = f"""<div class="consent"><p class="who">Not set up yet</p>
<p class="small">{ui.esc(email)} isn't authorized for this agami server.</p></div>
<p class="foot muted">Ask the administrator to add you, then sign in again.</p>"""
    return ui.auth_page("Not authorized", body)


def mcp_landing_body_html(base_url: str) -> str:
    """The branded body returned when a *browser* hits /mcp (a machine endpoint) unauthenticated."""
    body = f"""<div class="consent"><p class="who">This is an MCP endpoint</p>
<p class="small">It's meant for Claude, not a browser.</p></div>
{_connect_block(base_url)}
<p class="foot"><a href="/admin">Admin sign in →</a></p>"""
    return ui.auth_page("agami · MCP endpoint", body)


# ---------------------------------------------------------------------------
# Session auth — a browser session cookie (separate from the MCP bearer JWT)
# ---------------------------------------------------------------------------
#
# `/admin/*` is gated by a signed session cookie, NOT the MCP bearer token. The two share the HS256
# signing secret + algorithm, so the `purpose` claim keeps them from being interchangeable: a token
# minted for the query surface (`issue_jwt`, which carries no `purpose`) must never satisfy the admin
# gate — even for the admin's own user. The gate also requires `sub == AGAMI_ADMIN_USERNAME`, so a
# valid non-admin can never hold an admin session.

_SESSION_COOKIE = "agami_admin_session"
_SESSION_TTL = timedelta(hours=12)
_SESSION_PURPOSE = "admin_session"


def _admin_username() -> str | None:
    """The single admin's username = the **normalized** admin email (the admin-gate). Lowercased+trimmed
    to match how the seed stores it + how OIDC resolves it, so the gate is case-insensitive. Unset ⇒ the
    admin UI is disabled entirely."""
    name = os.environ.get("AGAMI_ADMIN_USERNAME", "").strip().lower()
    return name or None


def _admin_provider() -> str | None:
    """The admin's pinned OIDC provider (`AGAMI_ADMIN_PROVIDER`), or None — only when it's a provider
    that's actually configured (client id/secret present)."""
    key = os.environ.get("AGAMI_ADMIN_PROVIDER", "").strip().lower()
    if not key:
        return None
    import oidc  # lazy: the egress module, server-only

    return key if key in oidc.available_providers() else None


def _admin_login_provider() -> str | None:
    """The provider button to render on the admin login: the pinned, configured provider, but ONLY
    when the admin's stored row is actually bound to it. This avoids a dead button — e.g. if
    `AGAMI_ADMIN_PROVIDER` is set after the admin was seeded password-only (the seed is idempotent and
    won't backfill `oidc_provider`), the button would otherwise show but dead-end at "not an admin"."""
    provider = _admin_provider()
    admin = _admin_username()
    if provider is None or admin is None:
        return None
    store = _open_store()
    if store is None:
        return None
    try:
        row = user_store.get_user(store, admin)
    finally:
        store.close()
    return provider if row is not None and row.get("oidc_provider") == provider else None


def issue_session(username: str) -> str:
    """A short-TTL HS256 session JWT (sub=username, purpose=admin_session) for the cookie."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": username,
            "purpose": _SESSION_PURPOSE,
            "iat": int(now.timestamp()),
            "exp": int((now + _SESSION_TTL).timestamp()),
        },
        _signing_secret(),
        algorithm="HS256",
    )


def current_admin(request: Request) -> str | None:
    """The signed-in admin's username, or None: verify the cookie (sig + exp + purpose) AND that its
    subject is THE configured admin. Any failure → None (fail closed → redirect to login)."""
    admin = _admin_username()
    token = request.cookies.get(_SESSION_COOKIE)
    if admin is None or not token:
        return None
    try:
        claims = jwt.decode(
            token, _signing_secret(), algorithms=["HS256"], options={"require": ["exp", "sub"]}
        )
    except Exception:
        return None
    if claims.get("purpose") != _SESSION_PURPOSE:
        return None
    sub = claims.get("sub")
    if not isinstance(sub, str) or sub != admin:
        return None
    return sub


def _set_session(resp: Response, token: str) -> None:
    # HttpOnly (no JS read) + Secure (HTTPS only — deployments are HTTPS) + SameSite=Lax (not sent on
    # cross-site POSTs); scoped to /admin so it never rides along to /mcp or /static.
    resp.set_cookie(
        _SESSION_COOKIE,
        token,
        max_age=int(_SESSION_TTL.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/admin",
    )


def _clear_session(resp: Response) -> None:
    resp.delete_cookie(_SESSION_COOKIE, path="/admin")


# CSRF: a token derived from the session cookie via HMAC(secret, cookie). An attacker who can neither
# read the HttpOnly cookie nor know the server secret can't forge it — defense-in-depth over SameSite.
def _csrf_for(session_token: str) -> str:
    return hmac.new(_signing_secret().encode(), session_token.encode(), hashlib.sha256).hexdigest()


def _origin_ok(request: Request) -> bool:
    """A second, independent CSRF gate: if the browser sent an Origin (or Referer) on this POST, its
    scheme+host MUST match our own. Browsers always attach Origin to cross-site POSTs, so a forged
    request from another site is caught here even before the token check. When neither header is
    present (some same-origin form posts, test clients) we don't fail — the signed CSRF token carries
    the load — so this only ever *adds* protection."""
    from urllib.parse import urlsplit

    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True
    want = urlsplit(_base_url())
    got = urlsplit(origin)
    return (got.scheme, got.hostname, got.port) == (want.scheme, want.hostname, want.port)


def _csrf_ok(request: Request, presented: str) -> bool:
    token = request.cookies.get(_SESSION_COOKIE)
    if not token or not presented:
        return False
    if not _origin_ok(request):
        return False
    return hmac.compare_digest(_csrf_for(token), presented)


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------

# Flash text is server-owned (keyed by a short code in the redirect query) — never echoed user input,
# so a redirect can't be turned into a reflected-content vector.
_OK_FLASH = {"added": "User added.", "enabled": "User enabled.", "disabled": "User disabled."}
_ERR_FLASH = {
    "dup": "A user with that email already exists.",
    "bad_email": "Enter a valid email address.",
    "csrf": "Your session expired — please try again.",
    "self": "You can't change your own status.",
    "bad": "That action isn't allowed.",
    "notfound": "No such user.",
}


async def _form(request: Request) -> dict[str, str]:
    data = await request.form()
    return {k: (v if isinstance(v, str) else "") for k, v in data.items()}


def _base_url() -> str:
    from mcp_http import public_base_url

    return public_base_url()


def _is_integrity_error(exc: Exception) -> bool:
    # Portable across backends (sqlite3.IntegrityError / psycopg2 UniqueViolation) without importing
    # the driver here — a UNIQUE collision is "duplicate", any other error must surface.
    return any(cls.__name__ == "IntegrityError" for cls in type(exc).__mro__)


def _admin_chrome(store: Any, admin_username: str) -> dict[str, str]:
    """Avatar label + email for the top-bar account menu (from the admin's own user row)."""
    row = user_store.get_user(store, admin_username) if store is not None else None
    return {
        "admin_label": _full_name(row) if row else admin_username,
        "admin_email": (row or {}).get("email") or "",
    }


def complete_admin_oidc_login(username: str | None) -> Response:
    """Finish an admin OIDC login (called by the shared OIDC callback for an `admin_login` state): a
    verified identity that resolves to THE configured admin gets an admin session; anyone else — an
    unresolved identity, or a valid but non-admin user — is refused with the branded page, no session.
    The provider-pin + subject binding were already enforced upstream by `_resolve_oidc_user`."""
    if username is not None and username == _admin_username():
        resp: Response = RedirectResponse("/admin", status_code=302)
        _set_session(resp, issue_session(username))
        return resp
    return HTMLResponse(not_admin_body_html(_base_url()), status_code=403)


async def admin_login(request: Request) -> Response:
    """GET → the admin sign-in page; POST → authenticate, gate on the admin-username, mint a session."""
    if request.method == "GET":
        if current_admin(request) is not None:
            return RedirectResponse("/admin", status_code=302)
        return HTMLResponse(admin_login_body_html(provider=_admin_login_provider()))

    form = await _form(request)
    # Email is the identity: normalize the typed address (trim + lowercase) so login is
    # case-insensitive and matches the normalized username the seed stored.
    typed = form.get("username", "").strip().lower()
    store = _open_store()
    try:
        principal = (
            user_store.authenticate(store, typed, form.get("password", ""))
            if store is not None
            else None
        )
    finally:
        if store is not None:
            store.close()
    if principal is None:
        # Same generic message for wrong password, unknown user, or disabled — no enumeration oracle.
        # Keep the social button on the re-render so a failed password attempt doesn't hide it.
        return HTMLResponse(
            admin_login_body_html(error="Invalid email or password.", provider=_admin_login_provider()),
            status_code=401,
        )
    if principal.subject != _admin_username():
        # Valid credentials, but not THE admin: no session minted; a friendly "use via Claude" page.
        return HTMLResponse(not_admin_body_html(_base_url()), status_code=403)
    resp = RedirectResponse("/admin", status_code=302)
    _set_session(resp, issue_session(principal.subject))
    return resp


async def admin_logout(request: Request) -> Response:
    resp = RedirectResponse("/admin/login", status_code=302)
    _clear_session(resp)
    return resp


async def admin_home(request: Request) -> Response:
    """The console. `?tab=` picks Dashboard / Users (default) / Sessions. Session-gated."""
    admin = current_admin(request)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=302)
    store = _open_store()
    try:
        chrome = _admin_chrome(store, admin)
        tab = request.query_params.get("tab", "users")
        if tab == "dashboard":
            return HTMLResponse(dashboard_tab_html(**chrome))
        if tab == "sessions":
            return HTMLResponse(sessions_tab_html(**chrome))
        users = user_store.list_users(store) if store is not None else []
    finally:
        if store is not None:
            store.close()
    # current_admin already proved the cookie is present + valid; .get (not bracket) keeps a malformed
    # duplicate-cookie header from turning into an unhandled 500.
    csrf = _csrf_for(request.cookies.get(_SESSION_COOKIE, ""))
    ok = _OK_FLASH.get(request.query_params.get("ok", ""), "")
    err = _ERR_FLASH.get(request.query_params.get("err", ""), "")
    return HTMLResponse(
        users_tab_html(
            users, csrf, admin_username=admin, setup_links=_setup_links(users), ok=ok, error=err, **chrome
        )
    )


def _setup_links(users: list[dict[str, Any]]) -> dict[str, str]:
    """Per-pending-user setup links — but only in a **password** deployment (no OIDC configured). When
    an OIDC provider is configured, teammates onboard by signing in with it, so no link is offered."""
    import oidc  # lazy: the egress module, server-only

    if oidc.available_providers():
        return {}
    import onboarding

    base = _base_url()
    return {
        u["username"]: f"{base}/claim?token={onboarding.mint_setup_token(u['username'])}"
        for u in users
        if onboarding.is_pending(u)
    }


def _valid_email(email: str) -> bool:
    # A deliberately loose check — we're not validating deliverability, just rejecting obvious junk
    # before it becomes a username. Real verification happens when the user first signs in (a later
    # self-onboarding step).
    email = email.strip()
    return "@" in email and "." in email.rsplit("@", 1)[-1] and " " not in email


async def admin_create_user(request: Request) -> Response:
    """Onboard a teammate: a *pending* user (no password, no provider) keyed by their email. They pick
    their sign-in method on first login (a later self-onboarding step). Admin-gated + CSRF-checked."""
    admin = current_admin(request)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=302)
    form = await _form(request)
    if not _csrf_ok(request, form.get("csrf", "")):
        return RedirectResponse("/admin?err=csrf", status_code=302)
    email = (form.get("email") or "").strip()
    if not _valid_email(email):
        return RedirectResponse("/admin?err=bad_email", status_code=302)
    store = _open_store()
    if store is None:
        return RedirectResponse("/admin?err=bad", status_code=302)
    try:
        # Username == the normalized email, so the teammate signs in with the address they know.
        normalized = email.lower()
        user_store.create_user(
            store,
            username=normalized,
            email=normalized,
            first_name=form.get("first_name", ""),
            last_name=form.get("last_name", ""),
            password=None,
        )
    except Exception as exc:
        # A UNIQUE collision is a duplicate → flash, not a 500. Closing the store in `finally` rolls
        # back the failed INSERT so its write lock doesn't strand the connection.
        if _is_integrity_error(exc):
            return RedirectResponse("/admin?err=dup", status_code=302)
        raise
    finally:
        store.close()
    return RedirectResponse("/admin?ok=added", status_code=302)


async def admin_set_status(request: Request) -> Response:
    """Enable/disable a user (the existing active/disabled status flag). Admin-gated + CSRF-checked;
    can't disable self."""
    admin = current_admin(request)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=302)
    form = await _form(request)
    if not _csrf_ok(request, form.get("csrf", "")):
        return RedirectResponse("/admin?err=csrf", status_code=302)
    username = form.get("username", "")
    status = form.get("status", "")
    if status not in ("active", "disabled"):
        return RedirectResponse("/admin?err=bad", status_code=302)
    if username == admin:
        return RedirectResponse("/admin?err=self", status_code=302)
    store = _open_store()
    if store is None:
        # Mirror create: a missing datastore is an error, not a silent "done" (no false success flash).
        return RedirectResponse("/admin?err=bad", status_code=302)
    try:
        changed = user_store.set_status(store, username, status)
    finally:
        store.close()
    if not changed:
        # The username matched no row (e.g. a stale form) — don't flash a success for a no-op.
        return RedirectResponse("/admin?err=notfound", status_code=302)
    return RedirectResponse(
        f"/admin?ok={'disabled' if status == 'disabled' else 'enabled'}", status_code=302
    )


def routes() -> list:
    """The `/admin/*` routes, for the transport to mount. Each is session-gated in the handler (the
    transport adds these paths to the bearer public-skip — they do their own auth, not the MCP one)."""
    from starlette.routing import Route

    return [
        Route("/admin", admin_home, methods=["GET"]),
        Route("/admin/login", admin_login, methods=["GET", "POST"]),
        Route("/admin/logout", admin_logout, methods=["GET"]),
        # The admin OIDC start (handler lives in oauth_server, with the OIDC machinery). The IdP
        # redirects back to the shared /oauth/oidc/callback, which branches on the state's purpose.
        Route("/admin/oidc/start", admin_oidc_start, methods=["GET"]),
        Route("/admin/users", admin_create_user, methods=["POST"]),
        Route("/admin/users/status", admin_set_status, methods=["POST"]),
    ]


ADMIN_PATHS = (
    "/admin",
    "/admin/login",
    "/admin/logout",
    "/admin/oidc/start",
    "/admin/users",
    "/admin/users/status",
)
