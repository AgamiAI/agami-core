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
    foreign_keys(s)  -> rows with: from_table, from_column, to_table, to_column,
                                   constraint_name, key_ordinal
                                   (+ optional from_schema, to_schema — schema-ful dialects)
                        One row per column of a key: introspection groups a key's rows by
                        (from_schema, from_table, constraint_name), in key_ordinal order.
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


# Words most engines reserve, so a column named after one must be quoted.
_RESERVED_WORDS = frozenset({
    "all", "and", "any", "as", "asc", "between", "by", "case", "cast", "check", "column",
    "constraint", "create", "cross", "current_date", "current_time", "current_timestamp",
    "current_user", "default", "delete", "desc", "distinct", "drop", "else", "end", "except",
    "exists", "false", "fetch", "for", "foreign", "from", "full", "grant", "group", "having", "in",
    "index", "inner", "insert", "intersect", "into", "is", "join", "left", "like", "limit", "natural",
    "not", "null", "offset", "on", "or", "order", "outer", "primary", "references", "right",
    "select", "session_user", "table", "then", "to", "true", "union", "unique", "update", "user",
    "using", "values", "when", "where", "window", "with",
})
_PLAIN_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    unique_enforced: bool = True  # False => a declared unique key is confirmed on the data first
    # True where a backslash escapes inside a string literal by default, so a name ending in one
    # would end the literal early (#439). Introspection reads skip the model pass, so this escaping
    # is their remaining defence.
    backslash_escapes: bool = False
    # How the engine folds an unquoted identifier: "lower", "upper", or None where it matches
    # names case-insensitively as written.
    folds_unquoted: Optional[str] = None
    # Words this engine reserves beyond _RESERVED_WORDS. Only those are quoted: quoting every name
    # would change what an unquoted name means on a case-folding engine (Snowflake, Oracle).
    reserved_extra: frozenset = frozenset()
    # True where the day difference counts calendar-day boundaries, so an end a few hours before
    # its start on the same day still gives 0 and the guard must compare the values themselves.
    duration_rounds: bool = False

    # --- identifier / literal helpers ---

    def quote_ident(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def ident(self, name: str) -> str:
        """`name` for an expression, quoted only when this engine needs it: special characters, a
        reserved word, or a case the engine would fold away. A plain name stays bare, so a binding
        reads the way a person would write it. On an upper-folding engine an all-lower name stays
        bare too, as hand-written models spell upper-case catalog names that way."""
        low = name.lower()
        if not _PLAIN_IDENT_RE.match(name) or low in _RESERVED_WORDS or low in self.reserved_extra:
            return self.quote_ident(name)
        if self.folds_unquoted == "lower" and name != name.lower():
            return self.quote_ident(name)
        if self.folds_unquoted == "upper" and name not in (name.lower(), name.upper()):
            return self.quote_ident(name)
        return name

    def quote_lit(self, s: str) -> str:
        if self.backslash_escapes:
            s = s.replace("\\", "\\\\")
        return "'" + _esc(s) + "'"

    def qualified(self, schema: Optional[str], table: str) -> str:
        if schema:
            return f"{self.quote_ident(schema)}.{self.quote_ident(table)}"
        return self.quote_ident(table)

    def duration_days_expr(self, start: str, end: str, *, start_type: str = "timestamp",
                           end_type: str = "timestamp") -> str:
        """Per-row 'days between two columns' (identifiers as written) for an avg-duration metric.
        `start_type`/`end_type` are the columns' ColumnTypes. Postgres-family default: DATE minus
        DATE is already a whole number of days, and EXTRACT(EPOCH ...) of that integer fails, so
        only a pair with a timestamp goes through the interval's epoch."""
        if start_type == end_type == "date":
            return f"({end} - {start})"
        return f"EXTRACT(EPOCH FROM ({end} - {start})) / 86400.0"

    def duration_guard(self, start: str, end: str, *, start_type: str = "timestamp",
                       end_type: str = "timestamp") -> str:
        """The condition a row must meet for its duration to count: the end is not before the
        start."""
        if self.duration_rounds:
            return f"{end} >= {start}"
        days = self.duration_days_expr(start, end, start_type=start_type, end_type=end_type)
        return f"{days} >= 0"

    def flag_rate_expr(self, col: str, *, boolean: bool) -> str:
        """Per-row 1.0 / 0.0 for a flag column, NULL where the flag is NULL, so AVG of it is the
        rate among the rows the flag applies to rather than counting a NULL as false."""
        if boolean:
            return f"CASE WHEN {col} THEN 1.0 WHEN NOT {col} THEN 0.0 END"
        return f"CASE WHEN {col} <> 0 THEN 1.0 WHEN {col} = 0 THEN 0.0 END"

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

    def limited(self, select: str, n: int) -> str:
        """`select` bounded to `n` rows in this dialect's own row-limit syntax.

        The one place the TOP / FETCH FIRST / LIMIT switch is spelled for a statement built
        elsewhere. `TOP` goes after `DISTINCT` when there is one: T-SQL reads `SELECT DISTINCT TOP n`
        and rejects `SELECT TOP n DISTINCT`.
        """
        if self.limit_style == "top":
            head = "SELECT DISTINCT " if select.startswith("SELECT DISTINCT ") else "SELECT "
            return select.replace(head, f"{head}TOP {n} ", 1)
        if self.limit_style == "fetch":
            return f"{select} FETCH FIRST {n} ROWS ONLY"
        return f"{select} LIMIT {n}"

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

    def sql_columns_bulk(self, schemas: list[str]) -> Optional[str]:
        """All columns for every table across `schemas` in ONE query — used by the
        cheap discover/prune pass so a 500-table catalog is one round-trip, not 500.
        Returns None for dialects whose catalog can't enumerate columns in bulk
        (SQLite PRAGMA, BigQuery per-dataset, Oracle) — the caller falls back to
        per-table `sql_columns`. ANSI information_schema default covers most dialects."""
        if not schemas:
            return None
        inlist = ", ".join(self.quote_lit(s) for s in schemas)
        return (
            "SELECT table_schema, table_name, column_name, data_type, "
            "numeric_scale, ordinal_position FROM information_schema.columns "
            f"WHERE table_schema IN ({inlist}) "
            "ORDER BY table_schema, table_name, ordinal_position"
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

    def sql_unique_keys(self, schema: str, table: str) -> Optional[str]:
        """Each unique key other than the primary key, one row per column: `constraint_name`,
        `column_name`, in key order. None where the engine has no catalog of them."""
        return (
            "SELECT tc.constraint_name, kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema AND tc.table_name = kcu.table_name "
            "WHERE tc.constraint_type = 'UNIQUE' "
            f"AND tc.table_schema = {self.quote_lit(schema)} "
            f"AND tc.table_name = {self.quote_lit(table)} "
            "ORDER BY tc.constraint_name, kcu.ordinal_position"
        )

    def sql_foreign_keys(self, schema: str) -> str:
        # The referenced column is matched by POSITION (referential_constraints names the key the
        # FK points at, position_in_unique_constraint the column in it). constraint_column_usage
        # carries no position, so joining it on the constraint name alone paired every column of a
        # composite key with every referenced column. Also returns from_schema / to_schema so
        # introspect can flag cross-schema joins; the referenced table's schema may differ.
        return (
            "SELECT kcu.table_name AS from_table, kcu.column_name AS from_column, "
            "kcu.table_schema AS from_schema, "
            "ukc.table_name AS to_table, ukc.column_name AS to_column, "
            "ukc.table_schema AS to_schema, "
            "kcu.constraint_name AS constraint_name, kcu.ordinal_position AS key_ordinal "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema "
            "JOIN information_schema.referential_constraints rc "
            "ON rc.constraint_name = tc.constraint_name "
            "AND rc.constraint_schema = tc.constraint_schema "
            "JOIN information_schema.key_column_usage ukc "
            "ON ukc.constraint_name = rc.unique_constraint_name "
            "AND ukc.constraint_schema = rc.unique_constraint_schema "
            "AND ukc.ordinal_position = kcu.position_in_unique_constraint "
            "WHERE tc.constraint_type = 'FOREIGN KEY' "
            f"AND tc.table_schema = {self.quote_lit(schema)} "
            "ORDER BY kcu.table_name, kcu.constraint_name, kcu.ordinal_position"
        )

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        # Fallback: an exact COUNT(*). Dialects with cheap estimates override.
        return self.sql_row_count(schema, table)

    def sql_row_count(self, schema: str, table: str) -> Optional[str]:
        """An exact count, for when the catalog keeps no usable estimate (a view, or a table the
        engine has never analysed). Read-only, and bounded like every other introspection read."""
        return f"SELECT COUNT(*) AS estimated_rows FROM {self.qualified(schema, table)}"

    # --- declared value lists (#517) ---

    def sql_enum_values(self, schema: str, table: str) -> Optional[str]:
        """The table's enum-typed columns and their declared values: one row per value
        (`column_name`, `enum_value`) in declaration order, or one row per column holding its type
        text (`column_name`, `column_type`, e.g. `enum('a','b')`). None where the engine has no
        enum type."""
        return None

    def sql_check_constraints(self, schema: str, table: str) -> Optional[str]:
        """The text of each enforced CHECK constraint on the table, one row each (`check_clause`).
        None where the catalog keeps no CHECK constraints."""
        return None

    def sql_check_constraints_fallback(self, schema: str, table: str) -> Optional[str]:
        """A second form of the CHECK read, tried when the first one fails (another engine sharing
        this dialect lays out its catalog differently). None when there is none."""
        return None

    # The engine's default string comparison ignores case, so 'open' also matches 'Open'.
    case_insensitive_compare = False
    # ... and ignores accents too (MySQL/MariaDB's default *_ci collations).
    accent_insensitive_compare = False

    def distinct_sql(self, schema: Optional[str], table: str, column: str, n: int) -> str:
        """Up to `n` distinct non-null values of one column, as `v`."""
        c = self.quote_ident(column)
        return self.limited(
            f"SELECT DISTINCT {c} AS v FROM {self.qualified(schema, table)} WHERE {c} IS NOT NULL", n)

    # --- type mapping ---

    def map_type(self, raw: str, numeric_scale: Optional[int] = None) -> ColumnType:
        return normalize_type(raw, numeric_scale=numeric_scale)


# ---------------------------------------------------------------------------
# information_schema family
# ---------------------------------------------------------------------------


class PostgreSQL(Dialect):
    name = "PostgreSQL"
    folds_unquoted = "lower"

    # Keys come from pg_constraint, not information_schema: Postgres shows a constraint in
    # table_constraints only to the table's owner or a role holding a privilege other than SELECT,
    # so the read-only role a deploy is told to use would see no keys at all and every grain and
    # join would be guessed. pg_constraint is readable by any role. Each query also returns
    # constraint_type, spelled as information_schema spells it, so a caller can tell the two apart.

    def sql_primary_keys(self, schema: str, table: str) -> str:
        return (
            "SELECT a.attname AS column_name, 'PRIMARY KEY' AS constraint_type "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE "
            "JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum "
            "WHERE con.contype = 'p' "
            f"AND n.nspname = {self.quote_lit(schema)} "
            f"AND c.relname = {self.quote_lit(table)} "
            "ORDER BY k.ord"
        )

    def sql_unique_keys(self, schema: str, table: str) -> Optional[str]:
        # Unique constraints and unique indexes (a unique index with no constraint enforces the same
        # thing). Partial and expression indexes are left out: neither makes the column unique.
        # indnkeyatts leaves out a covering index's INCLUDE columns. An index a failed CREATE INDEX
        # CONCURRENTLY left invalid may sit over duplicates.
        return (
            "SELECT con.conname AS constraint_name, a.attname AS column_name, k.ord "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE "
            "JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum "
            "WHERE con.contype = 'u' "
            f"AND n.nspname = {self.quote_lit(schema)} "
            f"AND c.relname = {self.quote_lit(table)} "
            "UNION "
            "SELECT ic.relname AS constraint_name, a.attname AS column_name, k.ord "
            "FROM pg_index i "
            "JOIN pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) ON TRUE "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
            "WHERE i.indisunique AND NOT i.indisprimary AND i.indisvalid AND i.indisready "
            "AND i.indpred IS NULL AND i.indexprs IS NULL AND k.ord <= i.indnkeyatts "
            f"AND n.nspname = {self.quote_lit(schema)} "
            f"AND c.relname = {self.quote_lit(table)} "
            "ORDER BY constraint_name, ord"
        )

    def sql_foreign_keys(self, schema: str) -> str:
        # unnest(conkey, confkey) pairs the columns of a composite key in declaration order, and
        # to_schema is the referenced table's own schema, which may differ from the constraint's.
        return (
            "SELECT c.relname AS from_table, fa.attname AS from_column, fn.nspname AS from_schema, "
            "rc.relname AS to_table, ra.attname AS to_column, rn.nspname AS to_schema, "
            "con.conname AS constraint_name, k.ord AS key_ordinal, "
            "'FOREIGN KEY' AS constraint_type "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace fn ON fn.oid = c.relnamespace "
            "JOIN pg_class rc ON rc.oid = con.confrelid "
            "JOIN pg_namespace rn ON rn.oid = rc.relnamespace "
            "JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(conkey, confkey, ord) ON TRUE "
            "JOIN pg_attribute fa ON fa.attrelid = con.conrelid AND fa.attnum = k.conkey "
            "JOIN pg_attribute ra ON ra.attrelid = con.confrelid AND ra.attnum = k.confkey "
            "WHERE con.contype = 'f' "
            f"AND fn.nspname = {self.quote_lit(schema)} "
            "ORDER BY c.relname, con.conname, k.ord"
        )

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT reltuples::bigint AS estimated_rows FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = {self.quote_lit(schema)} "
            f"AND c.relname = {self.quote_lit(table)}"
        )

    # From the pg_* catalog for the same reason as the keys: any role can read it.
    def sql_enum_values(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT a.attname AS column_name, e.enumlabel AS enum_value "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_enum e ON e.enumtypid = a.atttypid "
            "WHERE a.attnum > 0 AND NOT a.attisdropped "
            f"AND n.nspname = {self.quote_lit(schema)} "
            f"AND c.relname = {self.quote_lit(table)} "
            "ORDER BY a.attnum, e.enumsortorder"
        )

    def sql_check_constraints(self, schema: str, table: str) -> Optional[str]:
        # The text comes from information_schema, which shows a CHECK only to the table's owner:
        # pg_get_constraintdef() would show it to any role, but the always-on recon gate refuses
        # every pg_* call. So a SELECT-only role reads the enums above and no CHECKs.
        return (
            "SELECT cc.check_clause "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN information_schema.check_constraints cc "
            "ON cc.constraint_schema = n.nspname AND cc.constraint_name = con.conname "
            # a NO INHERIT CHECK binds only the parent's own rows; reading the parent reads its children
            "WHERE con.contype = 'c' AND con.convalidated AND NOT con.connoinherit "
            # a name is unique only per table: one another table also uses could join to its text
            "AND NOT EXISTS (SELECT 1 FROM pg_constraint o WHERE o.connamespace = con.connamespace "
            "AND o.conname = con.conname AND o.oid <> con.oid) "
            f"AND n.nspname = {self.quote_lit(schema)} "
            f"AND c.relname = {self.quote_lit(table)} "
            "ORDER BY con.conname"
        )


class Supabase(PostgreSQL):
    # Supabase is hosted Postgres. Stored as storage_type=PostgreSQL; this alias
    # exists so a "supabase" credential type still resolves to a dialect.
    name = "PostgreSQL"


class Redshift(PostgreSQL):
    name = "Redshift"
    duration_rounds = True
    fk_enforced = False  # Redshift declares but does not enforce FKs
    unique_enforced = False  # nor unique keys

    # Redshift has no LATERAL and no unnest(...) WITH ORDINALITY, so the pg_constraint queries
    # PostgreSQL uses would fail there. It keeps the information_schema ones.
    sql_primary_keys = Dialect.sql_primary_keys
    sql_foreign_keys = Dialect.sql_foreign_keys
    sql_unique_keys = Dialect.sql_unique_keys
    # Nor enum types or CHECK constraints.
    sql_enum_values = Dialect.sql_enum_values
    sql_check_constraints = Dialect.sql_check_constraints

    # Schemas come from pg_namespace filtered on USAGE, not information_schema.schemata (#558). On
    # Redshift that view lists only the schemas the user OWNS, so a read-only role that can read every
    # view in a schema listed none, and discovery reported "no tables discovered" for a catalog that
    # was not locked down at all. svv_all_schemas is no better: it lists every database's schemas.
    # `LEFT(…, 3)` rather than LIKE 'pg\_%', so no escape character depends on the server's settings.
    def sql_schemas(self) -> str:
        excl = ", ".join(self.quote_lit(s) for s in self._SYS_SCHEMAS)
        return (
            "SELECT nspname AS schema_name FROM pg_namespace "
            "WHERE has_schema_privilege(nspname, 'USAGE') "
            f"AND nspname NOT IN ({excl}) AND LEFT(nspname, 3) <> 'pg_' "
            "ORDER BY nspname"
        )

    # Columns come from svv_columns, not information_schema.columns. A late-binding view (CREATE
    # VIEW ... WITH NO SCHEMA BINDING) is listed in information_schema.tables but its columns are not
    # in information_schema.columns, so a schema built from such views introspected as tables with no
    # columns. svv_columns lists the columns of tables, views and late-binding views alike, under the
    # same field names.
    def sql_columns(self, schema: str, table: str) -> str:
        return (
            "SELECT column_name, data_type, is_nullable, ordinal_position, "
            "numeric_scale FROM svv_columns "
            f"WHERE table_schema = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)} ORDER BY ordinal_position"
        )

    def sql_columns_bulk(self, schemas: list[str]) -> Optional[str]:
        if not schemas:
            return None
        inlist = ", ".join(self.quote_lit(s) for s in schemas)
        return (
            "SELECT table_schema, table_name, column_name, data_type, "
            "numeric_scale, ordinal_position FROM svv_columns "
            f"WHERE table_schema IN ({inlist}) "
            "ORDER BY table_schema, table_name, ordinal_position"
        )

    def duration_days_expr(self, start: str, end: str, *, start_type: str = "timestamp",
                           end_type: str = "timestamp") -> str:
        return f"DATEDIFF('day', {start}, {end})"

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT tbl_rows AS estimated_rows FROM svv_table_info "
            f"WHERE \"schema\" = {self.quote_lit(schema)} "
            f"AND \"table\" = {self.quote_lit(table)}"
        )


