# Introspection Queries

SQL the `agami-connect` skill runs against `information_schema` to build the per-schema yamls in `<artifacts_dir>/<profile>/`. Each query is **pure SQL** — no Python, no driver-specific calls. Runs identically on the native CLI, DuckDB, or the Python driver.

The `agami-connect` skill runs them in this order:

1. **List schemas** — show the user a multi-select picker of which schemas to introspect (Phase B's schema picker)
2. **List tables** — only within the selected schemas
3. **Columns / PK / FK / row counts** — per table
4. **Sample rows** — `SELECT * FROM <schema>.<table> LIMIT 5` (used by Phase C's description generation)

## PostgreSQL

### List schemas (excluding system + user-private)

```sql
SELECT schema_name
FROM information_schema.schemata
WHERE schema_name NOT IN ('pg_catalog', 'information_schema')
  AND schema_name NOT LIKE 'pg_toast%'
  AND schema_name NOT LIKE 'pg_temp_%'
  AND schema_name NOT LIKE 'pg_%'
ORDER BY schema_name;
```

Postgres treats `public` as the default schema. Pre-check it in the picker.

### List tables (excluding system schemas)

```sql
SELECT
  table_schema,
  table_name
FROM information_schema.tables
WHERE table_type = 'BASE TABLE'
  AND table_schema NOT IN ('pg_catalog', 'information_schema')
  AND table_schema NOT LIKE 'pg_toast%'
  AND table_schema NOT LIKE 'pg_temp_%'
ORDER BY table_schema, table_name;
```

### Columns for a table

```sql
SELECT
  column_name,
  data_type,
  udt_name,
  is_nullable,
  column_default,
  character_maximum_length,
  numeric_precision,
  numeric_scale
FROM information_schema.columns
WHERE table_schema = '{schema}'
  AND table_name   = '{table}'
ORDER BY ordinal_position;
```

### Primary keys

```sql
SELECT
  kcu.table_schema,
  kcu.table_name,
  kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
  AND tc.table_schema = kcu.table_schema
WHERE tc.constraint_type = 'PRIMARY KEY'
  AND tc.table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY kcu.table_schema, kcu.table_name, kcu.ordinal_position;
```

### Foreign keys

```sql
SELECT
  tc.table_schema       AS from_schema,
  tc.table_name         AS from_table,
  kcu.column_name       AS from_column,
  ccu.table_schema      AS to_schema,
  ccu.table_name        AS to_table,
  ccu.column_name       AS to_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
  AND tc.table_schema = kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
  ON ccu.constraint_name = tc.constraint_name
  AND ccu.table_schema = tc.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_schema NOT IN ('pg_catalog', 'information_schema');
```

### Row-count estimates (no full scan)

```sql
SELECT
  schemaname AS schema_name,
  relname    AS table_name,
  n_live_tup AS estimated_row_count
FROM pg_stat_user_tables
ORDER BY n_live_tup DESC;
```

### Indexes

```sql
SELECT
  schemaname AS schema_name,
  tablename  AS table_name,
  indexname,
  indexdef
FROM pg_indexes
WHERE schemaname NOT IN ('pg_catalog', 'information_schema');
```

---

## MySQL / MariaDB

### List schemas (databases)

In MySQL, "schema" and "database" are synonyms — each database is a single schema. The skill lists databases the user has access to and presents them as schemas:

```sql
SELECT schema_name
FROM information_schema.schemata
WHERE schema_name NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
ORDER BY schema_name;
```

Pre-check whichever database the user is currently connected to (the `database` field in their `~/.agami/credentials` profile).

### List tables

```sql
SELECT
  table_schema,
  table_name
FROM information_schema.tables
WHERE table_type = 'BASE TABLE'
  AND table_schema NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
ORDER BY table_schema, table_name;
```

### Columns for a table

```sql
SELECT
  column_name,
  data_type,
  column_type,
  is_nullable,
  column_default,
  character_maximum_length,
  numeric_precision,
  numeric_scale,
  column_key,
  extra
FROM information_schema.columns
WHERE table_schema = '{schema}'
  AND table_name   = '{table}'
ORDER BY ordinal_position;
```

### Primary keys

```sql
SELECT
  table_schema,
  table_name,
  column_name
FROM information_schema.key_column_usage
WHERE constraint_name = 'PRIMARY'
  AND table_schema NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
ORDER BY table_schema, table_name, ordinal_position;
```

### Foreign keys

```sql
SELECT
  table_schema           AS from_schema,
  table_name             AS from_table,
  column_name            AS from_column,
  referenced_table_schema AS to_schema,
  referenced_table_name   AS to_table,
  referenced_column_name  AS to_column
FROM information_schema.key_column_usage
WHERE referenced_table_name IS NOT NULL
  AND table_schema NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys');
```

### Row-count estimates

```sql
SELECT
  table_schema,
  table_name,
  table_rows AS estimated_row_count
FROM information_schema.tables
WHERE table_type = 'BASE TABLE'
  AND table_schema NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
ORDER BY table_rows DESC;
```

### Indexes

```sql
SELECT
  table_schema,
  table_name,
  index_name,
  GROUP_CONCAT(column_name ORDER BY seq_in_index) AS columns
FROM information_schema.statistics
WHERE table_schema NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
GROUP BY table_schema, table_name, index_name;
```

---

## Redshift

Redshift speaks the Postgres wire protocol, so the **Postgres queries above mostly work as-is**, with three tweaks:

1. Use `pg_catalog.svv_table_info` for accurate row-count estimates — Redshift's `pg_stat_user_tables` is unreliable.
   ```sql
   SELECT schema, "table" AS table_name, tbl_rows AS estimated_row_count
   FROM pg_catalog.svv_table_info
   WHERE schema NOT IN ('pg_catalog', 'information_schema')
   ORDER BY tbl_rows DESC;
   ```
2. Use `pg_catalog.svv_columns` if you want sort-key / dist-key info that informs `agami.performance_hints`:
   ```sql
   SELECT schema_name, table_name, column_name, data_type, ordinal_position
   FROM pg_catalog.svv_columns
   WHERE schema_name NOT IN ('pg_catalog', 'information_schema');
   ```
3. Skip the `pg_indexes` query — Redshift doesn't have traditional indexes (sort keys / dist keys take their place; pull those from `svv_columns` if needed).

Foreign keys behave the same (`information_schema.table_constraints`) but are advisory in Redshift — the engine doesn't enforce them, so an FK relationship may have orphans that wouldn't exist in a strictly-enforced Postgres. Run the live join check from [`fk-validation.md`](fk-validation.md) anyway.

For listing schemas, the Postgres query works as-is.

---

## Snowflake

Snowflake's metadata lives in `INFORMATION_SCHEMA` (per-database) and the account-wide `SNOWFLAKE.ACCOUNT_USAGE` views. Prefer `INFORMATION_SCHEMA` (faster, no role/grant hassle) unless you specifically need the account-wide view.

### List schemas

```sql
SHOW SCHEMAS IN DATABASE "<DATABASE>";
```

Or via `INFORMATION_SCHEMA`:

```sql
SELECT SCHEMA_NAME
FROM INFORMATION_SCHEMA.SCHEMATA
WHERE SCHEMA_NAME NOT IN ('INFORMATION_SCHEMA')
ORDER BY SCHEMA_NAME;
```

Snowflake account-level lookup (across databases): `SHOW SCHEMAS IN ACCOUNT;` — slower, requires higher privileges. Default to per-database listing.

Pre-check `PUBLIC` if it exists (Snowflake's default schema for newly-created databases).

### List tables (excluding system schemas)

```sql
SELECT
  TABLE_SCHEMA,
  TABLE_NAME
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_TYPE = 'BASE TABLE'
  AND TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA')
ORDER BY TABLE_SCHEMA, TABLE_NAME;
```

### Columns for a table

```sql
SELECT
  COLUMN_NAME,
  DATA_TYPE,
  IS_NULLABLE,
  COLUMN_DEFAULT,
  CHARACTER_MAXIMUM_LENGTH,
  NUMERIC_PRECISION,
  NUMERIC_SCALE,
  ORDINAL_POSITION
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = '{schema}'
  AND TABLE_NAME = '{table}'
ORDER BY ORDINAL_POSITION;
```

### Primary keys

Snowflake exposes constraints via `SHOW PRIMARY KEYS`:

```sql
SHOW PRIMARY KEYS IN SCHEMA "{database}"."{schema}";
-- Output columns: created_on, database_name, schema_name, table_name, column_name, key_sequence, ...
```

`INFORMATION_SCHEMA.TABLE_CONSTRAINTS` is also available but `SHOW` is the conventional path.

### Foreign keys

```sql
SHOW IMPORTED KEYS IN SCHEMA "{database}"."{schema}";
-- Output columns: pk_database_name, pk_schema_name, pk_table_name, pk_column_name,
--                 fk_database_name, fk_schema_name, fk_table_name, fk_column_name, ...
```

Snowflake foreign keys are **not enforced** by default (informational only) — same caveat as Redshift. Run the live join check before trusting them.

### Row-count estimates

```sql
SELECT
  TABLE_SCHEMA,
  TABLE_NAME,
  ROW_COUNT AS estimated_row_count,
  BYTES,
  CLUSTERING_KEY
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_TYPE = 'BASE TABLE'
  AND TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA')
ORDER BY ROW_COUNT DESC;
```

`ROW_COUNT` and `BYTES` are maintained by Snowflake's metadata service — accurate without a scan. `CLUSTERING_KEY` informs `agami.performance_hints.recommended_filters`.

### Database / schema discovery

If you don't know which database/schema to introspect:

```sql
SHOW DATABASES;
SHOW SCHEMAS IN DATABASE "{database}";
```

Skip `SNOWFLAKE`, `SNOWFLAKE_SAMPLE_DATA`, and any schema named `INFORMATION_SCHEMA` from the introspection scope.

### Snowflake-only quirks

- **Identifier casing**: by default, unquoted identifiers in Snowflake are uppercased. `customers` becomes `CUSTOMERS`. When generating SQL against a Snowflake-introspected model, keep identifiers uppercase unless they were originally created with double-quotes.
- **No `pg_indexes` analog** — Snowflake clusters via `CLUSTERING_KEY` instead. Surface this in `agami.performance_hints` rather than `indexes`.
- **`SHOW` results are session-scoped** — they aren't queryable via JOIN like an `information_schema` view. The skill captures the output from a single run and parses CSV.

---

## SQLite

### List schemas

SQLite has no concept of schemas in the SQL-standard sense. There's a single implicit `main` schema (or `temp` for in-memory tables). The skill **skips the schema picker** for SQLite and writes a single `main.yaml` in the profile directory.

### List tables

```sql
SELECT name FROM sqlite_master
WHERE type = 'table'
  AND name NOT LIKE 'sqlite_%'
ORDER BY name;
```

### Columns for a table

```sql
PRAGMA table_info('{table}');
-- Columns: cid, name, type, notnull, dflt_value, pk
```

### Foreign keys for a table

```sql
PRAGMA foreign_key_list('{table}');
-- Columns: id, seq, table, from, to, on_update, on_delete, match
```

### Indexes for a table

```sql
PRAGMA index_list('{table}');
PRAGMA index_info('{index_name}');
```

---

## Choice-field detection (low-cardinality scan)

For columns that look enum-shaped (small distinct value count), capture all values + their counts as `agami.choice_field`. Persisted in the schema yaml so every future query uses the right literals (`status='shipped'` not `status='Shipped'` not `is_shipped=true`).

**Candidate columns** — only scan columns matching all of:

- Type is `string` or `integer` (skip floats, dates, timestamps, blobs)
- NOT in `primary_key` and NOT in any FK's `from_columns`
- Column name suggests enum (matches `status`, `state`, `type`, `kind`, `category`, `priority`, `tier`, `level`, `mode`, `flag`, `role`, `phase`, `stage`) OR is at most 32 chars and not obviously a free-text name (`name`, `description`, `notes`, `comment`, `body`, `content`, `email`, `address`, `url` are excluded)

**Detection query** (Postgres/MySQL/Snowflake — same shape; quote per dialect):

```sql
SELECT "{column}", COUNT(*) AS cnt
FROM "{schema}"."{table}"
WHERE "{column}" IS NOT NULL
GROUP BY "{column}"
ORDER BY cnt DESC
LIMIT 21;
```

The `LIMIT 21` is the trick: if the result has ≤ 20 rows, the column is enum-like and we capture all values. If it has 21 rows, distinct count > 20 and we skip — it's not a choice_field.

**For very large tables** (`estimated_row_count > 10_000_000`), sample first to keep the scan cheap:

- Postgres: `TABLESAMPLE BERNOULLI(1)` before the GROUP BY
- Snowflake: `SAMPLE (10000 ROWS)` after `FROM`
- MySQL: no native sampling — limit the scan to a recent slice via `LIMIT 100000` on the inner subquery, with a note that very-rare values may be missed
- SQLite: `LIMIT 100000` similar

If sampling is used, the captured choice_field is **best-effort** — the user can hand-edit if a rare value is missing.

**Output mapping.** The display label defaults to the stored value (label = value). Cleaning up labels (`SHIPPED` → `Shipped`) is the user's job via hand-edit or `agami-save-correction` `field_metadata` correction. The skill never invents labels.

```yaml
# example output
custom_extensions:
  - vendor_name: COMMON
    data: '{"agami": {"type": "string", "choice_field": {"pending": "pending", "shipped": "shipped", "delivered": "delivered", "cancelled": "cancelled"}}}'
```

## Sample rows (for Phase C — auto-generated descriptions)

For each table, the skill fetches up to 5 sample rows for use as evidence when auto-generating descriptions:

```sql
-- Generic
SELECT * FROM "{schema}"."{table}" LIMIT 5;
```

For very large Snowflake tables, use the `SAMPLE` clause to avoid scanning a giant prefix:

```sql
SELECT * FROM "{SCHEMA}"."{TABLE}" SAMPLE (5 ROWS);
```

The sample is **never sent in telemetry** and is **never written to disk**. It lives only in the description-generation prompt's context, then is discarded.

## How the skill uses these

For each new database (or when the user says "re-introspect"):

1. **List schemas** — show the user a multi-select picker (Phase B).
2. List tables in the selected schemas (filter system schemas).
3. For each table:
   a. Pull columns + types.
   b. Pull primary key.
   c. Pull foreign keys.
   d. Pull row-count estimate (Postgres `pg_stat_user_tables` / MySQL `table_rows` / SQLite count if cheap).
   e. Pull indexes.
   f. Pull 5 sample rows (Phase C — auto-generated descriptions).
4. **Detect `agami.choice_field`** — for each candidate column (per heuristic above), run the low-cardinality scan and capture values when distinct count ≤ 20.
5. Build the per-schema yaml entries per [`schema-reference.md`](schema-reference.md).
6. After all tables: validate FKs via live `LEFT JOIN` orphan checks (see [`fk-validation.md`](fk-validation.md)) — drop any FK with high orphan ratio.
7. Validate the model end-to-end (directory-mode `validate_semantic_model.py --directory`).
8. Write `<artifacts_dir>/<profile>/index.yaml` + every `<schema>.yaml`.
9. Hand off to the seed-examples step.

The skill executes each query via the chosen tool (native CLI / DuckDB / Python driver) and parses CSV / TSV output. No driver-specific calls.

## Type mapping

Database-specific types collapse to the simple set used by [`schema-reference.md`](schema-reference.md):

| Source type | Maps to |
|---|---|
| `varchar`, `text`, `char`, `nvarchar`, `string`, `uuid`, `enum`, `set` | `string` |
| `int`, `bigint`, `smallint`, `tinyint`, `mediumint`, `integer` | `integer` |
| `decimal`, `numeric`, `real`, `float`, `double`, `money` | `decimal` |
| `timestamp`, `datetime`, `timestamptz` | `timestamp` |
| `date` | `date` |
| `bool`, `boolean`, `bit(1)`, `tinyint(1)` | `boolean` |
| anything else | `string` (with a comment in the description noting the original type) |
