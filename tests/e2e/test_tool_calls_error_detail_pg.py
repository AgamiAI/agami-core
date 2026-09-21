"""`tool_calls.error_detail` on a real Postgres (027, ACE-152).

The migration's DDL is portable by construction, and SQLite accepting it proves nothing about
Postgres: the two engines disagree on enough of `ALTER TABLE` that "it ran on SQLite" is not a claim
about production. So the column is added, written and read back here, through the same sink the
served transport writes with, in the directory the Postgres job runs.

It writes the application's own tables, so it works in a schema of its own that is dropped at the end,
for the reason `test_live_model_version_pg.py` gives.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import itdeps  # noqa: E402

itdeps.importorfail("psycopg2")

import harness  # noqa: E402

if not harness.PG_ENABLED:
    itdeps.skip_or_fail(
        "set AGAMI_IT_PG_PASSWORD to apply migration 027 on the compose fixture",
        module_level=True,
    )

import psycopg2  # noqa: E402
import tools  # noqa: E402
from store import Store  # noqa: E402


@pytest.fixture()
def store_url():
    """A migrated store in a throwaway schema, as the owner the fixture seeds with."""
    # The same guard `seed_postgres` applies: this creates and drops a schema, so only locally.
    assert harness.PG_HOST in harness.LOOPBACK_HOSTS
    schema = f"ed_{uuid.uuid4().hex[:12]}"
    owner = harness.pg_dsn(harness.PG_USER, harness.PG_PASSWORD)
    admin = psycopg2.connect(owner)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
        url = f"{owner}?options=-csearch_path%3D{schema}"
        s = Store.connect(url)
        try:
            s.run_migrations()
        finally:
            s.close()
        yield url
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


def test_027_applies_and_a_crash_is_recorded_on_postgres(store_url, monkeypatch):
    monkeypatch.setenv("AGAMI_DB_URL", store_url)

    tools.record_tool_call(
        name="probe",
        arguments={},
        result_text=None,
        execution_ms=1,
        actor="you@example.com",
        raised=True,
        error_detail="RuntimeError: marker-7f3a",
    )

    s = Store.connect(store_url)
    try:
        applied = [r["id"] for r in s.query("SELECT id FROM schema_migrations")]
        (row,) = s.query("SELECT success, error_kind, error_detail FROM tool_calls")
    finally:
        s.close()
    assert any(mid.endswith("027_tool_calls_error_detail.sql") for mid in applied)
    assert (row["success"], row["error_kind"], row["error_detail"]) == (
        0,
        "exception",
        "RuntimeError: marker-7f3a",
    )