class MySQL(Dialect):
    name = "MySQL"
    backslash_escapes = True
    duration_rounds = True
    reserved_extra = frozenset({
        "add", "alter", "change", "condition", "cube", "cume_dist", "database", "databases", "dec",
        "dense_rank", "div", "dual", "empty", "explain", "first_value", "function", "generated",
        "groups", "if", "ignore", "interval", "key", "keys", "kill", "lag", "last_value", "lateral",
        "lead", "leave", "lines", "load", "lock", "long", "loop", "match", "mod", "nth_value",
        "ntile", "of", "option", "out", "over", "partition", "percent_rank", "procedure", "range",
        "rank", "read", "recursive", "regexp", "release", "rename", "repeat", "replace", "require",
        "restrict", "return", "revoke", "rlike", "row", "row_number", "rows", "schema", "separator",
        "show", "signal", "spatial", "sql", "system", "trigger", "undo", "unlock", "unsigned",
        "usage", "use", "varying", "virtual", "while", "write", "xor", "year_month", "zerofill",
    })

    def sql_row_count(self, schema: str, table: str) -> Optional[str]:
        # No exact-count fallback here until introspection reads carry this engine's own bound
        # (#459): a count is billed by bytes scanned on BigQuery and keeps running server-side
        # after a client timeout on MySQL. A view stays unsized, as it was before.
        return None
    _SYS_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")

    def quote_ident(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def duration_days_expr(self, start: str, end: str, *, start_type: str = "timestamp",
                           end_type: str = "timestamp") -> str:
        return f"DATEDIFF({end}, {start})"

    def sql_unique_keys(self, schema: str, table: str) -> Optional[str]:
        # Every unique index (a UNIQUE constraint is one). sub_part set means a prefix index, which
        # only makes the prefix unique; a functional index part has no column_name.
        return (
            "SELECT index_name AS constraint_name, column_name FROM information_schema.statistics "
            f"WHERE table_schema = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)} "
            "AND non_unique = 0 AND index_name <> 'PRIMARY' "
            "AND index_name NOT IN (SELECT index_name FROM information_schema.statistics "
            f"WHERE table_schema = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)} "
            "AND (sub_part IS NOT NULL OR column_name IS NULL)) "
            "ORDER BY index_name, seq_in_index"
        )

    def sql_foreign_keys(self, schema: str) -> str:
        # MySQL exposes the target (incl. its schema) directly on key_column_usage.
        return (
            "SELECT table_name AS from_table, column_name AS from_column, "
            "table_schema AS from_schema, "
            "referenced_table_name AS to_table, referenced_column_name AS to_column, "
            "referenced_table_schema AS to_schema, "
            "constraint_name AS constraint_name, ordinal_position AS key_ordinal "
            "FROM information_schema.key_column_usage "
            f"WHERE table_schema = {self.quote_lit(schema)} "
            "AND referenced_table_name IS NOT NULL "
            "ORDER BY table_name, constraint_name, ordinal_position"
        )

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT table_rows AS estimated_rows FROM information_schema.tables "
            f"WHERE table_schema = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)}"
        )

    def sql_enum_values(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT column_name, column_type FROM information_schema.columns "
            f"WHERE table_schema = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)} "
            "AND data_type = 'enum' ORDER BY ordinal_position"
        )

    case_insensitive_compare = True
    accent_insensitive_compare = True

    def sql_check_constraints(self, schema: str, table: str) -> Optional[str]:
        # check_constraints names no table on MySQL 8, so the table comes from table_constraints;
        # a name used by two of the schema's CHECKs is skipped rather than risk the other's text.
        # MariaDB has no `enforced` column, so there this fails and the fallback runs. Before
        # MySQL 8.0.16 the view doesn't exist; both fail and nothing is declared.
        return (
            "SELECT cc.check_clause FROM information_schema.check_constraints cc "
            "JOIN information_schema.table_constraints tc "
            "ON tc.constraint_schema = cc.constraint_schema "
            "AND tc.constraint_name = cc.constraint_name "
            "WHERE tc.constraint_type = 'CHECK' AND tc.enforced = 'YES' "
            f"AND tc.table_schema = {self.quote_lit(schema)} "
            f"AND tc.table_name = {self.quote_lit(table)} "
            "AND (SELECT COUNT(*) FROM information_schema.table_constraints t2 "
            "WHERE t2.constraint_schema = tc.constraint_schema "
            "AND t2.constraint_name = tc.constraint_name AND t2.constraint_type = 'CHECK') = 1"
        )

    def sql_check_constraints_fallback(self, schema: str, table: str) -> Optional[str]:
        # MariaDB's check_constraints names the table itself, and enforces every CHECK.
        return (
            "SELECT cc.check_clause FROM information_schema.check_constraints cc "
            f"WHERE cc.constraint_schema = {self.quote_lit(schema)} "
            f"AND cc.table_name = {self.quote_lit(table)}"
        )


