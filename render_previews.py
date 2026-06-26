#!/usr/bin/env python3
"""Render every server-rendered page to standalone previews/*.html with sample values.

A dev tool (not shipped/imported by the server) so the UI can be eyeballed in a browser without
running the service. Rewrites the absolute /static/ asset paths to relative + copies the assets in,
so each preview file opens correctly via file://.
"""

import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent
PKG = ROOT / "packages" / "agami-core" / "src"
sys.path.insert(0, str(PKG))

import admin  # noqa: E402
import oauth_server  # noqa: E402

OUT = ROOT / "previews"
OUT.mkdir(exist_ok=True)
shutil.copytree(PKG / "static", OUT / "static", dirs_exist_ok=True)

BASE = "https://demo-a1b2c3.trycloudflare.com"
OAUTH = {
    "client_id": "cid_9f2c",
    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
    "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    "state": "xyz789",
}
ADMIN_EMAIL = "you@example.com"
USERS = [
    {"username": ADMIN_EMAIL, "first_name": "Alex", "last_name": "Kim", "email": ADMIN_EMAIL,
     "status": "active", "oidc_provider": None, "has_password": True},
    {"username": "jordan@example.com", "first_name": "Jordan", "last_name": "Lee",
     "email": "jordan@example.com", "status": "active", "oidc_provider": "google", "has_password": False},
    {"username": "sam@example.com", "first_name": "Sam", "last_name": "Okafor",
     "email": "sam@example.com", "status": "active", "oidc_provider": "microsoft", "has_password": False},
    {"username": "riley@example.com", "first_name": "Riley", "last_name": "Chen",
     "email": "riley@example.com", "status": "disabled", "oidc_provider": None, "has_password": False},
    {"username": "morgan@example.com", "first_name": "Morgan", "last_name": "Diaz",
     "email": "morgan@example.com", "status": "active", "oidc_provider": None, "has_password": False},
]


def write(name: str, html: str) -> None:
    html = html.replace('"/static/', '"static/')  # absolute → relative so file:// resolves
    (OUT / name).write_text(html)


ADMIN = {"admin_username": ADMIN_EMAIL, "admin_label": "Alex Kim", "admin_email": ADMIN_EMAIL}
CHROME = {"admin_label": "Alex Kim", "admin_email": ADMIN_EMAIL}

write("01-login.html", oauth_server.login_body_html(OAUTH, providers=("google", "microsoft"), wrap=True))
write("02-login-password-only.html", oauth_server.login_body_html(OAUTH, wrap=True))
write(
    "03-login-error.html",
    oauth_server.login_body_html(OAUTH, error="Invalid email or password.", providers=("google", "microsoft"), wrap=True),
)
write("04-admin-login.html", admin.admin_login_body_html(provider="google"))
write("05-admin-users.html", admin.users_tab_html(USERS, csrf="t0ken", ok="User added.", **ADMIN))
write("06-admin-dashboard.html", admin.dashboard_tab_html(**CHROME))
write("07-admin-sessions.html", admin.sessions_tab_html(**CHROME))
write("08-not-admin.html", admin.not_admin_body_html(BASE))
write("09-landing.html", admin.landing_body_html(BASE))
write("10-mcp-in-browser.html", admin.mcp_landing_body_html(BASE))
write("11-not-authorized.html", admin.not_authorized_body_html("morgan@example.com"))

print(f"Wrote {len(list(OUT.glob('*.html')))} previews to {OUT}/")
for p in sorted(OUT.glob("*.html")):
    print(f"  open {p}")
