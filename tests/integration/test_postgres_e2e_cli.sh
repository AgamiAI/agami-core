#!/usr/bin/env bash
# Tier 1 (psql CLI) end-to-end test against the docker-compose Postgres fixture.
# Verifies: connect → list tables → introspect columns → run a join query.
# No Python required.

set -euo pipefail

PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-55432}"
PGUSER="${PGUSER:-agami_test}"
PGPASSWORD="${PGPASSWORD:-agami_test_pw}"
PGDATABASE="${PGDATABASE:-shop}"

export PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE

PSQL="psql -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE --csv -t"

red()   { printf '\033[31m%s\033[0m\n' "$*" >&2; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

assert_eq() {
  local got="$1" want="$2" desc="$3"
  if [ "$got" = "$want" ]; then
    green "  ✓ $desc"
  else
    red   "  ✗ $desc (got=$got want=$want)"
    exit 1
  fi
}

echo "[1/5] Connect probe"
got=$($PSQL -c "SELECT 1" | tr -d '[:space:]')
assert_eq "$got" "1" "SELECT 1 returns 1"

echo "[2/5] List tables (excluding system schemas)"
tables=$($PSQL -c "
  SELECT table_name FROM information_schema.tables
  WHERE table_type = 'BASE TABLE'
    AND table_schema NOT IN ('pg_catalog', 'information_schema')
  ORDER BY table_name
" | tr -d ' ' | grep -v '^$')
expected=$(printf 'customers\norder_items\norders\nproducts\n')
assert_eq "$tables" "$expected" "tables match expected fixture"

echo "[3/5] Introspect orders columns"
cols=$($PSQL -c "
  SELECT column_name FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name = 'orders'
  ORDER BY ordinal_position
" | tr -d ' ' | grep -v '^$')
expected_cols=$(printf 'id\ncustomer_id\nstatus\nplaced_at\nshipped_at\n')
assert_eq "$cols" "$expected_cols" "orders columns match"

echo "[4/5] Introspect foreign keys"
fk_count=$($PSQL -c "
  SELECT COUNT(*) FROM information_schema.table_constraints
  WHERE constraint_type = 'FOREIGN KEY'
    AND table_schema = 'public'
" | tr -d '[:space:]')
assert_eq "$fk_count" "3" "3 foreign keys (orders.customer_id, items.order_id, items.product_id)"

echo "[5/5] Sample analytical query (top customers by spend)"
top=$($PSQL -c "
  SELECT c.name
  FROM customers c
  JOIN orders o ON o.customer_id = c.id
  JOIN order_items i ON i.order_id = o.id
  GROUP BY c.id, c.name
  ORDER BY SUM(i.quantity * i.unit_price) DESC
  LIMIT 1
" | tr -d ' ' | grep -v '^$')
assert_eq "$top" "CarolChen" "top spender is Carol Chen (Order 4: 5x9.99 + 1x99.00 = 148.95)"

green "\n✓ All Postgres tier-1 (psql) checks passed"
