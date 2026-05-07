# Database Connection Reference

How `agami` connects to your database. Used by the `connect`, `query-database`, and `save-correction` skills.

## HARD RULES — read first

These are non-negotiable. Skills that read this document must follow them under every circumstance.

1. **Connect ONLY to the host/port/database/user/password in `~/.agami/credentials`** (or `AGAMI_DATABASE_URL`). Never use `localhost` or any other host as a fallback. If the credentials say `host = remote-prod.example.com`, the only acceptable connection is to `remote-prod.example.com` — not also to `localhost` "to see if there's something there".
2. **Never ask the user for connection details in chat.** Credentials live in `~/.agami/credentials` only. If the file is missing, invoke the agami-init skill (which writes a `credentials.example` template the user edits). Never accept host / port / database / user / password values typed inline.
3. **Never scan or guess.** Tool detection is `which <tool>` and `python3 -c 'import <module>'`. Nothing else. No `pgrep`, `ps`, `lsof`, `find /`, `ls /Applications`, port scanning, or hostname guessing. Tool paths are cached in `~/.agami/.config.tool_paths` so subsequent skill invocations don't even re-probe — they read the cached path and use it.
4. **If the cached tool path is broken** (binary moved or uninstalled), surface the failure cleanly and offer to re-detect. Do not silently fall through to localhost-probing or any other discovery technique.
5. **NEVER put the password (or any credential field) in a Bash command line.** Hosts render Bash tool calls in their UI — anything in the command, env-var assignment, or stdin is visible to anyone scrolling the chat. Use the provider-native auth files written by `scripts/setup_pgauth.py`:
   - For postgres: `PGPASSFILE=$HOME/.agami/.pgpass psql -h <host> -p <port> -U <user> -d <db> -c "$SQL" --csv`
   - For mysql: `mysql --defaults-file=$HOME/.agami/.mysql.cnf --defaults-group-suffix=_<profile> -h <host> -P <port> <db> -e "$SQL" --batch --raw`
   These auth files are chmod 600 in `~/.agami/`. The visible Bash command contains NO password — psql / mysql read the password from the auth file silently. **Patterns that are FORBIDDEN**: `export PGPASSWORD='<literal>'`, `export MYSQL_PWD='<literal>'`, `psql -W <password>`, `mysql -p<password>`, or anything else where the password appears in the command, env assignment, or stdin.

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
db_type := credentials → type   (e.g., "postgres", "redshift", "snowflake")

