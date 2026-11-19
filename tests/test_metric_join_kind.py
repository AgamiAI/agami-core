"""A two-table metric can say how its tables join, and a figure written as its binding over that
join is credited as the metric.

The shape: an approved first-contact-resolution count over tickets and their
handoff history, written word for word with a LEFT JOIN, which is right for it — a ticket never
handed off has no history row. Its binding does not say how the tables join, and an outer join's
null-extended rows can change the figure, so the receipt left it `undetermined` with `unconfirmed`
naming the metric. A metric now declares `joins: [{table, kind}]`; a statement
whose join between the metric's tables is that kind, on a declared relationship's key, is matched.
Any other join stays exactly as before: fail closed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from semantic_model import curate  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402
from semantic_model import validator as V  # noqa: E402

from test_curate_edit_goes_stale import _edit, _write_model  # noqa: E402

BINDING = (
    "COUNT(DISTINCT CASE WHEN state = 'closed' AND (reassignments = 0 OR (reassignments = 1 "
    "AND kind = 'routing' AND value = 'front_desk')) THEN number END)"
)
WRITTEN = (
    BINDING.replace("state", "t.state")
    .replace("reassignments", "t.reassignments")
    .replace("kind", "h.kind")
    .replace("value", "h.value")
    .replace("THEN number", "THEN t.number")
)
# The key alone, and the usual shape: the key plus a condition on the history in the ON. Without a
# declaration the second is the one left unconfirmed (an ON condition on the optional side, whose
# null-extended rows still count); the first, undeclared, is still read as a join that keeps only
# the tickets with a history row, unchanged here.
KEYED = (
    f"SELECT {WRITTEN} AS first_contact_resolved FROM public.tickets t "
    "LEFT JOIN public.handoffs h ON h.ticket_id = t.id"
)
LEFT = KEYED + " AND h.kind = 'routing'"
INNER = LEFT.replace("LEFT JOIN", "JOIN")


def _col(name: str, kind: str = "string") -> m.Column:
    return m.Column(name=name, type=kind)


def _org(joins: "list | None" = None, *, related: bool = True) -> m.Datasource:
    metric = {
        "name": "first_contact_resolutions",
        "description": "Tickets closed on first contact.",
        "calculation": "Closed tickets never handed off, or handed off once from the front desk.",
        "bindings": {"PostgreSQL": BINDING},
        "source_tables": ["tickets", "handoffs"],
        "review_state": "approved",
        "signed_off_by": "you@example.com",
        "signed_off_at": "2026-01-01T00:00:00Z",
        "signed_off_role": "data_lead",
    }
    if joins is not None:
        metric["joins"] = joins
    rels = (
        [
            m.Relationship(
                from_table="handoffs",
                to_table="tickets",
                from_column="ticket_id",
                to_column="id",
                relationship="many_to_one",
            )
        ]
        if related
        else []
    )
    return m.Datasource(
        datasource="acme",
        storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
        subject_areas=[
            m.SubjectArea(
                name="support",
                tables_defined=[
                    m.Table(
                        name="tickets",
                        schema="public",
                        storage_connection="c",
                        grain=["id"],
                        columns=[
                            _col("id"),
                            _col("number"),
                            _col("state"),
                            _col("reassignments", "integer"),
                        ],
                    ),
                    m.Table(
                        name="handoffs",
                        schema="public",
                        storage_connection="c",
                        grain=["id"],
                        columns=[_col("id"), _col("ticket_id"), _col("kind"), _col("value")],
                    ),
                ],
                relationships=rels,
                metrics=[m.Metric(**metric)],
            )
        ],
    )


def _first(org: m.Datasource, sql: str) -> dict:
    items = rt.assemble_receipt(org, sql)["columns"]["items"]
    return next(i for i in items if i["kind"] == "output")


UNCONFIRMED = {"name": "first_contact_resolutions", "area": "support"}


# --- the runtime rule ----------------------------------------------------------------------------


def test_a_left_join_the_metric_declares_is_credited_as_the_metric():
    # On the key alone. This once credited the live shape (`LEFT`, a history condition in the ON)
    # and listed that condition as a restriction, but the same line is what a WHERE on the history
    # lists, and the two count differently (2 against 1 on the rows below): that shape is now left
    # unconfirmed (`far_side_in_on`), and the credited one is the join exactly as declared.
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), KEYED)
    assert item["status"] == rt.MATCHED and item["name"] == "first_contact_resolutions"
    assert item["review_state"] == "approved" and item["unconfirmed"] is None
    assert not item["joins_not_declared"]
    # The declared join itself is not a restriction.
    assert not any("matching" in r for r in item["restrictions"])


def test_a_declared_left_join_on_the_key_alone_lists_no_restriction():
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), KEYED)
    assert item["status"] == rt.MATCHED and item["name"] == "first_contact_resolutions"
    # Not "only handoffs rows that have a matching tickets row": the metric keeps a ticket with none.
    assert not item["restrictions"]


def test_a_left_join_with_no_declaration_stays_unconfirmed():
    item = _first(_org(), LEFT)
    assert item["status"] == rt.UNDETERMINED and item["name"] is None
    assert item["unconfirmed"] == UNCONFIRMED


def test_an_inner_join_where_the_metric_declares_left_is_not_the_metric():
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), INNER)
    assert item["status"] == rt.UNDETERMINED and item["name"] is None
    assert item["unconfirmed"] == UNCONFIRMED


@pytest.mark.parametrize("sql", [LEFT, KEYED], ids=["live", "keyed"])
def test_a_left_join_where_the_metric_declares_inner_stays_unconfirmed(sql):
    item = _first(_org([{"table": "handoffs", "kind": "inner"}]), sql)
    assert item["status"] == rt.UNDETERMINED and item["unconfirmed"] == UNCONFIRMED


def test_an_inner_join_the_metric_declares_is_credited():
    sql = KEYED.replace("LEFT JOIN", "JOIN")
    item = _first(_org([{"table": "handoffs", "kind": "inner"}]), sql)
    assert item["status"] == rt.MATCHED and item["name"] == "first_contact_resolutions"


# A declared inner join is credited on the same terms as a declared LEFT one: its ON exactly the
# declared relationship's key, nothing more. Otherwise a declaration of `kind: inner` would credit
# a join on another relationship altogether (`h.ticket_id = t.number`, which the joins section
# reads `undeclared`). Fails closed: `undetermined`, the metric named as unconfirmed.
INNER_NOT_CREDITED = {
    "another_key": KEYED.replace("LEFT JOIN", "JOIN").replace("t.id", "t.number"),
    "extra_on_condition": INNER,
    "extra_key": KEYED.replace("LEFT JOIN", "JOIN") + " AND h.ticket_id = t.number",
}


@pytest.mark.parametrize("sql", list(INNER_NOT_CREDITED.values()), ids=list(INNER_NOT_CREDITED))
def test_a_declared_inner_join_with_other_than_its_key_is_not_credited(sql):
    item = _first(_org([{"table": "handoffs", "kind": "inner"}]), sql)
    assert item["status"] == rt.UNDETERMINED and item["name"] is None
    assert item["unconfirmed"] == UNCONFIRMED


def test_a_left_join_on_another_key_is_not_credited():
    sql = LEFT.replace("h.ticket_id = t.id", "h.ticket_id = t.number")
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), sql)
    assert item["status"] != rt.MATCHED and item["name"] is None


def test_a_left_join_with_no_declared_relationship_is_not_credited():
    item = _first(_org([{"table": "handoffs", "kind": "left"}], related=False), LEFT)
    assert item["status"] != rt.MATCHED and item["name"] is None


def test_a_left_join_from_the_other_side_is_not_credited():
    # The tickets are now the optional side: a different figure from the one declared.
    sql = (
        f"SELECT {WRITTEN} AS first_contact_resolved FROM public.handoffs h "
        "LEFT JOIN public.tickets t ON h.ticket_id = t.id"
    )
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), sql)
    assert item["status"] != rt.MATCHED and item["name"] is None


def test_a_full_join_is_never_credited():
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), LEFT.replace("LEFT", "FULL"))
    assert item["status"] != rt.MATCHED and item["name"] is None


def test_a_windowed_figure_is_never_credited():
    sql = LEFT.replace(" AS first_contact_resolved", " OVER () AS first_contact_resolved")
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), sql)
    assert item["status"] != rt.MATCHED and item["name"] is None


# --- a declared LEFT join is credited only as written: the exact key, nothing else on the far side
#
# Each shape below gives a different figure from the key-alone join over the same rows (counted in
# DuckDB beside it), and each once came back `matched` with a receipt that could not tell it from
# the key-alone one. A receipt fix fails closed: `undetermined`, naming the metric as unconfirmed.

KEPT_WHERE = KEYED + " WHERE t.state = 'open'"
JOINED = "FROM public.tickets t LEFT JOIN public.handoffs h ON h.ticket_id = t.id"


def _filtered(cond: str) -> str:
    return f"SELECT {WRITTEN} FILTER (WHERE {cond}) AS first_contact_resolved {JOINED}"


def _outside(column: str, value: str) -> str:
    # The figure counted per group inside, and one group picked by an outer query's WHERE.
    return (
        f"SELECT s.fcr FROM (SELECT {column} AS g, {WRITTEN} AS fcr {JOINED} "
        f"GROUP BY {column}) s WHERE s.g = '{value}'"
    )


NOT_CREDITED = {
    "extra_key_on_ids": KEYED + " AND h.id = t.id",
    "extra_key_on_number": KEYED + " AND h.ticket_id = t.number",
    # The live shape: the history condition in the ON keeps every ticket, the same text in the
    # WHERE keeps only tickets with such a row. The restriction line read the same for both.
    "far_side_in_on": LEFT,
    "far_side_in_where": KEYED + " WHERE h.kind = 'routing'",
    # A kept-side condition in the ON filters nothing (each ticket is kept, partner or not); in the
    # WHERE it drops tickets. Both listed `t.state = 'open'`.
    "kept_side_in_on": KEYED + " AND t.state = 'open'",
    # An inner join in effect, which written as INNER is already refused.
    "far_side_not_null": KEYED + " WHERE h.id IS NOT NULL",
    "far_side_in_having": KEYED + " HAVING MAX(h.kind) = 'routing'",
    # A subquery is not looked inside: a correlated one reads the far side all the same, and
    # whichever side one reads, it is refused rather than analysed.
    "far_side_in_exists": KEYED + " WHERE EXISTS (SELECT 1 WHERE h.kind = 'routing')",
    "subquery_in_where": KEYED
    + " WHERE t.id IN (SELECT ticket_id FROM public.handoffs WHERE kind = 'routing')",
    # The far side read where the figure's own rows are restricted other than by a WHERE: the
    # aggregate's FILTER, and an outer query picking a group. The same as `far_side_in_where`.
    "far_side_in_filter": _filtered("h.kind = 'routing'"),
    "far_side_outside": _outside("h.kind", "routing"),
}


@pytest.mark.parametrize("sql", list(NOT_CREDITED.values()), ids=list(NOT_CREDITED))
def test_a_declared_left_join_with_more_than_its_key_is_not_credited(sql):
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), sql)
    assert item["status"] == rt.UNDETERMINED and item["name"] is None
    assert item["unconfirmed"] == UNCONFIRMED


def test_a_kept_side_where_filter_on_the_declared_left_join_is_credited_and_listed():
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), KEPT_WHERE)
    assert item["status"] == rt.MATCHED and item["name"] == "first_contact_resolutions"
    assert item["restrictions"] == ["t.state = 'open'"]


@pytest.mark.parametrize(
    "sql", [_filtered("t.state = 'open'"), _outside("t.state", "open")], ids=["filter", "outside"]
)
def test_a_kept_side_filter_or_outer_where_is_credited_and_listed(sql):
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), sql)
    assert item["status"] == rt.MATCHED and item["name"] == "first_contact_resolutions"
    assert item["restrictions"] == ["t.state = 'open'"]


@pytest.fixture()
def duck():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE SCHEMA IF NOT EXISTS public")
    con.execute(
        "CREATE TABLE public.tickets (id VARCHAR, number VARCHAR, state VARCHAR, "
        "reassignments INTEGER)"
    )
    con.execute(
        "INSERT INTO public.tickets VALUES ('t1', 'N1', 'closed', 0), ('t2', 'N2', 'closed', 1), "
        "('t3', 'N3', 'open', 0)"
    )
    con.execute(
        "CREATE TABLE public.handoffs (id VARCHAR, ticket_id VARCHAR, kind VARCHAR, value VARCHAR)"
    )
    con.execute(
        "INSERT INTO public.handoffs VALUES ('h1', 't2', 'routing', 'front_desk'), "
        "('h2', 't2', 'other', 'front_desk')"
    )
    yield con
    con.close()


def test_each_refused_shape_counts_something_other_than_its_credited_twin(duck):
    def count(sql: str) -> int:
        return duck.execute(sql).fetchone()[0]

    keyed, kept_where = count(KEYED), count(KEPT_WHERE)
    assert (keyed, kept_where) == (2, 0)
    got = {name: count(sql) for name, sql in NOT_CREDITED.items() if name != "far_side_in_having"}
    assert got["extra_key_on_ids"] != keyed and got["extra_key_on_number"] != keyed
    assert got["far_side_in_on"] != got["far_side_in_where"]
    assert got["kept_side_in_on"] != kept_where
    assert got["far_side_not_null"] != keyed
    assert got["far_side_in_exists"] != keyed and got["subquery_in_where"] != keyed
    assert got["far_side_in_filter"] != keyed and got["far_side_outside"] != keyed


# --- every other place the far side, or a change to the rows, could hide on the counted path ---
#
# The rule: credited only when nothing on the counted path reads the optional side and the join is
# exactly the declared one. Each shape below is refused; some were already, and stay pinned here.

COLS = (
    "t.id AS id, t.number AS number, t.state AS state, t.reassignments AS reassignments, "
    "h.kind AS kind, h.value AS value"
)
OVER_STEP = (
    BINDING.replace("state", "s.state")
    .replace("reassignments", "s.reassignments")
    .replace("kind", "s.kind")
    .replace("value", "s.value")
    .replace("THEN number", "THEN s.number")
)
STEP = f"(SELECT {COLS} {JOINED})"


def _over_step(tail: str = "", *, head: str = "", inner: str = "") -> str:
    step = STEP if not inner else f"(SELECT {COLS} {JOINED} {inner})"
    return f"SELECT {head}{OVER_STEP} AS fcr FROM {step} s {tail}".strip()


SWEEP_REFUSED = {
    # A WITH step or derived table, the far side filtered outside it.
    "step_filter_outside": _over_step("WHERE s.kind = 'routing'"),
    "cte_filter_outside": f"WITH s AS {STEP} SELECT {OVER_STEP} AS fcr FROM s "
    "WHERE s.kind = 'routing'",
    "cte_not_null_outside": f"WITH s AS {STEP} SELECT {OVER_STEP} AS fcr FROM s "
    "WHERE s.value IS NOT NULL",
    "far_filter_inside_step": _over_step(inner="WHERE h.kind = 'routing'"),
    "filter_over_step": _over_step(head="").replace(
        " AS fcr", " FILTER (WHERE s.kind = 'routing') AS fcr"
    ),
    # Windows and QUALIFY over the far side.
    "qualify_far": _over_step(
        inner="QUALIFY ROW_NUMBER() OVER (PARTITION BY t.id ORDER BY h.kind) = 1"
    ),
    "window_far_filtered": f"SELECT {OVER_STEP} AS fcr FROM (SELECT {COLS}, ROW_NUMBER() OVER "
    f"(PARTITION BY t.id ORDER BY h.kind) AS rn {JOINED}) s WHERE s.rn = 1",
    "distinct_on_in_step": f"SELECT {OVER_STEP} AS fcr FROM (SELECT DISTINCT ON (t.id) {COLS} "
    f"{JOINED} ORDER BY t.id, h.kind) s",
    "distinct_on": f"SELECT DISTINCT ON (h.kind) {WRITTEN} AS fcr {JOINED} GROUP BY h.kind",
    # A far-side read inside the aggregate that is not the binding: not this metric at all.
    "case_far_extra": f"SELECT COUNT(DISTINCT CASE WHEN h.kind = 'x' THEN NULL "
    f"ELSE t.number END) AS fcr {JOINED}",
    "case_wrap_far": f"SELECT {WRITTEN.replace('CASE WHEN', 'CASE WHEN h.id IS NOT NULL AND', 1)}"
    f" AS fcr {JOINED}",
    # Grouped, or groups picked, by the far side, however the key is spelled and wherever.
    "group_far": KEYED + " GROUP BY h.kind",
    "group_far_rollup": KEYED + " GROUP BY ROLLUP (h.kind)",
    "group_far_and_kept": KEYED + " GROUP BY t.state, h.value",
    "group_far_ordinal": f"SELECT h.kind AS g, {WRITTEN} AS fcr {JOINED} GROUP BY 1",
    "group_far_alias": f"SELECT h.kind AS g, {WRITTEN} AS fcr {JOINED} GROUP BY g",
    "group_far_over_step": _over_step("GROUP BY s.kind", head="s.kind AS g, "),
    "group_far_ordinal_over_step": _over_step("GROUP BY 1", head="s.kind AS g, "),
    "group_far_in_cte": f"WITH s AS {STEP} SELECT s.kind AS g, {OVER_STEP} AS fcr FROM s "
    "GROUP BY s.kind",
    "having_far": KEYED + " GROUP BY t.state HAVING COUNT(h.id) > 0",
    "having_far_over_step": _over_step(
        "GROUP BY s.state HAVING MAX(s.kind) = 'routing'", head="s.state AS g, "
    ),
    # LATERAL and UNNEST.
    "lateral": KEYED + ", LATERAL (SELECT 1 AS one WHERE h.kind = 'routing') l",
    "unnest": KEYED + " CROSS JOIN UNNEST([1, 2]) AS u(x)",
    # The far table joined again, the join spelled another way, or another join on its side.
    "second_far_inner": KEYED + " JOIN public.handoffs h2 ON h2.ticket_id = t.id",
    "self_join_far": KEYED + " JOIN public.handoffs h2 ON h2.id = h.id",
    "right_from_the_other_side": f"SELECT {WRITTEN} AS fcr FROM public.handoffs h "
    "RIGHT JOIN public.tickets t ON h.ticket_id = t.id",
    "using": f"SELECT {WRITTEN} AS fcr FROM public.tickets t LEFT JOIN public.handoffs h "
    "USING (id)",
    "natural": f"SELECT {WRITTEN} AS fcr FROM public.tickets t NATURAL LEFT JOIN "
    "public.handoffs h",
    "on_true": f"SELECT {WRITTEN} AS fcr FROM public.tickets t LEFT JOIN public.handoffs h "
    "ON TRUE",
    "inner_on_true_mixed": KEYED + " JOIN public.tickets t2 ON TRUE",
    "cross_join_far_filter": KEYED + " CROSS JOIN public.tickets t2 WHERE t2.id = h.ticket_id",
}
# Nothing on the path reads the far side and the join is the declared one: still credited.
SWEEP_CREDITED = {
    "over_step": _over_step(),
    "in_cte": f"WITH s AS {STEP} SELECT {OVER_STEP} AS fcr FROM s",
    "group_kept": KEYED + " GROUP BY t.state",
    "group_kept_over_step": _over_step("GROUP BY s.state", head="s.state AS g, "),
    "kept_filter_inside_step": _over_step(inner="WHERE t.state = 'open'"),
    # Repeats the counted rows without reading them: a distinct count is unmoved (2 below).
    "second_far_left_unread": KEYED + " LEFT JOIN public.handoffs h2 ON h2.ticket_id = t.id",
}


def _figure(org: m.Datasource, sql: str) -> dict:
    items = rt.assemble_receipt(org, sql)["columns"]["items"]
    return next(i for i in items if i["kind"] == "output" and i["aggregate"])


@pytest.mark.parametrize("sql", list(SWEEP_REFUSED.values()), ids=list(SWEEP_REFUSED))
def test_a_far_side_read_or_a_changed_row_set_anywhere_on_the_path_is_not_credited(sql):
    item = _figure(_org([{"table": "handoffs", "kind": "left"}]), sql)
    assert item["status"] != rt.MATCHED and item["name"] is None


@pytest.mark.parametrize("sql", list(SWEEP_CREDITED.values()), ids=list(SWEEP_CREDITED))
def test_the_declared_join_with_nothing_reading_its_far_side_is_still_credited(sql):
    item = _figure(_org([{"table": "handoffs", "kind": "left"}]), sql)
    assert item["status"] == rt.MATCHED and item["name"] == "first_contact_resolutions"


def test_the_swept_shapes_count_something_other_than_the_declared_join(duck):
    def rows(sql: str) -> list:
        return duck.execute(sql).fetchall()

    keyed = rows(KEYED)[0][0]
    assert keyed == 2
    for name in ("step_filter_outside", "cte_filter_outside", "cte_not_null_outside",
                 "far_filter_inside_step", "filter_over_step", "qualify_far",
                 "window_far_filtered", "distinct_on_in_step", "case_wrap_far", "lateral",
                 "second_far_inner", "self_join_far", "using", "natural",
                 "cross_join_far_filter"):
        assert rows(SWEEP_REFUSED[name]) != [(keyed,)], name
    # Grouping by the far side is the far-side WHERE, one group at a time.
    where = rows(KEYED + " WHERE h.kind = 'routing'")[0][0]
    grouped = dict(rows(SWEEP_REFUSED["group_far_over_step"]))
    assert grouped["routing"] == where != keyed
    assert {r[0] for r in rows(SWEEP_CREDITED["second_far_left_unread"])} == {keyed}


# --- how a grouping key is spelled: an alias an input column shadows, ordinals, GROUP BY ALL ---
#
# A grouping name that is both a select-list alias and an input column binds differently by engine
# (SQLite and MySQL can read the input column, Postgres the alias), so it is refused whichever it
# would be. An ordinal is a grouping item that is itself an integer, inside ROLLUP, CUBE or GROUPING
# SETS too; an integer inside an expression is a constant and reads nothing.

KIND_IS_STATE = "s.state AS kind, "
GROUPING_REFUSED = {
    "alias_shadowed_by_input": _over_step("GROUP BY kind, s.state", head=KIND_IS_STATE),
    "alias_shadowed_direct": f"SELECT t.state AS kind, {WRITTEN} AS fcr {JOINED} GROUP BY kind",
    "alias_shadowed_in_rollup": _over_step("GROUP BY ROLLUP (kind)", head=KIND_IS_STATE),
    "alias_shadowed_in_cube": _over_step("GROUP BY CUBE (kind)", head=KIND_IS_STATE),
    "alias_shadowed_in_grouping_sets": _over_step(
        "GROUP BY GROUPING SETS ((kind), ())", head=KIND_IS_STATE
    ),
    "alias_shadowed_in_having": _over_step(
        "GROUP BY s.state HAVING MAX(kind) = 'routing'", head=KIND_IS_STATE
    ),
    "alias_shadowed_other_case": _over_step("GROUP BY kind", head='s.state AS "Kind", '),
    "alias_shadowed_quoted": _over_step('GROUP BY "KIND"', head=KIND_IS_STATE),
    "aliases_differ_only_by_case": _over_step("GROUP BY g", head='s.kind AS "G", s.state AS g, '),
    "alias_to_correlated_subquery": _over_step(
        "GROUP BY g",
        head="(SELECT MAX(h2.kind) FROM public.handoffs h2 WHERE h2.ticket_id = s.id) AS g, ",
    ),
    "ordinal_far_in_rollup": _over_step("GROUP BY ROLLUP (1)", head="s.kind AS g, "),
    "ordinal_far_in_cube": _over_step("GROUP BY CUBE (1)", head="s.kind AS g, "),
    "ordinal_far_in_grouping_sets": _over_step(
        "GROUP BY GROUPING SETS ((1), ())", head="s.kind AS g, "
    ),
    "ordinal_far_second": _over_step("GROUP BY 1, 2", head="s.state AS g, s.kind AS k, "),
    "ordinal_out_of_range": _over_step("GROUP BY 3", head="s.state AS g, "),
    "ordinal_out_of_range_in_rollup": _over_step("GROUP BY ROLLUP (3)", head="s.state AS g, "),
    "ordinal_zero": _over_step("GROUP BY 0", head="s.state AS g, "),
    "distinct_on_ordinal_far": f"SELECT DISTINCT ON (1) MAX(s.kind) AS m, s.state AS g, "
    f"{OVER_STEP} AS fcr FROM {STEP} s GROUP BY s.state",
    "group_all_far": _over_step("GROUP BY ALL", head="s.kind AS g, "),
    "group_all_kept": _over_step("GROUP BY ALL", head="s.state AS g, "),
    "mixed_kept_and_far": _over_step("GROUP BY s.state || s.kind", head="s.state || s.kind AS g, "),
    "mixed_in_coalesce": _over_step("GROUP BY COALESCE(s.state, s.kind)"),
}
GROUPING_CREDITED = {
    "coalesce_constant": KEYED + " GROUP BY COALESCE(t.reassignments, 0)",
    "coalesce_constant_over_step": _over_step(
        "GROUP BY COALESCE(s.reassignments, 0)", head="COALESCE(s.reassignments, 0) AS g, "
    ),
    "arithmetic_constant": _over_step("GROUP BY s.reassignments + 1"),
    "constant_in_rollup": _over_step("GROUP BY ROLLUP (COALESCE(s.reassignments, 0))"),
    "ordinal_kept": _over_step("GROUP BY 1", head="s.state AS g, "),
    "ordinal_kept_in_rollup": _over_step("GROUP BY ROLLUP (1)", head="s.state AS g, "),
    "ordinal_kept_in_grouping_sets": _over_step(
        "GROUP BY GROUPING SETS ((1), ())", head="s.state AS g, "
    ),
    "alias_no_input_namesake": _over_step("GROUP BY g", head="s.state AS g, "),
}


@pytest.mark.parametrize("sql", list(GROUPING_REFUSED.values()), ids=list(GROUPING_REFUSED))
def test_a_grouping_key_that_may_read_the_far_side_is_not_credited(sql):
    item = _figure(_org([{"table": "handoffs", "kind": "left"}]), sql)
    assert item["status"] != rt.MATCHED and item["name"] is None


@pytest.mark.parametrize("sql", list(GROUPING_CREDITED.values()), ids=list(GROUPING_CREDITED))
def test_a_grouping_key_that_reads_only_the_kept_side_is_credited(sql):
    item = _figure(_org([{"table": "handoffs", "kind": "left"}]), sql)
    assert item["status"] == rt.MATCHED and item["name"] == "first_contact_resolutions"


# --- the model and validator ---------------------------------------------------------------------


def test_an_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        _org([{"table": "handoffs", "kind": "full"}])


def test_a_table_outside_source_tables_is_rejected():
    with pytest.raises(ValueError, match="source_tables"):
        _org([{"table": "agents", "kind": "left"}])


def test_a_table_declared_twice_is_rejected():
    with pytest.raises(ValueError, match="once"):
        _org([{"table": "handoffs", "kind": "left"}, {"table": "handoffs", "kind": "inner"}])


def test_a_declared_join_with_no_relationship_is_flagged():
    res = V.validate(_org([{"table": "handoffs", "kind": "left"}], related=False))
    assert any(f.code == "metric_join_unrelated" for f in res.findings), res.findings
    assert V.validate(_org([{"table": "handoffs", "kind": "left"}])).ok


def test_a_metric_without_joins_dumps_without_the_key():
    # So a file written by this release still loads on an older one, which forbids unknown keys.
    assert "joins" not in _org().subject_areas[0].metrics[0].model_dump()
    dumped = _org([{"table": "handoffs", "kind": "left"}]).subject_areas[0].metrics[0].model_dump()
    assert dumped["joins"] == [{"table": "handoffs", "kind": "left"}]


# --- curation ------------------------------------------------------------------------------------


def test_curate_edits_a_metrics_joins(tmp_path):
    root = tmp_path / "demo"
    _write_model(root)
    ops = [
        _edit("metric", "revenue", "source_tables", ["orders", "customers"]),
        _edit("metric", "revenue", "joins", [{"table": "customers", "kind": "left"}]),
    ]
    res = curate.apply(root, ops, signer="editor@example.com", role="analyst")
    assert res.validated, res.errors
    met = L.load_datasource(root, include_rejected=True).subject_areas[0].metrics[0]
    assert [(j.table, j.kind) for j in met.joins] == [("customers", "left")]
    assert met.review_state == "stale", "how the tables join changes what the metric counts"

    res = curate.apply(
        root, [_edit("metric", "revenue", "joins", [])], signer="editor@example.com", role="analyst"
    )
    assert res.validated, res.errors
    doc = yaml.safe_load((root / "subject_areas/sales/metrics/revenue.yaml").read_text())
    assert "joins" not in doc, "clearing the joins removes the key"


def test_a_joins_only_edit_on_an_approved_metric_sends_it_back_to_review(tmp_path):
    root = tmp_path / "demo"
    _write_model(root)
    kw = {"signer": "editor@example.com", "role": "analyst"}
    # reach a state where the metric reads both tables and is approved, then change only its joins
    curate.apply(root, [_edit("metric", "revenue", "source_tables", ["orders", "customers"])], **kw)
    res = curate.apply(
        root, [{"op": "approve", "kind": "metric", "area": "sales", "name": "revenue"}], **kw)
    assert res.validated, res.errors
    met = L.load_datasource(root, include_rejected=True).subject_areas[0].metrics[0]
    assert met.review_state == "approved"
    res = curate.apply(
        root, [_edit("metric", "revenue", "joins", [{"table": "customers", "kind": "left"}])], **kw)
    assert res.validated, res.errors
    met = L.load_datasource(root, include_rejected=True).subject_areas[0].metrics[0]
    assert met.review_state == "stale", "a joins-only edit changes what the metric counts"


def test_curate_refuses_a_join_on_a_table_the_metric_does_not_read(tmp_path):
    root = tmp_path / "demo"
    _write_model(root)
    before = (root / "subject_areas/sales/metrics/revenue.yaml").read_text()
    res = curate.apply(
        root,
        [_edit("metric", "revenue", "joins", [{"table": "customers", "kind": "left"}])],
        signer="editor@example.com",
        role="analyst",
    )
    assert not res.validated
    assert (root / "subject_areas/sales/metrics/revenue.yaml").read_text() == before


def test_the_schema_reply_serves_the_declared_join_with_the_binding():
    import tools

    met = _org([{"table": "handoffs", "kind": "left"}]).subject_areas[0].metrics[0]
    entry = tools._metric_full(met, "support", "PostgreSQL")
    assert entry["binding"] == BINDING
    assert entry["joins"] == [{"table": "handoffs", "kind": "left"}]
    assert "joins" not in tools._metric_full(
        _org().subject_areas[0].metrics[0], "support", "PostgreSQL"
    )
