"""The admin web surface — the page builders (HTML + escaping) and the routes end to end.

Two layers, both proven here:
- **Page builders** (`ui` / `admin` return plain strings): structure + the security properties — every
  interpolated value is escaped, and a listing never carries the password hash.
- **Routes** (Starlette TestClient over an https base, so the Secure session cookie is accepted): the
  session-cookie gate, the admin-gate (a valid non-admin is refused), CSRF on mutations, the
  create/disable/enable loop, and the bearer-vs-session credential separation.

SQLite-backed (the portable backend the gate runs on). Flat access only — re-asserts no role column.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("jwt")
pytest.importorskip("argon2")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import admin  # noqa: E402
import mcp_http  # noqa: E402
import oauth_server  # noqa: E402
import ui  # noqa: E402
import user_store  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

BASE = "https://your-host.example.com"
SECRET = "x" * 40  # throwaway HS256 key (≥32 bytes); obviously not a real secret
ADMIN_USER = "admin@example.com"
ADMIN_PW = "admin-password-localtest"


# ---------------------------------------------------------------------------
# Page-builder tests (pure functions — no app, no DB)
# ---------------------------------------------------------------------------


def _roster():
    return [
        {"username": ADMIN_USER, "first_name": "Alex", "last_name": "Kim", "email": ADMIN_USER,
         "status": "active", "oidc_provider": None, "has_password": 1},
        {"username": "jordan@example.com", "first_name": "Jordan", "last_name": "Lee",
         "email": "jordan@example.com", "status": "active", "oidc_provider": "google", "has_password": 0},
        {"username": "morgan@example.com", "first_name": "Morgan", "last_name": "Diaz",
         "email": "morgan@example.com", "status": "disabled", "oidc_provider": None, "has_password": 0},
    ]


def test_initials_are_two_leading_letters():
    assert ui.initials("Jordan Lee") == "JL"
    assert ui.initials("madonna") == "M"
    assert ui.initials("") == "?"


def test_users_tab_renders_roster_with_signin_and_status():
    html = admin.users_tab_html(_roster(), csrf="tok", admin_username=ADMIN_USER, admin_label="Alex Kim")
    assert "Jordan Lee" in html and "jordan@example.com" in html
    assert ">google<" in html  # OIDC provider shown as the sign-in method
    assert "not set yet" in html  # a pending user (no password, no provider)
    assert "pill disabled" in html and "pill active" in html
    # The admin's own row offers no enable/disable action (can't lock themselves out).
    admin_row = next(r for r in html.split("<tr>") if ADMIN_USER in r and "<td" in r)
    assert "Disable" not in admin_row and "—" in admin_row
    # A non-admin row DOES carry an action.
    jordan_row = next(r for r in html.split("<tr>") if "jordan@example.com" in r and "<td" in r)
    assert "Disable" in jordan_row


def test_listing_never_emits_the_password_hash():
    # Even if a row dict carried a hash, the builder must not render it. (The store's list_users never
    # selects it, but the page is the last line of defense.)
    rows = _roster()
    rows[0]["password_hash"] = "argon2-secret-should-never-appear"
    html = admin.users_tab_html(rows, csrf="tok", admin_username=ADMIN_USER)
    assert "argon2-secret-should-never-appear" not in html
    assert "password_hash" not in html


def test_every_interpolated_value_is_escaped():
    # A name/email is attacker-influenceable (a teammate's own data) — it must never break out of HTML.
    rows = [{"username": 'x"><script>alert(1)</script>@e.com', "first_name": '<b>Eve</b>',
             "last_name": "", "email": '"><img src=x onerror=alert(1)>', "status": "active",
             "oidc_provider": None, "has_password": 0}]
    html = admin.users_tab_html(rows, csrf="t", admin_username=ADMIN_USER)
    assert "<script>" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;" in html


def test_admin_login_is_password_only_with_no_marketing_copy():
    html = admin.admin_login_body_html()
    assert 'name="password"' in html and 'name="username"' in html
    # Admin social login isn't built — no provider buttons (they'd point at an unrouted path).
    assert "Continue with" not in html
    # No leftover marketing copy (the deliberately-stripped "Admin sign in" / "Manage who…" headings).
    assert "Manage who" not in html


def test_oauth_login_renders_provider_buttons_with_icon_and_label():
    # The OAuth login is the wired social surface (buttons → the real /oauth/oidc/start route).
    html = oauth_server.login_body_html(
        {"redirect_uri": "https://claude.ai/cb"}, providers=("google", "microsoft"), wrap=True
    )
    assert "Continue with Google" in html and "Continue with Microsoft" in html
    assert "/static/google_logo.svg" in html and "/static/microsoft_logo.svg" in html


def test_not_admin_and_not_authorized_pages_are_branded():
    assert "/static/logo_h.svg" in admin.not_admin_body_html(BASE)
    assert "isn't authorized" in admin.not_authorized_body_html("nope@example.com")


# ---------------------------------------------------------------------------
# Route tests (TestClient over an https base)
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_url = "sqlite://" + str(tmp_path / "admin.db")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", ADMIN_PW)
    s = Store.connect(db_url)
    s.run_migrations()
    user_store.seed_admin_from_env(s)
    # A valid, active, non-admin password user (to prove the admin-gate refuses non-admins).
    user_store.create_user(s, "bob@example.com", password="bob-password-localtest", email="bob@example.com")
    s.close()
    return db_url


@pytest.fixture
def client(env):
    return TestClient(mcp_http.build_app(), base_url=BASE)


def _login(client) -> None:
    r = client.post(
        "/admin/login", data={"username": ADMIN_USER, "password": ADMIN_PW}, follow_redirects=False
    )
    assert r.status_code == 302 and r.headers["location"] == "/admin"


def _csrf(client) -> str:
    return re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/admin").text).group(1)


def test_static_logo_and_root_landing_are_public(client):
    assert client.get("/static/logo_h.svg").status_code == 200
    root = client.get("/")
    assert root.status_code == 200 and f"{BASE}/mcp" in root.text


def test_admin_requires_a_session(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/admin/login"


def test_admin_login_page_renders_without_a_bearer(client):
    assert client.get("/admin/login").status_code == 200


def test_wrong_password_is_a_generic_401(client):
    r = client.post(
        "/admin/login", data={"username": ADMIN_USER, "password": "nope"}, follow_redirects=False
    )
    assert r.status_code == 401
    assert "Invalid email or password" in r.text


def test_valid_non_admin_is_refused_with_no_session(client):
    r = client.post(
        "/admin/login",
        data={"username": "bob@example.com", "password": "bob-password-localtest"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "set-cookie" not in {k.lower() for k in r.headers}  # no session minted
    # And they still can't reach the console.
    assert client.get("/admin", follow_redirects=False).status_code == 302


def test_admin_login_is_case_insensitive_on_the_email(client):
    # Email is the identity — a differently-cased address still authenticates as the admin.
    r = client.post(
        "/admin/login",
        data={"username": "ADMIN@Example.COM", "password": ADMIN_PW},
        follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["location"] == "/admin"


def test_admin_login_sets_a_hardened_session_cookie(client):
    r = client.post(
        "/admin/login", data={"username": ADMIN_USER, "password": ADMIN_PW}, follow_redirects=False
    )
    sc = r.headers["set-cookie"]
    assert "agami_admin_session=" in sc
    assert "HttpOnly" in sc and "Secure" in sc and "SameSite=lax" in sc and "Path=/admin" in sc


def test_create_user_appears_pending_and_cannot_yet_authenticate(client, env):
    _login(client)
    csrf = _csrf(client)
    r = client.post(
        "/admin/users",
        data={"csrf": csrf, "email": "Jordan@Example.com", "first_name": "Jordan", "last_name": "Lee"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/admin?ok=added"
    page = client.get("/admin").text
    assert "jordan@example.com" in page and "Jordan Lee" in page and "not set yet" in page
    # Pending = no password set → can't password-login yet (the later self-onboarding claim flow).
    s = Store.connect(env)
    assert user_store.authenticate(s, "jordan@example.com", "anything") is None
    s.close()


def test_duplicate_and_bad_email_flash_not_500(client):
    _login(client)
    csrf = _csrf(client)
    client.post("/admin/users", data={"csrf": csrf, "email": "dupe@example.com"}, follow_redirects=False)
    dup = client.post(
        "/admin/users", data={"csrf": csrf, "email": "dupe@example.com"}, follow_redirects=False
    )
    assert dup.headers["location"] == "/admin?err=dup"
    bad = client.post("/admin/users", data={"csrf": csrf, "email": "nope"}, follow_redirects=False)
    assert bad.headers["location"] == "/admin?err=bad_email"


def test_disable_then_enable_round_trip(client, env):
    _login(client)
    csrf = _csrf(client)
    client.post("/admin/users", data={"csrf": csrf, "email": "kim@example.com"}, follow_redirects=False)
    s = Store.connect(env)
    # Give them a password directly so we can prove disable actually blocks login.
    user_store.create_user(s, "pat@example.com", password="pat-password-localtest", email="pat@example.com")
    s.close()
    client.post(
        "/admin/users/status",
        data={"csrf": csrf, "username": "pat@example.com", "status": "disabled"},
        follow_redirects=False,
    )
    s = Store.connect(env)
    assert user_store.authenticate(s, "pat@example.com", "pat-password-localtest") is None
    s.close()
    client.post(
        "/admin/users/status",
        data={"csrf": csrf, "username": "pat@example.com", "status": "active"},
        follow_redirects=False,
    )
    s = Store.connect(env)
    assert user_store.authenticate(s, "pat@example.com", "pat-password-localtest") is not None
    s.close()


def test_status_change_for_an_unknown_user_is_not_a_false_success(client):
    _login(client)
    csrf = _csrf(client)
    r = client.post(
        "/admin/users/status",
        data={"csrf": csrf, "username": "ghost@example.com", "status": "disabled"},
        follow_redirects=False,
    )
    # A username that matches no row must not flash "User disabled." (no-op ≠ success).
    assert r.headers["location"] == "/admin?err=notfound"


def test_cannot_disable_self(client):
    _login(client)
    csrf = _csrf(client)
    r = client.post(
        "/admin/users/status",
        data={"csrf": csrf, "username": ADMIN_USER, "status": "disabled"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/admin?err=self"


def test_mutations_require_a_valid_csrf_token(client, env):
    _login(client)
    # No CSRF token → rejected, and nothing is created.
    r = client.post(
        "/admin/users", data={"email": "sneak@example.com"}, follow_redirects=False
    )
    assert r.headers["location"] == "/admin?err=csrf"
    # A wrong token is rejected too.
    r = client.post(
        "/admin/users", data={"csrf": "deadbeef", "email": "sneak@example.com"}, follow_redirects=False
    )
    assert r.headers["location"] == "/admin?err=csrf"
    s = Store.connect(env)
    assert user_store.get_user(s, "sneak@example.com") is None
    s.close()


def test_status_mutation_without_session_redirects_and_does_not_mutate(client, env):
    # No login → the handler bounces to login before touching the store.
    r = client.post(
        "/admin/users/status",
        data={"csrf": "x", "username": "bob@example.com", "status": "disabled"},
        follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["location"] == "/admin/login"
    s = Store.connect(env)
    assert user_store.get_user(s, "bob@example.com")["status"] == "active"
    s.close()


def test_logout_clears_the_session(client):
    _login(client)
    assert client.get("/admin", follow_redirects=False).status_code == 200
    client.get("/admin/logout", follow_redirects=False)
    assert client.get("/admin", follow_redirects=False).status_code == 302


def test_an_mcp_bearer_jwt_is_not_an_admin_session(client):
    # A token minted for the query surface (no `purpose` claim) must not satisfy the admin gate —
    # even though its subject equals the admin's username and it's signed with the same secret.
    token = oauth_server.issue_jwt(ADMIN_USER)
    r = client.get("/admin", cookies={"agami_admin_session": token}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/admin/login"


def test_an_admin_session_is_not_an_mcp_bearer(client):
    # The reverse direction: a session cookie value used as a Bearer token is rejected at /mcp (it
    # carries no issuer, which the JWT validator requires).
    session = admin.issue_session(ADMIN_USER)
    r = client.post("/mcp", headers={"Authorization": f"Bearer {session}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_admin_ui_is_disabled_when_no_admin_configured(env, monkeypatch):
    # Unset the admin-gate: even valid credentials can't mint a session, and /admin redirects.
    monkeypatch.delenv("AGAMI_ADMIN_USERNAME", raising=False)
    c = TestClient(mcp_http.build_app(), base_url=BASE)
    r = c.post("/admin/login", data={"username": ADMIN_USER, "password": ADMIN_PW}, follow_redirects=False)
    assert r.status_code == 403  # no configured admin ⇒ nobody is the admin
    assert c.get("/admin", follow_redirects=False).headers["location"] == "/admin/login"


def test_dashboard_and_sessions_tabs_are_placeholders(client):
    _login(client)
    assert "Coming soon" in client.get("/admin?tab=dashboard").text
    assert "Coming soon" in client.get("/admin?tab=sessions").text


def test_login_page_redirects_when_already_signed_in(client):
    _login(client)
    r = client.get("/admin/login", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/admin"


def test_create_without_session_redirects_and_does_not_mutate(client, env):
    r = client.post(
        "/admin/users", data={"csrf": "x", "email": "ghost@example.com"}, follow_redirects=False
    )
    assert r.status_code == 302 and r.headers["location"] == "/admin/login"
    s = Store.connect(env)
    assert user_store.get_user(s, "ghost@example.com") is None
    s.close()


def test_status_mutation_needs_csrf_and_a_known_status(client, env):
    _login(client)
    csrf = _csrf(client)
    # No CSRF token → rejected.
    assert (
        client.post(
            "/admin/users/status",
            data={"username": "bob@example.com", "status": "disabled"},
            follow_redirects=False,
        ).headers["location"]
        == "/admin?err=csrf"
    )
    # An unknown status value is refused (only active/disabled are allowed).
    assert (
        client.post(
            "/admin/users/status",
            data={"csrf": csrf, "username": "bob@example.com", "status": "superuser"},
            follow_redirects=False,
        ).headers["location"]
        == "/admin?err=bad"
    )
    s = Store.connect(env)
    assert user_store.get_user(s, "bob@example.com")["status"] == "active"
    s.close()


def test_a_garbage_session_cookie_is_rejected(client):
    r = client.get("/admin", cookies={"agami_admin_session": "not-a-jwt"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/admin/login"


def test_a_foreign_origin_is_rejected_on_mutations(client, env):
    # A cross-site POST carries the attacker's Origin; the second CSRF gate rejects it even though it
    # could never have a valid token anyway (defense in depth).
    _login(client)
    csrf = _csrf(client)
    r = client.post(
        "/admin/users",
        data={"csrf": csrf, "email": "evil@example.com"},
        headers={"origin": "https://evil.example.com"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/admin?err=csrf"
    s = Store.connect(env)
    assert user_store.get_user(s, "evil@example.com") is None
    s.close()


def test_a_well_formed_session_for_a_non_admin_subject_is_rejected(client):
    # A correctly-signed, purpose-marked session — but for a subject that isn't the configured admin
    # — must not pass the gate (the sub == admin check is the last line).
    forged = admin.issue_session("bob@example.com")
    r = client.get("/admin", cookies={"agami_admin_session": forged}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/admin/login"


def test_no_role_column_was_introduced(env):
    # Flat access is a hard invariant — the admin surface must not have smuggled in a role/permission.
    s = Store.connect(env)
    cols = {row["name"] for row in s.query("PRAGMA table_info(users)")}
    s.close()
    assert "role" not in cols and "permission" not in cols
    assert {"first_name", "last_name", "username", "email", "status"} <= cols
