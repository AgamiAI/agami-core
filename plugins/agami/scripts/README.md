# Python helpers

Two categories of Python scripts ship with agami:

- **Runtime helpers** (used by the skill): `execute_sql.py`. The agami skill itself runs this when tier 3 (Python driver) is selected — that's when the user has Python + a driver but no native CLI and no DuckDB. Bundled in every Desktop zip and on the marketplace install path.
- **Optional samples** (`sample_*.py`): one-off reference scripts you can copy into your own toolchain. Not required for the skill to work.

## Runtime helpers

| Script | What it does | Requires |
|---|---|---|
| `execute_sql.py` | Reads `~/.agami/credentials`, opens a connection, runs ONE SQL statement, emits RFC 4180 CSV on stdout. Supports postgres / mysql / sqlite. Hard-exits with a clear message if credentials are missing — never asks in chat. | One of: `psycopg2-binary` (postgres), `pymysql` (mysql); sqlite uses stdlib |
| `mcp_server.py` | **Optional** local stdio MCP server (`agami serve`) — exposes the local semantic model + read-only local SQL execution over the Model Context Protocol, so agami can be used from Claude Code / Claude Desktop and not just inside the plugin. Pure stdlib; routes execution through `execute_sql.py`; no network, no auth, no telemetry. See [`docs/mcp-server.md`](../../../docs/mcp-server.md). | stdlib only (plus the relevant `execute_sql.py` driver for non-SQLite DBs) |
| `setup_desktop_mcp.py` | One-command wiring of `mcp_server.py` into the Claude Desktop app (used by the `agami-serve` skill). Auto-detects the right Python interpreter, copies the two self-contained server files to a stable `~/.agami/serve/`, and safely merges the entry into `claude_desktop_config.json` (timestamped backup + atomic write, preserving every other key). `--dry-run` previews; `--in-place` skips the copy for dev checkouts. | stdlib only |

The agami skill calls `execute_sql.py` via:

```bash
# Single-line SQL via --sql
python3 scripts/execute_sql.py --profile default --sql "SELECT COUNT(*) FROM orders"

# Multi-line / quote-heavy SQL via --sql-file (preferred for anything non-trivial)
python3 scripts/execute_sql.py --profile default --sql-file /tmp/agami-query.sql
```

Exit codes are documented in `execute_sql.py`'s docstring. The skill routes non-zero exits through the DB error classifier in `../shared/db_error_classifier.md`.

## Other helpers

| Script | What it does | Requires |
|---|---|---|
| `render_chart.py` | Substitutes `chart-template.html` placeholders programmatically. Used by the agami-query-database SKILL to produce HTML reports (Phase 4e). | stdlib only |

## Install dependencies (only if you want the Python driver path)

```bash
pip install psycopg2-binary pymysql
```

That's it. No `agami` package on PyPI. The skill itself is just markdown + reference docs.

## Running a script directly

```bash
# Render a chart (single section)
python render_chart.py \
  --title "Top customers" \
  --summary "" \
  --section '{"title":"Top customers by spend","insights":"Carol Chen leads at $148.95.","chart_type":"bar","labels":["Carol Chen","Dave Davis","Bob Brown"],"datasets":[{"label":"Spend","data":[148.95,93.96,45.0]}],"table_headers":["Customer","Spend"],"table_rows":[["Carol Chen",148.95],["Dave Davis",93.96],["Bob Brown",45.0]]}' \
  --out ~/.agami/charts/top-customers.html
```

These helpers work without any other agami code installed — they read `~/.agami/credentials` and `~/.agami/.config` themselves.
