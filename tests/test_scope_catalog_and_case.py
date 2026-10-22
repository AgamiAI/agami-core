"""The scope gates compare a table reference the way its engine resolves it (#337).

Two comparisons were looser than the engines they guard. A catalog (database / project) part was
ignored, so `other_db.sales.orders` matched the declared `sales.orders` while the engine read a
different table. And a quoted part was folded like an unquoted one, so on Postgres `"Orders"` matched
the declared `orders`, which is another relation there.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
import tools  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402


def _org(engine: str, name: str = "orders", schema: str = "sales",
         catalog: str | None = None) -> m.Datasource:
    table = m.Table(name=name, schema=schema, catalog=catalog, storage_connection="c",
                    grain=["id"], description="orders",
                    columns=[m.Column(name="id", type="integer")])
    return m.Datasource(
        datasource="acme",
        storage_connections=[m.StorageConnection(name="c", storage_type=engine)],
        subject_areas=[m.SubjectArea(name="sales", tables_defined=[table])],
    )


def _table_scope(sql: str, org: m.Datasource):
    """The verdict, asserted identical with and without a shared guard context."""
    alone = rt.check_table_scope(sql, org)
    assert rt.check_table_scope(sql, org, ctx=rt.build_guard_context(sql, org)) == alone
    return alone


# ---------------------------------------------------------------- name case


@pytest.mark.parametrize("engine,declared,sql", [
    ("PostgreSQL", "orders", 'SELECT id FROM sales."Orders"'),
    ("PostgreSQL", "orders", 'SELECT id FROM "Sales".orders'),
    ("Snowflake", "ORDERS", 'SELECT id FROM sales."orders"'),
    ("Oracle", "ORDERS", 'SELECT id FROM sales."Orders"'),
])
def test_a_quoted_name_in_another_case_is_another_table(engine, declared, sql):
    res = _table_scope(sql, _org(engine, declared))
    assert res is not None and res.rule == guardrail.RULE_TABLE_SCOPE


@pytest.mark.parametrize("engine,declared,sql", [
    ("PostgreSQL", "orders", 'SELECT id FROM sales."orders"'),
    ("PostgreSQL", "orders", "SELECT id FROM SALES.ORDERS"),
    ("PostgreSQL", "Orders", 'SELECT id FROM sales."Orders"'),
    # A single-case name may be the model's habit, not the warehouse's: its fold counts too.
    ("PostgreSQL", "ORDERS", 'SELECT id FROM sales."orders"'),
    ("Snowflake", "ORDERS", 'SELECT id FROM sales."ORDERS"'),
    ("Snowflake", "orders", 'SELECT id FROM sales."ORDERS"'),
    ("Snowflake", "ORDERS", "SELECT id FROM sales.orders"),
    # These engines match a quoted name without regard to case, so folding it is their rule.
    ("Redshift", "orders", 'SELECT id FROM sales."Orders"'),
    ("DuckDB", "orders", 'SELECT id FROM sales."Orders"'),
    ("SQLServer", "orders", "SELECT id FROM sales.[Orders]"),
    # Unquoted still matches folded, by decision rather than oversight: a hand-written model's case
    # is not reliably the warehouse's, and a statement the engine cannot resolve fails there.
    ("PostgreSQL", "Orders", "SELECT id FROM sales.orders"),
])
def test_the_engines_own_spelling_passes(engine, declared, sql):
    assert _table_scope(sql, _org(engine, declared)) is None


@pytest.mark.parametrize("sql,refused", [
    ("SELECT id FROM sales.Orders", True),
    ("SELECT id FROM sales.`Orders`", True),
    ("SELECT id FROM SALES.orders", True),
    ("SELECT id FROM sales.orders", False),
    ("SELECT id FROM sales.`orders`", False),
])
def test_mysql_compares_table_and_database_names_exactly_quoted_or_not(sql, refused):
    """On Linux MySQL keeps the case a table was created with: `sales.Orders` is another table."""
    res = _table_scope(sql, _org("MySQL"))
    assert (res is not None) is refused


def test_column_scope_binds_through_the_same_resolver():
    """A reference the table gate refuses never vouches for a column."""
    org = _org("PostgreSQL")
    sql = 'SELECT o.secret FROM sales."Orders" o'
    assert rt.check_column_scope(sql, org) is None
    assert rt.check_column_scope("SELECT o.secret FROM sales.orders o", org) is not None


# ---------------------------------------------------------------- catalog in the model


def test_a_catalog_the_model_records_must_match():
    org = _org("Snowflake", "ORDERS", "SALES", catalog="ANALYTICS")
    assert _table_scope("SELECT id FROM analytics.sales.orders", org) is None
    res = _table_scope("SELECT id FROM other_db.sales.orders", org)
    assert res is not None and "other_db.sales.orders" in res.detail


@pytest.mark.parametrize("sql", [
    "SELECT id FROM srv.analytics.sales.orders",
    "SELECT id FROM [srv].[analytics].[sales].[orders]",
])
def test_a_four_part_name_never_names_a_declared_table(sql):
    """`server.database.schema.table` reads through a linked server."""
    res = _table_scope(sql, _org("SQLServer"))
    assert res is not None and res.rule == guardrail.RULE_TABLE_SCOPE


# ---------------------------------------------------------------- catalog of the connection


@pytest.mark.parametrize("engine,sql,connection", [
    ("Snowflake", "SELECT id FROM other_db.sales.orders", "ANALYTICS"),
    ("BigQuery", "SELECT id FROM `other-project.sales.orders`", "acme-project"),
    ("SQLServer", "SELECT id FROM other_db.sales.orders", "analytics"),
    ("DuckDB", "SELECT id FROM other.sales.orders", "warehouse"),
    # Unknown: the qualifier could name any catalog, and dropping it always works.
    ("Snowflake", "SELECT id FROM analytics.sales.orders", None),
])
def test_another_catalog_than_the_connections_is_refused(engine, sql, connection):
    res = rt.check_catalog_scope(sql, _org(engine), connection)
    assert res is not None and res.rule == guardrail.RULE_TABLE_SCOPE
    assert connection is None or connection not in res.detail + res.remediation


@pytest.mark.parametrize("engine,sql,connection", [
    ("Snowflake", "SELECT id FROM ANALYTICS.SALES.ORDERS", "analytics"),
    ("BigQuery", "SELECT id FROM `acme-project.sales.orders`", "acme-project"),
    ("SQLServer", "SELECT id FROM analytics.sales.orders", "analytics"),
    ("DuckDB", "SELECT id FROM warehouse.sales.orders", "warehouse"),
    ("Snowflake", "SELECT id FROM sales.orders", None),
    # A CTE and an undeclared table are not this check's: the table gate already ruled on them.
    ("Snowflake", "WITH o AS (SELECT 1 AS id) SELECT id FROM o", None),
    ("Snowflake", "SELECT id FROM other_db.sales.missing", "analytics"),
])
def test_the_connections_own_catalog_passes(engine, sql, connection):
    assert rt.check_catalog_scope(sql, _org(engine), connection) is None


def test_a_catalog_the_model_records_is_not_judged_against_the_connection():
    org = _org("Snowflake", catalog="ARCHIVE")
    assert rt.check_catalog_scope("SELECT id FROM archive.sales.orders", org, "ANALYTICS") is None


@pytest.mark.parametrize("creds,expected", [
    ({"type": "snowflake", "database": "ANALYTICS"}, "ANALYTICS"),
    ({"type": "postgres", "database": "shop"}, "shop"),
    ({"type": "sqlserver", "database": "shop"}, "shop"),
    ({"type": "bigquery", "project": "acme-project"}, "acme-project"),
    ({"type": "trino", "catalog": "hive"}, "hive"),
    ({"type": "databricks", "host": "h"}, None),
    ({"type": "duckdb", "path": "/data/warehouse.duckdb"}, "warehouse"),
    ({"type": "duckdb"}, "memory"),
    ({"type": "mysql", "database": "shop"}, None),
    ({"type": "snowflake"}, None),
])
def test_the_connections_catalog_comes_from_its_credentials(creds, expected):
    assert execute_sql._connection_catalog(creds) == expected


def test_the_execution_path_refuses_another_catalog(monkeypatch):
    """Checked once credentials are loaded, against the model the gates already resolved."""
    org = _org("Snowflake")
    token = execute_sql._guard_model.set(org)
    try:
        creds = {"type": "snowflake", "database": "ANALYTICS"}
        assert execute_sql._foreign_catalog("SELECT id FROM analytics.sales.orders", creds) is None
        res = execute_sql._foreign_catalog("SELECT id FROM other_db.sales.orders", creds)
        assert res is not None and res.rule == guardrail.RULE_TABLE_SCOPE
    finally:
        execute_sql._guard_model.reset(token)
    assert execute_sql._foreign_catalog("SELECT id FROM other_db.sales.orders", creds) is None


def test_an_older_runtime_without_the_check_skips_it(monkeypatch):
    """The executor is vendored into the plugin, beside a separately-versioned runtime."""
    monkeypatch.delattr(rt, "check_catalog_scope")
    token = execute_sql._guard_model.set(_org("Snowflake"))
    try:
        assert execute_sql._foreign_catalog("SELECT id FROM other_db.sales.orders",
                                            {"type": "snowflake", "database": "A"}) is None
    finally:
        execute_sql._guard_model.reset(token)


def test_a_statement_that_does_not_parse_is_left_to_the_gates_that_refuse_it():
    assert rt.check_catalog_scope("SELECT FROM a.b.c WHERE ((", _org("Snowflake"), "A") is None


# ---------------------------------------------------------------- joins_to_other_tables (#484)


def _edge(from_schema: str | None) -> dict:
    return {"from_table": "orders", "from_schema": from_schema, "from_column": "customer_id",
            "to_table": "customers", "to_schema": "sales", "to_column": "id",
            "relationship": "many_to_one"}


def test_an_edge_whose_schema_the_returned_table_cannot_confirm_stays_whole():
    """The returned `orders` has no schema metadata, and the edge says `sales`: compacted, it would
    read `from: orders` and a caller resolves that in the connection's default schema."""
    kept, compact = tools._compact_other_joins([_edge("sales")], {"orders"}, {"orders": None})
    assert kept == [_edge("sales")] and compact == []


def test_an_edge_the_returned_tables_schema_confirms_is_compacted():
    kept, compact = tools._compact_other_joins([_edge("Sales")], {"orders"}, {"orders": "sales"})
    assert kept == [] and compact[0]["table"] == "customers"
