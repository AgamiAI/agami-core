# SQL Dialect Rules

Database-specific syntax rules. Read the section matching your datasource's `database_type` from the YAML config.

## Contents
- PostgreSQL
- Amazon Redshift
- Snowflake
- MySQL / MariaDB
- SQLite
- BigQuery
- SQL Server

---

## PostgreSQL

PostgreSQL has the richest SQL feature set. Prefer native PostgreSQL features when available.

### Date Functions
- `DATE_TRUNC('month', date)` — truncate to precision (year, quarter, month, week, day)
- `CURRENT_DATE` — current date (no parentheses)
- `NOW()` or `CURRENT_TIMESTAMP` — current timestamp
- `date + INTERVAL '1 month'` — interval arithmetic (NOT DATEADD)
- `AGE(end_date, start_date)` — interval difference
- `EXTRACT(YEAR FROM date)` — extract date part
- Do NOT use `DATEDIFF` or `DATEADD` (those are Redshift/SQL Server)

### Conditional Aggregation
- Use `FILTER` clause: `COUNT(*) FILTER (WHERE status = 'active')` — cleaner than CASE
- `SUM(amount) FILTER (WHERE is_won = true)`

### Booleans
- Native BOOLEAN type: use `true`/`false` literals directly
- `WHERE is_active = true` (not `= 1` or `= 'true'`)

### String Aggregation
- `STRING_AGG(name, ', ' ORDER BY name)` — NOT LISTAGG

### DISTINCT ON
- `SELECT DISTINCT ON (account_id) * FROM contacts ORDER BY account_id, created_date DESC`
- Use for "latest per group" without subqueries

### Window Functions
- Full support: ROW_NUMBER(), RANK(), LAG(), LEAD(), etc.
- No QUALIFY clause — use subquery: `SELECT * FROM (SELECT *, ROW_NUMBER() OVER (...) AS rn FROM t) sub WHERE rn = 1`

### Other
- `GENERATE_SERIES(start, end, step)` for sequences and date ranges
- `LATERAL` joins supported for correlated subqueries
- Recursive CTEs with `WITH RECURSIVE`
- `PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY col)` for median

---

## Amazon Redshift

PostgreSQL-based but with significant restrictions. Many PostgreSQL features are NOT available.

### Date Functions
- `DATEDIFF(unit, start, end)` — returns integer difference (day, month, year, etc.)
- `DATEADD(unit, amount, date)` — adds interval
- `DATE_TRUNC('unit', date)` — truncates to precision
- `GETDATE()` or `CURRENT_DATE` — current date
- Do NOT use `date + INTERVAL` syntax

### Conditional Aggregation
- **NO FILTER clause** — use CASE inside aggregates:
  - `SUM(CASE WHEN status = 'Open' THEN 1 ELSE 0 END)` for conditional counts
  - For `AVG`, use `NULL` in ELSE (not 0) to exclude non-matching rows

### String Aggregation
- `LISTAGG(column, ', ') WITHIN GROUP (ORDER BY col)` — NOT STRING_AGG
- **LISTAGG does NOT support DISTINCT** — pre-deduplicate in a CTE:
  ```sql
  WITH deduped AS (SELECT DISTINCT name FROM t)
  SELECT LISTAGG(name, ', ') WITHIN GROUP (ORDER BY name) FROM deduped
  ```
- **CRITICAL**: Cannot use LISTAGG in same SELECT as COUNT(DISTINCT) or other DISTINCT aggregates. Pre-deduplicate in a CTE or put them in separate CTEs then JOIN.

### Window Functions
- Standard window functions supported
- **No QUALIFY clause** — use a subquery instead:
  ```sql
  SELECT * FROM (SELECT *, ROW_NUMBER() OVER (...) AS rn FROM t) sub WHERE rn = 1
  ```
- Cannot use window functions in same SELECT that has GROUP BY — aggregate first in subquery

### Not Supported
- No `DISTINCT ON` — use ROW_NUMBER() in a subquery
- No `GENERATE_SERIES` — use recursive CTEs
- No `LATERAL` joins
- No `FILTER` clause on aggregates
- Limited ARRAY support