# Detect every tier in parallel and cache the absolute path of each.
tool_paths := {
  psql:    which("psql")  || ls /opt/homebrew/Cellar/libpq/*/bin/psql /opt/homebrew/opt/libpq/bin/psql,
  mysql:   which("mysql") || ls /opt/homebrew/opt/mysql-client/bin/mysql,
  snowsql: which("snowsql"),
  sqlite3: which("sqlite3"),
  duckdb:  which("duckdb"),
  python3: which("python3"),
}
tool_imports := {
  psycopg2:                    python_import_ok("psycopg2"),
  pymysql:                     python_import_ok("pymysql"),
  snowflake_connector_python:  python_import_ok("snowflake.connector"),
}

# Pick the highest tier with the right tool for db_type.
# postgres / redshift share psql + psycopg2 (Redshift speaks Postgres wire protocol)
if db_type in {postgres, redshift} and tool_paths.psql:                  return tier=cli
if db_type == "mysql"    and tool_paths.mysql:                           return tier=cli
if db_type == "snowflake" and tool_paths.snowsql:                        return tier=cli
if db_type == "sqlite"   and tool_paths.sqlite3:                         return tier=cli
if db_type in {postgres, redshift, mysql, sqlite} and tool_paths.duckdb: return tier=duckdb
if db_type in {postgres, redshift} and tool_imports.psycopg2:            return tier=python
if db_type == "mysql"    and tool_imports.pymysql:                       return tier=python
if db_type == "snowflake" and tool_imports.snowflake_connector_python:   return tier=python
if db_type == "sqlite"   and tool_paths.python3:                         return tier=python  # stdlib

# Nothing worked.
offer_install()  # AskUserQuestion — never install silently
```

DuckDB's `postgres_scanner` extension can also scan Redshift over the wire (since Redshift is Postgres-protocol-compatible). DuckDB cannot scan Snowflake natively in v1.1.

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

| Database | Default Port | CLI Tool | Python Driver | SSL |
|---|---|---|---|---|
| PostgreSQL | 5432 | `psql` | `psycopg2` (`pip install psycopg2-binary`) | `prefer` (default) |
| **Redshift** | **5439** | `psql` (Redshift speaks Postgres wire protocol) | `psycopg2` | **`require`** (default for Redshift) |
| MySQL / MariaDB | 3306 | `mysql` | `pymysql` (`pip install pymysql`) | optional |
| **Snowflake** | 443 (HTTPS) | `snowsql` | `snowflake-connector-python` (`pip install snowflake-connector-python`) | TLS always (managed by client) |
| SQLite | N/A (file) | `sqlite3` | built-in `sqlite3` | n/a |
| DuckDB | N/A (file) | `duckdb` | built-in or `pip install duckdb` | n/a |

v1.1 supports Postgres + Redshift + MySQL + Snowflake + SQLite end-to-end. SQLite also works via DuckDB. Other databases (BigQuery, SQL Server, Oracle, Databricks, ClickHouse) are deferred — track the v1.2+ roadmap.

---

## CLI Connection Commands

### PostgreSQL

```bash
# Ensure the auth file exists for the active profile (idempotent, fast).
# Generates ~/.agami/.pgpass from credentials. Bash command line contains
# NO password.
python3 "$AGAMI_PLUGIN_ROOT/scripts/setup_pgauth.py" --profile "$PROFILE"

# Execute a query and return CSV. PGPASSFILE points at the auth file;
# psql reads the password silently. The bash command itself is password-free.
PGPASSFILE="$HOME/.agami/.pgpass" PGSSLMODE="${sslmode:-prefer}" \
  psql -h "$host" -p "$port" -U "$user" -d "$database" -c "$SQL" --csv

# Execute from a file
PGPASSFILE="$HOME/.agami/.pgpass" PGSSLMODE="${sslmode:-prefer}" \
  psql -h "$host" -p "$port" -U "$user" -d "$database" -f query.sql --csv
```

**Security / SSL**:
- **Never** pass the password on the command line, in `export PGPASSWORD='...'`, or via `-W <password>`. Always use `PGPASSFILE` pointing at `~/.agami/.pgpass` (chmod 600, generated by `scripts/setup_pgauth.py`). The visible Bash command must be password-free.
- Set `PGSSLMODE` from the credentials profile's `sslmode` field. Cloud Postgres providers (Supabase, Neon, RDS in many configs) **require** SSL — set `sslmode = require` in `~/.agami/credentials` or use a DSN with `?sslmode=require` and the parser will pick it up. Default is `prefer` which works for both SSL-required and non-SSL servers.

**Supabase pooler**: the SQLAlchemy-style DSN that Supabase shows (`postgresql+asyncpg://...`) is accepted as-is in the `url = ...` credentials field — see [`credentials-format.md → Supabase`](credentials-format.md). The `+asyncpg` driver suffix is stripped before connecting.

### Redshift

Redshift speaks the PostgreSQL wire protocol, so **psql works as-is**. The only differences from regular Postgres:

- Default port is **5439** (not 5432)
- SSL is **required** by default (`sslmode=require`)
- The hostname is the cluster's full DNS — `<cluster>.<region>.redshift.amazonaws.com` for provisioned clusters, or `<workgroup>.<account>.<region>.redshift-serverless.amazonaws.com` for Redshift Serverless.

`scripts/setup_pgauth.py` writes a `.pgpass` line for Redshift profiles the same as for postgres profiles — no special handling needed.

```bash
# Same invocation as postgres, just with the Redshift host/port and sslmode=require.
PGPASSFILE="$HOME/.agami/.pgpass" PGSSLMODE="require" \
  psql -h "$host" -p 5439 -U "$user" -d "$database" -c "$SQL" --csv
```

`type = redshift` in the credentials profile (or a `redshift://` DSN) sets the right defaults. For psycopg2 (tier 3) the connection params are identical to postgres; agami's `execute_sql.py` routes `type=redshift` through the postgres execution path.

### Snowflake

Snowflake doesn't speak the Postgres wire protocol. It needs its own native CLI (`snowsql`) or the `snowflake-connector-python` Python driver.

#### Tier 1 — `snowsql`

`scripts/setup_pgauth.py` writes a `~/.agami/.snowsql.cnf` config file with a `[connections.<profile>]` block per Snowflake profile in your credentials. The skill invokes snowsql with `--config` pointing at it:

```bash
# Ensure the snowsql config exists (idempotent).
python3 "$AGAMI_PLUGIN_ROOT/scripts/setup_pgauth.py" --profile "$PROFILE"

# Run a query — snowsql reads the password from the config silently.
snowsql --config "$HOME/.agami/.snowsql.cnf" -c "$PROFILE" \
        -q "$SQL" -o output_format=csv -o header=true -o friendly=false -o timing=false
```

The `-o output_format=csv -o header=true` flags produce parseable output. `-o friendly=false -o timing=false` strips the human-friendly banner and timing line so the CSV is clean.

Install snowsql: see <https://docs.snowflake.com/en/user-guide/snowsql-install-config>. macOS: download from Snowflake's website (Homebrew formula isn't official).

#### Tier 3 — `snowflake-connector-python`

```bash
pip install snowflake-connector-python
python3 "$AGAMI_PLUGIN_ROOT/scripts/execute_sql.py" --profile "$PROFILE" --sql-file /tmp/agami-q.sql
```

`execute_sql.py` handles Snowflake natively when `type=snowflake`. Connection params: `account`, `user`, `password` (or `authenticator` for SSO), `warehouse`, `database`, `schema`, `role`. All optional except `account` and `user`; either `password` or `authenticator` is required.

#### Account identifier formats

Snowflake's `account` field is **not** a hostname. Examples:

- `xy12345` — short locator (legacy, AWS US-West-2)
- `xy12345.us-east-1` — locator + region (AWS)
- `xy12345.us-east-1.aws` — locator + region + cloud
- `myorg-myaccount` — newer org-account format (recommended by Snowflake)

The connector / snowsql appends `.snowflakecomputing.com` automatically. Use whatever your Snowflake admin gave you.

### MySQL / MariaDB

```bash
# Ensure the auth file exists for the active profile (idempotent, fast).
# Generates ~/.agami/.mysql.cnf with [client_<profile>] sections. Bash
# command line contains NO password.
python3 "$AGAMI_PLUGIN_ROOT/scripts/setup_pgauth.py" --profile "$PROFILE"

# Execute a query — mysql reads creds from the auth file via --defaults-file
# + --defaults-group-suffix=_<profile>. Visible bash command is password-free.
mysql --defaults-file="$HOME/.agami/.mysql.cnf" \
      --defaults-group-suffix="_$PROFILE" \
      -h "$host" -P "$port" "$database" \
      -e "$SQL" --batch --raw

# CSV-like output
mysql --defaults-file="$HOME/.agami/.mysql.cnf" \
      --defaults-group-suffix="_$PROFILE" \
      -h "$host" -P "$port" "$database" \
      -e "$SQL" --batch --raw | tr '\t' ','
```

**Security**: Never use `-p<password>`, `--password=...`, or `export MYSQL_PWD='...'` — all of those leak the password into Bash command-line / process listings / chat transcripts. Always use `--defaults-file=$HOME/.agami/.mysql.cnf` (chmod 600, generated by `scripts/setup_pgauth.py`).

### SQLite
```bash
sqlite3 "$path" "$SQL" -csv -header
```

---

## Python Driver Fallback

When CLI tools are not available, use the bundled runtime helper:

```bash
# Single-line SQL via --sql
python3 "$AGAMI_PLUGIN_ROOT/scripts/execute_sql.py" --sql "SELECT COUNT(*) FROM orders"

# Multi-line / quote-heavy SQL via --sql-file (preferred)
python3 "$AGAMI_PLUGIN_ROOT/scripts/execute_sql.py" --sql-file /tmp/agami-query.sql
```

`execute_sql.py` reads `~/.agami/credentials` itself (with chmod check) and connects via `psycopg2` / `pymysql` / `sqlite3`. The visible Bash command contains no credentials. SQL is passed via `--sql-file` (preferred for non-trivial queries) so single quotes, backticks, `$`, and `\` in the SQL don't get mangled by the shell.

The legacy inline `python3 -c '...'` pattern (with `export PGPASSWORD=...` / `export MYSQL_PWD=...` ahead of it) is **forbidden** — it puts the password in the visible Bash command line. Use `execute_sql.py` instead.

The DuckDB scanner approach (tier 2) currently has a similar weakness for cloud-credentialed databases — DuckDB's `ATTACH 'host=... password=...'` requires the password in the SQL string. For Supabase / Neon / RDS connections, prefer tier 1 (psql with `PGPASSFILE`) or tier 3 (`execute_sql.py`) over tier 2.

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

- **NEVER** put passwords in any visible Bash command — not in `export PGPASSWORD='...'`, not in `mysql -p<password>`, not in stdin heredocs that interpolate the password. Hosts render Bash tool calls in their UI; the password leaks into the chat. Use the auth files generated by `scripts/setup_pgauth.py`:
  - psql: `PGPASSFILE=$HOME/.agami/.pgpass psql -h <host> -p <port> -U <user> -d <db> -c "$SQL" --csv`
  - mysql: `mysql --defaults-file=$HOME/.agami/.mysql.cnf --defaults-group-suffix=_<profile> -h <host> -P <port> <db> -e "$SQL" --batch --raw`
  - tier 3: `python3 scripts/execute_sql.py --sql-file ...` (reads creds internally; never echoes them)
- Use `--csv` or `--batch` output modes (not interactive) for predictable parsing
- **Result-set size policy** — default cap is 1000 rows with explicit "show more" prompt. User can override per-query with "top N" or "limit N" framing.
- **NEVER** generate DDL or DML statements (`DROP`, `DELETE`, `INSERT`, `UPDATE`, `ALTER`, etc.)
- Sanitize user input before including in SQL queries
- `~/.agami/credentials` must be `chmod 600`. The `init` skill enforces this; refuse to read otherwise.
