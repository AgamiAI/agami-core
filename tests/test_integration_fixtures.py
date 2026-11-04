"""The integration tiers' hard-coded answers must match their fixtures (#173).

The `.sh` tiers need Docker and DB clients, so CI never runs them; this runs their expected answer
against the SQLite fixture instead, and checks the three fixtures hold the same data.
"""

import re
import sqlite3
from pathlib import Path

INTEGRATION = Path(__file__).resolve().parent / "integration"
FIXTURES = INTEGRATION / "fixtures"

TOP_SPENDER_SQL = """
    SELECT c.id, c.name, ROUND(SUM(i.quantity * i.unit_price), 2)
    FROM customers c
    JOIN orders o ON o.customer_id = c.id
    JOIN order_items i ON i.order_id = o.id
    GROUP BY c.id, c.name
    ORDER BY SUM(i.quantity * i.unit_price) DESC
    LIMIT 1
"""


def _sqlite_top_spender() -> tuple[int, str, float]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript((FIXTURES / "sqlite-shop-init.sql").read_text(encoding="utf-8"))
        return conn.execute(TOP_SPENDER_SQL).fetchone()
    finally:
        conn.close()


def _insert_rows(fixture: str) -> list[str]:
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    blocks = re.findall(r"INSERT INTO .*?;", text, flags=re.DOTALL)
    return [" ".join(b.split()) for b in blocks]


def test_fixtures_hold_the_same_rows() -> None:
    sqlite_rows = _insert_rows("sqlite-shop-init.sql")
    assert sqlite_rows
    assert _insert_rows("postgres-init.sql") == sqlite_rows
    assert _insert_rows("mysql-init.sql") == sqlite_rows


def test_shell_tiers_expect_the_fixture_top_spender() -> None:
    cust_id, name, spend = _sqlite_top_spender()
    squashed = name.replace(" ", "")

    pg_cli = (INTEGRATION / "test_postgres_e2e_cli.sh").read_text(encoding="utf-8")
    assert f'assert_eq "$top" "{squashed}"' in pg_cli

    mysql_cli = (INTEGRATION / "test_mysql_e2e_cli.sh").read_text(encoding="utf-8")
    assert f'assert_eq "$top" "{name}"' in mysql_cli

    duckdb = (INTEGRATION / "test_postgres_e2e_duckdb.sh").read_text(encoding="utf-8")
    assert f"WHERE id = {cust_id};" in duckdb
    assert f"WHERE o.customer_id = {cust_id};" in duckdb
    assert f'[ "$top" = "{squashed}" ]' in duckdb
    assert f'[ "$spend" = "{spend:.2f}" ]' in duckdb

    readme = (INTEGRATION / "README.md").read_text(encoding="utf-8")
    assert f"**{name}** at ${spend:.2f}" in readme