### Boolean Handling
- BOOLEAN type exists but sometimes stored as INT. Use explicit `true`/`false` or `1`/`0`.

---

## Snowflake

Snowflake uppercases all unquoted identifiers. Use UPPERCASE for column/table names unless schema uses quoted identifiers.

### Date Functions
- `DATE_TRUNC('month', date)` — same as PostgreSQL
- `DATEADD(unit, amount, date)` — NOT interval arithmetic
- `DATEDIFF(unit, start, end)` — date difference
- `CURRENT_DATE()` — with parentheses (unlike PostgreSQL)
- `TO_DATE(string, format)`, `TO_TIMESTAMP(string, format)`
- Do NOT use `date + INTERVAL` syntax

### Conditional Aggregation
- No FILTER clause — use CASE inside aggregates
- `IFF(condition, true_val, false_val)` — shorthand for simple conditionals

### String Aggregation
- `LISTAGG(name, ', ') WITHIN GROUP (ORDER BY name)` — NOT STRING_AGG

### QUALIFY Clause
- **Natively supported**: `SELECT *, ROW_NUMBER() OVER (...) AS rn FROM t QUALIFY rn = 1`
- Prefer QUALIFY over subquery wrapper

### Type Conversion
- `TRY_CAST(value AS type)` — safe cast (returns NULL on failure)
- `value::type` — standard cast

### Case-Insensitive Matching
- `ILIKE` for case-insensitive LIKE: `WHERE name ILIKE '%search%'`

### Semi-Structured Data
- `column:field::type` — colon path for VARIANT columns
- `LATERAL FLATTEN(input => array_col)` — expand arrays to rows

### Not Supported
- No `DISTINCT ON`
- No `GENERATE_SERIES` — use `TABLE(GENERATOR(ROWCOUNT => N))`
- No `FILTER` clause
- No `LATERAL` joins (except with FLATTEN)

---

## MySQL / MariaDB

### Date Functions
- `DATE_FORMAT(date, '%Y-%m')` — format dates
- `DATE_ADD(date, INTERVAL 1 MONTH)` — add interval
- `DATEDIFF(end, start)` — days between (days only, not arbitrary units)
- `TIMESTAMPDIFF(unit, start, end)` — difference in specific unit
- `NOW()`, `CURDATE()` — current timestamp / date
- `YEAR(date)`, `MONTH(date)`, `DAY(date)` — extract parts

### Quoting
- Use backticks for identifiers: `` `table_name`.`column_name` ``
- Single quotes for strings only

### String Aggregation
- `GROUP_CONCAT(name ORDER BY name SEPARATOR ', ')` — NOT STRING_AGG or LISTAGG

### LIMIT
- `LIMIT N` at end of query (same as PostgreSQL)
- `LIMIT N OFFSET M` for pagination

### Window Functions
- Supported in MySQL 8.0+ and MariaDB 10.2+
- No QUALIFY clause — use subquery

### CTEs
- Supported in MySQL 8.0+ (including recursive)
- Not available in MySQL 5.7 — use subqueries

### Boolean
- `TINYINT(1)` used as boolean. `TRUE`/`FALSE` keywords work.

---

## SQLite

### Date Functions
- `date('now')` — current date
- `datetime('now')` — current timestamp
- `date(col, '+1 month')` — date arithmetic
- `strftime('%Y-%m', date_col)` — format dates
- No DATE_TRUNC — use strftime for truncation

### Quoting
- Double quotes for identifiers, single quotes for strings
- Backticks also accepted

### Types
- Dynamic typing — all columns can hold any type
- No native BOOLEAN, DATE, or TIMESTAMP types — stored as TEXT, INTEGER, or REAL

### Limitations
- No window functions in older versions (added in 3.25.0)
- No RIGHT JOIN or FULL OUTER JOIN
- No ALTER TABLE DROP COLUMN (before 3.35.0)
- No TRUNCATE — use `DELETE FROM`

### String Aggregation
- `GROUP_CONCAT(name, ', ')` — simpler than others, no ORDER BY option

---

## BigQuery

Dialect is **GoogleSQL**. Identifiers are case-insensitive; type names in `CAST` are strict (use `INT64`, not `INTEGER`).

