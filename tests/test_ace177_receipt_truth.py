"""ACE-177 criterion 10: every `matched` claim in the battery, checked against the numbers.

`receipt_truth.check` runs each statement and the matched metric's own binding with the listed
restrictions applied, on the same synthetic data, and the two must agree. SQLite and DuckDB always;
Postgres when `AGAMI_IT_PG_PASSWORD` is set (temp tables only).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import loader as L  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

import receipt_truth as truth  # noqa: E402
from receipt_trick_queries import (  # noqa: E402
    CURATED_SPELLINGS,
    NOT_A_PLAIN_MATCH,
    SEMI_ANTI,
    TWIN_TRICKS,
)
from test_ace177_filters_on_a_matched_metric import (  # noqa: E402
    ALL_METRICS,
    UNFILTERED,
    _write_model,
)

# The battery's statements that read `matched` with something to check, beyond the trick lists.
MATCHED_EXAMPLES = [
    "SELECT type, COUNT(DISTINCT number) AS changes FROM change_requests "
    "WHERE state = 'Work in Progress' GROUP BY type",
    "SELECT type, COUNT(DISTINCT number) AS changes FROM change_requests GROUP BY type",
    "SELECT customer_id, SUM(total_amount) AS s FROM orders WHERE status = 'paid' "
    "GROUP BY customer_id",
    "SELECT customer_id, COUNT(*) FILTER (WHERE status = 'paid') AS n FROM orders "
    "GROUP BY customer_id",
    "SELECT customer_id, SUM(CASE WHEN status = 'paid' THEN total_amount END) AS s FROM orders "
    "GROUP BY customer_id",
    "SELECT SUM(total_amount) AS s FROM orders WHERE customer_id = 3 AND status = 'paid'",
    "SELECT SUM(total_amount) AS s FROM orders WHERE status = 'paid' AND customer_id = 7",
    "SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id HAVING COUNT(*) > 1",
    "SELECT COUNT(*) AS n FROM orders o JOIN customers c ON c.id = o.customer_id",
    "SELECT COUNT(*) AS n FROM orders o, customers c WHERE c.id = o.customer_id",
    "SELECT COUNT(*) AS n FROM orders a JOIN orders b ON a.id = b.customer_id",
    "SELECT COUNT(*) AS n FROM orders o LEFT JOIN payments p ON p.order_id = o.id "
    "AND p.status = 'paid'",
    "SELECT SUM(o.total_amount) AS s FROM orders o LEFT JOIN payments p ON p.order_id = o.id "
    "AND p.status = 'paid'",
    "SELECT o.customer_id, SUM(p.amount) AS paid FROM orders o LEFT JOIN payments p "
    "ON p.order_id = o.id AND p.status = 'paid' GROUP BY o.customer_id",
    "SELECT SUM(o.total_amount) AS s FROM orders o RIGHT JOIN payments p ON p.order_id = o.id "
    "AND o.status = 'open'",
    "SELECT COUNT(*) AS n FROM orders o WHERE o.is_deleted = false AND o.status = 'shipped'",
    "SELECT o.status, COUNT(*) AS n FROM orders o WHERE o.is_deleted = false GROUP BY o.status",
    "SELECT COUNT(*) AS n FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE c.is_deleted = false",
    "SELECT COUNT(*) AS n FROM orders WHERE customer_id = 1 UNION ALL SELECT COUNT(*) AS n "
    "FROM orders",
    "SELECT SUM(o.total_amount) AS s FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE o.status = 'x' AND c.status = 'x'",
]

QUERIES = [*MATCHED_EXAMPLES, *NOT_A_PLAIN_MATCH, *TWIN_TRICKS, *CURATED_SPELLINGS]


def _org(tmp_path, name: str, storage_type: str, metrics: dict):
    root = tmp_path / name
    root.mkdir()
    _write_model(root, storage_type=storage_type, metrics=metrics)
    return L.load_datasource(root)


def _connect(engine: str):
    connect, storage_type = truth.ENGINES[engine]
    if engine == "duckdb":
        pytest.importorskip("duckdb")
    conn = connect()
    if conn is None:
        pytest.skip("set AGAMI_IT_PG_PASSWORD (and a reachable Postgres) to run the Postgres half")
    truth.load_fixture(conn, engine)
    return conn, storage_type


# The least each engine must actually compare: a run where most claims were skipped is not a pass.
MIN_COMPARED = 60


@pytest.mark.parametrize("engine", ["sqlite", "duckdb", "postgres"])
def test_every_matched_claim_agrees_with_the_numbers(tmp_path, engine):
    conn, storage_type = _connect(engine)
    try:
        outcome = truth.Outcome()
        for name, metrics in (("all", ALL_METRICS), ("unfiltered", UNFILTERED)):
            org = _org(tmp_path, name, storage_type, metrics)
            for sql in QUERIES:
                outcome.add(truth.check(org, sql, conn))
            if engine == "duckdb":
                for _, sql in SEMI_ANTI:
                    outcome.add(truth.check(org, sql, conn))
    finally:
        conn.close()
    assert outcome.mismatches == []
    assert outcome.compared >= MIN_COMPARED, (outcome.compared, outcome.skipped)


def test_the_harness_catches_a_claim_that_hides_a_restriction(tmp_path, monkeypatch):
    """Teeth: with every WHERE condition dropped from what the receipt lists, a filtered count reads
    as the plain metric — and the numbers disagree."""
    conn, storage_type = _connect("sqlite")
    org = _org(tmp_path, "all", storage_type, ALL_METRICS)
    real = rt._arm_restrictions

    def hiding(*args, **kwargs):
        return real(*args, **kwargs)._replace(rows=[])

    monkeypatch.setattr(rt, "_arm_restrictions", hiding)
    outcome = truth.check(org, MATCHED_EXAMPLES[0], conn)
    conn.close()
    assert outcome.compared == 1 and len(outcome.mismatches) == 1
