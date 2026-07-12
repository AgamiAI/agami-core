"""Integration guard: run the REAL Postgres path (a psycopg2 server-side *named* cursor) against a
live Postgres and assert it returns the actual rows. This is the coverage the fake-cursor unit tests
can't give — a named cursor reports `description is None` until the first fetch, and only a real
driver reproduces that.

Opt-in: it **skips unless `AGAMI_IT_PG_PASSWORD` is set** (so normal CI, which has no Postgres, is
unaffected, and no test password lives in the source). To run it against the integration fixture:

    docker compose -f tests/integration/docker-compose.yml up -d postgres
    AGAMI_IT_PG_PASSWORD=<the fixture's POSTGRES_PASSWORD> \
        uv run pytest tests/test_postgres_named_cursor_integration.py

Host/port/user/db default to the fixture's values and can be overridden via the other AGAMI_IT_PG_*
vars. Regression target: before the fetch-first fix, every Postgres/Redshift query returned 0 rows.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402


def _pg_creds() -> dict[str, str]:
    # The password is env-only (no source-embedded test secret); host/port/user/db default to the
    # fixture's values. Override any of them via AGAMI_IT_PG_* to point at another Postgres.
    return {
        "type": "postgres",
        "host": os.environ.get("AGAMI_IT_PG_HOST", "127.0.0.1"),
        "port": os.environ.get("AGAMI_IT_PG_PORT", "55432"),
        "user": os.environ.get("AGAMI_IT_PG_USER", "agami_test"),
        "password": os.environ["AGAMI_IT_PG_PASSWORD"],
        "database": os.environ.get("AGAMI_IT_PG_DB", "shop"),
    }


@pytest.fixture
def pg_conn():
    psycopg2 = pytest.importorskip("psycopg2")
    if not os.environ.get("AGAMI_IT_PG_PASSWORD"):
        pytest.skip("set AGAMI_IT_PG_PASSWORD to run the live-Postgres integration test")
    creds = _pg_creds()
    try:
        conn = psycopg2.connect(
            host=creds["host"], port=int(creds["port"]), user=creds["user"],
            password=creds["password"], dbname=creds["database"], connect_timeout=3,
        )
    except Exception as exc:  # no DB in this environment → skip, don't fail
        pytest.skip(f"no reachable Postgres for the integration test ({exc})")
    try:
        yield conn, creds
    finally:
        conn.close()


def test_run_postgres_returns_real_rows_through_the_named_cursor(pg_conn, monkeypatch):
    conn, creds = pg_conn
    # Self-contained: create our own table so the test doesn't depend on any fixture schema.
    with conn.cursor() as c:
        c.execute("DROP TABLE IF EXISTS agami_it_named_cursor")
        c.execute("CREATE TABLE agami_it_named_cursor (id int, label text)")
        c.execute("INSERT INTO agami_it_named_cursor VALUES (1, 'a'), (2, 'b'), (3, 'c')")
        conn.commit()

    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "1000")
    try:
        # _run_postgres opens its own connection and uses cursor(name="agami_bounded") — the REAL
        # server-side named cursor whose description is None until the first fetch.
        result = execute_sql._run_postgres(
            creds, "SELECT id, label FROM agami_it_named_cursor ORDER BY id"
        )
        assert result.columns == ["id", "label"]
        assert result.rows == [(1, "a"), (2, "b"), (3, "c")]  # 0 rows before the fetch-first fix
        assert result.truncated is False
    finally:
        with conn.cursor() as c:
            c.execute("DROP TABLE IF EXISTS agami_it_named_cursor")
            conn.commit()
