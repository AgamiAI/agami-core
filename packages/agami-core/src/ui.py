"""Shared HTML shell for the server's web pages (login, consent, admin).

One styling system + two shells — `auth_page` (centered sign-in/consent) and `admin_shell` (the admin
console with tab nav) — so every server-rendered page matches the agami product design (the same look
as the web app: brand blue #0b57d0, white surface, pill buttons, the agami logo). This carries into
the hosted/enterprise products, so it's built to look professional. Everything interpolated MUST go
through `esc()` — these pages render attacker-influenceable values (usernames, emails, query params).
Pure strings; no template engine; a tiny CSS-only drawer + a native `<details>` account menu (no JS).
"""

from __future__ import annotations

import html

# Palette + components mirror the agami web app (brand #0b57d0, line #D2DBF1, chip #f4f5fb). Embedded
# so pages are self-contained — no build step, no asset pipeline. Layout is responsive: the media
# query at the end collapses the admin chrome and tightens the auth padding on a phone.
_CSS = """
:root{
  --brand:#0b57d0; --brand-600:#0a4ab1; --line:#d2dbf1; --chip:#f4f5fb;
  --ink:#171717; --muted:#737373; --bg:#ffffff; --ok:#047857; --ok-bg:#ecfdf5;
  --off:#737373; --off-bg:#f4f5fb; --danger:#b42318; --danger-bg:#fef3f2; --danger-line:#fecdca;
}
*{box-sizing:border-box}
body{
  margin:0; min-height:100vh; background:var(--bg); color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
a{color:var(--brand);text-decoration:none}
a:hover{text-decoration:underline}
h1{font-size:20px;font-weight:600;letter-spacing:-.01em;margin:0}
.sub{color:var(--muted);font-size:14px;margin:4px 0 0}
.muted{color:var(--muted)}
.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--chip);
  border:1px solid var(--line);border-radius:8px;padding:3px 8px;font-size:13px;color:var(--ink)}

/* form controls */
label{display:block;font-weight:550;font-size:13px;margin:16px 0 6px}
input[type=text],input[type=email],input[type=password],select{
  width:100%; height:46px; padding:0 14px; border:1px solid var(--line); border-radius:10px;
  background:#fff; font-size:15px; color:var(--ink); transition:border-color .15s, box-shadow .15s;
}
input:focus,select:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(11,87,208,.14)}

/* buttons — pill, like the app */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:10px;width:100%;height:48px;
  padding:0 18px;border-radius:999px;border:1px solid var(--brand);background:var(--brand);color:#fff;
  font-size:15px;font-weight:600;cursor:pointer;transition:background .15s,border-color .15s;}
.btn:hover{background:var(--brand-600);border-color:var(--brand-600);text-decoration:none}
.btn.secondary{background:#fff;color:var(--ink);border-color:var(--line)}
.btn.secondary:hover{background:var(--chip);border-color:#b9c5e8}
.btn.provider{background:#fff;color:var(--ink);border-color:var(--line);font-weight:550}
.btn.provider:hover{background:var(--chip);border-color:#b9c5e8}
.btn.provider img{height:18px;width:18px}
.btn.tiny{width:auto;height:34px;padding:0 14px;font-size:13px;font-weight:550}
.btn.danger{background:#fff;color:var(--danger);border-color:var(--danger-line)}
.btn.danger:hover{background:var(--danger-bg)}
.providers{display:flex;flex-direction:column;gap:10px}
.divider{display:flex;align-items:center;gap:12px;color:var(--muted);font-size:13px;margin:18px 0}
.divider::before,.divider::after{content:"";flex:1;height:1px;background:var(--line)}
.alert{padding:11px 13px;border-radius:12px;font-size:14px;margin:0 0 16px}
.alert.error{background:var(--danger-bg);color:var(--danger);border:1px solid var(--danger-line)}
.alert.ok{background:var(--ok-bg);color:var(--ok);border:1px solid #a7f3d0}
.pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
.pill.active{background:var(--ok-bg);color:var(--ok)}
.pill.disabled{background:var(--off-bg);color:var(--off)}

/* auth shell — centered on white, no card; generous so it doesn't read as cramped */
.auth{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:56px 24px}
.auth-inner{width:100%;max-width:400px}
.auth .brand{display:flex;justify-content:center;margin-bottom:36px}
.auth .brand img{height:42px}
.consent{text-align:center;margin-bottom:26px}
.consent .small{color:var(--muted);font-size:14px}
.consent .who{font-size:19px;font-weight:600;margin:3px 0}
.foot{margin-top:28px;text-align:center;font-size:14px}

/* admin console shell */
.topbar{height:62px;border-bottom:1px solid var(--line);display:flex;align-items:center;
  justify-content:space-between;padding:0 28px}
.topbar img{height:26px}
.main{max-width:1080px;margin:0 auto;padding:30px 28px 64px}
.head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px}
.tabs{display:flex;gap:6px;margin-top:18px;border-bottom:1px solid var(--line)}
.tabs a{padding:0 14px;height:40px;display:inline-flex;align-items:center;font-size:14px;
  font-weight:550;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px}
.tabs a:hover{color:var(--ink);text-decoration:none}
.tabs a.active{color:var(--ink);border-bottom-color:var(--brand)}
.panel{margin-top:24px}
.empty{text-align:center;color:var(--muted);padding:64px 20px;border:1px dashed var(--line);
  border-radius:14px;margin-top:8px}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px;min-width:560px}
th{text-align:left;font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;
  letter-spacing:.04em;padding:0 12px 10px}
td{padding:13px 12px;border-top:1px solid var(--line);vertical-align:middle}

/* account menu — an avatar-initials circle with a native <details> dropdown (no JS) */
.usermenu{position:relative}
.usermenu>summary{list-style:none;cursor:pointer;display:inline-flex}
.usermenu>summary::-webkit-details-marker{display:none}
.avatar{height:36px;width:36px;border-radius:999px;background:var(--brand);color:#fff;
  font-size:13px;font-weight:600;letter-spacing:.02em;display:inline-flex;align-items:center;
  justify-content:center;text-transform:uppercase}
.usermenu[open]>summary .avatar{box-shadow:0 0 0 3px rgba(11,87,208,.22)}
.usermenu-pop{position:absolute;right:0;top:46px;width:230px;background:#fff;border:1px solid var(--line);
  border-radius:14px;box-shadow:0 10px 34px rgba(23,23,23,.12);overflow:hidden;z-index:40}
.um-id{padding:13px 15px;border-bottom:1px solid var(--line)}
.um-name{font-weight:600;color:var(--ink)}
.um-email{font-size:12px;color:var(--muted);margin-top:1px;overflow:hidden;text-overflow:ellipsis}
.usermenu-pop a{display:block;padding:11px 15px;color:var(--ink);font-size:14px}
.usermenu-pop a:hover{background:var(--chip);text-decoration:none}

/* CSS-only right drawer (no JS) */
.drawer-toggle{position:absolute;opacity:0;pointer-events:none}
.drawer-wrap{position:fixed;inset:0;z-index:50;pointer-events:none;visibility:hidden}
.drawer-backdrop{position:absolute;inset:0;background:rgba(23,23,23,.32);opacity:0;transition:opacity .22s}
.drawer{position:absolute;top:0;right:0;height:100%;width:430px;max-width:92vw;background:#fff;
  box-shadow:-10px 0 40px rgba(23,23,23,.14);transform:translateX(100%);transition:transform .26s ease;
  padding:26px 28px;overflow:auto}
.drawer-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.drawer-x{cursor:pointer;color:var(--muted);font-size:22px;line-height:1;border:0;background:none}
.drawer-toggle:checked ~ .drawer-wrap{pointer-events:auto;visibility:visible}
.drawer-toggle:checked ~ .drawer-wrap .drawer-backdrop{opacity:1}
.drawer-toggle:checked ~ .drawer-wrap .drawer{transform:translateX(0)}

@media (max-width:560px){
  .auth{padding:32px 18px}
  .topbar{padding:0 16px;height:56px}
  .main{padding:20px 16px 48px}
}
"""


