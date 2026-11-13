"""Tests for the discover → prune first pass of agami-connect.

`introspect.discover_inventory` must be CHEAP — it lists tables + columns and
nothing else (no grain count-distincts, no FK-overlap probes, no row-count
scans). `sm discover` writes that inventory and no page: tables are picked on a card.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import cli  # noqa: E402
from semantic_model import introspect as I  # noqa: E402

# --- canned runners ---------------------------------------------------------

SCHEMATA = [{"schema_name": "public"}]
TABLES = [
    {"schema_name": "public", "table_name": "customers", "table_type": "BASE TABLE"},
    {"schema_name": "public", "table_name": "orders", "table_type": "BASE TABLE"},
]
BULK_COLS = [
    {"table_schema": "public", "table_name": "customers", "column_name": "id",
     "data_type": "integer", "numeric_scale": "", "ordinal_position": "1"},
    {"table_schema": "public", "table_name": "customers", "column_name": "email",
     "data_type": "varchar", "numeric_scale": "", "ordinal_position": "2"},
    {"table_schema": "public", "table_name": "orders", "column_name": "id",
     "data_type": "integer", "numeric_scale": "", "ordinal_position": "1"},
    {"table_schema": "public", "table_name": "orders", "column_name": "total",
     "data_type": "numeric", "numeric_scale": "2", "ordinal_position": "2"},
]


def _bulk_runner(calls):
    def run(sql):
        s = " ".join(sql.split())
        calls.append(s)
        if "information_schema.schemata" in s:
            return list(SCHEMATA)
        if "information_schema.tables" in s and "table_type" in s:
            return list(TABLES)
        if "information_schema.columns" in s and "IN (" in s:   # the bulk query
            return list(BULK_COLS)
        return []
    return run


def _per_table_runner(calls):
    def run(sql):
        s = " ".join(sql.split())
        calls.append(s)
        if "information_schema.schemata" in s:
            return list(SCHEMATA)
        if "information_schema.tables" in s and "table_type" in s:
            return list(TABLES)
        if "information_schema.columns" in s and "IN (" in s:
            return []   # pretend the DB rejected the bulk read → force per-table fallback
        if "information_schema.columns" in s and "table_name =" in s:
            if "'customers'" in s:
                return [{"column_name": "id", "data_type": "integer", "numeric_scale": "", "ordinal_position": "1"}]
            return [{"column_name": "id", "data_type": "integer", "numeric_scale": "", "ordinal_position": "1"},
                    {"column_name": "total", "data_type": "numeric", "numeric_scale": "2", "ordinal_position": "2"}]
        return []
    return run


# --- discover_inventory -----------------------------------------------------

# Markers of the EXPENSIVE work the discover pass must never trigger.
_FORBIDDEN = ("PRIMARY KEY", "FOREIGN KEY", "reltuples", "COUNT(DISTINCT", "COUNT(*)")


def test_discover_is_cheap_no_grain_fk_or_count():
    calls = []
    inv = I.discover_inventory("main", "postgres", runner=_bulk_runner(calls))
    assert inv["table_count"] == 2
    for sql in calls:
        for marker in _FORBIDDEN:
            assert marker not in sql, f"discover issued an expensive query: {sql!r}"


def test_discover_bulk_columns_single_round_trip():
    calls = []
    inv = I.discover_inventory("main", "postgres", runner=_bulk_runner(calls))
    assert inv["column_mode"] == "catalog-bulk"
    # exactly one columns query (the bulk one) — not one per table
    col_queries = [s for s in calls if "information_schema.columns" in s]
    assert len(col_queries) == 1
    by = {t["table"]: [c["name"] for c in t["columns"]] for t in inv["tables"]}
    assert by["customers"] == ["id", "email"]
    assert by["orders"] == ["id", "total"]


def test_discover_falls_back_to_per_table_when_bulk_empty():
    calls = []
    inv = I.discover_inventory("main", "postgres", runner=_per_table_runner(calls))
    assert inv["column_mode"] in ("catalog", "per-table")
    by = {t["table"]: [c["name"] for c in t["columns"]] for t in inv["tables"]}
    assert by["customers"] == ["id"]
    assert by["orders"] == ["id", "total"]
    # still cheap
    for sql in calls:
        for marker in _FORBIDDEN:
            assert marker not in sql


def test_discover_raises_when_no_tables():
    with pytest.raises(RuntimeError):
        I.discover_inventory("main", "postgres", runner=lambda sql: [])


# --- cmd_discover writes the inventory and no page ---------------------------


def _inventory():
    return {
        "profile": "main", "db_type": "postgres", "schemas": ["public"], "table_count": 1,
        "column_mode": "catalog-bulk",
        "tables": [{"schema": "public", "table": "orders", "columns": [{"name": "id", "type": "integer"}]}],
    }


@pytest.mark.parametrize("out", [None, "prune.html"])
def test_discover_writes_the_inventory_and_no_page(tmp_path, monkeypatch, capsys, out):
    monkeypatch.setattr(I, "make_execute_sql_runner", lambda *a, **k: None)
    monkeypatch.setattr(I, "discover_inventory", lambda *a, **k: _inventory())
    argv = ["discover", "--profile", "main", "--db-type", "postgres", "--artifacts", str(tmp_path)]
    if out:
        argv += ["--out", str(tmp_path / out)]
    args = cli.build_parser().parse_args(argv)
    assert args.func(args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["prune_html"] is None
    assert json.loads((tmp_path / "main" / ".introspect" / "inventory.json").read_text())["table_count"] == 1
    assert not list(tmp_path.rglob("*.html"))
    assert ("note" in printed) == bool(out)


# --- row estimates for the table picker (#654) -------------------------------

from semantic_model import dialects as D  # noqa: E402


def _estimating_runner(calls, estimates):
    base = _bulk_runner(calls)

    def run(sql):
        if "estimated_rows" in sql:
            calls.append(" ".join(sql.split()))
            if isinstance(estimates, Exception):
                raise estimates
            return estimates
        return base(sql)
    return run


@pytest.mark.parametrize("unknown", [-1, 0, "n/a", None])
def test_row_estimates_are_one_catalog_read_and_never_a_count(unknown):
    calls = []
    est = [{"table_schema": "public", "table_name": "orders", "estimated_rows": "1200"},
           {"table_schema": "public", "table_name": "customers", "estimated_rows": unknown}]
    inv = I.discover_inventory("main", "postgres", runner=_estimating_runner(calls, est),
                               row_estimates=True)
    reads = [s for s in calls if "estimated_rows" in s]
    assert len(reads) == 1 and "reltuples" in reads[0] and "COUNT(" not in reads[0]
    by = {t["table"]: t.get("rows") for t in inv["tables"]}
    assert by == {"orders": 1200, "customers": None}  # -1 (never analysed), 0 or junk: unknown, not empty


def test_row_estimates_are_off_unless_asked():
    calls = []
    inv = I.discover_inventory("main", "postgres", runner=_estimating_runner(calls, []))
    assert not [s for s in calls if "estimated_rows" in s]
    assert all("rows" not in t for t in inv["tables"])


def test_a_refused_estimate_read_leaves_the_sizes_unknown():
    inv = I.discover_inventory("main", "postgres", runner=_estimating_runner([], RuntimeError("denied")),
                               row_estimates=True)
    assert inv["table_count"] == 2 and all("rows" not in t for t in inv["tables"])


@pytest.mark.parametrize(("db", "source", "rows"), [
    ("postgres", "pg_class", "reltuples"),
    ("supabase", "pg_class", "reltuples"),
    ("redshift", "svv_table_info", "tbl_rows"),
    ("mysql", "information_schema.tables", "table_rows"),
    ("snowflake", "information_schema.tables", "row_count"),
])
def test_each_catalog_with_estimates_reads_them_in_one_query(db, source, rows):
    sql = D.get_dialect(db).sql_row_estimates_bulk(["sales", "o'brien"])
    assert source in sql and rows in sql and "COUNT(" not in sql.upper()
    assert "'o''brien'" in sql  # names are quoted as literals
    assert D.get_dialect(db).sql_row_estimates_bulk([]) is None


@pytest.mark.parametrize("db", ["sqlite", "duckdb", "bigquery", "oracle", "sqlserver", "trino", "databricks"])
def test_a_catalog_without_a_bulk_estimate_offers_none(db):
    assert D.get_dialect(db).sql_row_estimates_bulk(["sales"]) is None


def test_discover_passes_row_estimates_on(tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(I, "make_execute_sql_runner", lambda *a, **k: None)
    monkeypatch.setattr(I, "discover_inventory", lambda *a, **k: seen.update(k) or _inventory())
    argv = ["discover", "--profile", "main", "--db-type", "postgres", "--artifacts", str(tmp_path),
            "--row-estimates"]
    args = cli.build_parser().parse_args(argv)
    assert args.func(args) == 0 and seen["row_estimates"] is True
