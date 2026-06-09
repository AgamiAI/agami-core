"""Per-dialect catalog strategies for live-DB introspection.

The introspection engine (`introspect.py`) is dialect-agnostic: it asks a Dialect
for catalog SQL, runs it through an injected query-runner, and parses a **fixed,
canonical set of output columns** regardless of backend. Each Dialect's job is to
produce SQL that aliases its catalog into those canonical names, plus supply
identifier quoting, row-limit syntax, and a native-type → ColumnType map.

Canonical catalog result columns the engine expects:
    schemas()        -> rows with: schema_name
    tables(schema)   -> rows with: schema_name, table_name, table_type
    columns(s, t)    -> rows with: column_name, data_type, is_nullable,
                                   ordinal_position, numeric_scale (nullable)
    primary_keys(s,t)-> rows with: column_name            (ordered by key position)
    foreign_keys(s)  -> rows with: from_table, from_column, to_table, to_column
    row_estimate(s,t)-> rows with: estimated_rows         (or None to skip)

Three catalog families:
  * information_schema (ANSI)         — most dialects (with quoting/type tweaks)
  * PRAGMA                            — SQLite
  * data-dictionary views (ALL_*)    — Oracle

Probe mode (in introspect.py) is dialect-agnostic and reuses only quote_ident,
quote_lit, sample_sql, and header_sql, so it works for every dialect — including
ones whose catalog is locked down.

Coverage note: only PostgreSQL and Snowflake are live-verified (the creds we
have). The other ten are catalog-implemented + unit-tested against canned rows,
NOT live-verified — treat them as such until exercised against a real instance.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from .models import ColumnType, StorageType


# ---------------------------------------------------------------------------
# Shared native-type → ColumnType normalizer
# ---------------------------------------------------------------------------


def normalize_type(raw: str, *, numeric_scale: Optional[int] = None) -> ColumnType:
    """Best-effort native-type-name → ColumnType. Substring-matched so it tolerates
    parameterized forms like VARCHAR(255), NUMBER(10,2), TIMESTAMP_NTZ(9)."""
    t = (raw or "").strip().lower()
    if not t:
        return "string"
    # exact-ish fast paths
    if t in ("bool", "boolean", "bit"):
        return "boolean"
    if "uuid" in t or "uniqueidentifier" in t:
        return "uuid"
    if "json" in t or "variant" in t or "object" in t:
        return "json"
    if "array" in t or t.startswith("struct") or t.startswith("map") or t.startswith("row"):
        return "array"
    if "byte" in t or "binary" in t or t in ("blob", "raw", "bytea", "varbinary", "image"):
        return "bytes"
    if "timestamp" in t or "datetime" in t or t == "smalldatetime" or "datetimeoffset" in t:
        return "timestamp"
    if t.startswith("time"):
        return "time"
    if "date" in t:
        return "date"
    # numerics
    if any(k in t for k in ("int", "serial")) and "interval" not in t and "point" not in t:
        return "integer"
    if any(k in t for k in ("numeric", "decimal", "number", "money", "dec")):
        # NUMBER(p,0) / NUMERIC(p,0) is an integer
        if numeric_scale is not None and numeric_scale == 0:
            return "integer"
        return "decimal"
    if any(k in t for k in ("float", "double", "real", "binary_double", "binary_float")):
        return "float"
    # strings
    if any(k in t for k in ("char", "text", "string", "clob", "nchar", "nvarchar", "varchar", "enum", "set")):
        return "string"
    return "string"


def _esc(s: str) -> str:
    """Escape a string literal for embedding in SQL (single-quote doubling)."""
    return s.replace("'", "''")


# ---------------------------------------------------------------------------
# Base dialect
# ---------------------------------------------------------------------------


class Dialect:
    name: StorageType = "PostgreSQL"
    limit_style: str = "limit"  # "limit" | "top" | "fetch"
    fk_enforced: bool = True    # False => always confirm inferred FKs by overlap

    # --- identifier / literal helpers ---

    def quote_ident(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def quote_lit(self, s: str) -> str:
        return "'" + _esc(s) + "'"

    def qualified(self, schema: Optional[str], table: str) -> str:
        if schema:
            return f"{self.quote_ident(schema)}.{self.quote_ident(table)}"
        return self.quote_ident(table)

    # --- row-limit syntax (probe mode) ---

    def sample_sql(self, schema: Optional[str], table: str, n: int) -> str:
        q = self.qualified(schema, table)
        if self.limit_style == "top":
            return f"SELECT TOP {n} * FROM {q}"
        if self.limit_style == "fetch":
            return f"SELECT * FROM {q} FETCH FIRST {n} ROWS ONLY"
        return f"SELECT * FROM {q} LIMIT {n}"

    def header_sql(self, schema: Optional[str], table: str) -> str:
        # Universal zero-row describe — returns the header on every dialect.
        return f"SELECT * FROM {self.qualified(schema, table)} WHERE 1=0"

    def count_distinct_sql(self, schema: Optional[str], table: str, column: str) -> str:
        q = self.qualified(schema, table)
        c = self.quote_ident(column)
        return (
            f"SELECT COUNT(*) AS total, COUNT(DISTINCT {c}) AS distinct_count, "
            f"COUNT(*) - COUNT({c}) AS null_count FROM {q}"
        )

    # --- catalog SQL (ANSI information_schema defaults) ---

    _SYS_SCHEMAS = ("information_schema", "pg_catalog")

    def sql_schemas(self) -> str:
        excl = ", ".join(self.quote_lit(s) for s in self._SYS_SCHEMAS)
        return (
            "SELECT schema_name FROM information_schema.schemata "
            f"WHERE schema_name NOT IN ({excl}) ORDER BY schema_name"
        )

    def sql_tables(self, schema: str) -> str:
        return (
            "SELECT table_schema AS schema_name, table_name, table_type "
            "FROM information_schema.tables "
            f"WHERE table_schema = {self.quote_lit(schema)} "
            "ORDER BY table_name"
        )

    def sql_columns(self, schema: str, table: str) -> str:
        return (
            "SELECT column_name, data_type, is_nullable, ordinal_position, "
            "numeric_scale FROM information_schema.columns "
            f"WHERE table_schema = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)} ORDER BY ordinal_position"
        )

    def sql_primary_keys(self, schema: str, table: str) -> str:
        return (
            "SELECT kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema "
            "WHERE tc.constraint_type = 'PRIMARY KEY' "
            f"AND tc.table_schema = {self.quote_lit(schema)} "
            f"AND tc.table_name = {self.quote_lit(table)} "
            "ORDER BY kcu.ordinal_position"
        )

    def sql_foreign_keys(self, schema: str) -> str:
        return (
            "SELECT kcu.table_name AS from_table, kcu.column_name AS from_column, "
            "ccu.table_name AS to_table, ccu.column_name AS to_column "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema "
            "JOIN information_schema.constraint_column_usage ccu "
            "ON tc.constraint_name = ccu.constraint_name "
            "WHERE tc.constraint_type = 'FOREIGN KEY' "
            f"AND tc.table_schema = {self.quote_lit(schema)}"
        )

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        # Fallback: an exact COUNT(*). Dialects with cheap estimates override.
        return f"SELECT COUNT(*) AS estimated_rows FROM {self.qualified(schema, table)}"

    # --- type mapping ---

    def map_type(self, raw: str, numeric_scale: Optional[int] = None) -> ColumnType:
        return normalize_type(raw, numeric_scale=numeric_scale)


# ---------------------------------------------------------------------------
# information_schema family
# ---------------------------------------------------------------------------


class PostgreSQL(Dialect):
    name = "PostgreSQL"

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT reltuples::bigint AS estimated_rows FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = {self.quote_lit(schema)} "
            f"AND c.relname = {self.quote_lit(table)}"
        )


class Supabase(PostgreSQL):
    # Supabase is hosted Postgres. Stored as storage_type=PostgreSQL; this alias
    # exists so a "supabase" credential type still resolves to a dialect.
    name = "PostgreSQL"


class Redshift(PostgreSQL):
    name = "Redshift"
    fk_enforced = False  # Redshift declares but does not enforce FKs

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT tbl_rows AS estimated_rows FROM svv_table_info "
            f"WHERE \"schema\" = {self.quote_lit(schema)} "
            f"AND \"table\" = {self.quote_lit(table)}"
        )


class MySQL(Dialect):
    name = "MySQL"
    _SYS_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")

    def quote_ident(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def sql_foreign_keys(self, schema: str) -> str:
        # MySQL exposes the target directly on key_column_usage.
        return (
            "SELECT table_name AS from_table, column_name AS from_column, "
            "referenced_table_name AS to_table, referenced_column_name AS to_column "
            "FROM information_schema.key_column_usage "
            f"WHERE table_schema = {self.quote_lit(schema)} "
            "AND referenced_table_name IS NOT NULL"
        )

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT table_rows AS estimated_rows FROM information_schema.tables "
            f"WHERE table_schema = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)}"
        )


class Snowflake(Dialect):
    name = "Snowflake"
    _SYS_SCHEMAS = ("INFORMATION_SCHEMA",)

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT row_count AS estimated_rows FROM information_schema.tables "
            f"WHERE table_schema = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)}"
        )


class DuckDB(Dialect):
    name = "DuckDB"


class Databricks(Dialect):
    name = "Databricks"
    fk_enforced = False  # Unity Catalog constraints are informational

    def quote_ident(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"


class Trino(Dialect):
    name = "Trino"
    fk_enforced = False  # Trino generally has no constraints -> probe FKs

    def sql_primary_keys(self, schema: str, table: str) -> str:
        # Most Trino connectors don't expose PK constraints; return nothing and
        # let probe-mode infer the grain.
        return "SELECT NULL AS column_name WHERE 1=0"

    def sql_foreign_keys(self, schema: str) -> str:
        return "SELECT NULL AS from_table, NULL AS from_column, NULL AS to_table, NULL AS to_column WHERE 1=0"

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return None  # varies per connector; skip


class SQLServer(Dialect):
    name = "SQLServer"
    limit_style = "top"
    _SYS_SCHEMAS = ("sys", "INFORMATION_SCHEMA")

    def quote_ident(self, name: str) -> str:
        return "[" + name.replace("]", "]]") + "]"

    def sql_foreign_keys(self, schema: str) -> str:
        # SQL Server's constraint_column_usage works for FK targets.
        return super().sql_foreign_keys(schema)

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT SUM(ps.row_count) AS estimated_rows "
            "FROM sys.dm_db_partition_stats ps "
            "JOIN sys.tables t ON t.object_id = ps.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            f"WHERE s.name = {self.quote_lit(schema)} AND t.name = {self.quote_lit(table)} "
            "AND ps.index_id IN (0,1)"
        )


class BigQuery(Dialect):
    name = "BigQuery"

    def __init__(self, region: str = "region-us"):
        self.region = region

    def quote_ident(self, name: str) -> str:
        return "`" + name.replace("`", "") + "`"

    def qualified(self, schema: Optional[str], table: str) -> str:
        # BigQuery: dataset.table, backtick-wrapped as one path.
        path = f"{schema}.{table}" if schema else table
        return "`" + path.replace("`", "") + "`"

    def sql_schemas(self) -> str:
        return (
            f"SELECT schema_name FROM `{self.region}`.INFORMATION_SCHEMA.SCHEMATA "
            "ORDER BY schema_name"
        )

    def sql_tables(self, schema: str) -> str:
        return (
            f"SELECT table_schema AS schema_name, table_name, table_type "
            f"FROM `{schema}`.INFORMATION_SCHEMA.TABLES ORDER BY table_name"
        )

    def sql_columns(self, schema: str, table: str) -> str:
        return (
            "SELECT column_name, data_type, is_nullable, ordinal_position, "
            "NULL AS numeric_scale "
            f"FROM `{schema}`.INFORMATION_SCHEMA.COLUMNS "
            f"WHERE table_name = {self.quote_lit(table)} ORDER BY ordinal_position"
        )

    def sql_primary_keys(self, schema: str, table: str) -> str:
        return (
            "SELECT kcu.column_name "
            f"FROM `{schema}`.INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
            f"JOIN `{schema}`.INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
            "ON tc.constraint_name = kcu.constraint_name "
            "WHERE tc.constraint_type = 'PRIMARY KEY' "
            f"AND tc.table_name = {self.quote_lit(table)} "
            "ORDER BY kcu.ordinal_position"
        )

    def sql_foreign_keys(self, schema: str) -> str:
        return (
            "SELECT kcu.table_name AS from_table, kcu.column_name AS from_column, "
            "ccu.table_name AS to_table, ccu.column_name AS to_column "
            f"FROM `{schema}`.INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
            f"JOIN `{schema}`.INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
            "ON tc.constraint_name = kcu.constraint_name "
            f"JOIN `{schema}`.INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE ccu "
            "ON tc.constraint_name = ccu.constraint_name "
            "WHERE tc.constraint_type = 'FOREIGN KEY'"
        )

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT total_rows AS estimated_rows "
            f"FROM `{schema}`.INFORMATION_SCHEMA.TABLE_STORAGE "
            f"WHERE table_name = {self.quote_lit(table)}"
        )


# ---------------------------------------------------------------------------
# PRAGMA family — SQLite
# ---------------------------------------------------------------------------


class SQLite(Dialect):
    name = "SQLite"

    def sql_schemas(self) -> str:
        # SQLite has no schemas; emit a single synthetic "main".
        return "SELECT 'main' AS schema_name"

    def sql_tables(self, schema: str) -> str:
        return (
            "SELECT 'main' AS schema_name, name AS table_name, type AS table_type "
            "FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )

    def sql_columns(self, schema: str, table: str) -> str:
        # pragma_table_info is a table-valued function usable in SELECT.
        return (
            "SELECT name AS column_name, type AS data_type, "
            "CASE WHEN \"notnull\" = 0 THEN 'YES' ELSE 'NO' END AS is_nullable, "
            "cid AS ordinal_position, NULL AS numeric_scale "
            f"FROM pragma_table_info({self.quote_lit(table)})"
        )

    def sql_primary_keys(self, schema: str, table: str) -> str:
        return (
            "SELECT name AS column_name FROM pragma_table_info("
            f"{self.quote_lit(table)}) WHERE pk > 0 ORDER BY pk"
        )

    def sql_foreign_keys(self, schema: str) -> str:
        # foreign_key_list is per-table; the engine special-cases SQLite to call
        # sql_foreign_keys_for_table per table. This whole-schema form is unused.
        return "SELECT NULL AS from_table, NULL AS from_column, NULL AS to_table, NULL AS to_column WHERE 1=0"

    def sql_foreign_keys_for_table(self, table: str) -> str:
        return (
            f"SELECT {self.quote_lit(table)} AS from_table, \"from\" AS from_column, "
            "\"table\" AS to_table, \"to\" AS to_column "
            f"FROM pragma_foreign_key_list({self.quote_lit(table)})"
        )

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return f"SELECT COUNT(*) AS estimated_rows FROM {self.quote_ident(table)}"


# ---------------------------------------------------------------------------
# Data-dictionary family — Oracle
# ---------------------------------------------------------------------------


class Oracle(Dialect):
    name = "Oracle"
    limit_style = "fetch"

    def sql_schemas(self) -> str:
        return (
            "SELECT username AS schema_name FROM all_users "
            "WHERE username NOT IN ('SYS','SYSTEM','OUTLN','XDB','CTXSYS','MDSYS') "
            "ORDER BY username"
        )

    def sql_tables(self, schema: str) -> str:
        return (
            "SELECT owner AS schema_name, table_name, 'BASE TABLE' AS table_type "
            f"FROM all_tables WHERE owner = {self.quote_lit(schema)} ORDER BY table_name"
        )

    def sql_columns(self, schema: str, table: str) -> str:
        return (
            "SELECT column_name, data_type, "
            "CASE nullable WHEN 'Y' THEN 'YES' ELSE 'NO' END AS is_nullable, "
            "column_id AS ordinal_position, data_scale AS numeric_scale "
            "FROM all_tab_columns "
            f"WHERE owner = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)} ORDER BY column_id"
        )

    def sql_primary_keys(self, schema: str, table: str) -> str:
        return (
            "SELECT acc.column_name FROM all_constraints ac "
            "JOIN all_cons_columns acc ON ac.constraint_name = acc.constraint_name "
            "AND ac.owner = acc.owner "
            "WHERE ac.constraint_type = 'P' "
            f"AND ac.owner = {self.quote_lit(schema)} "
            f"AND ac.table_name = {self.quote_lit(table)} ORDER BY acc.position"
        )

    def sql_foreign_keys(self, schema: str) -> str:
        return (
            "SELECT ac.table_name AS from_table, acc.column_name AS from_column, "
            "rcc.table_name AS to_table, rcc.column_name AS to_column "
            "FROM all_constraints ac "
            "JOIN all_cons_columns acc ON ac.constraint_name = acc.constraint_name "
            "AND ac.owner = acc.owner "
            "JOIN all_cons_columns rcc ON ac.r_constraint_name = rcc.constraint_name "
            "AND acc.position = rcc.position "
            "WHERE ac.constraint_type = 'R' "
            f"AND ac.owner = {self.quote_lit(schema)}"
        )

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT num_rows AS estimated_rows FROM all_tables "
            f"WHERE owner = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)}"
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Callable[[], Dialect]] = {
    "postgres": PostgreSQL,
    "postgresql": PostgreSQL,
    "supabase": Supabase,
    "redshift": Redshift,
    "mysql": MySQL,
    "mariadb": MySQL,
    "snowflake": Snowflake,
    "duckdb": DuckDB,
    "databricks": Databricks,
    "trino": Trino,
    "presto": Trino,
    "sqlserver": SQLServer,
    "mssql": SQLServer,
    "bigquery": BigQuery,
    "sqlite": SQLite,
    "oracle": Oracle,
}


def get_dialect(db_type: str) -> Dialect:
    """Resolve a credential db_type (or storage_type) to a Dialect instance."""
    key = (db_type or "").strip().lower().replace(" ", "")
    factory = _REGISTRY.get(key)
    if factory is None:
        raise ValueError(
            f"unsupported db_type {db_type!r}; known: {sorted(set(_REGISTRY))}"
        )
    return factory()


def supported_db_types() -> list[str]:
    return sorted(_REGISTRY)


__all__ = [
    "Dialect",
    "PostgreSQL", "Supabase", "Redshift", "MySQL", "Snowflake", "DuckDB",
    "Databricks", "Trino", "SQLServer", "BigQuery", "SQLite", "Oracle",
    "get_dialect", "supported_db_types", "normalize_type",
]
