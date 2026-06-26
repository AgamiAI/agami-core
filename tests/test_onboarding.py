"""Teammate self-onboarding via the admin setup link (the password-deployment path).

Proves the setup token is unforgeable + single-use, the /claim flow sets a password for a pending user
only, and the admin roster surfaces a copy-able setup link for pending users — but only when the
deployment has no OIDC configured. SQLite-backed; https base so the Secure admin cookie round-trips.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("jwt")
pytest.importorskip("argon2")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import jwt  # noqa: E402
import mcp_http  # noqa: E402
import onboarding  # noqa: E402
import user_store  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

BASE = "https://your-host.example.com"
SECRET = "x" * 40
ADMIN_USER = "admin@example.com"
ADMIN_PW = "admin-password-localtest"
PENDING = "newbie@example.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_url = "sqlite://" + str(tmp_path / "onboard.db")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", ADMIN_PW)
    # A password deployment — no OIDC configured.
    for var in ("AGAMI_OIDC_GOOGLE_CLIENT_ID", "AGAMI_OIDC_GOOGLE_CLIENT_SECRET",
                "AGAMI_OIDC_MICROSOFT_CLIENT_ID", "AGAMI_OIDC_MICROSOFT_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    s = Store.connect(db_url)
    s.run_migrations()
    user_store.seed_admin_from_env(s)
    user_store.create_user(s, username=PENDING, email=PENDING, password=None)  # pending teammate
    s.close()
    return db_url


@pytest.fixture
def client(env):
    return TestClient(mcp_http.build_app(), base_url=BASE)


# --- the token in isolation --------------------------------------------------


def test_setup_token_round_trips(env):
    token = onboarding.mint_setup_token(PENDING)
    assert onboarding.verify_setup_token(token) == PENDING


def test_setup_token_rejects_forged_expired_and_wrong_purpose(env):
    assert onboarding.verify_setup_token("not-a-jwt") is None
    # signed with a different key → bad signature
    forged = jwt.encode({"sub": PENDING, "purpose": "setup", "exp": 9_999_999_999}, "y" * 40, algorithm="HS256")
    assert onboarding.verify_setup_token(forged) is None
    # expired
    expired = jwt.encode(
        {"sub": PENDING, "purpose": "setup", "exp": int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp())},
        SECRET, algorithm="HS256",
    )
    assert onboarding.verify_setup_token(expired) is None
    # right key, wrong purpose (e.g. a bearer/admin-session token replayed as a setup link)
    wrong = jwt.encode({"sub": PENDING, "purpose": "admin_session", "exp": 9_999_999_999}, SECRET, algorithm="HS256")
    assert onboarding.verify_setup_token(wrong) is None


# --- the /claim flow ---------------------------------------------------------


def test_claim_sets_a_password_for_a_pending_user(client, env):
    token = onboarding.mint_setup_token(PENDING)
    assert "Set up your account" in client.get("/claim", params={"token": token}).text
    r = client.post("/claim", data={"token": token, "password": "teammate-pw-123"})
    assert r.status_code == 200 and f"{BASE}/mcp" in r.text
    s = Store.connect(env)
    assert user_store.authenticate(s, PENDING, "teammate-pw-123") is not None
    s.close()


def test_claim_link_is_single_use(client, env):
    token = onboarding.mint_setup_token(PENDING)
    client.post("/claim", data={"token": token, "password": "teammate-pw-123"})
    # replay: the user is no longer pending → generic invalid page, password unchanged
    r = client.post("/claim", data={"token": token, "password": "attacker-pw-999"})
    assert r.status_code == 400 and "isn't valid" in r.text
    s = Store.connect(env)
    assert user_store.authenticate(s, PENDING, "teammate-pw-123") is not None  # original still works
    assert user_store.authenticate(s, PENDING, "attacker-pw-999") is None
    s.close()


def test_claim_rejects_a_bad_token(client):
    assert client.get("/claim", params={"token": "nope"}, follow_redirects=False).status_code == 400
    assert client.post("/claim", data={"token": "nope", "password": "whatever-123"}).status_code == 400


def test_claim_rejects_a_short_password(client, env):
    token = onboarding.mint_setup_token(PENDING)
    r = client.post("/claim", data={"token": token, "password": "short"})
    assert r.status_code == 400 and "at least" in r.text
    s = Store.connect(env)
    assert onboarding.is_pending(user_store.get_user(s, PENDING))  # still pending — nothing set
    s.close()


def test_claim_for_an_already_password_user_is_refused(client, env):
    # The admin (a password user) isn't pending; a token for them can't overwrite their password.
    token = onboarding.mint_setup_token(ADMIN_USER)
    assert client.get("/claim", params={"token": token}).status_code == 400


# --- the admin roster setup link ---------------------------------------------


def _login(client):
    client.post("/admin/login", data={"username": ADMIN_USER, "password": ADMIN_PW})


def test_admin_roster_shows_a_setup_link_for_pending_users(client):
    _login(client)
    html = client.get("/admin").text
    assert "Setup link" in html
    m = re.search(r'/claim\?token=([\w.\-]+)', html)
    assert m and onboarding.verify_setup_token(m.group(1)) == PENDING
    # the admin's own (password) row offers no setup link
    admin_row = next(r for r in html.split("<tr>") if ADMIN_USER in r and "<td" in r)
    assert "Setup link" not in admin_row


def test_admin_roster_hides_setup_links_in_an_oidc_deployment(client, monkeypatch):
    # Configure an OIDC provider → it's no longer a password deployment → no setup links.
    monkeypatch.setenv("AGAMI_OIDC_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("AGAMI_OIDC_GOOGLE_CLIENT_SECRET", "secret")
    _login(client)
    assert "Setup link" not in client.get("/admin").text