class Snowflake(Dialect):
    name = "Snowflake"
    folds_unquoted = "upper"
    backslash_escapes = True
    unique_enforced = False  # Snowflake declares unique keys but does not enforce them
    duration_rounds = True
    reserved_extra = frozenset({
        "account", "alter", "connect", "connection", "database", "gscluster", "ilike", "increment",
        "issue", "lateral", "localtime", "localtimestamp", "minus", "of", "organization", "qualify",
        "regexp", "revoke", "rlike", "row", "rows", "sample", "schema", "set", "some", "start",
        "tablesample", "trigger", "try_cast", "view",
    })
    _SYS_SCHEMAS = ("INFORMATION_SCHEMA",)

    def duration_days_expr(self, start: str, end: str, *, start_type: str = "timestamp",
                           end_type: str = "timestamp") -> str:
        return f"DATEDIFF('day', {start}, {end})"

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT row_count AS estimated_rows FROM information_schema.tables "
            f"WHERE table_schema = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)}"
        )


class DuckDB(Dialect):
    name = "DuckDB"

    def sql_check_constraints(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT expression AS check_clause FROM duckdb_constraints() "
            "WHERE constraint_type = 'CHECK' "
            f"AND schema_name = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)}"
        )

    def sql_unique_keys(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT constraint_index AS constraint_name, unnest(constraint_column_names) AS column_name "
            "FROM duckdb_constraints() WHERE constraint_type = 'UNIQUE' "
            f"AND schema_name = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)} "
            "ORDER BY constraint_index"
        )