### Identifier quoting
- Backticks quote identifiers: `` `my_col` ``. Single quotes are for string literals only — never for identifiers.
- Fully-qualified table refs: `` `project-id.dataset.table` `` (project IDs often contain hyphens, which **require** backticks).
- Backticks are **required** when an identifier contains a hyphen, starts with a digit, or collides with a reserved word.
- Unquoted identifiers are case-insensitive; backtick-quoted identifiers are still case-insensitive (BigQuery preserves the case but matches case-insensitively).
- Three-part name in `FROM`: `` FROM `proj.dataset.table` `` (one set of backticks around the whole path is the safest form).

### Data types
- `STRING` — variable-length UTF-8 text.
- `BYTES` — raw binary.
- `INT64` — 64-bit signed integer. **In `CAST`, use `INT64`, not `INTEGER` or `INT`.**
- `FLOAT64` — double-precision float. **In `CAST`, use `FLOAT64`, not `FLOAT` or `DOUBLE`.**
- `NUMERIC` — fixed-point decimal, 38 digits precision / 9 scale. Use for money.
- `BIGNUMERIC` — extended fixed-point, 76.76 digits precision / 38 scale. Use for very high-precision arithmetic.
- `BOOL` — boolean. Literals `TRUE` / `FALSE`. (`BOOLEAN` is not the canonical name.)
- `DATE` — calendar date, no time, no zone.
- `TIME` — wall-clock time, no date, no zone.
- `DATETIME` — date + time, **no zone**. Use when timezone is not meaningful.
- `TIMESTAMP` — absolute instant, microsecond precision, UTC-anchored. Use for event times.
- `INTERVAL` — duration between two date/time points; built via `INTERVAL n part` literals.
- `GEOGRAPHY` — geospatial value (point/line/polygon) on the WGS84 sphere.
- `JSON` — structured JSON document; access fields with dot/bracket paths.
- `RANGE<T>` — bounded interval where `T` is `DATE`, `DATETIME`, or `TIMESTAMP`.
- `ARRAY<T>` — ordered list of `T`. **Arrays of arrays are not allowed**; wrap in a `STRUCT`.
- `STRUCT<name1 T1, name2 T2, ...>` — record with named, typed fields.

### Type conversion
- `CAST(expr AS type)` — throws on failure. Type name must be canonical (`INT64`, `FLOAT64`, `STRING`, `DATE`, etc.).
- `SAFE_CAST(expr AS type)` — returns `NULL` on failure instead of throwing. Prefer this for user-supplied input.
- `PARSE_DATE(format, string)` — e.g. `PARSE_DATE('%Y-%m-%d', '2024-01-15')`.
- `PARSE_DATETIME(format, string)` — e.g. `PARSE_DATETIME('%Y-%m-%d %H:%M:%S', '2024-01-15 14:30:00')`.
- `PARSE_TIME(format, string)` — e.g. `PARSE_TIME('%H:%M:%S', '14:30:00')`.
- `PARSE_TIMESTAMP(format, string[, timezone])` — e.g. `PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', '2024-01-15 14:30:00', 'UTC')`.
- `PARSE_NUMERIC(string)`, `PARSE_BIGNUMERIC(string)`, `PARSE_JSON(string)`.
- `FORMAT_DATE(format, date)`, `FORMAT_DATETIME(format, datetime)`, `FORMAT_TIME(format, time)`, `FORMAT_TIMESTAMP(format, ts[, timezone])`.
- Format strings use `strftime`-style tokens (`%Y %m %d %H %M %S`).

### Date / time functions

**HARD RULE — match the function family to the column's actual type.** BigQuery has FOUR distinct date / time types and they are NOT interchangeable in arithmetic:

| Column type | Use this family | Current-time function |
|---|---|---|
| `DATE` | `DATE_TRUNC`, `DATE_ADD`, `DATE_SUB`, `DATE_DIFF` | `CURRENT_DATE()` |
| `DATETIME` (no timezone) | `DATETIME_TRUNC`, `DATETIME_ADD`, `DATETIME_SUB`, `DATETIME_DIFF` | `CURRENT_DATETIME()` |
| `TIMESTAMP` (timezone-aware, absolute point in time) | `TIMESTAMP_TRUNC`, `TIMESTAMP_ADD`, `TIMESTAMP_SUB`, `TIMESTAMP_DIFF` | `CURRENT_TIMESTAMP()` |
| `TIME` (time of day, no date) | `TIME_TRUNC`, `TIME_ADD`, `TIME_SUB`, `TIME_DIFF` | `CURRENT_TIME()` |

