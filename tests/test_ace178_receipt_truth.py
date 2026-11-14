"""ACE-178 criterion 10: every `matched` claim about a number followed through steps, checked
against the numbers — by a reference the code under check had no part in.

Each case says by hand what the receipt must claim (status, metric, restrictions, joins) and how
that claim reads over the TABLE: the group keys as the table's own columns, and the reference
query's FROM and WHERE written out (a lookup step's value as the subquery computing it, a join as
the join). Only the metric's binding is filled in, from the test's model. Two checks:

* the receipt's claim is exactly the hand-written one;
* the statement's numbers equal the reference's, group by group (a group the reference lacks must
  hold the value of an aggregate over no rows — the one difference a FILTER and a WHERE allow).

So a claim is wrong if the receipt says something else, and the hand claim is wrong if the numbers
disagree. Nothing here imports the receipt's internals. The data and the engines are
`receipt_truth`'s fixture.

`test_a_planted_bug_is_caught` plants must-fix 1 (one step read through two references) and
must-fix 2 (a set-returning step read as a lookup) and checks that each is refuted; the second by
the numbers alone.
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
from test_ace177_filters_on_a_matched_metric import (  # noqa: E402
    ALL_METRICS,
    UNFILTERED,
    _write_model,
)

# The table each metric is defined over, as the model declares it.
TABLE = {name: sources[0] for name, (_, sources) in ALL_METRICS.items()}

# (model, statement, output keys, the same keys over the table, status, metric, restrictions,
#  joins, the reference's FROM, its WHERE). `{key}` columns in the reference are aliased k0, k1….
CASES = [
    (
        "all",
        "WITH x AS (SELECT SUM(total_amount) AS n FROM orders) SELECT n FROM x",
        [],
        [],
        "matched",
        "total_sales",
        [],
        [],
        "orders",
        None,
    ),
    (
        "all",
        "WITH x AS (SELECT status, SUM(total_amount) AS n FROM orders WHERE customer_id = 3 "
        "GROUP BY status) SELECT status, n FROM x",
        ["status"],
        ["status"],
        "matched",
        "total_sales",
        ["customer_id = 3"],
        [],
        "orders",
        "customer_id = 3",
    ),
    (
        "all",
        "WITH x AS (SELECT status, SUM(total_amount) AS n FROM orders WHERE customer_id > 1 "
        "GROUP BY status) SELECT status, n FROM x WHERE status = 'paid'",
        ["status"],
        ["status"],
        "matched",
        "paid_sales",
        ["customer_id > 1"],
        [],
        "orders",
        "customer_id > 1 AND status = 'paid'",
    ),
    (
        "all",
        "WITH g AS (SELECT status AS st, COUNT(*) AS n FROM orders GROUP BY 1) "
        "SELECT st, n FROM g WHERE st = 'paid'",
        ["st"],
        ["status"],
        "matched",
        "paid_order_count",
        [],
        [],
        "orders",
        "status = 'paid'",
    ),
    (
        "unf",
        "SELECT COUNT(*) AS n FROM (SELECT id FROM orders WHERE status = 'paid') s",
        [],
        [],
        "matched",
        "order_count",
        ["status = 'paid'"],
        [],
        "orders",
        "status = 'paid'",
    ),
    (
        "all",
        "SELECT COUNT(*) AS n FROM (SELECT id FROM orders WHERE status = 'paid') s",
        [],
        [],
        "matched",
        "paid_order_count",
        [],
        [],
        "orders",
        None,
    ),
    (
        "all",
        "SELECT t.n FROM (SELECT s.n FROM (SELECT COUNT(*) AS n FROM orders "
        "WHERE customer_id > 1) s) t",
        [],
        [],
        "matched",
        "order_count",
        ["customer_id > 1"],
        [],
        "orders",
        "customer_id > 1",
    ),
    (
        "all",
        "WITH r AS (SELECT id, total_amount FROM orders WHERE status = 'paid'), c AS (SELECT "
        "SUM(total_amount) AS n FROM r WHERE id > 3) SELECT n FROM c",
        [],
        [],
        "matched",
        "paid_sales",
        ["id > 3"],
        [],
        "orders",
        "id > 3",
    ),
    (
        "unf",
        "WITH a AS (SELECT MAX(opened) AS d FROM orders) SELECT COUNT(*) AS n FROM orders o "
        "CROSS JOIN a WHERE o.opened >= a.d",
        [],
        [],
        "matched",
        "order_count",
        ["o.opened >= a.d"],
        [],
        "orders",
        "opened >= (SELECT MAX(opened) FROM orders)",
    ),
    (
        "unf",
        "WITH a AS (SELECT MIN(opened) AS d FROM orders WHERE status = 'paid') "
        "SELECT o.status, COUNT(*) AS n FROM orders o, a WHERE o.opened > a.d GROUP BY o.status",
        ["status"],
        ["status"],
        "matched",
        "order_count",
        ["o.opened > a.d"],
        [],
        "orders",
        "opened > (SELECT MIN(opened) FROM orders WHERE status = 'paid')",
    ),
    (
        "all",
        "WITH x AS (SELECT COUNT(*) FILTER (WHERE status = 'paid') AS n FROM orders) "
        "SELECT n FROM x",
        [],
        [],
        "matched",
        "paid_order_count",
        [],
        [],
        "orders",
        None,
    ),
    (
        "all",
        "WITH x AS (SELECT customer_id, SUM(CASE WHEN status = 'paid' THEN total_amount END) "
        "AS n FROM orders GROUP BY customer_id) SELECT customer_id, n FROM x",
        ["customer_id"],
        ["customer_id"],
        "matched",
        "paid_sales",
        [],
        [],
        "orders",
        None,
    ),
    (
        "all",
        "WITH x AS (SELECT SUM(total_amount) AS s FROM orders WHERE customer_id = 1) "
        "SELECT ROUND(s, 2) AS n FROM x",
        [],
        [],
        "matched",
        "total_sales",
        ["customer_id = 1"],
        [],
        "orders",
        "customer_id = 1",
    ),
    (
        "all",
        "WITH s(a, b) AS (SELECT status, COUNT(*) FROM orders GROUP BY status) "
        "SELECT a, b AS n FROM s",
        ["a"],
        ["status"],
        "matched",
        "order_count",
        [],
        [],
        "orders",
        None,
    ),
    # The applied declared filter is part of what `orders` means: not listed, but in the reference.
    (
        "all",
        "WITH x AS (SELECT o.id, o.total_amount FROM orders o WHERE o.is_deleted = false) "
        "SELECT SUM(total_amount) AS n FROM x",
        [],
        [],
        "matched",
        "total_sales",
        [],
        [],
        "orders",
        "is_deleted = false",
    ),
    (
        "all",
        "WITH x AS (SELECT id, status, total_amount FROM orders WHERE customer_id = 3) "
        "SELECT SUM(x.total_amount) AS n FROM x WHERE x.status = 'paid'",
        [],
        [],
        "matched",
        "paid_sales",
        ["customer_id = 3"],
        [],
        "orders",
        "customer_id = 3",
    ),
    (
        "all",
        "WITH x AS (SELECT number, type FROM change_requests WHERE state = 'Work in Progress') "
        "SELECT type, COUNT(DISTINCT number) AS n FROM x GROUP BY type",
        ["type"],
        ["type"],
        "matched",
        "change_count",
        ["state = 'Work in Progress'"],
        [],
        "change_requests",
        "state = 'Work in Progress'",
    ),
    (
        "all",
        "WITH x AS (SELECT DISTINCT number, type FROM change_requests WHERE state = 'Closed') "
        "SELECT COUNT(DISTINCT number) AS n FROM x",
        [],
        [],
        "matched",
        "change_count",
        ["state = 'Closed'"],
        [],
        "change_requests",
        "state = 'Closed'",
    ),
    (
        "all",
        "WITH p AS (SELECT order_id AS o, amount AS a FROM payments WHERE status <> "
        "'refunded') SELECT o, SUM(a) AS n FROM p GROUP BY o",
        ["o"],
        ["order_id"],
        "matched",
        "payment_total",
        ["status <> 'refunded'"],
        [],
        "payments",
        "status <> 'refunded'",
    ),
    (
        "all",
        "WITH t AS (SELECT type, COUNT(DISTINCT number) AS c FROM change_requests GROUP BY "
        "type), total AS (SELECT COUNT(DISTINCT number) AS all_c FROM change_requests) "
        "SELECT t.type, t.c AS n FROM t CROSS JOIN total",
        ["type"],
        ["type"],
        "matched",
        "change_count",
        [],
        [],
        "change_requests",
        None,
    ),
    (
        "all",
        "WITH x AS (SELECT status AS total_amount, total_amount AS status FROM orders) "
        "SELECT SUM(status) AS n FROM x WHERE total_amount = 'paid'",
        [],
        [],
        "matched",
        "paid_sales",
        [],
        [],
        "orders",
        None,
    ),
    # Joins on the path: the reference keeps the join.
    (
        "unf",
        "WITH x AS (SELECT o.total_amount FROM orders o JOIN payments p ON p.order_id = o.id) "
        "SELECT SUM(total_amount) AS n FROM x",
        [],
        [],
        "matched",
        "total_sales",
        [],
        ["orders → payments"],
        "orders o JOIN payments p ON p.order_id = o.id",
        None,
    ),
    (
        "unf",
        "WITH f AS (SELECT id FROM orders WHERE status = 'paid') SELECT SUM(o.total_amount) "
        "AS n FROM orders o JOIN f ON f.id = o.id",
        [],
        [],
        "matched",
        "total_sales",
        ["status = 'paid'"],
        ["orders → f"],
        "orders o JOIN (SELECT id, status FROM orders) f ON f.id = o.id",
        "f.status = 'paid'",
    ),
    (
        "unf",
        "WITH s AS (SELECT o.total_amount FROM orders o JOIN customers c ON c.id = "
        "o.customer_id AND c.region = 'EU') SELECT SUM(total_amount) AS n FROM s",
        [],
        [],
        "matched",
        "total_sales",
        ["c.region = 'EU'"],
        ["orders → customers"],
        "orders o JOIN customers c ON c.id = o.customer_id",
        "c.region = 'EU'",
    ),
    (
        "unf",
        "SELECT SUM(o.total_amount) AS n FROM orders o JOIN (SELECT id FROM customers WHERE "
        "region = 'EU') c ON c.id = o.customer_id",
        [],
        [],
        "matched",
        "total_sales",
        ["region = 'EU'"],
        ["orders → c"],
        "orders o JOIN customers c ON c.id = o.customer_id",
        "c.region = 'EU'",
    ),
    (
        "unf",
        "SELECT SUM(s.total_amount) AS n FROM (SELECT o.total_amount FROM orders o, customers c "
        "WHERE c.id = o.customer_id AND c.region = 'EU') s",
        [],
        [],
        "matched",
        "total_sales",
        ["c.region = 'EU'"],
        ["orders → customers"],
        "orders o, customers c",
        "c.id = o.customer_id AND c.region = 'EU'",
    ),
    (
        "unf",
        "WITH x AS (SELECT o.status, o.total_amount FROM orders o JOIN payments p ON "
        "p.order_id = o.id WHERE p.status = 'paid') SELECT status, SUM(total_amount) AS n "
        "FROM x GROUP BY status",
        ["status"],
        ["o.status"],
        "matched",
        "total_sales",
        ["p.status = 'paid'"],
        ["orders → payments"],
        "orders o JOIN payments p ON p.order_id = o.id",
        "p.status = 'paid'",
    ),
    # Not modelled, so not claimed (must-fix 1 and 2).
    (
        "all",
        "WITH x AS (SELECT id, customer_id, total_amount, status FROM orders) SELECT "
        "SUM(a.total_amount) AS n FROM x a JOIN x b ON a.customer_id = b.id WHERE b.status = 'paid'",
        [],
        [],
        "undetermined",
        None,
        None,
        None,
        None,
        None,
    ),
    (
        "all",
        "WITH x AS (SELECT id, customer_id, total_amount, status FROM orders), p AS (SELECT id "
        "FROM x WHERE status = 'paid'), q AS (SELECT customer_id, total_amount FROM x) "
        "SELECT SUM(q.total_amount) AS n FROM q JOIN p ON q.customer_id = p.id",
        [],
        [],
        "undetermined",
        None,
        None,
        None,
        None,
        None,
    ),
    (
        "unf",
        "WITH a AS (SELECT generate_series(1, 3) AS k) SELECT SUM(o.total_amount) AS n "
        "FROM orders o CROSS JOIN a",
        [],
        [],
        "undetermined",
        None,
        None,
        None,
        None,
        None,
    ),
    (
        "unf",
        "SELECT COUNT(*) AS n FROM orders o CROSS JOIN (SELECT UNNEST([1, 2, 3]) AS k) a",
        [],
        [],
        "undetermined",
        None,
        None,
        None,
        None,
        None,
    ),
]

# What each engine must compare, and of that how many with a join on the path. Every `matched` case
# is compared, so a run that compared fewer skipped one, which is a failure.
MATCHED = sum(1 for c in CASES if c[4] == "matched")
JOINED = sum(1 for c in CASES if c[4] == "matched" and c[7])


def _org(tmp_path, name: str, storage_type: str, metrics: dict):
    root = tmp_path / name
    root.mkdir(parents=True)
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


def _item(org, sql: str) -> dict:
    (item,) = [
        i
        for i in rt.assemble_receipt(org, sql)["columns"]["items"]
        if i["kind"] == "output" and i["column"] == "n"
    ]
    return item


def _statement(sql: str, keys: list) -> str:
    cols = ", ".join([*(f"q.{k} AS k{i}" for i, k in enumerate(keys)), "q.n AS v"])
    return f"SELECT {cols} FROM ({sql}) q"


def _reference(metric: str, base_keys: list, frm: str, where: "str | None") -> str:
    keys = ", ".join(f"{k} AS k{i}" for i, k in enumerate(base_keys))
    sql = f"SELECT {keys + ', ' if keys else ''}{ALL_METRICS[metric][0]} AS v FROM {frm}"
    if where:
        sql += f" WHERE {where}"
    if base_keys:
        sql += " GROUP BY " + ", ".join(base_keys)
    return sql


def _agrees(conn, statement: str, reference: str) -> "str | None":
    """None when every group the statement returns holds the reference's value; else why not."""
    got, want = truth._rows(conn, statement), truth._rows(conn, reference)
    if isinstance(got, str) or isinstance(want, str):
        return f"the engine rejected it: {got if isinstance(got, str) else want}"
    ref = {tuple(truth._norm(v) for v in r[:-1]): truth._norm(r[-1]) for r in want}
    for row in got:
        key, value = tuple(truth._norm(v) for v in row[:-1]), truth._norm(row[-1])
        if not (ref[key] == value if key in ref else value in (None, 0.0)):
            return f"group {key}: statement {value!r}, metric {ref.get(key)!r}"
    return None


