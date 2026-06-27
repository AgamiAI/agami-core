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
import onboarding  # noqa: E402

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
# In a password deployment the roster shows a copy-able setup link per pending user.
SETUP_LINKS = {
    u["username"]: f"{BASE}/claim?token=eyJhbGciOi.SAMPLE-SETUP-TOKEN.xyz"
    for u in USERS
    if onboarding.is_pending(u)
}
write("05-admin-users.html",
      admin.users_tab_html(USERS, csrf="t0ken", ok="User added.", setup_links=SETUP_LINKS, **ADMIN))
write("06-admin-dashboard.html", admin.dashboard_tab_html(**CHROME))

# The activity views — rendered from the REAL builders + read helpers over a temp store (no drift).
import os  # noqa: E402
import tempfile  # noqa: E402

import model_store  # noqa: E402
from contracts import ToolCallRecord  # noqa: E402
from model_store import DbActivitySink  # noqa: E402
from store import Store  # noqa: E402

_fd, _db_path = tempfile.mkstemp(suffix=".db")  # atomic, not the race-prone mktemp
os.close(_fd)
_s = Store.connect("sqlite://" + _db_path)
_s.run_migrations()
_sink = DbActivitySink(_s)
_SAMPLE_CALLS = [
    dict(ts="2026-06-27T10:39:02Z", tool_name="execute_sql", source="mcp_server", actor="jordan@example.com",
         datasource="SALES_DATA", sql="SELECT id, customer_id, amount\nFROM orders\nORDER BY created_at DESC\nLIMIT 10",
         row_count=10, execution_ms=73, success=True, user_question="Show me the 10 most recent orders",
         agent_query="recent orders", thread_id="t1"),
    dict(ts="2026-06-27T10:40:55Z", tool_name="get_datasource_schema", source="mcp_server",
         actor="jordan@example.com", datasource="SALES_DATA", execution_ms=12, success=True),
    dict(ts="2026-06-27T10:42:17Z", tool_name="execute_sql", source="mcp_server", actor="jordan@example.com",
         datasource="SALES_DATA", sql="SELECT region, SUM(amount) AS revenue\nFROM orders\nGROUP BY region\nORDER BY revenue DESC",
         row_count=5, execution_ms=84, success=True, user_question="What's our revenue by region this quarter?",
         agent_query="revenue by region", thread_id="t1"),
    dict(ts="2026-06-27T10:41:50Z", tool_name="execute_sql", source="mcp_server", actor="sam@example.com",
         datasource="SALES_DATA", sql="SELECT * FROM ordrs", execution_ms=31, success=False,
         error_kind="syntax", thread_id="t2"),
    dict(ts="2026-06-27T10:40:03Z", tool_name="list_datasources", source="mcp_server",
         actor="jordan@example.com", execution_ms=3, success=True),
]
for _c in _SAMPLE_CALLS:
    _sink.record_tool_call(ToolCallRecord(**_c))
write("07-admin-sessions.html", admin.sessions_tab_html(model_store.list_sessions(_s), **CHROME))
write("15-tool-calls.html", admin.calls_tab_html(model_store.list_tool_calls(_s), **CHROME))
_s.close()
write("08-not-admin.html", admin.not_admin_body_html(BASE))
write("09-landing.html", admin.landing_body_html(BASE))
write("10-mcp-in-browser.html", admin.mcp_landing_body_html(BASE))
write("11-not-authorized.html", admin.not_authorized_body_html("morgan@example.com"))
write("12-setup-password.html", onboarding.setup_page_html("eyJhbGciOi.SAMPLE.xyz"))
write("13-setup-done.html", onboarding.setup_done_html(BASE))
write("14-setup-invalid.html", onboarding.setup_invalid_html())

print(f"Wrote {len(list(OUT.glob('*.html')))} previews to {OUT}/")
for p in sorted(OUT.glob("*.html")):
    print(f"  open {p}")
