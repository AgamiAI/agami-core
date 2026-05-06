# Database Connection Reference

How `agami` connects to your database. Used by the `connect`, `query-database`, and `save-correction` skills.

## HARD RULES — read first

These are non-negotiable. Skills that read this document must follow them under every circumstance.

1. **Connect ONLY to the host/port/database/user/password in `~/.agami/credentials`** (or `AGAMI_DATABASE_URL`). Never use `localhost` or any other host as a fallback. If the credentials say `host = remote-prod.example.com`, the only acceptable connection is to `remote-prod.example.com` — not also to `localhost` "to see if there's something there".
2. **Never ask the user for connection details in chat.** Credentials live in `~/.agami/credentials` only. If the file is missing, invoke the agami-init skill (which writes a `credentials.example` template the user edits). Never accept host / port / database / user / password values typed inline.
3. **Never scan or guess.** Tool detection is `which <tool>` and `python3 -c 'import <module>'`. Nothing else. No `pgrep`, `ps`, `lsof`, `find /`, `ls /Applications`, port scanning, or hostname guessing. Tool paths are cached in `~/.agami/.config.tool_paths` so subsequent skill invocations don't even re-probe — they read the cached path and use it.
4. **If the cached tool path is broken** (binary moved or uninstalled), surface the failure cleanly and offer to re-detect. Do not silently fall through to localhost-probing or any other discovery technique.

## Contents
- Execution Tiers (pick highest available)
- Tier 1 — Native CLI tool
- Tier 2 — DuckDB universal client
- Tier 3 — Python driver (optional)
- Connection Defaults
- CLI Connection Commands
- Python Driver Fallback
- Reading Credentials
- System Schema Exclusions
- Security Rules

---

## Execution Tiers

`agami` picks the highest-priority tier available for your database type. Falls through to the next tier on error. If no tier works, surfaces the list of options so you can install one.

| # | Tier | When it's the right pick | Pros | Cons |
|---|------|--------------------------|------|------|
| 1 | **Native CLI tool** (`psql`, `mysql`, `sqlite3`) | Most common path on a developer laptop | Fast, idiomatic, no extra layer | Requires the per-DB CLI on `PATH` |
| 2 | **DuckDB universal client** | When you don't have / don't want the native CLI | Single binary install; handles Postgres / MySQL / SQLite / Parquet / CSV | Doesn't natively cover Snowflake, BigQuery, SQL Server, Oracle, Databricks |
| 3 | **Python driver** (`psycopg2`, `pymysql`, …) | If you already have Python set up | Works in environments without the CLI | Adds a Python dependency |

`agami` runs entirely on your machine. There is no hosted/server tier.

### Tier-selection algorithm

Tier detection runs **once**, in the agami-init skill's Phase 3. The result (chosen tier + absolute paths of every detected tool) is persisted in `~/.agami/.config.tool_paths`. Every subsequent skill invocation reads the cached paths — they do NOT re-probe.

Init's tier-selection pseudocode:

```text
db_type := credentials → type   (e.g., "postgres")

# Detect every tier in parallel and cache the absolute path of each.
tool_paths := {
  psql:    which("psql")  || ls /opt/homebrew/Cellar/libpq/*/bin/psql /opt/homebrew/opt/libpq/bin/psql,
  mysql:   which("mysql") || ls /opt/homebrew/opt/mysql-client/bin/mysql,
  sqlite3: which("sqlite3"),
  duckdb:  which("duckdb"),
  python3: which("python3"),
}
tool_imports := {
  psycopg2: python_import_ok("psycopg2"),
  pymysql:  python_import_ok("pymysql"),
}

# Pick the highest tier with the right tool for db_type.
if db_type == "postgres" and tool_paths.psql:                return tier=cli
if db_type == "mysql"    and tool_paths.mysql:               return tier=cli
if db_type == "sqlite"   and tool_paths.sqlite3:             return tier=cli
if db_type in {postgres, mysql, sqlite} and tool_paths.duckdb: return tier=duckdb
if db_type == "postgres" and tool_imports.psycopg2:          return tier=python
if db_type == "mysql"    and tool_imports.pymysql:           return tier=python
if db_type == "sqlite"   and tool_paths.python3:             return tier=python  # stdlib

# Nothing worked.
offer_install()  # AskUserQuestion — never install silently
```

Other skills look up `tier` and `tool_paths.<tool>` from `~/.agami/.config` and use them directly. They do not re-run `which`. If the cached path no longer exists on disk (`! -x "$path"`), they offer to re-detect — they do NOT silently scan or fall back to localhost.

### When all tiers fail

Surface a single, specific error — never a generic "connection failed":

