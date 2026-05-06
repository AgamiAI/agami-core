# Integration tests

End-to-end tests against Postgres + MySQL fixtures, exercising each execution tier.

## Prerequisites

- Docker (or Podman with `docker` symlink) for the DB fixtures
- For tier-1 tests: `psql` and `mysql` CLIs on `PATH`
- For tier-2 tests: `duckdb` on `PATH` (`brew install duckdb`)
- For tier-3 tests: Python with `psycopg2-binary` and `pymysql` (`pip install psycopg2-binary pymysql`)

Each tier's test is independent — skip the tiers you don't have. The release gate is **at least one tier passes for each DB**, plus tier 1 (the documented happy path) passes on a clean dev machine.

## Run

```bash
cd tests/integration

# Spin up fixtures (postgres on :55432, mysql on :53306)
docker compose up -d

# Wait for healthchecks (~5-10s)
docker compose ps

# Tier 1 — native CLI (default, documented happy path)
./test_postgres_e2e_cli.sh
./test_mysql_e2e_cli.sh

# Tier 2 — DuckDB universal binary (fallback when no native CLI)
./test_postgres_e2e_duckdb.sh

# Tier 3 — Python driver (optional power-user path)
python3 test_postgres_e2e_python.py    # auto-skips if psycopg2 missing
python3 test_mysql_e2e_python.py       # auto-skips if pymysql missing

# Tear down
docker compose down -v
```

## What's tested

For each tier × each DB:

1. Connect probe (`SELECT 1`)
2. List tables, excluding system schemas
3. Introspect columns for one table (`orders`)
4. Count foreign keys via `information_schema`
5. A real analytical query (top customer by total spend)

The fixture is a small "shop" model with FK relationships:

- `customers` (5 rows)
- `products` (5 rows)
- `orders` (6 rows) → `customers`
- `order_items` (8 rows) → `orders`, `products`

Top spender across both fixtures is **Carol Chen** at $148.95 — that's the assertion baked into every tier's test.

## Adding a tier or DB

1. Add a fixture init SQL file in `fixtures/`
2. Add a service to `docker-compose.yml`
3. Write a `test_<db>_e2e_<tier>.{sh,py}` that exercises the same five steps
4. Update this README's "Run" section