**Mixing families fails to compile.** A query like `WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 180 DAY)` against a `DATETIME` column raises *"No matching signature for operator >= for argument types: DATETIME, TIMESTAMP"*. The fix is to switch to the column's family: `WHERE created_at >= DATETIME_SUB(CURRENT_DATETIME(), INTERVAL 180 DAY)`.

**Before emitting any time-arithmetic SQL, look up the column's actual type** in the loaded model (`agami.original_type` on the field; falls back to `agami.type` mapped to BigQuery: `timestamp` could be DATETIME or TIMESTAMP — `original_type` is the canonical source). Then pick the matching function family. Don't default to `TIMESTAMP_SUB` / `CURRENT_TIMESTAMP()` — the introspect step preserves `original_type` precisely so SQL generation doesn't have to guess.

**Convert between families when you must** (e.g., joining a DATETIME column to a TIMESTAMP literal):
- `DATETIME(timestamp_expr, timezone)` — TIMESTAMP → DATETIME (timezone-naive)
- `TIMESTAMP(datetime_expr, timezone)` — DATETIME → TIMESTAMP (assumes given timezone)
- `DATE(timestamp_or_datetime_expr)` — strip time, keep date
- `CAST(x AS DATE)` / `CAST(x AS DATETIME)` / `CAST(x AS TIMESTAMP)` — also work

**Argument order quirk:** the unit is a **bare identifier** (not a string), and for `*_TRUNC` it's the **second** arg; for `*_DIFF` it's the **third**.
- `CURRENT_DATE()`, `CURRENT_DATETIME()`, `CURRENT_TIMESTAMP()`. Optional timezone arg: `CURRENT_DATE('America/Los_Angeles')`.
- `DATE(year, month, day)` or `DATE(timestamp, timezone)` or `DATE(datetime)` — DATE constructor.
- `DATE_TRUNC(date_expr, part)` — e.g. `DATE_TRUNC(order_date, MONTH)`. Parts: `DAY`, `WEEK`, `WEEK(MONDAY)`, `ISOWEEK`, `MONTH`, `QUARTER`, `YEAR`, `ISOYEAR`.
- `DATETIME_TRUNC(datetime_expr, part)`, `TIMESTAMP_TRUNC(ts_expr, part[, timezone])`.
- `DATE_ADD(date_expr, INTERVAL n part)` — e.g. `DATE_ADD(d, INTERVAL 1 MONTH)`. `INTERVAL` keyword is required.
- `DATE_SUB(date_expr, INTERVAL n part)`.
- `DATETIME_ADD(dt, INTERVAL n part)`, `DATETIME_SUB(dt, INTERVAL n part)`.
- `TIMESTAMP_ADD(ts, INTERVAL n part)`, `TIMESTAMP_SUB(ts, INTERVAL n part)` — timestamp parts limited to sub-day (`MICROSECOND`/`MILLISECOND`/`SECOND`/`MINUTE`/`HOUR`) + `DAY`.
- `DATE_DIFF(end, start, part)` — returns INT64. Note: `end` first, `start` second.
- `DATETIME_DIFF(end, start, part)`, `TIMESTAMP_DIFF(end, start, part)`.
- `EXTRACT(part FROM date_expr)` — e.g. `EXTRACT(YEAR FROM order_date)`. Use this syntax (with `FROM`), not function-call form.
- `LAST_DAY(date_expr[, part])` — last day of the month (or other part).
- `TIMESTAMP_SECONDS(int)`, `TIMESTAMP_MILLIS(int)`, `TIMESTAMP_MICROS(int)` — epoch → TIMESTAMP.
- `UNIX_SECONDS(ts)`, `UNIX_MILLIS(ts)`, `UNIX_MICROS(ts)` — TIMESTAMP → epoch.