```
No execution path found for your Postgres database:
  ✗ psql not on PATH
  ✗ duckdb not on PATH
  ✗ psycopg2 not importable

Options (listed in recommended order):
  a) Install psql:           `brew install postgresql`       (simplest — most common)
  b) Install DuckDB:         `brew install duckdb`           (universal client, one binary)
  c) Install psycopg2:       `pip install psycopg2-binary`   (only if you prefer Python)

Reply with a/b/c or install manually.
```

---

## Tier 1 — Native CLI tool

The default tier. Most Postgres users already have `psql`; most MySQL users have `mysql`. See **CLI Connection Commands** below for the canonical invocation per database.

---

## Tier 2 — DuckDB universal client

DuckDB ships as a single binary (`brew install duckdb` / `apt install duckdb` / download from duckdb.org). It natively reads from:

- **PostgreSQL** (via the `postgres_scanner` extension — auto-installed on first use)
- **MySQL** (`mysql_scanner`)
- **SQLite** (built-in)
- **File sources**: Parquet, CSV, JSONL, Excel, Arrow, S3

It does **not** natively cover Snowflake, BigQuery, SQL Server, Oracle, or Databricks. For those, DuckDB is not a valid fallback — drop straight to the "all tiers failed" message.

### Offering DuckDB to the user

Only offer DuckDB when:
1. The database type is in `{postgres, mysql, sqlite, duckdb, file}`, AND
2. Tier 1 (native CLI) is not available.

Never install silently — prompt via **AskUserQuestion** with the install command specific to the user's OS. Respect a "no" answer and fall through to the all-tiers-failed error.

### Connecting from DuckDB to Postgres / MySQL

```bash
# Postgres
duckdb <<SQL
  INSTALL postgres_scanner; LOAD postgres_scanner;
  ATTACH 'host=$PGHOST port=$PGPORT dbname=$PGDATABASE user=$PGUSER password=$PGPASSWORD' AS pg (TYPE POSTGRES);
  SELECT * FROM pg.public.<table> LIMIT 10;
SQL
```

```bash
# MySQL
duckdb <<SQL
  INSTALL mysql_scanner; LOAD mysql_scanner;
  ATTACH 'host=$MYSQL_HOST port=$MYSQL_PORT user=$MYSQL_USER password=$MYSQL_PWD database=$MYSQL_DATABASE' AS my (TYPE MYSQL);
  SELECT * FROM my.<table> LIMIT 10;
SQL
```

CSV output: append `-csv` or wrap in `COPY (<query>) TO '/dev/stdout' (FORMAT CSV)`.

---

## Tier 3 — Python driver

Used when neither tier 1 nor tier 2 is available, but Python with the right driver is. The agami skill ships a runtime helper for this:

```bash
python3 plugins/agami/scripts/execute_sql.py --profile <profile> --sql-file /tmp/agami-query.sql
```

`execute_sql.py` reads `~/.agami/credentials` itself, opens a connection via `psycopg2` / `pymysql` / `sqlite3` based on the profile's `type` field, runs the SQL, emits RFC 4180 CSV on stdout. Exit codes communicate the failure category (config, driver missing, connect error, execution error). See [`plugins/agami/scripts/README.md`](../scripts/README.md) for full usage.

Skills should always use `--sql-file` for non-trivial SQL. The `--sql` flag is fine for short statements; `--sql-file` avoids any shell-quoting issues for SQL containing single quotes, backticks, `$`, or backslashes.

The "Python Driver Fallback" section further down shows the inline `python3 -c '...'` form that does the same thing without the helper — useful only if `execute_sql.py` isn't bundled (e.g., a legacy install). Prefer `execute_sql.py`.

---

## Connection Defaults

| Database | Default Port | CLI Tool | Python Driver |
|----------|-------------|----------|---------------|
| PostgreSQL | 5432 | `psql` | `psycopg2` (`pip install psycopg2-binary`) |
| MySQL / MariaDB | 3306 | `mysql` | `pymysql` (`pip install pymysql`) |
| SQLite | N/A (file) | `sqlite3` | built-in `sqlite3` |
| DuckDB | N/A (file) | `duckdb` | built-in or `pip install duckdb` |

v1 supports Postgres + MySQL end-to-end. SQLite works via DuckDB. Other databases (Snowflake, BigQuery, SQL Server, Oracle, Databricks, Redshift, ClickHouse) are deferred — track the v1.1+ roadmap.

---

## CLI Connection Commands

### PostgreSQL
```bash
# Execute a query and return CSV
PGPASSWORD="$password" psql -h "$host" -p "$port" -U "$user" -d "$database" -c "$SQL" --csv

# Execute from a file
PGPASSWORD="$password" psql -h "$host" -p "$port" -U "$user" -d "$database" -f query.sql --csv
```

**Security**: Always use `PGPASSWORD` env var, never `-p password` flag (visible in `ps`).

