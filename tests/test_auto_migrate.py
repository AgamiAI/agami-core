"""ACE-019 — the server applies pending migrations on first DB open (idempotent, locked, fail-closed).

Covers the read helper (`Store.run_migrations`: the Postgres advisory-lock bracketing, idempotency, and
fail-closed-on-error) and the startup wiring (`mcp_http` lifespan applies them before serving + propagates).
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
from store import Store  # noqa: E402

BASE = "https://your-host.example.com"
SECRET = "x" * 40
ADMIN_USER = "admin@example.com"
ADMIN_PW = "admin-password-localtest"


def _write_migration(d: Path, name: str, sql: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(sql)


# --- run_migrations: idempotency + fail-closed (real SQLite) ------------------


def test_run_migrations_applies_then_is_idempotent(tmp_path):
    mig = tmp_path / "m"
    _write_migration(mig, "001_demo.sql", "CREATE TABLE demo_x (a INTEGER);")
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    first = s.run_migrations(mig)
    second = s.run_migrations(mig)  # nothing pending now
    rows = s.query("SELECT id FROM schema_migrations")
    s.close()
    assert first == ["001_demo.sql"]
    assert second == []  # idempotent — a re-run applies nothing
    assert [r["id"] for r in rows] == ["001_demo.sql"]


def test_failing_migration_raises_and_is_not_recorded(tmp_path):
    mig = tmp_path / "m"
    _write_migration(mig, "001_bad.sql", "CREATE TABLE oops (")  # invalid SQL
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    with pytest.raises(Exception):  # noqa: B017 — backend raises its own error type; we only need "raised"
        s.run_migrations(mig)
    applied = {r["id"] for r in s.query("SELECT id FROM schema_migrations")}
    s.close()
    assert "001_bad.sql" not in applied  # fail-closed: a failed migration is never recorded


# --- the advisory lock (dialect-branch, no real Postgres needed) -------------


class _Recorder:
    """A minimal stand-in for Store that records the SQL `run_migrations` issues, so we can assert the
    advisory-lock bracketing per dialect without a live Postgres."""

    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.sql: list[str] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.sql.append(sql)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        self.sql.append(sql)
        return []  # nothing applied yet

    def commit(self) -> None:
        pass

    def _run_script(self, sql: str) -> None:
        pass


def test_postgres_brackets_the_apply_with_a_session_advisory_lock(tmp_path):
    mig = tmp_path / "m"
    _write_migration(mig, "001_demo.sql", "CREATE TABLE demo_x (a INTEGER);")
    rec = _Recorder("postgres")
    Store.run_migrations(rec, mig)  # call the unbound method with our recorder as self
    joined = " | ".join(rec.sql)
    assert "pg_advisory_lock" in joined and "pg_advisory_unlock" in joined
    # the lock is taken BEFORE the work and released AFTER it
    assert rec.sql[0].startswith("SELECT pg_advisory_lock")
    assert rec.sql[-1].startswith("SELECT pg_advisory_unlock")
    assert "pg_advisory_unlock" not in " | ".join(rec.sql[:-1])  # unlock only at the very end


def test_sqlite_takes_no_advisory_lock(tmp_path):
    mig = tmp_path / "m"
    _write_migration(mig, "001_demo.sql", "CREATE TABLE demo_x (a INTEGER);")
    rec = _Recorder("sqlite")
    Store.run_migrations(rec, mig)
    assert not any("pg_advisory" in s for s in rec.sql)  # single-writer — no lock


# --- the startup wiring (lifespan applies on open, fail-closed) --------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = "sqlite://" + str(tmp_path / "startup.db")  # FRESH, deliberately NOT pre-migrated
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", ADMIN_PW)
    for v in ("AGAMI_OIDC_GOOGLE_CLIENT_ID", "AGAMI_OIDC_GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)
    return url


def test_startup_applies_pending_migrations(env):
    # Entering the TestClient runs the lifespan = the server's startup. The DB starts EMPTY; after startup
    # the real migrations must be applied (the footgun fix — no manual migrate step).
    with TestClient(mcp_http.build_app()):
        pass
    s = Store.connect(env)
    applied = s.query("SELECT id FROM schema_migrations")
    has_tool_calls = s.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_calls'"
    )
    s.close()
    assert applied, "startup applied no migrations"  # at least one ran
    assert has_tool_calls, "a migrated table is missing after startup"


def test_startup_is_fail_closed_on_migration_error(env, monkeypatch):
    # A failing migration must abort startup, not be swallowed — the lifespan propagates the error.
    def _boom(self, migrations_dir=None):
        raise RuntimeError("migration blew up")

    monkeypatch.setattr(Store, "run_migrations", _boom)
    with pytest.raises(RuntimeError, match="migration blew up"):
        with TestClient(mcp_http.build_app()):
            pass
