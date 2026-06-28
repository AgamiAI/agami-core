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
    import sqlite3

    mig = tmp_path / "m"
    _write_migration(mig, "001_bad.sql", "CREATE TABLE oops (")  # invalid SQL
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    with pytest.raises(sqlite3.OperationalError):  # the raise comes from APPLYING the bad migration
        s.run_migrations(mig)
    applied = {r["id"] for r in s.query("SELECT id FROM schema_migrations")}
    leaked = s.query("SELECT name FROM sqlite_master WHERE type='table' AND name='oops'")
    s.close()
    assert "001_bad.sql" not in applied  # fail-closed: a failed migration is never recorded
    assert not leaked  # and no partial DDL leaked from the half-run script


# --- the advisory lock (dialect-branch, no real Postgres needed) -------------


class _Conn:
    """Stub psycopg2 connection — records whether the failed-migration rollback ran."""

    def __init__(self) -> None:
        self.rolled_back = False

    def rollback(self) -> None:
        self.rolled_back = True


class _Recorder:
    """A minimal stand-in for Store that records the SQL `run_migrations` issues, so we can assert the
    advisory-lock bracketing per dialect (and the error-path cleanup) without a live Postgres."""

    def __init__(self, dialect: str, *, fail_on_script: bool = False) -> None:
        self.dialect = dialect
        self.sql: list[str] = []
        self.conn = _Conn()
        self._fail = fail_on_script

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.sql.append(sql)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        self.sql.append(sql)
        return []  # nothing applied yet

    def commit(self) -> None:
        pass

    def _run_script(self, sql: str) -> None:
        if self._fail:
            raise RuntimeError("bad migration")


def test_postgres_brackets_the_apply_with_a_session_advisory_lock(tmp_path):
    mig = tmp_path / "m"
    _write_migration(mig, "001_demo.sql", "CREATE TABLE demo_x (a INTEGER);")
    rec = _Recorder("postgres")
    Store.run_migrations(rec, mig)  # call the unbound method with our recorder as self
    # SESSION lock (survives the per-migration commits), not the xact variant — taken first, freed last.
    assert rec.sql[0] == "SELECT pg_advisory_lock(?)"
    assert "pg_advisory_xact_lock" not in " | ".join(rec.sql)
    assert rec.sql[-1] == "SELECT pg_advisory_unlock(?)"
    assert "pg_advisory_unlock" not in " | ".join(rec.sql[:-1])  # unlock only at the very end
    assert not rec.conn.rolled_back  # success path doesn't roll back (only the error path does)


def test_sqlite_takes_no_advisory_lock(tmp_path):
    mig = tmp_path / "m"
    _write_migration(mig, "001_demo.sql", "CREATE TABLE demo_x (a INTEGER);")
    rec = _Recorder("sqlite")
    Store.run_migrations(rec, mig)
    assert not any("pg_advisory" in s for s in rec.sql)  # single-writer — no lock
    assert not rec.conn.rolled_back  # sqlite skips the whole locked-cleanup block


def test_postgres_error_path_rolls_back_then_unlocks_without_masking(tmp_path):
    # A failing migration must propagate the REAL error (not a masking "transaction aborted"), roll the
    # aborted txn back, and still release the lock — see the run_migrations finally.
    mig = tmp_path / "m"
    _write_migration(mig, "001_demo.sql", "CREATE TABLE demo_x (a INTEGER);")
    rec = _Recorder("postgres", fail_on_script=True)
    with pytest.raises(RuntimeError, match="bad migration"):  # original error, not masked
        Store.run_migrations(rec, mig)
    assert rec.conn.rolled_back  # cleared the aborted state before unlocking
    assert rec.sql[-1] == "SELECT pg_advisory_unlock(?)"  # lock still released on the error path


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