def _runs_here(engine: str, sql: str) -> bool:
    """A set-returning function is DuckDB's and Postgres's syntax, not SQLite's."""
    return engine != "sqlite" or ("generate_series" not in sql and "UNNEST" not in sql)


@pytest.mark.parametrize("engine", ["sqlite", "duckdb", "postgres"])
def test_every_matched_claim_through_a_step_agrees_with_the_numbers(tmp_path, engine):
    conn, storage_type = _connect(engine)
    orgs = {
        "all": _org(tmp_path, "all", storage_type, ALL_METRICS),
        "unf": _org(tmp_path, "unf", storage_type, UNFILTERED),
    }
    compared = joined = 0
    try:
        for model, sql, keys, base_keys, status, metric, restrictions, joins, frm, where in CASES:
            item = _item(orgs[model], sql)
            assert (item["status"], item["name"]) == (status, metric), sql
            if status != "matched":
                continue
            assert (item["restrictions"], item["joins_not_declared"]) == (restrictions, joins), sql
            differs = _agrees(
                conn, _statement(sql, keys), _reference(metric, base_keys, frm, where)
            )
            assert differs is None, (sql, differs)
            compared += 1
            joined += bool(joins)
    finally:
        conn.close()
    assert (compared, joined) == (MATCHED, JOINED) and JOINED >= 5