def esc(value: str | None) -> str:
    """HTML-escape (attribute-safe) any interpolated value. Use for EVERYTHING user-influenced."""
    return html.escape(value or "", quote=True)


def initials(name: str) -> str:
    """Up to two leading-letter initials for the avatar (falls back to '?' for an empty name)."""
    parts = [p for p in (name or "").split() if p]
    letters = "".join(p[0] for p in parts[:2])
    return letters.upper() or "?"


def _doc(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="icon" href="/static/logo_icon.png">
<style>{_CSS}</style>
</head><body>{body}</body></html>"""


def auth_page(title: str, body: str) -> str:
    """Centered sign-in / consent shell: the agami logo above `body`, on plain white."""
    return _doc(
        title,
        f'<div class="auth"><div class="auth-inner">'
        f'<div class="brand"><img src="/static/logo_h.svg" alt="agami"></div>'
        f"{body}</div></div>",
    )


_PROVIDER_LABELS = {"google": "Google", "microsoft": "Microsoft"}


def provider_button(key: str, href: str) -> str:
    """A pill 'Continue with <provider>' button (provider icon + label)."""
    label = _PROVIDER_LABELS.get(key, key.title())
    return (
        f'<a class="btn provider" href="{esc(href)}">'
        f'<img src="/static/{esc(key)}_logo.svg" alt=""> Continue with {esc(label)}</a>'
    )


_TABS = (
    ("dashboard", "Dashboard"),
    ("users", "Users"),
    ("sessions", "Sessions"),
    ("calls", "Tool calls"),
)


def _account_menu(label: str, email: str) -> str:
    """The top-right avatar + dropdown (signed-in identity + Sign out), native <details>, no JS."""
    return f"""<details class="usermenu">
<summary aria-label="Account menu"><span class="avatar">{esc(initials(label))}</span></summary>
<div class="usermenu-pop">
<div class="um-id"><div class="um-name">{esc(label)}</div><div class="um-email">{esc(email)}</div></div>
<a href="/admin/logout">Sign out</a>
</div></details>"""


def admin_shell(
    title: str,
    active: str,
    body: str,
    *,
    admin_label: str = "",
    admin_email: str = "",
    extra: str = "",
) -> str:
    """The admin console shell: a top bar (logo + account menu) and the Dashboard/Users/Sessions tabs.
    `extra` is emitted at the body root before the bar — used for the CSS-only drawer (whose toggle
    checkbox must be a sibling of `.drawer-wrap`)."""
    tabs = "".join(
        f'<a href="/admin?tab={esc(key)}" class="{"active" if key == active else ""}">{esc(label)}</a>'
        for key, label in _TABS
    )
    body_html = f"""{extra}<div class="topbar">
<img src="/static/logo_h.svg" alt="agami">
{_account_menu(admin_label or "Admin", admin_email)}
</div>
<div class="main">
<div class="head"><h1>Admin Console</h1></div>
<nav class="tabs">{tabs}</nav>
<div class="panel">{body}</div>
</div>"""
    return _doc(title, body_html)