```sql
SELECT
  DATE_TRUNC(order_date, MONTH)        AS month,
  DATE_ADD(order_date, INTERVAL 7 DAY) AS plus_week,
  DATE_DIFF(CURRENT_DATE(), order_date, DAY) AS days_old,
  EXTRACT(YEAR FROM order_date)        AS yr
FROM `proj.ds.orders`;
```

### String functions
- **`||` is NOT a concatenation operator in BigQuery.** Use `CONCAT`.
- `CONCAT(a, b, ...)` — concatenate (any number of args).
- `SUBSTR(value, position[, length])` — 1-based position; negative position counts from end.
- `LENGTH(value)` — bytes for BYTES, characters for STRING. `CHAR_LENGTH` / `CHARACTER_LENGTH` are aliases.
- `LOWER(value)`, `UPPER(value)`.
- `TRIM(value[, characters])`, `LTRIM(value[, characters])`, `RTRIM(value[, characters])`.
- `REPLACE(value, from, to)`.
- `SPLIT(value[, delimiter])` — returns `ARRAY<STRING>`. Default delimiter is `,`.
- `REGEXP_CONTAINS(value, regex)` — returns BOOL. Preferred over `LIKE` for regex.
- `REGEXP_EXTRACT(value, regex[, position[, occurrence]])`.
- `REGEXP_EXTRACT_ALL(value, regex)` — returns `ARRAY<STRING>`.
- `REGEXP_REPLACE(value, regex, replacement)`.
- `STARTS_WITH(value, prefix)`, `ENDS_WITH(value, suffix)` — both return BOOL.
- `STRPOS(value, search)` — 1-based; 0 if not found.
- `LPAD(value, length[, pad])`, `RPAD(value, length[, pad])`, `REPEAT(value, n)`, `REVERSE(value)`.
- `FORMAT(format_string, args...)` — printf-style; e.g. `FORMAT('%d items', n)`.
- `LIKE` / `NOT LIKE` use `%` and `_` wildcards. No `ILIKE` — use `LOWER(x) LIKE LOWER(pat)` or `REGEXP_CONTAINS`.

### Numeric / math
- `ROUND(x[, digits])`, `TRUNC(x[, digits])`, `CEIL(x)` (alias `CEILING`), `FLOOR(x)`.
- `ABS(x)`, `SIGN(x)`, `MOD(x, y)` — **`MOD` is a function, not an operator** (no `%` for modulo).
- `POWER(x, y)` (alias `POW`), `SQRT(x)`, `EXP(x)`, `LN(x)`, `LOG(x[, base])`, `LOG10(x)`.
- `GREATEST(x1, x2, ...)`, `LEAST(x1, x2, ...)` — return `NULL` if **any** argument is `NULL`.
- `SAFE_DIVIDE(x, y)` — returns `NULL` on division by zero (regular `/` throws).
- `SAFE_ADD`, `SAFE_SUBTRACT`, `SAFE_MULTIPLY`, `SAFE_NEGATE` — return `NULL` on overflow.

### Conditional
- `CASE WHEN cond THEN x [WHEN ...] [ELSE y] END` — standard searched CASE.
- `CASE expr WHEN v1 THEN x WHEN v2 THEN y ELSE z END` — simple CASE.
- `IF(cond, then_val, else_val)` — `else_val` is **required** (not optional).
- `IFNULL(x, default)` — returns `default` if `x` is `NULL`.
- `COALESCE(x1, x2, ...)` — first non-NULL.
- `NULLIF(a, b)` — `NULL` if `a = b`, else `a`.