class Databricks(Dialect):
    name = "Databricks"
    backslash_escapes = True
    fk_enforced = False  # Unity Catalog constraints are informational
    duration_rounds = True

    def sql_unique_keys(self, schema: str, table: str) -> Optional[str]:
        return None  # Unity Catalog has no unique constraint

    def quote_ident(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def duration_days_expr(self, start: str, end: str, *, start_type: str = "timestamp",
                           end_type: str = "timestamp") -> str:
        return f"datediff({end}, {start})"


class Trino(Dialect):
    name = "Trino"
    fk_enforced = False  # Trino generally has no constraints -> probe FKs
    duration_rounds = True

    def duration_days_expr(self, start: str, end: str, *, start_type: str = "timestamp",
                           end_type: str = "timestamp") -> str:
        return f"date_diff('day', {start}, {end})"

    def sql_primary_keys(self, schema: str, table: str) -> str:
        # Most Trino connectors don't expose PK constraints; return nothing and
        # let probe-mode infer the grain.
        return "SELECT NULL AS column_name WHERE 1=0"

    def sql_foreign_keys(self, schema: str) -> str:
        return ("SELECT NULL AS from_table, NULL AS from_column, NULL AS to_table, NULL AS to_column, "
                "NULL AS constraint_name, NULL AS key_ordinal WHERE 1=0")

    def sql_unique_keys(self, schema: str, table: str) -> Optional[str]:
        return None

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return None  # varies per connector; skip


class SQLServer(Dialect):
    name = "SQLServer"
    limit_style = "top"
    duration_rounds = True
    reserved_extra = frozenset({
        "add", "alter", "backup", "begin", "break", "browse", "bulk", "cascade", "checkpoint",
        "close", "clustered", "coalesce", "commit", "compute", "contains", "continue", "convert",
        "current", "cursor", "database", "dbcc", "deallocate", "declare", "deny", "disk",
        "distributed", "double", "dump", "errlvl", "escape", "exec", "execute", "exit", "external",
        "file", "fillfactor", "freetext", "function", "goto", "holdlock", "identity",
        "identitycol", "if", "key", "kill", "lineno", "load", "merge", "national", "nocheck",
        "nonclustered", "nullif", "of", "off", "offsets", "open", "option", "over", "percent",
        "pivot", "plan", "precision", "print", "proc", "procedure", "public", "raiserror", "read",
        "readtext", "reconfigure", "replication", "restore", "restrict", "return", "revert",
        "revoke", "rollback", "rowcount", "rowguidcol", "rule", "save", "schema", "set", "setuser",
        "shutdown", "some", "statistics", "system_user", "tablesample", "textsize", "top", "tran",
        "transaction", "trigger", "truncate", "tsequal", "unpivot", "updatetext", "use", "varying",
        "view", "waitfor", "while", "within", "writetext",
    })
    _SYS_SCHEMAS = ("sys", "INFORMATION_SCHEMA")
    case_insensitive_compare = True   # the default collations are *_CI_*

    def quote_ident(self, name: str) -> str:
        return "[" + name.replace("]", "]]") + "]"

    def duration_days_expr(self, start: str, end: str, *, start_type: str = "timestamp",
                           end_type: str = "timestamp") -> str:
        return f"DATEDIFF(day, {start}, {end})"

    def sql_unique_keys(self, schema: str, table: str) -> Optional[str]:
        # Unique constraints and unique indexes alike; a filtered index is unique only over its
        # filter, and an INCLUDE column is not part of the key. A disabled index enforces nothing.
        return (
            "SELECT i.name AS constraint_name, col.name AS column_name "
            "FROM sys.indexes i "
            "JOIN sys.tables t ON t.object_id = i.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
            "JOIN sys.columns col ON col.object_id = ic.object_id AND col.column_id = ic.column_id "
            "WHERE i.is_unique = 1 AND i.is_primary_key = 0 AND i.has_filter = 0 "
            "AND i.is_disabled = 0 AND i.is_hypothetical = 0 AND ic.is_included_column = 0 "
            f"AND s.name = {self.quote_lit(schema)} AND t.name = {self.quote_lit(table)} "
            "ORDER BY i.name, ic.key_ordinal"
        )

    def flag_rate_expr(self, col: str, *, boolean: bool) -> str:
        # A BIT is not a predicate in T-SQL: `CASE WHEN flag THEN` is a syntax error.
        return super().flag_rate_expr(col, boolean=False)

    def sql_foreign_keys(self, schema: str) -> str:
        # sys.foreign_key_columns pairs each column with its referenced column; SQL Server's
        # key_column_usage has no position_in_unique_constraint for the generic query to match on.
        return (
            "SELECT tp.name AS from_table, cp.name AS from_column, sp.name AS from_schema, "
            "tr.name AS to_table, cr.name AS to_column, sr.name AS to_schema, "
            "fk.name AS constraint_name, fkc.constraint_column_id AS key_ordinal, "
            "'FOREIGN KEY' AS constraint_type "
            "FROM sys.foreign_keys fk "
            "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
            "JOIN sys.tables tp ON tp.object_id = fkc.parent_object_id "
            "JOIN sys.schemas sp ON sp.schema_id = tp.schema_id "
            "JOIN sys.columns cp ON cp.object_id = fkc.parent_object_id AND cp.column_id = fkc.parent_column_id "
            "JOIN sys.tables tr ON tr.object_id = fkc.referenced_object_id "
            "JOIN sys.schemas sr ON sr.schema_id = tr.schema_id "
            "JOIN sys.columns cr ON cr.object_id = fkc.referenced_object_id AND cr.column_id = fkc.referenced_column_id "
            f"WHERE sp.name = {self.quote_lit(schema)} "
            "ORDER BY tp.name, fk.name, fkc.constraint_column_id"
        )

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT SUM(ps.row_count) AS estimated_rows "
            "FROM sys.dm_db_partition_stats ps "
            "JOIN sys.tables t ON t.object_id = ps.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            f"WHERE s.name = {self.quote_lit(schema)} AND t.name = {self.quote_lit(table)} "
            "AND ps.index_id IN (0,1)"
        )

    def sql_check_constraints(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT cc.definition AS check_clause FROM sys.check_constraints cc "
            "JOIN sys.tables t ON t.object_id = cc.parent_object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            f"WHERE s.name = {self.quote_lit(schema)} AND t.name = {self.quote_lit(table)} "
            # a disabled or NOCHECK-added constraint says nothing about the rows already stored
            "AND cc.is_disabled = 0 AND cc.is_not_trusted = 0"
        )


