"""Primary and foreign keys are visible to the read-only role a deploy is told to connect as.

Postgres shows a constraint in `information_schema.table_constraints` only to the table's owner or a
role holding a privilege other than SELECT. The recipe in `plugins/agami/shared/readonly-grants.md`
grants SELECT and nothing else, so a catalog read through those views returns no keys, every grain
is guessed and every join is inferred from column names, which can bind the wrong parent. The
PostgreSQL dialect reads `pg_constraint` instead; this file runs its key queries as that role against
a real server.

The fixture builds its own schemas as the owner rather than reusing the corpus tables: it needs a
composite key and a key into a second schema, and the corpus declares neither.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import itdeps  # noqa: E402

itdeps.importorfail("psycopg2")

import harness  # noqa: E402

if not harness.PG_ENABLED:
    itdeps.skip_or_fail(
        "set AGAMI_IT_PG_PASSWORD to read catalog keys as the read-only role against the compose fixture",
        module_level=True,
    )

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from semantic_model import dialects as D  # noqa: E402

SCHEMA, OTHER = "keys_main", "keys_ref"

DDL = f"""
CREATE SCHEMA {OTHER};
CREATE SCHEMA {SCHEMA};
CREATE TABLE {OTHER}.regions (id int PRIMARY KEY);
CREATE TABLE {SCHEMA}.customers (id int PRIMARY KEY, region_id int REFERENCES {OTHER}.regions (id));
CREATE TABLE {SCHEMA}.orders (id int PRIMARY KEY, customer_id int REFERENCES {SCHEMA}.customers (id));
CREATE TABLE {SCHEMA}.shipments (order_id int, line_no int, PRIMARY KEY (order_id, line_no));
CREATE TABLE {SCHEMA}.shipment_events (
    id int PRIMARY KEY, ship_line int, ship_order int,
    FOREIGN KEY (ship_order, ship_line) REFERENCES {SCHEMA}.shipments (order_id, line_no)
);
GRANT USAGE ON SCHEMA {SCHEMA}, {OTHER} TO {harness.PG_RO_USER};
GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA}, {OTHER} TO {harness.PG_RO_USER};
"""

EXPECTED_FKS = {
    ("customers", "region_id", SCHEMA, "regions", "id", OTHER),
    ("orders", "customer_id", SCHEMA, "customers", "id", SCHEMA),
    ("shipment_events", "ship_order", SCHEMA, "shipments", "order_id", SCHEMA),
    ("shipment_events", "ship_line", SCHEMA, "shipments", "line_no", SCHEMA),
}


def _drop(cur) -> None:
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; DROP SCHEMA IF EXISTS {OTHER} CASCADE")


@pytest.fixture(scope="module")
def readonly_cursor():
    assert harness.PG_HOST in harness.LOOPBACK_HOSTS, "this fixture creates and drops schemas; local only"
    owner = psycopg2.connect(harness.pg_dsn(harness.PG_USER, harness.PG_PASSWORD))
    owner.autocommit = True
    with owner.cursor() as cur:
        _drop(cur)
        cur.execute(DDL)
    reader = psycopg2.connect(harness.pg_readonly_dsn())
    reader.autocommit = True
    try:
        with reader.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT current_user AS u")
            assert cur.fetchone()["u"] == harness.PG_RO_USER, "connected as the wrong role"
            yield cur
    finally:
        reader.close()
        with owner.cursor() as cur:
            _drop(cur)
        owner.close()


def test_information_schema_hides_the_keys_from_the_read_only_role(readonly_cursor):
    # The negative control: without it the tests below would pass on a role that happened to see
    # everything, and prove nothing about the one the recipe creates.
    readonly_cursor.execute(
        "SELECT count(*) AS n FROM information_schema.table_constraints "
        f"WHERE table_schema IN ('{SCHEMA}', '{OTHER}') AND constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')"
    )
    assert readonly_cursor.fetchone()["n"] == 0


def test_primary_keys_are_read_in_key_order(readonly_cursor):
    dialect = D.get_dialect("postgres")
    readonly_cursor.execute(dialect.sql_primary_keys(SCHEMA, "shipments"))
    assert [r["column_name"] for r in readonly_cursor.fetchall()] == ["order_id", "line_no"]
    readonly_cursor.execute(dialect.sql_primary_keys(SCHEMA, "orders"))
    assert [r["column_name"] for r in readonly_cursor.fetchall()] == ["id"]


def test_foreign_keys_pair_composite_columns_and_name_the_referenced_schema(readonly_cursor):
    readonly_cursor.execute(D.get_dialect("postgres").sql_foreign_keys(SCHEMA))
    rows = {
        (r["from_table"], r["from_column"], r["from_schema"], r["to_table"], r["to_column"], r["to_schema"])
        for r in readonly_cursor.fetchall()
    }
    # ship_line is declared before ship_order in the table but after it in the key, so a pairing by
    # column position rather than key position would cross them.
    assert rows == EXPECTED_FKS
