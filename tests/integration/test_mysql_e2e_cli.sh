#!/usr/bin/env bash
# Tier 1 (mysql CLI) end-to-end test against the docker-compose MySQL fixture.
# Verifies: connect → list tables → introspect columns → run a join query.
# No Python required.

set -euo pipefail

MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-53306}"
MYSQL_USER="${MYSQL_USER:-agami_test}"
MYSQL_PWD="${MYSQL_PWD:-agami_test_pw}"
MYSQL_DB="${MYSQL_DB:-shop}"

export MYSQL_PWD

MYSQL="mysql -h $MYSQL_HOST -P $MYSQL_PORT -u $MYSQL_USER --batch --skip-column-names $MYSQL_DB"

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
got=$($MYSQL -e "SELECT 1" | tr -d '[:space:]')
assert_eq "$got" "1" "SELECT 1 returns 1"

echo "[2/5] List tables (excluding system schemas)"
tables=$($MYSQL -e "
  SELECT table_name FROM information_schema.tables
  WHERE table_type = 'BASE TABLE'
    AND table_schema = '$MYSQL_DB'
  ORDER BY table_name
" | grep -v '^$' | sort)
expected=$(printf 'customers\norder_items\norders\nproducts\n' | sort)
assert_eq "$tables" "$expected" "tables match expected fixture"

echo "[3/5] Introspect orders columns"
cols=$($MYSQL -e "
  SELECT column_name FROM information_schema.columns
  WHERE table_schema = '$MYSQL_DB' AND table_name = 'orders'
  ORDER BY ordinal_position
" | grep -v '^$')
expected_cols=$(printf 'id\ncustomer_id\nstatus\nplaced_at\nshipped_at\n')
assert_eq "$cols" "$expected_cols" "orders columns match"

echo "[4/5] Introspect foreign keys"
fk_count=$($MYSQL -e "
  SELECT COUNT(*) FROM information_schema.key_column_usage
  WHERE referenced_table_name IS NOT NULL
    AND table_schema = '$MYSQL_DB'
" | tr -d '[:space:]')
assert_eq "$fk_count" "3" "3 foreign keys (orders.customer_id, items.order_id, items.product_id)"

echo "[5/5] Sample analytical query (top customers by spend)"
top=$($MYSQL -e "
  SELECT c.name
  FROM customers c
  JOIN orders o ON o.customer_id = c.id
  JOIN order_items i ON i.order_id = o.id
  GROUP BY c.id, c.name
  ORDER BY SUM(i.quantity * i.unit_price) DESC
  LIMIT 1
")
assert_eq "$top" "Carol Chen" "top spender is Carol Chen"

green "\n✓ All MySQL tier-1 (mysql CLI) checks passed"