### Aggregation
- `COUNT(*)`, `COUNT(expr)`, `COUNT(DISTINCT expr)`.
- `SUM(x)`, `AVG(x)`, `MIN(x)`, `MAX(x)`.
- `COUNTIF(bool_expr)` — count rows where condition is `TRUE`.
- **No `SUMIF`** — use `SUM(IF(cond, val, 0))` or `SUM(CASE WHEN cond THEN val ELSE 0 END)`.
- `ANY_VALUE(x)` — arbitrary non-null value from the group.
- `LOGICAL_AND(bool_expr)`, `LOGICAL_OR(bool_expr)` — AND/OR aggregation.
- `BIT_AND(int_expr)`, `BIT_OR(int_expr)`, `BIT_XOR(int_expr)`.
- `APPROX_COUNT_DISTINCT(x)` — HLL-based approximate distinct count.
- `ARRAY_AGG(expr [IGNORE NULLS] [ORDER BY ...] [LIMIT n])` — aggregate into an `ARRAY`. `ARRAY_AGG` **errors on NULL inputs unless `IGNORE NULLS` is used**.
- `STRING_AGG(expr [, delimiter] [ORDER BY ...] [LIMIT n])` — concatenates string values with delimiter (default `,`).
- BigQuery does **not** have a Postgres-style `FILTER (WHERE ...)` aggregate clause — use `IF`/`CASE` inside the aggregate.

### Window functions
- Syntax: `func(...) OVER ([PARTITION BY ...] [ORDER BY ...] [frame_clause])`.
- Frame clause: `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`, `ROWS BETWEEN n PRECEDING AND n FOLLOWING`, `RANGE BETWEEN ...`. Bounds: `UNBOUNDED PRECEDING`, `n PRECEDING`, `CURRENT ROW`, `n FOLLOWING`, `UNBOUNDED FOLLOWING`.
- Numbering: `ROW_NUMBER()`, `RANK()`, `DENSE_RANK()`, `NTILE(n)`, `PERCENT_RANK()`, `CUME_DIST()`.
- Navigation: `LAG(expr[, offset[, default]])`, `LEAD(expr[, offset[, default]])`, `FIRST_VALUE(expr [IGNORE NULLS|RESPECT NULLS])`, `LAST_VALUE(expr [IGNORE NULLS|RESPECT NULLS])`, `NTH_VALUE(expr, n)`.
- Percentiles (as window funcs): `PERCENTILE_CONT(expr, fraction)`, `PERCENTILE_DISC(expr, fraction)` — these require `OVER()` in BigQuery; they are not aggregate functions.
- **`QUALIFY` IS supported.** Use it to filter on window-function results without a subquery wrapper. Requires at least one of `WHERE`, `GROUP BY`, `HAVING`, or a window function in the SELECT to be present.

```sql
SELECT customer_id, order_date, total
FROM `proj.ds.orders`
QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_date DESC) = 1;
```

### SELECT extensions
- `SELECT * EXCEPT (col1, col2) FROM t` — all columns except the listed ones.
- `SELECT * REPLACE (expr AS col) FROM t` — replace one column's expression while keeping `*`.
- `AS` keyword is optional for aliases (`SELECT x alias_name`), but writing `AS` explicitly is recommended.
- **Column aliases declared in `SELECT` are referenceable in `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY`, and `QUALIFY`** — a BigQuery (and Snowflake) convenience that does NOT work in Postgres/Redshift.
- `SELECT AS STRUCT ...` / `SELECT AS VALUE ...` — produce a struct-typed or value-typed row (used inside `ARRAY(...)` subqueries).

### FROM / JOIN
- `[INNER] JOIN`, `LEFT [OUTER] JOIN`, `RIGHT [OUTER] JOIN`, `FULL [OUTER] JOIN`, `CROSS JOIN`.
- Comma in `FROM` (`FROM a, b`) is treated as `CROSS JOIN` — allowed but discouraged for clarity.
- Join condition: `ON cond` or `USING (col_list)`.
- **`LATERAL` is NOT a keyword in BigQuery.** Correlation across `FROM` items happens implicitly via `UNNEST` of a struct/array column, or via correlated subqueries in `SELECT`/`WHERE`.
- `UNNEST(array_expr)` in `FROM` flattens arrays into rows: `FROM t, UNNEST(t.tags) AS tag`. Add positions with `WITH OFFSET`: `FROM t, UNNEST(t.tags) AS tag WITH OFFSET AS pos`.
- The comma between `t` and `UNNEST(...)` is the BigQuery idiom for the implicit lateral join; do **not** write `CROSS JOIN UNNEST` with an `ON` clause.

