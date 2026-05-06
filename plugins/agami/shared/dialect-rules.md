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

### Identifier Quoting
- Use backticks for project/dataset/table: `` `project.dataset.table` ``
- Column names are case-insensitive

### Date Functions
- `DATE_TRUNC(date, MONTH)` — note: unit is second argument (not string)
- `DATE_ADD(date, INTERVAL 1 MONTH)` — interval syntax
- `DATE_DIFF(end, start, UNIT)` — difference
- `CURRENT_DATE()`, `CURRENT_TIMESTAMP()`
- `FORMAT_DATE('%Y-%m', date)` — format
- `EXTRACT(YEAR FROM date)` — extract part

### String Aggregation
- `STRING_AGG(name, ', ' ORDER BY name)` — same as PostgreSQL

### Conditional Aggregation
- `COUNTIF(condition)` — BigQuery shorthand
- `SUMIF` not available — use `SUM(IF(condition, value, 0))`

### QUALIFY Clause
- Supported: `QUALIFY ROW_NUMBER() OVER (...) = 1`

### Array Functions
- `ARRAY_AGG(value)`, `UNNEST(array)` — native array support
- `STRUCT(field1, field2)` — composite types

### Not Supported
- No `DISTINCT ON`
- No `LATERAL` keyword (use correlated subqueries or UNNEST)

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