### MySQL / MariaDB
```bash
# Execute a query (MYSQL_PWD avoids password in process listing)
MYSQL_PWD="$password" mysql -h "$host" -P "$port" -u "$user" "$database" -e "$SQL" --batch --raw

# CSV-like output
MYSQL_PWD="$password" mysql -h "$host" -P "$port" -u "$user" "$database" -e "$SQL" --batch --raw | tr '\t' ','
```

**Security**: Use `MYSQL_PWD` env var instead of `-p"$password"` flag (visible in `ps`).

### SQLite
```bash
sqlite3 "$path" "$SQL" -csv -header
```

---

## Python Driver Fallback

When CLI tools are not available, use Python:

**Never interpolate `$SQL` into the Python source via shell expansion** (e.g., `cur.execute('''$SQL''')` inside a `python3 -c "…"` heredoc). SQL containing single quotes, backticks, `$`, or `\` will break the Python string literal or, worse, inject arbitrary code into the shell-expanded script. Pass the SQL as a positional argument and read it from `sys.argv[1]` instead — shell quotes protect a single arg without the script needing to know anything about its contents.

### PostgreSQL
```bash
export PGHOST="$host" PGPORT="$port" PGUSER="$user" PGPASSWORD="$password" PGDATABASE="$database"
python3 -c '
import psycopg2, csv, sys, os
conn = psycopg2.connect(host=os.environ["PGHOST"], port=os.environ["PGPORT"],
                        user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"],
                        dbname=os.environ["PGDATABASE"])
cur = conn.cursor()
cur.execute(sys.argv[1])
writer = csv.writer(sys.stdout)
writer.writerow([d[0] for d in cur.description])
for row in cur.fetchall():
    writer.writerow(row)
conn.close()
' "$SQL"
```

### MySQL
```bash
export MYSQL_HOST="$host" MYSQL_PORT="$port" MYSQL_USER="$user" MYSQL_PWD="$password" MYSQL_DB="$database"
python3 -c '
import pymysql, csv, sys, os
conn = pymysql.connect(host=os.environ["MYSQL_HOST"], port=int(os.environ["MYSQL_PORT"]),
                       user=os.environ["MYSQL_USER"], password=os.environ["MYSQL_PWD"],
                       database=os.environ["MYSQL_DB"])
cur = conn.cursor()
cur.execute(sys.argv[1])
writer = csv.writer(sys.stdout)
writer.writerow([d[0] for d in cur.description])
for row in cur.fetchall():
    writer.writerow(row)
conn.close()
' "$SQL"
```

A more polished version of these snippets lives at [`plugins/agami/scripts/sample_introspect_postgres.py`](../scripts/sample_introspect_postgres.py) and [`sample_introspect_mysql.py`](../scripts/sample_introspect_mysql.py).

---

## Reading Credentials

Credentials live in `~/.agami/credentials` (an INI-style file, `chmod 600`) or are passed via the `AGAMI_DATABASE_URL` env var. Format spec: [`credentials-format.md`](credentials-format.md).

### Reading the file

```bash
# Refuse to read if too permissive
perms=$(stat -c '%a' ~/.agami/credentials 2>/dev/null || stat -f '%A' ~/.agami/credentials)
case "$perms" in
  600|400) ;;
  *)
    echo "~/.agami/credentials must be chmod 600 (currently $perms). Run: chmod 600 ~/.agami/credentials" >&2
    exit 1
    ;;
esac

# Parse a profile (default: [default])
profile="${AGAMI_PROFILE:-default}"
# Use awk or python to extract host/port/user/password/database/type for the named profile
```

### `AGAMI_DATABASE_URL` override

Standard DSN, parsed by tier 1/2/3 the same way:

```
AGAMI_DATABASE_URL=postgres://user:password@host:5432/database
AGAMI_DATABASE_URL=mysql://user:password@host:3306/database
```

When set, `~/.agami/credentials` is ignored.

---

## System Schema Exclusions

When introspecting databases, exclude system schemas:

| Database | Exclude |
|----------|---------|
| PostgreSQL | `pg_catalog`, `information_schema`, `pg_toast`, `pg_internal`, `pg_temp_*` |
| MySQL/MariaDB | `information_schema`, `mysql`, `performance_schema`, `sys` |
| SQLite | `sqlite_master`, `sqlite_sequence` (filter by name prefix) |

---

## Security Rules

- **NEVER** echo passwords in commands visible in shell history or logs
- Use env var injection (`PGPASSWORD`, `MYSQL_PWD`) instead of command-line flags
- Use `--csv` or `--batch` output modes (not interactive) for predictable parsing
- **Result-set size policy** — default cap is 1000 rows with explicit "show more" prompt. User can override per-query with "top N" or "limit N" framing.
- **NEVER** generate DDL or DML statements (`DROP`, `DELETE`, `INSERT`, `UPDATE`, `ALTER`, etc.)
- Sanitize user input before including in SQL queries
- `~/.agami/credentials` must be `chmod 600`. The `init` skill enforces this; refuse to read otherwise.
