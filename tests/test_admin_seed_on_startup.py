"""ACE-009 — the server seeds the configured admin on startup, so a fresh deploy can sign in.

Nothing else creates the configured admin in a deployment (the local `seed.py` is test-only), so the
`mcp_http` lifespan seeds it from `AGAMI_ADMIN_*` right after migrating — create-if-absent + idempotent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("argon2")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import mcp_http  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

BASE = "https://your-host.example.com"
SECRET = "x" * 40
ADMIN_USER = "admin@example.com"
ADMIN_PW = "admin-password-localtest"


@pytest.fixture
def env(tmp_path, monkeypatch):
    # A FRESH DB, deliberately not seeded by anything other than startup.
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_DB_URL", "sqlite://" + str(tmp_path / "seed.db"))
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", ADMIN_PW)
    for v in ("AGAMI_OIDC_GOOGLE_CLIENT_ID", "AGAMI_OIDC_GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)


def test_startup_seeds_the_configured_admin(env):
    # Entering the TestClient runs the lifespan (startup) — which must seed the admin so the login works.
    with TestClient(mcp_http.build_app()) as c:
        r = c.post(
            "/admin/login",
            data={"username": ADMIN_USER, "password": ADMIN_PW},
            follow_redirects=False,
        )
    assert r.status_code == 302  # the seeded admin can sign in (302 = success)


def test_startup_admin_seed_is_idempotent(env):
    # Two boots against the same DB must not duplicate or break the admin (redeploy is safe).
    with TestClient(mcp_http.build_app()):
        pass
    with TestClient(mcp_http.build_app()) as c:
        r = c.post(
            "/admin/login",
            data={"username": ADMIN_USER, "password": ADMIN_PW},
            follow_redirects=False,
        )
    assert r.status_code == 302  # still exactly one working admin after a redeploy


def test_startup_survives_a_seed_race(env, monkeypatch):
    # If the admin seed raises (e.g. a concurrent boot won the INSERT — a UNIQUE violation), startup must
    # NOT abort: the admin is seeded either way. Best-effort, unlike migrations.
    import user_store

    def _boom(_store):
        raise RuntimeError("UNIQUE constraint failed: users.username")

    monkeypatch.setattr(user_store, "seed_admin_from_env", _boom)
    with TestClient(mcp_http.build_app()):  # entering runs the lifespan — must not raise
        pass


def test_startup_without_admin_env_seeds_nothing(env, monkeypatch):
    # No configured admin → startup still succeeds (no crash); there is just no admin to sign in as.
    monkeypatch.delenv("AGAMI_ADMIN_USERNAME", raising=False)
    with TestClient(mcp_http.build_app()) as c:
        r = c.post(
            "/admin/login",
            data={"username": ADMIN_USER, "password": ADMIN_PW},
            follow_redirects=False,
        )
    assert r.status_code != 302  # nothing seeded → login does not succeed
