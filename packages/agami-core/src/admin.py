"""The admin web surface — onboard/enable/disable/list users, plus the friendly browser landings.

Two auth surfaces live in this server: the MCP bearer JWT (claude.ai) and — here — a browser
**session cookie** for the human admin. `/admin/*` is session-gated (the admin-gate = the
env-configured `AGAMI_ADMIN_USERNAME`); a non-admin, even with valid credentials, can't get in. This
module also renders the friendly landings a human sees if they point a browser at the server.

The page builders (`*_html`) are split from the request handlers so previews can render them with
sample values. Every interpolated value goes through `ui.esc` (these pages show emails/names).
"""

from __future__ import annotations

from typing import Any

import ui


def _full_name(user: dict[str, Any]) -> str:
    """A user's display name from first/last; falls back to the email's local part when unnamed."""
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p).strip()
    return name or (user.get("email") or "").split("@")[0]


# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------


def admin_login_body_html(error: str = "", providers: tuple[str, ...] = ()) -> str:
    """The admin sign-in page (password + any configured social providers). No banner copy — the
    logo and the form speak for themselves (this is the admin's own login, not a client consent)."""
    buttons = "".join(ui.provider_button(k, f"/admin/oidc/start?provider={k}") for k in providers)
    social = f'<div class="providers">{buttons}</div><div class="divider">or</div>' if buttons else ""
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


def _row_action(user: dict[str, Any], csrf: str, admin_email: str) -> str:
    # The admin can't disable their own account (it would lock themselves out).
    if user.get("email") == admin_email:
        return '<span class="muted">—</span>'
    active = user["status"] == "active"
    target, label, cls = ("disabled", "Disable", "danger") if active else ("active", "Enable", "secondary")
    return (
        '<form method="post" action="/admin/users/status" style="display:inline">'
        f'<input type="hidden" name="csrf" value="{ui.esc(csrf)}">'
        f'<input type="hidden" name="email" value="{ui.esc(user.get("email"))}">'
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


def users_tab_html(
    users: list[dict[str, Any]],
    csrf: str,
    *,
    admin_email: str = "",
    admin_label: str = "",
    error: str = "",
    ok: str = "",
) -> str:
    """The Users tab: a roster table + an 'Add user' button that opens the drawer."""
    rows = ""
    for u in users:
        sign_in = u.get("oidc_provider") or ("password" if u.get("has_password") else "not set yet")
        rows += (
            "<tr>"
            f'<td><strong>{ui.esc(_full_name(u))}</strong></td>'
            f'<td class="muted">{ui.esc(u.get("email") or "—")}</td>'
            f'<td class="muted">{ui.esc(sign_in)}</td>'
            f"<td>{_status_pill(u['status'])}</td>"
            f'<td style="text-align:right">{_row_action(u, csrf, admin_email)}</td>'
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
        admin_label=admin_label,
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
    """Shown after a successful Google/Microsoft sign-in by someone who hasn't been onboarded — their
    identity is real, but no admin has added them. No connector hint (they can't use it yet)."""
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