class BigQuery(Dialect):
    name = "BigQuery"
    backslash_escapes = True

    def sql_row_count(self, schema: str, table: str) -> Optional[str]:
        # No exact-count fallback here until introspection reads carry this engine's own bound
        # (#459): a count is billed by bytes scanned on BigQuery and keeps running server-side
        # after a client timeout on MySQL. A view stays unsized, as it was before.
        return None

    def __init__(self, region: str = "region-us"):
        self.region = region

    def quote_ident(self, name: str) -> str:
        return "`" + name.replace("`", "") + "`"

    def duration_days_expr(self, start: str, end: str, *, start_type: str = "timestamp",
                           end_type: str = "timestamp") -> str:
        if start_type == end_type == "date":
            return f"DATE_DIFF({end}, {start}, DAY)"
        # TIMESTAMP_DIFF takes neither a DATE nor a DATETIME (both normalize to these types), and
        # a CAST of a TIMESTAMP to TIMESTAMP is a no-op.
        return f"TIMESTAMP_DIFF(CAST({end} AS TIMESTAMP), CAST({start} AS TIMESTAMP), DAY)"

    def duration_guard(self, start: str, end: str, *, start_type: str = "timestamp",
                       end_type: str = "timestamp") -> str:
        # TIMESTAMP_DIFF truncates, so a reversed sub-day pair gives 0. DATE, DATETIME and
        # TIMESTAMP don't compare with each other, so both sides get the cast the difference uses.
        if start_type == end_type == "date":
            return f"{end} >= {start}"
        return f"CAST({end} AS TIMESTAMP) >= CAST({start} AS TIMESTAMP)"

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

    def sql_columns_bulk(self, schemas: list[str]) -> Optional[str]:
        # BigQuery's INFORMATION_SCHEMA.COLUMNS is per-dataset (region-qualified) — no
        # single cross-dataset query. The discover pass falls back to per-table.
        return None

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

    def sql_unique_keys(self, schema: str, table: str) -> Optional[str]:
        return None  # BigQuery has no unique constraint

    def sql_foreign_keys(self, schema: str) -> str:
        # No REFERENTIAL_CONSTRAINTS here, so the referenced column is found among the referenced
        # table's own primary-key columns (rk) at the FK column's position_in_unique_constraint;
        # matching CONSTRAINT_COLUMN_USAGE on the constraint name alone crossed a composite key's
        # columns. Only this dataset's keys are readable: a single-column key (kn.n = 1) keeps the
        # name-only pairing, which is exact for one column, so a key into another dataset is kept;
        # a composite key there has no positions, so its rows come back with no constraint name
        # and are left ungrouped for review.
        ds = f"`{schema}`.INFORMATION_SCHEMA"
        return (
            "SELECT kcu.table_name AS from_table, kcu.column_name AS from_column, "
            "kcu.table_schema AS from_schema, "
            "ccu.table_name AS to_table, ccu.column_name AS to_column, "
            "ccu.table_schema AS to_schema, "
            "CASE WHEN kn.n = 1 OR rk.column_name IS NOT NULL THEN kcu.constraint_name END AS constraint_name, "
            "kcu.ordinal_position AS key_ordinal "
            f"FROM {ds}.TABLE_CONSTRAINTS tc "
            f"JOIN {ds}.KEY_COLUMN_USAGE kcu "
            "ON tc.constraint_name = kcu.constraint_name "
            f"JOIN {ds}.CONSTRAINT_COLUMN_USAGE ccu "
            "ON tc.constraint_name = ccu.constraint_name "
            f"JOIN (SELECT table_name, constraint_name, COUNT(*) AS n FROM {ds}.KEY_COLUMN_USAGE "
            "GROUP BY table_name, constraint_name) kn "
            "ON kn.table_name = kcu.table_name AND kn.constraint_name = kcu.constraint_name "
            "LEFT JOIN (SELECT k.table_schema, k.table_name, k.column_name, k.ordinal_position "
            f"FROM {ds}.KEY_COLUMN_USAGE k JOIN {ds}.TABLE_CONSTRAINTS c "
            "ON c.constraint_name = k.constraint_name AND c.table_name = k.table_name "
            "AND c.constraint_type <> 'FOREIGN KEY') rk "
            "ON rk.table_schema = ccu.table_schema AND rk.table_name = ccu.table_name "
            "AND rk.column_name = ccu.column_name "
            "AND rk.ordinal_position = kcu.position_in_unique_constraint "
            f"LEFT JOIN (SELECT DISTINCT table_schema, table_name FROM {ds}.TABLE_CONSTRAINTS "
            "WHERE constraint_type <> 'FOREIGN KEY') rpk "
            "ON rpk.table_schema = ccu.table_schema AND rpk.table_name = ccu.table_name "
            # a composite key whose referenced key is readable keeps only its position-matched rows
            "WHERE tc.constraint_type = 'FOREIGN KEY' "
            "AND (kn.n = 1 OR rk.column_name IS NOT NULL OR rpk.table_name IS NULL) "
            "ORDER BY kcu.table_name, kcu.constraint_name, kcu.ordinal_position"
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
    reserved_extra = frozenset({
        "abort", "action", "add", "after", "alter", "always", "analyze", "attach", "autoincrement",
        "before", "begin", "cascade", "collate", "commit", "conflict", "current", "database",
        "deferrable", "deferred", "detach", "do", "each", "escape", "exclude", "exclusive",
        "explain", "fail", "filter", "first", "following", "generated", "glob", "groups", "if",
        "ignore", "immediate", "indexed", "initially", "instead", "isnull", "key", "last",
        "match", "materialized", "no", "nothing", "notnull", "nulls", "of", "others", "over",
        "partition", "plan", "pragma", "preceding", "query", "raise", "range", "recursive",
        "regexp", "reindex", "release", "rename", "replace", "restrict", "returning", "rollback",
        "row", "rows", "savepoint", "temp", "temporary", "ties", "transaction", "trigger",
        "unbounded", "vacuum", "view", "virtual", "without",
    })

    def duration_days_expr(self, start: str, end: str, *, start_type: str = "timestamp",
                           end_type: str = "timestamp") -> str:
        return f"julianday({end}) - julianday({start})"

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

    def sql_columns_bulk(self, schemas: list[str]) -> Optional[str]:
        # pragma_table_info is per-table; the discover pass falls back to per-table.
        return None

    def sql_primary_keys(self, schema: str, table: str) -> str:
        return (
            "SELECT name AS column_name FROM pragma_table_info("
            f"{self.quote_lit(table)}) WHERE pk > 0 ORDER BY pk"
        )

    def sql_unique_keys(self, schema: str, table: str) -> Optional[str]:
        # UNIQUE constraints and CREATE UNIQUE INDEX alike; origin 'pk' is the primary key's own
        # index, and a partial index is unique only over its WHERE. An index written with its own
        # COLLATE may be unique under a comparison a join's `=` does not use, so it isn't trusted.
        return (
            "SELECT il.name AS constraint_name, ii.name AS column_name "
            f"FROM pragma_index_list({self.quote_lit(table)}) il "
            "JOIN pragma_index_info(il.name) ii "
            "WHERE il.\"unique\" = 1 AND il.origin <> 'pk' AND il.partial = 0 "
            "AND NOT EXISTS (SELECT 1 FROM sqlite_master m WHERE m.type = 'index' "
            "AND m.name = il.name AND m.sql IS NOT NULL AND UPPER(m.sql) LIKE '%COLLATE%') "
            "ORDER BY il.name, ii.seqno"
        )

    def sql_foreign_keys(self, schema: str) -> str:
        # foreign_key_list is per-table; the engine special-cases SQLite to call
        # sql_foreign_keys_for_table per table. This whole-schema form is unused.
        return ("SELECT NULL AS from_table, NULL AS from_column, NULL AS to_table, NULL AS to_column, "
                "NULL AS constraint_name, NULL AS key_ordinal WHERE 1=0")

    def sql_foreign_keys_for_table(self, table: str) -> str:
        # `id` numbers each foreign key of the table and `seq` its columns, so they group a key.
        return (
            f"SELECT {self.quote_lit(table)} AS from_table, \"from\" AS from_column, "
            "\"table\" AS to_table, \"to\" AS to_column, "
            "id AS constraint_name, seq AS key_ordinal "
            f"FROM pragma_foreign_key_list({self.quote_lit(table)}) ORDER BY id, seq"
        )

    def sql_check_constraints(self, schema: str, table: str) -> Optional[str]:
        # SQLite keeps no constraint catalog: the CHECKs are read out of the CREATE TABLE text.
        return f"SELECT sql AS check_clause FROM sqlite_master WHERE type = 'table' AND name = {self.quote_lit(table)}"



# ---------------------------------------------------------------------------
# Data-dictionary family — Oracle
# ---------------------------------------------------------------------------


class Oracle(Dialect):
    name = "Oracle"
    limit_style = "fetch"
    folds_unquoted = "upper"
    reserved_extra = frozenset({
        "access", "add", "alter", "audit", "char", "cluster", "comment", "compress", "connect",
        "date", "decimal", "exclusive", "file", "float", "identified", "immediate", "increment",
        "initial", "integer", "level", "lock", "long", "maxextents", "minus", "mlslabel", "mode",
        "modify", "noaudit", "nocompress", "nowait", "number", "of", "offline", "online", "option",
        "pctfree", "prior", "privileges", "public", "raw", "rename", "resource", "row", "rowid",
        "rownum", "rows", "session", "set", "share", "size", "smallint", "start", "successful",
        "synonym", "sysdate", "uid", "validate", "varchar", "varchar2", "view", "whenever",
    })

    def duration_days_expr(self, start: str, end: str, *, start_type: str = "timestamp",
                           end_type: str = "timestamp") -> str:
        if start_type == end_type == "date":
            return f"({end} - {start})"  # Oracle DATE subtraction yields days
        # TIMESTAMP minus TIMESTAMP is an INTERVAL, which AVG rejects.
        return f"(CAST({end} AS DATE) - CAST({start} AS DATE))"

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

    def sql_columns_bulk(self, schemas: list[str]) -> Optional[str]:
        if not schemas:
            return None
        inlist = ", ".join(self.quote_lit(s) for s in schemas)
        return (
            "SELECT owner AS table_schema, table_name, column_name, data_type, "
            "data_scale AS numeric_scale, column_id AS ordinal_position "
            f"FROM all_tab_columns WHERE owner IN ({inlist}) "
            "ORDER BY owner, table_name, column_id"
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

    def sql_unique_keys(self, schema: str, table: str) -> Optional[str]:
        # A disabled or NOVALIDATE constraint does not vouch for the rows already there.
        return (
            "SELECT ac.constraint_name, acc.column_name FROM all_constraints ac "
            "JOIN all_cons_columns acc ON ac.constraint_name = acc.constraint_name "
            "AND ac.owner = acc.owner "
            "WHERE ac.constraint_type = 'U' AND ac.status = 'ENABLED' AND ac.validated = 'VALIDATED' "
            f"AND ac.owner = {self.quote_lit(schema)} "
            f"AND ac.table_name = {self.quote_lit(table)} "
            "ORDER BY ac.constraint_name, acc.position"
        )

    def sql_foreign_keys(self, schema: str) -> str:
        return (
            "SELECT ac.table_name AS from_table, acc.column_name AS from_column, "
            "ac.owner AS from_schema, "
            "rcc.table_name AS to_table, rcc.column_name AS to_column, rcc.owner AS to_schema, "
            "ac.constraint_name AS constraint_name, acc.position AS key_ordinal "
            "FROM all_constraints ac "
            "JOIN all_cons_columns acc ON ac.constraint_name = acc.constraint_name "
            "AND ac.owner = acc.owner "
            "JOIN all_cons_columns rcc ON ac.r_constraint_name = rcc.constraint_name "
            "AND ac.r_owner = rcc.owner "
            "AND acc.position = rcc.position "
            "WHERE ac.constraint_type = 'R' "
            f"AND ac.owner = {self.quote_lit(schema)} "
            "ORDER BY ac.table_name, ac.constraint_name, acc.position"
        )

    def sql_row_estimate(self, schema: str, table: str) -> Optional[str]:
        return (
            "SELECT num_rows AS estimated_rows FROM all_tables "
            f"WHERE owner = {self.quote_lit(schema)} "
            f"AND table_name = {self.quote_lit(table)}"
        )

    def sql_check_constraints(self, schema: str, table: str) -> Optional[str]:
        # Type 'C' also holds every NOT NULL ("COL" IS NOT NULL); the parser ignores those.
        return (
            "SELECT search_condition AS check_clause FROM all_constraints "
            "WHERE constraint_type = 'C' AND status = 'ENABLED' AND validated = 'VALIDATED' "
            f"AND owner = {self.quote_lit(schema)} "
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
