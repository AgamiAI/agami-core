#!/usr/bin/env bash
# Tier 2 (DuckDB universal binary) end-to-end test against the docker-compose
# Postgres fixture. Verifies a user without psql installed can still query
# their Postgres DB via the DuckDB postgres_scanner extension.

set -euo pipefail

PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-55432}"
PGUSER="${PGUSER:-agami_test}"
PGPASSWORD="${PGPASSWORD:-agami_test_pw}"
PGDATABASE="${PGDATABASE:-shop}"

red()   { printf '\033[31m%s\033[0m\n' "$*" >&2; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

if ! command -v duckdb >/dev/null 2>&1; then
  red "duckdb not on PATH — skipping tier 2 test"
  echo "Install: brew install duckdb"
  exit 0
fi

DSN="host=$PGHOST port=$PGPORT dbname=$PGDATABASE user=$PGUSER password=$PGPASSWORD"

echo "[1/2] Connect via DuckDB postgres_scanner"
top=$(duckdb -csv -noheader <<SQL | tr -d '[:space:]'
INSTALL postgres_scanner;
LOAD postgres_scanner;
ATTACH '$DSN' AS pg (TYPE POSTGRES, READ_ONLY);
SELECT name FROM pg.public.customers WHERE id = 3;
SQL
)
if [ "$top" = "CarolChen" ]; then
  green "  ✓ DuckDB → Postgres scan returns Carol Chen"
else
  red   "  ✗ Expected 'CarolChen', got '$top'"
  exit 1
fi

echo "[2/2] Run a join query through DuckDB"
spend=$(duckdb -csv -noheader <<SQL | tr -d '[:space:]'
INSTALL postgres_scanner;
LOAD postgres_scanner;
ATTACH '$DSN' AS pg (TYPE POSTGRES, READ_ONLY);
SELECT ROUND(SUM(quantity * unit_price), 2)
FROM pg.public.order_items i
JOIN pg.public.orders o ON o.id = i.order_id
WHERE o.customer_id = 3;
SQL
)
if [ "$spend" = "148.95" ]; then
  green "  ✓ Carol Chen total spend = 148.95"
else
  red   "  ✗ Expected 148.95, got $spend"
  exit 1
fi

green "\n✓ All Postgres tier-2 (DuckDB) checks passed"
