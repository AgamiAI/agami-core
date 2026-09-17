"""The live model version, read from a real Postgres (#364).

The bug this pins only exists on Postgres. `ORDER BY created_at DESC` puts NULLs FIRST there and last
on SQLite, so a store holding one undated row from before #364 reported that row as the live version
in production while every SQLite test passed. A test of the fix has to run where the bug did, which is
why this lives in the directory the Postgres job runs.

It writes the application's own tables, so it works in a schema of its own that is dropped at the end.
The corpus database's `public` schema is what the read-only role is granted, and the app tables do not
belong beside the warehouse the role-floor test reasons about.
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
        "set AGAMI_IT_PG_PASSWORD to read the live model version from the compose fixture",
        module_level=True,
    )

import model_store  # noqa: E402
import psycopg2  # noqa: E402
from store import Store  # noqa: E402

UNDATED_OLD = "ffff00000000"
DATED_LIVE = "1111aaaabbbb"


@pytest.fixture()
def store():
    """A migrated store in a throwaway schema, as the owner the fixture seeds with."""
    # The same guard `seed_postgres` applies: this creates and drops a schema, so only locally.
    assert harness.PG_HOST in harness.LOOPBACK_HOSTS
    schema = f"mv_{uuid.uuid4().hex[:12]}"
    owner = harness.pg_dsn(harness.PG_USER, harness.PG_PASSWORD)
    admin = psycopg2.connect(owner)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
        s = Store.connect(f"{owner}?options=-csearch_path%3D{schema}")
        try:
            s.run_migrations()
            yield s
        finally:
            s.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


def _insert(store: Store, version: str, created_at: str | None) -> None:
    store.execute(
        "INSERT INTO model_version (org_id, datasource, version, created_at) VALUES (?, ?, ?, ?)",
        ("local", "main", version, created_at),
    )
    store.commit()


def test_an_undated_row_does_not_outrank_a_dated_one_on_postgres(store):
    _insert(store, UNDATED_OLD, None)
    _insert(store, DATED_LIVE, "2026-09-16T00:00:00.000000+00:00")
    # The control: the ordering the store used before #364 does pick the undated row on this
    # engine. Without it, a fixture that sorted NULLs last would let this test pass on nothing.
    before = store.query(
        "SELECT version FROM model_version WHERE org_id = ? AND datasource = ? "
        "ORDER BY created_at DESC, version DESC",
        ("local", "main"),
    )
    assert before[0]["version"] == UNDATED_OLD
    assert model_store.newest_model_version(store, "main") == DATED_LIVE


def test_writing_a_version_leaves_one_dated_row_on_postgres(store):
    _insert(store, UNDATED_OLD, None)
    model_store.write_model_version(store, "main", DATED_LIVE)
    rows = store.query("SELECT version, created_at FROM model_version WHERE datasource = 'main'")
    assert [r["version"] for r in rows] == [DATED_LIVE]
    assert rows[0]["created_at"]