def _disagreements(tmp_path, monkeypatch, plant) -> list:
    """Every case on which the receipt, under a planted bug, disagrees with the hand-written claim,
    or makes a plain `matched` claim the numbers refute (the metric's binding over its table)."""
    plant(monkeypatch)
    conn, storage_type = _connect("duckdb")
    orgs = {
        "all": _org(tmp_path, "all", storage_type, ALL_METRICS),
        "unf": _org(tmp_path, "unf", storage_type, UNFILTERED),
    }
    found = []
    try:
        for model, sql, keys, base_keys, status, metric, restrictions, joins, frm, where in CASES:
            item = _item(orgs[model], sql)
            claim = (item["status"], item["name"], item["restrictions"], item["joins_not_declared"])
            if claim != (status, metric, restrictions, joins):
                found.append((sql, f"claims {claim}"))
            if item["status"] != "matched" or item["restrictions"] or item["joins_not_declared"]:
                continue
            same = (item["status"], item["name"]) == (status, metric)
            differs = _agrees(
                conn,
                _statement(sql, keys),
                _reference(
                    item["name"],
                    base_keys,
                    frm if same else TABLE[item["name"]],
                    where if same else None,
                ),
            )
            if differs and "rejected" not in differs:
                found.append((sql, differs))
    finally:
        conn.close()
    return found


def test_a_planted_bug_is_caught(tmp_path, monkeypatch):
    """Must-fix 1 planted (a step read through two references is followed as one) and must-fix 2
    planted (a set-returning step is a one-row lookup): each makes a claim this check refutes — the
    second by the numbers alone. With nothing planted, nothing is refuted."""
    assert _disagreements(tmp_path / "clean", monkeypatch, lambda mp: None) == []

    def twice(mp):
        mp.setattr(rt, "_reached_again", lambda reached, body, via: False)

    def series(mp):
        mp.setattr(rt, "_SET_RETURNING", ())

    found = _disagreements(tmp_path / "twice", monkeypatch, twice)
    assert any("x a JOIN x b" in sql and "paid_sales" in why for sql, why in found), found
    monkeypatch.undo()
    found = _disagreements(tmp_path / "series", monkeypatch, series)
    # DuckDB's `generate_series` in a SELECT list returns a list, not rows, so it moves no number
    # here; the claim is refuted. UNNEST returns rows, and the numbers refute that one alone.
    assert any("generate_series" in sql for sql, _ in found), found
    assert any("UNNEST" in sql and why.startswith("group") for sql, why in found), found