### WHERE / GROUP BY / HAVING
- `WHERE bool_expr` — column aliases from `SELECT` are visible here (unlike Postgres).
- `GROUP BY col, col2, ...` or `GROUP BY 1, 2` (ordinal references) or `GROUP BY ALL` (auto-group by all non-aggregated `SELECT` items — newer BigQuery feature, supported).
- `HAVING bool_expr` — runs after `GROUP BY`; can reference `SELECT` aliases and aggregates.
- `GROUP BY ROLLUP(...)`, `GROUP BY CUBE(...)`, `GROUP BY GROUPING SETS(...)` — all supported.

### ORDER BY / LIMIT
- `ORDER BY col [ASC|DESC] [NULLS FIRST | NULLS LAST]` — both `NULLS FIRST` and `NULLS LAST` are supported.
- Default null ordering: `NULLS FIRST` for `ASC`, `NULLS LAST` for `DESC`.
- `LIMIT n` and `LIMIT n OFFSET m` — `OFFSET` is supported only with `LIMIT`. Use `LIMIT`, not `TOP N`.

### Set operators
- **You must specify `ALL` or `DISTINCT`** — bare `UNION`, `INTERSECT`, `EXCEPT` are **not** valid.
- `UNION ALL`, `UNION DISTINCT`.
- `INTERSECT DISTINCT` (no `INTERSECT ALL`).
- `EXCEPT DISTINCT` (no `EXCEPT ALL`).
- Inputs must have the same column count; types must be compatible by position (column names come from the first query).

### CTEs (WITH)
- `WITH name AS (SELECT ...), name2 AS (SELECT ...) SELECT ...` — standard non-recursive CTE.
- `WITH RECURSIVE` is supported: `WITH RECURSIVE cte AS (base UNION ALL recursive_step) SELECT ...`. Only `UNION ALL` is allowed between base and recursive parts.
- CTEs are scoped to the immediately following statement.

### Arrays
- Array literal: `[1, 2, 3]` or `ARRAY<INT64>[1, 2, 3]`.
- Indexing: `arr[OFFSET(0)]` is **0-based**; `arr[ORDINAL(1)]` is **1-based**. Out-of-bounds throws — use `arr[SAFE_OFFSET(n)]` / `arr[SAFE_ORDINAL(n)]` for `NULL` on out-of-bounds.
- `ARRAY_LENGTH(arr)`, `ARRAY_CONCAT(a, b, ...)`, `ARRAY_TO_STRING(arr, delim[, null_text])`, `ARRAY_REVERSE(arr)`.
- `GENERATE_ARRAY(start, end[, step])` — inclusive on both ends.
- `GENERATE_DATE_ARRAY(start_date, end_date[, INTERVAL n part])`.
- `UNNEST(arr)` flattens an array to rows (use in `FROM`).
- `ARRAY(SELECT ...)` — turn a subquery into an array.
- `ARRAY_AGG(expr ORDER BY ...)` — aggregate rows into an array.

### Structs
- Struct literal: `STRUCT(1 AS a, 'x' AS b)` or untyped tuple `(1, 'x')` (named form is clearer).
- Typed: `STRUCT<a INT64, b STRING>(1, 'x')`.
- Field access: `s.a` (or `t.s.a` when nested under a row).
- Struct equality requires field-by-field comparison; structs cannot be `GROUP BY`-ed directly unless all fields are groupable.

### SAFE function prefix
- `SAFE.<function>(...)` — runs the function and returns `NULL` instead of raising an error.
- Works with most scalar functions: `SAFE.PARSE_DATE('%Y-%m-%d', maybe_bad)`, `SAFE.DIVIDE(...)`, `SAFE.REGEXP_EXTRACT(...)`.
- Does **not** apply to aggregate, analytic, table-valued, or user-defined functions.
- `SAFE_CAST` and `SAFE_DIVIDE` are the dedicated short forms for the two most common cases.

