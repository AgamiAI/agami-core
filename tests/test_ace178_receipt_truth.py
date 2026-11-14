"""ACE-178 criterion 10: every `matched` claim about a number followed through steps, checked
against the numbers.

ACE-177's `receipt_truth.check` rebuilds the reference on the statement's own SELECT. A number
computed inside a step, or counted over rows a step filtered, has its binding's columns and its
restrictions in different scopes, so that rebuild cannot hold it. This one builds the reference
from the TABLE instead:

    SELECT <the computing step's group keys>, <the metric's binding>
    FROM <the one table the counted rows came from>
    WHERE <every listed restriction, its columns followed back to that table>
      AND <the table's declared filters the statement applied>
    GROUP BY <those keys>

A lookup step's value in a restriction (`o.opened >= a.d`) becomes the scalar subquery that
computes it. Every group the statement returns must hold the reference's value (or the value of an
aggregate over no rows, where a FILTER condition and a WHERE differ — see `receipt_truth`). A
reference group the statement does not return is not compared: a result filter dropped it.

What this cannot rebuild — a path through a join, a restriction it cannot follow to the table, a
group key the statement does not return, a withheld value — is SKIPPED and never counted as
agreement, and the test asserts how many were compared.
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
from sqlglot import exp  # noqa: E402

import receipt_truth as truth  # noqa: E402
from receipt_trick_queries import STEP_TRICKS  # noqa: E402
from test_ace177_filters_on_a_matched_metric import (  # noqa: E402
    ALL_METRICS,
    UNFILTERED,
    _write_model,
)

# Step-shaped statements over the harness's tables (`orders`, `customers`, `payments`,
# `change_requests`), each with a number the receipt reads as `matched` on at least one model.
STEP_MATCHED = [
    "WITH x AS (SELECT SUM(total_amount) AS n FROM orders) SELECT n FROM x",
    "WITH x AS (SELECT status, SUM(total_amount) AS n FROM orders WHERE customer_id = 3 "
    "GROUP BY status) SELECT status, n FROM x",
    "WITH x AS (SELECT status, SUM(total_amount) AS n FROM orders WHERE customer_id > 1 "
    "GROUP BY status) SELECT status, n FROM x WHERE status = 'paid'",
    "WITH g AS (SELECT status, COUNT(*) AS n FROM orders GROUP BY status) "
    "SELECT status, n FROM g WHERE n > 1",
    "WITH g AS (SELECT status AS st, COUNT(*) AS n FROM orders GROUP BY 1) "
    "SELECT st, n FROM g WHERE st = 'paid'",
    "SELECT COUNT(*) AS n FROM (SELECT id FROM orders WHERE status = 'paid') s",
    "SELECT s.n FROM (SELECT SUM(total_amount) AS n FROM orders WHERE status <> 'x') s",
    "SELECT t.n FROM (SELECT s.n FROM (SELECT COUNT(*) AS n FROM orders WHERE customer_id > 1) s) t",
    "WITH r AS (SELECT id, total_amount FROM orders WHERE status = 'paid'), c AS (SELECT "
    "SUM(total_amount) AS n FROM r WHERE id > 3) SELECT n FROM c",
    "WITH a AS (SELECT id, total_amount, status FROM orders WHERE customer_id IS NOT NULL), "
    "b AS (SELECT id, total_amount, status FROM a WHERE id > 1) "
    "SELECT status, SUM(total_amount) AS n FROM b GROUP BY status",
    "WITH a AS (SELECT MAX(opened) AS d FROM orders) SELECT COUNT(*) AS n FROM orders o "
    "CROSS JOIN a WHERE o.opened >= a.d",
    "WITH a AS (SELECT MIN(opened) AS d FROM orders WHERE status = 'paid') "
    "SELECT o.status, COUNT(*) AS n FROM orders o, a WHERE o.opened > a.d GROUP BY o.status",
    "WITH x AS (SELECT COUNT(*) FILTER (WHERE status = 'paid') AS n FROM orders) SELECT n FROM x",
    "WITH x AS (SELECT customer_id, SUM(CASE WHEN status = 'paid' THEN total_amount END) AS n "
    "FROM orders GROUP BY customer_id) SELECT customer_id, n FROM x",
    "WITH x AS (SELECT SUM(total_amount) AS s FROM orders WHERE customer_id = 1) "
    "SELECT ROUND(s, 2) AS n FROM x",
    "WITH s(a, b) AS (SELECT status, COUNT(*) FROM orders GROUP BY status) SELECT a, b AS n FROM s",
    "SELECT s.a, s.b AS n FROM (SELECT status, SUM(total_amount) FROM orders GROUP BY status) "
    "AS s(a, b)",
    "WITH x AS (SELECT o.id, o.total_amount FROM orders o WHERE o.is_deleted = false) "
    "SELECT SUM(total_amount) AS n FROM x",
    "WITH x AS (SELECT o.id, o.total_amount, o.status FROM orders o WHERE o.is_deleted = false "
    "AND o.status <> 'open') SELECT SUM(total_amount) AS n FROM x",
    "WITH x AS (SELECT id, status, total_amount FROM orders WHERE customer_id = 3) "
    "SELECT SUM(x.total_amount) AS n FROM x WHERE x.status = 'paid'",
    "WITH x AS (SELECT number, type FROM change_requests WHERE state = 'Work in Progress') "
    "SELECT type, COUNT(DISTINCT number) AS n FROM x GROUP BY type",
    "WITH x AS (SELECT DISTINCT number, type FROM change_requests WHERE state = 'Closed') "
    "SELECT COUNT(DISTINCT number) AS n FROM x",
    "WITH x AS (SELECT closed, total_amount, customer_id FROM orders WHERE closed IS NOT NULL) "
    "SELECT customer_id, SUM(total_amount) AS n FROM x GROUP BY customer_id",
    "WITH p AS (SELECT order_id, amount FROM payments WHERE status = 'paid') "
    "SELECT SUM(amount) AS n FROM p",
    "WITH p AS (SELECT order_id AS o, amount AS a FROM payments WHERE status <> 'refunded') "
    "SELECT o, SUM(a) AS n FROM p GROUP BY o",
    "WITH t AS (SELECT type, COUNT(DISTINCT number) AS c FROM change_requests GROUP BY type), "
    "total AS (SELECT COUNT(DISTINCT number) AS all_c FROM change_requests) "
    "SELECT t.type, t.c AS n FROM t CROSS JOIN total",
]

# The least each engine must actually compare: 51 on SQLite and 53 on DuckDB when written. The rest
# are skipped for a join on the path, a group key the statement does not return, a step named like
# its table, or an engine without `AS s(a, b)` — so a run where more is skipped is not a pass.
MIN_COMPARED = 50


def _org(tmp_path, name: str, storage_type: str, metrics: dict):
    root = tmp_path / name
    root.mkdir()
    _write_model(root, storage_type=storage_type, metrics=metrics)
    return L.load_datasource(root)


def _matches(org, tree) -> list:
    """(output column, `_ColumnMatch`) per output column, built the way `assemble_receipt` builds
    them — the condition NODES are needed, and the receipt carries only their text."""
    dialect = rt._dialect_of(org)[0]
    storage = rt._storage_type_of(org)
    cands = rt._metric_candidates(org, storage, dialect)
    tidx = rt._model_table_index(org)
    visible = set(tidx) - rt._cte_names(tree)
    tr = rt._Trace(dialect, tidx, visible, {}, {}, {}, {})
    out = []
    for oc in rt._output_columns(tree):
        out.append(
            (
                oc,
                rt._match_output_column(
                    oc,
                    rt._declarations_unread(org, storage, cands),
                    rt._by_binding(cands),
                    visible,
                    dialect,
                    tr.amemo,
                    rt._by_binding(cands, grouped=True),
                    rt._binding_shapes(cands),
                    tidx=tidx,
                    trace=tr,
                    second_order=rt._second_order_index(cands, storage),
                ),
            )
        )
    return out, tr


def _to_table(
    node: exp.Expression, sel: exp.Select, tr, computing: exp.Select
) -> "exp.Expression | str":
    """A condition (or key) written in `sel`, rewritten onto the table's own columns: a column read
    through steps is the table column behind it, unqualified; a lookup step's column is the scalar
    subquery computing it. A string says why it cannot be."""
    out = node.copy()
    for orig, copy in zip(rt._own_nodes(node, exp.Column), rt._own_nodes(out, exp.Column)):
        src = rt._source_named(sel, orig, tr.smemo)
        step = rt._step_of(src, tr.smemo) if src is not None else None
        if step is not None and step.one_row:
            proj = rt._projection_for(step.body, orig.name, step.listed)
            if proj is None:
                return "a lookup column cannot be found"
            body = step.body.copy()
            body.set("expressions", [(proj.this if isinstance(proj, exp.Alias) else proj).copy()])
            new: exp.Expression = exp.Subquery(this=body)
        else:
            found = rt._ground(orig, sel, tr.smemo, set())
            if found is None:
                target = rt._follow_to(orig, sel, computing, tr.smemo)
                found = (
                    rt._ground(target, computing, tr.smemo, set()) if target is not None else None
                )
            if found is None:
                return "a column cannot be followed to the table"
            new = exp.Column(this=found[2].this.copy())
        if copy is out:
            out = new
        else:
            copy.replace(new)
    return out


def _check(org, sql: str, conn) -> truth.Outcome:
    out = truth.Outcome()
    dialect = rt._dialect_of(org)[0]
    receipt = rt.assemble_receipt(org, sql)
    items = [i for i in receipt["columns"]["items"] if i["kind"] == "output"]
    tree = rt._parse_sql(sql, dialect)
    matches, tr = _matches(org, tree)
    applied = [
        f["expr"]
        for t in receipt["tables"]["items"]
        for f in (t.get("filters") or [])
        if f["status"] == "applied"
    ]
    for item, (oc, cm) in zip(items, matches):
        if item["status"] != rt.MATCHED:
            continue
        why = _reference(item, oc, cm, tr, applied, dialect)
        if isinstance(why, str):
            out.skipped.append((sql, item["column"], why))
            continue
        statement, reference = why
        got, want = truth._rows(conn, statement), truth._rows(conn, reference)
        if isinstance(got, str) or isinstance(want, str):
            out.skipped.append((sql, item["column"], f"the engine rejected it ({got}, {want})"))
            continue
        out.compared += 1
        ref = {tuple(truth._norm(v) for v in r[:-1]): truth._norm(r[-1]) for r in want}
        for row in got:
            key, value = tuple(truth._norm(v) for v in row[:-1]), truth._norm(row[-1])
            # A group the metric's side lacks holds the value of an aggregate over no rows, or it
            # is a mismatch (see `receipt_truth`).
            agrees = ref[key] == value if key in ref else value in (None, 0.0)
            if not agrees:
                out.mismatches.append(
                    (
                        sql,
                        item["column"],
                        f"group {key}: statement {value!r}, metric {ref.get(key)!r}",
                    )
                )
                break
    return out


def _reference(item: dict, oc, cm, tr, applied: list, dialect) -> "tuple[str, str] | str":
    if item["joins_not_declared"] or any(rt._WITHHELD in t for t in item["restrictions"]):
        return "a join on the path, or a withheld value"
    flow = rt._flow(oc, tr)
    computing = flow.sel
    read, wide = rt._value_reads(
        rt._peeled(
            rt._equivalent_form(
                rt._fold_unquoted_identifiers(flow.expr), False, rt._count_kind(dialect)
            )
        )[0],
        computing,
        tr,
    )
    path = rt._path(computing, frozenset(wide), tr)
    tables = set(path.amap.values())
    if len(path.amap) != 1:
        return "the counted rows come from more than one table"
    table = next(iter(tables))
    if table in rt._cte_names(oc.sel.root()):
        return "a step shadows the table's name"
    conds = []
    for node in cm.restrictions:
        # The column's own FILTER or CASE condition is a peeled copy, written in the computing step.
        sel = rt._enclosing_select(node) or computing
        rebuilt = _to_table(node, sel, tr, computing)
        if isinstance(rebuilt, str):
            return rebuilt
        conds.append(rebuilt)
    for text in applied:
        parsed = rt._parse_declared_predicate(text, dialect)
        if parsed is None:
            return "a declared filter does not parse"
        conds.append(rt._strip_qualifiers(parsed))
    keys = rt._group_columns(computing) if rt._arm_is_grouped(computing) else []
    group = computing.args.get("group")
    if group is not None and len(keys) != len(group.expressions):
        return "a group key is not a column"
    base_keys = []
    for k in keys:
        rebuilt = _to_table(k, computing, tr, computing)
        if isinstance(rebuilt, str):
            return rebuilt
        base_keys.append(rebuilt)
    # The statement's side: its own output, the keys the computing step groups by, then the value.
    others = [o for o in rt._output_columns(oc.sel.root()) if o.sel is oc.sel and o is not oc]
    shown = []
    for k in keys:
        hit = [
            o
            for o in others
            if o.expr is not None
            and isinstance(target := (rt._flow(o, tr).value), exp.Column)
            and rt._flow(o, tr).sel is computing
            and rt._tkey(target.name) == rt._tkey(k.name)
        ]
        if not hit:
            return "a group key the statement does not return"
        shown.append(hit[0].expr.copy())
    statement = oc.sel.root().copy() if oc.sel is oc.sel.root() else None
    if statement is None:
        return "the column is not in the outermost SELECT"
    statement.set(
        "expressions", [*(e.as_(f"k{i}") for i, e in enumerate(shown)), oc.expr.copy().as_("v")]
    )
    statement.set("order", None)
    binding = rt._parse_sql(f"SELECT {item['expression']}", dialect).expressions[0]
    reference = exp.select(*(k.as_(f"k{i}") for i, k in enumerate(base_keys)), binding.as_("v"))
    reference = reference.from_(table)
    if conds:
        reference = reference.where(exp.and_(*conds))
    if base_keys:
        reference = reference.group_by(*base_keys)
    return statement.sql(dialect=dialect), reference.sql(dialect=dialect)


@pytest.mark.parametrize("engine", ["sqlite", "duckdb", "postgres"])
def test_every_matched_claim_through_a_step_agrees_with_the_numbers(tmp_path, engine):
    connect, storage_type = truth.ENGINES[engine]
    if engine == "duckdb":
        pytest.importorskip("duckdb")
    conn = connect()
    if conn is None:
        pytest.skip("set AGAMI_IT_PG_PASSWORD (and a reachable Postgres) to run the Postgres half")
    truth.load_fixture(conn, engine)
    outcome = truth.Outcome()
    try:
        for name, metrics in (("all", ALL_METRICS), ("unfiltered", UNFILTERED)):
            org = _org(tmp_path, name, storage_type, metrics)
            for sql in STEP_MATCHED + STEP_TRICKS:
                outcome.add(_check(org, sql, conn))
    finally:
        conn.close()
    assert outcome.mismatches == []
    assert outcome.compared >= MIN_COMPARED, (outcome.compared, outcome.skipped)


def test_the_check_catches_a_step_restriction_left_unlisted(tmp_path, monkeypatch):
    """Teeth: with the conditions of every step dropped from what the receipt collects, a count over
    rows a step filtered reads as the plain metric — and the numbers disagree."""
    connect, storage_type = truth.ENGINES["sqlite"]
    conn = connect()
    truth.load_fixture(conn, "sqlite")
    org = _org(tmp_path, "u", storage_type, UNFILTERED)
    real = rt._arm_restrictions

    def hiding(sel, *args, **kwargs):
        got = real(sel, *args, **kwargs)
        return got._replace(rows=[]) if sel is not sel.root() else got

    monkeypatch.setattr(rt, "_arm_restrictions", hiding)
    outcome = _check(
        org, "SELECT COUNT(*) AS n FROM (SELECT id FROM orders WHERE status = 'paid') s", conn
    )
    conn.close()
    assert outcome.compared == 1 and len(outcome.mismatches) == 1, outcome
