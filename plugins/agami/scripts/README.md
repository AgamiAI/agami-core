# Optional Python sample scripts

The files in this directory are **samples**, not runtime dependencies. The `agami` skill itself does not require Python on your machine — it drives execution via Bash + native CLIs (`psql`, `mysql`, `sqlite3`) or [DuckDB](https://duckdb.org/) as a universal fallback. See [`../shared/connection-reference.md`](../shared/connection-reference.md) for the tier model.

These scripts are here for two reasons:

1. **Reference** — if you prefer to introspect / chart / send telemetry from Python (e.g., wiring agami into your own toolchain), copy these into your project as starting points.
2. **Tier 3 fallback** — users who already have Python + the right driver, but no native CLI and no DuckDB binary, can call these directly. The `init` skill detects this case and points the user here.

| Script | What it does | Requires |
|---|---|---|
| `sample_introspect_postgres.py` | Connects to Postgres, dumps `information_schema` to YAML matching [`schema-reference.md`](../shared/schema-reference.md) | `psycopg2-binary` |
| `sample_introspect_mysql.py` | Same, for MySQL | `pymysql` |
| `sample_render_chart.py` | Substitutes `chart-template.html` placeholders programmatically | stdlib only |
| `sample_send_telemetry.py` | Builds + POSTs a telemetry payload, enforcing the allowlist from [`telemetry-payload.md`](../shared/telemetry-payload.md) | stdlib only |

## Install dependencies (only if you want tier 3)

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

# Render a chart
python sample_render_chart.py \
  --title "Top customers" \
  --type bar \
  --labels '["Carol Chen","Dave Davis","Bob Brown"]' \
  --datasets '[{"label":"Spend","data":[148.95,93.96,45.0]}]' \
  --out ~/.agami/charts/top-customers.html

# Send a telemetry event (only if you've opted in)
python sample_send_telemetry.py \
  --event-type query --tier cli --db-type postgres --latency-p50 250 --latency-p95 1100
```

All four scripts work without any other agami code installed — they read `~/.agami/credentials` and `~/.agami/.config` themselves.
