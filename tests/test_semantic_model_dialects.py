"""Unit tests for semantic_model/dialects.py — catalog SQL + type maps per dialect.

These verify the *generated SQL* (canonical aliases present, right catalog objects,
correct quoting / row-limit syntax) and the native-type → ColumnType maps. No live
DB — only PostgreSQL + Snowflake are live-verified elsewhere; the other ten are
catalog-implemented + unit-tested here against their generated SQL.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import dialects as D  # noqa: E402

ALL = ["postgres", "supabase", "redshift", "mysql", "snowflake", "bigquery",
       "sqlite", "sqlserver", "databricks", "trino", "oracle", "duckdb"]


@pytest.mark.parametrize("key", ALL)
def test_dialect_resolves(key):
    d = D.get_dialect(key)
    assert d.name in (
        "PostgreSQL", "MySQL", "Snowflake", "BigQuery", "Redshift", "SQLite",
        "DuckDB", "SQLServer", "Databricks", "Trino", "Oracle",
    )


@pytest.mark.parametrize("key", ALL)
def test_columns_sql_has_canonical_aliases(key):
    d = D.get_dialect(key)
    sql = d.sql_columns("S", "T")
    assert "column_name" in sql and "data_type" in sql


@pytest.mark.parametrize("key", ALL)
def test_header_is_universal_where_1_0(key):
    d = D.get_dialect(key)
    assert "WHERE 1=0" in d.header_sql("S", "T")


def test_unknown_dialect_raises():
    with pytest.raises(ValueError):
        D.get_dialect("cassandra")


def test_supabase_maps_to_postgres():
    assert D.get_dialect("supabase").name == "PostgreSQL"


# --- quoting ---


def test_quoting_per_family():
    assert D.get_dialect("postgres").quote_ident("x") == '"x"'
    assert D.get_dialect("mysql").quote_ident("x") == "`x`"
    assert D.get_dialect("databricks").quote_ident("x") == "`x`"
    assert D.get_dialect("sqlserver").quote_ident("x") == "[x]"


# --- row-limit syntax ---


def test_limit_syntax():
    assert "LIMIT 5" in D.get_dialect("postgres").sample_sql("s", "t", 5)
    assert "TOP 5" in D.get_dialect("sqlserver").sample_sql("s", "t", 5)
    assert "FETCH FIRST 5 ROWS ONLY" in D.get_dialect("oracle").sample_sql("s", "t", 5)


# --- catalog-source specifics ---


def test_sqlite_uses_pragma_not_information_schema():
    d = D.get_dialect("sqlite")
    assert "pragma_table_info" in d.sql_columns("main", "t")
    assert "information_schema" not in d.sql_columns("main", "t").lower()
    assert "pragma_foreign_key_list" in d.sql_foreign_keys_for_table("t")


def test_oracle_uses_data_dictionary():
    d = D.get_dialect("oracle")
    assert "all_tab_columns" in d.sql_columns("S", "T").lower()
    assert "all_constraints" in d.sql_foreign_keys("S").lower()


def test_bigquery_regioned_information_schema():
    d = D.get_dialect("bigquery")
    assert "INFORMATION_SCHEMA.SCHEMATA" in d.sql_schemas()
    assert "region-" in d.sql_schemas()


def test_mysql_fk_uses_referenced_columns():
    d = D.get_dialect("mysql")
    assert "referenced_table_name" in d.sql_foreign_keys("s")


def test_unenforced_fk_dialects_flagged():
    assert D.get_dialect("redshift").fk_enforced is False
    assert D.get_dialect("databricks").fk_enforced is False
    assert D.get_dialect("trino").fk_enforced is False
    assert D.get_dialect("postgres").fk_enforced is True


# --- type maps ---


@pytest.mark.parametrize("raw,scale,expected", [
    ("INT64", None, "integer"),
    ("STRING", None, "string"),
    ("FLOAT64", None, "float"),
    ("NUMERIC", None, "decimal"),
    ("BOOL", None, "boolean"),
    ("TIMESTAMP", None, "timestamp"),
    ("DATE", None, "date"),
    ("BYTES", None, "bytes"),
    ("STRUCT<x INT64>", None, "array"),
    ("NUMBER", 0, "integer"),
    ("NUMBER", 2, "decimal"),
    ("nvarchar", None, "string"),
    ("uniqueidentifier", None, "uuid"),
    ("bit", None, "boolean"),
    ("VARCHAR2", None, "string"),
    ("jsonb", None, "json"),
])
def test_type_normalization(raw, scale, expected):
    assert D.normalize_type(raw, numeric_scale=scale) == expected