### Reserved words that must be backticked when used as identifiers
`ALL`, `AND`, `ANY`, `ARRAY`, `AS`, `ASC`, `ASSERT_ROWS_MODIFIED`, `AT`, `BETWEEN`, `BY`, `CASE`, `CAST`, `COLLATE`, `CONTAINS`, `CREATE`, `CROSS`, `CUBE`, `CURRENT`, `DEFAULT`, `DEFINE`, `DESC`, `DISTINCT`, `ELSE`, `END`, `ENUM`, `ESCAPE`, `EXCEPT`, `EXCLUDE`, `EXISTS`, `EXTRACT`, `FALSE`, `FETCH`, `FOLLOWING`, `FOR`, `FROM`, `FULL`, `GROUP`, `GROUPING`, `GROUPS`, `HASH`, `HAVING`, `IF`, `IGNORE`, `IN`, `INNER`, `INTERSECT`, `INTERVAL`, `INTO`, `IS`, `JOIN`, `LATERAL`, `LEFT`, `LIKE`, `LIMIT`, `LOOKUP`, `MERGE`, `NATURAL`, `NEW`, `NO`, `NOT`, `NULL`, `NULLS`, `OF`, `ON`, `OR`, `ORDER`, `OUTER`, `OVER`, `PARTITION`, `PRECEDING`, `PROTO`, `QUALIFY`, `RANGE`, `RECURSIVE`, `RESPECT`, `RIGHT`, `ROLLUP`, `ROWS`, `SELECT`, `SET`, `SOME`, `STRUCT`, `TABLESAMPLE`, `THEN`, `TO`, `TREAT`, `TRUE`, `UNBOUNDED`, `UNION`, `UNNEST`, `USING`, `WHEN`, `WHERE`, `WINDOW`, `WITH`, `WITHIN`.

### Not supported / common LLM mistakes
- **No `DISTINCT ON`** — emulate with `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...) = 1` + `QUALIFY`.
- **No `LATERAL` keyword** — use `UNNEST` for array flattening or correlated scalar subqueries.
- **No `||` for string concat** — use `CONCAT(a, b)`.
- **No `SUMIF`** — use `SUM(IF(cond, val, 0))`.
- **No `%` modulo operator** — use `MOD(x, y)`.
- **No `ILIKE`** — use `LOWER(col) LIKE LOWER(pat)` or `REGEXP_CONTAINS`.
- **No `TOP N`** — use `LIMIT N`.
- **No `FILTER (WHERE ...)`** clause on aggregates — use `IF`/`CASE` inside the aggregate or `COUNTIF`.
- **No bare `UNION`** — must say `UNION ALL` or `UNION DISTINCT`. Same for `INTERSECT DISTINCT`, `EXCEPT DISTINCT`.
- **`INTEGER` / `FLOAT` are not canonical** in `CAST` — use `INT64` / `FLOAT64`.
- **`IF(cond, then)` is not valid** — `IF` requires three args; use `IFNULL` if you want a two-arg null-default.
- `DECLARE` / `SET` / `BEGIN ... END` are scripting statements — do not emit them inside a single-statement `SELECT` query.
- No mid-statement semicolons; one statement per query unless using procedural scripting mode.
- `INTERVAL` keyword is required in `*_ADD`/`*_SUB` — `DATE_ADD(d, 1, DAY)` is invalid; the call is `DATE_ADD(d, INTERVAL 1 DAY)`.

---

## SQL Server

### Identifier Quoting
- Use square brackets: `[table_name].[column_name]`

### Date Functions
- `DATEADD(unit, amount, date)`, `DATEDIFF(unit, start, end)`
- `GETDATE()` — current timestamp
- `CAST(date AS DATE)` — truncate to date
- `FORMAT(date, 'yyyy-MM')` — format
- No DATE_TRUNC — use `DATEADD(MONTH, DATEDIFF(MONTH, 0, date), 0)` pattern

### LIMIT
- Use `TOP N` instead of LIMIT: `SELECT TOP 1000 * FROM table`
- Or `OFFSET M ROWS FETCH NEXT N ROWS ONLY` (SQL Server 2012+)

### String Aggregation
- `STRING_AGG(name, ', ')` — SQL Server 2017+ (no `WITHIN GROUP` — ordering requires a subquery)
  ```sql
  SELECT STRING_AGG(name, ', ') FROM (SELECT name FROM t ORDER BY name) sub
  ```
- Older: use `FOR XML PATH('')` trick

### Window Functions
- Full support in modern versions
- No QUALIFY — use subquery

### Boolean
- `BIT` type (0/1), no native BOOLEAN keyword
