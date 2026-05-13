# Python helpers

Two categories of Python scripts ship with agami:

- **Runtime helpers** (used by the skill): `execute_sql.py`. The agami skill itself runs this when tier 3 (Python driver) is selected — that's when the user has Python + a driver but no native CLI and no DuckDB. Bundled in every Desktop zip and on the marketplace install path.
- **Optional samples** (`sample_*.py`): one-off reference scripts you can copy into your own toolchain. Not required for the skill to work.

## Runtime helpers

| Script | What it does | Requires |
|---|---|---|
| `execute_sql.py` | Reads `~/.agami/credentials`, opens a connection, runs ONE SQL statement, emits RFC 4180 CSV on stdout. Supports postgres / mysql / sqlite. Hard-exits with a clear message if credentials are missing — never asks in chat. | One of: `psycopg2-binary` (postgres), `pymysql` (mysql); sqlite uses stdlib |

The agami skill calls it via:

```bash
# Single-line SQL via --sql
python3 scripts/execute_sql.py --profile default --sql "SELECT COUNT(*) FROM orders"

# Multi-line / quote-heavy SQL via --sql-file (preferred for anything non-trivial)
python3 scripts/execute_sql.py --profile default --sql-file /tmp/agami-query.sql
```

Exit codes are documented in `execute_sql.py`'s docstring. The skill routes non-zero exits through the DB error classifier in `../shared/db_error_classifier.md`.

## Optional samples

| Script | What it does | Requires |
|---|---|---|
| `sample_introspect_postgres.py` | Connects to Postgres, dumps `information_schema` to YAML matching [`schema-reference.md`](../shared/schema-reference.md) | `psycopg2-binary` |
| `sample_introspect_mysql.py` | Same, for MySQL | `pymysql` |
| `render_chart.py` | Substitutes `chart-template.html` placeholders programmatically. Used by the agami-query-database SKILL to produce HTML reports (Phase 4e). | stdlib only |

## Install dependencies (only if you want the Python driver path)

```bash
pip install psycopg2-binary pymysql
```

That's it. No `agami` package on PyPI. The skill itself is just markdown + reference docs.

## Running a script directly

```bash
# Dump schema to a YAML file
python sample_introspect_postgres.py \
  --host localhost --port 5432 --db shop --user agami_test --password agami_test_pw \
  --out ~/.agami/shop.yaml

# Render a chart (single section)
python render_chart.py \
  --title "Top customers" \
  --summary "" \
  --section '{"title":"Top customers by spend","insights":"Carol Chen leads at $148.95.","chart_type":"bar","labels":["Carol Chen","Dave Davis","Bob Brown"],"datasets":[{"label":"Spend","data":[148.95,93.96,45.0]}],"table_headers":["Customer","Spend"],"table_rows":[["Carol Chen",148.95],["Dave Davis",93.96],["Bob Brown",45.0]]}' \
  --out ~/.agami/charts/top-customers.html

# Send a telemetry event (only if you've opted in)
python sample_send_telemetry.py \
  --event-type query --tier cli --db-type postgres --latency-p50 250 --latency-p95 1100
```

All four scripts work without any other agami code installed — they read `~/.agami/credentials` and `~/.agami/.config` themselves.
