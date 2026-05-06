# Introspection Queries

SQL the `connect` skill runs against `information_schema` to build `~/.agami/<dbname>.yaml`. Each query is **pure SQL** — no Python, no driver-specific calls. Runs identically on tier 1 (CLI), tier 2 (DuckDB), tier 3 (Python).

## PostgreSQL

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

## SQLite

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

## How the skill uses these

For each new database (or when the user says "re-introspect"):

1. List tables (filter system schemas).
2. For each table:
   a. Pull columns + types.
   b. Pull primary key.
   c. Pull foreign keys.
   d. Pull row-count estimate (Postgres `pg_stat_user_tables` / MySQL `table_rows` / SQLite count if cheap).
   e. Pull indexes.
3. Build the table entry per [`schema-reference.md`](schema-reference.md).
4. After all tables: validate FKs via live `LEFT JOIN` orphan checks (see [`fk-validation.md`](fk-validation.md)) — drop any FK with high orphan ratio.
5. Validate the model end-to-end (validation rules from [`schema-reference.md`](schema-reference.md)).
6. Write `~/.agami/<dbname>.yaml`.
7. Hand off to the seed-examples step.

The skill executes each query via the chosen tier (CLI / DuckDB / Python) and parses CSV / TSV output. No driver-specific calls.

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
