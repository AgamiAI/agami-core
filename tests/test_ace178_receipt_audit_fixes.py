"""ACE-178 part 3 — what an audit of real receipts found the receipt claiming that it could not prove.

Every repro is rewritten over synthetic tables. Five shapes:

1. An OR among a number's conditions was listed bare, so the list a reader joins with AND read
   `(year AND a) OR b`. A condition looser than AND is now listed in brackets, and a condition cut
   to the length bound can never read as a complete, different one.
2. A set operation's column was labelled with the first arm's metric even where another arm counted
   something else. Now an arm's value is `matched` only when every arm's value in that column
   matched the same metric.
3. A filter in a later step on a step column computed from a table's (`... LIKE 'x%' AS flag`, then
   `WHERE p.flag`) was listed by the step's name for it. It is listed over the tables now, or the
   number is undetermined when that cannot be done.
4. A condition on a table reached by one of several declared relationships between the two tables
   says which one the statement used, or the number is undetermined.
5. An inner join on the counted rows' path drops the rows with no partner, so it is listed.
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

import sqlglot  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

import receipt_truth as truth  # noqa: E402
from receipt_trick_queries import (  # noqa: E402
    AUDIT_REAL_SHAPES,
    FAIL_CLOSED,
    GROUP_KEY_THROUGH_STEP,
    INLINE_WITHHELD,
    INNER_JOIN_TRICKS,
    LINK_TRICKS,
    OPTIONAL_SIDE_FILTERED,
    OR_GROUPS,
    READABLE_CONDITIONS,
    REAUDIT_JOIN_PATH,
    ROUND3_JOIN_PATH,
    SET_OP_TRICKS,
    STEP_BASE_TERMS,
    STEP_UNSTATED,
)
from test_ace177_filters_on_a_matched_metric import UNFILTERED, _outputs  # noqa: E402
from test_ace177_filters_on_a_matched_metric import _org as _acme  # noqa: E402


@pytest.fixture()
def unf(tmp_path):
    return _acme(tmp_path, "unf", metrics=UNFILTERED)


def _item(org, sql: str) -> dict:
    """The one aggregating output item of a statement."""
    (item,) = [i for i in _outputs(org, sql) if i["aggregate"]]
    return item


# --- 1. an OR group keeps its brackets ------------------------------------------------------------


def test_an_or_condition_is_listed_in_brackets(unf):
    item = _item(unf, OR_GROUPS[0])
    assert item["status"] == rt.MATCHED
    assert item["restrictions"] == [
        "opened >= '2026-01-01'",
        "(LOWER(status) LIKE '%ship%' OR LOWER(status) LIKE '%paid%')",
    ]


ENGINES = ["sqlite", "duckdb"]


def _rows(engine: str, sql: str) -> list:
    """Rows on `receipt_truth`'s fixture. Postgres is not needed: nothing here is engine-specific
    beyond what SQLite and DuckDB already disagree on."""
    if engine == "duckdb":
        pytest.importorskip("duckdb")
    conn = truth.ENGINES[engine][0]()
    truth.load_fixture(conn, engine)
    try:
        return [tuple(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("sql", OR_GROUPS)
def test_a_restriction_list_joined_with_and_keeps_the_statements_rows(unf, sql, engine):
    """The reader's view, on the numbers: the metric over the list joined with AND, as a renderer
    shows it, is the statement's number. Without the brackets the first case counts every year."""
    item = _item(unf, sql)
    assert item["status"] == rt.MATCHED
    table = "change_requests" if "change_requests" in sql else "orders"
    where = " AND ".join(item["restrictions"])
    reference = f"SELECT {item['expression']} FROM {table} WHERE {where}"
    assert _rows(engine, sql) == _rows(engine, reference), (where, reference)


def test_every_condition_list_brackets_an_or(tmp_path, unf):
    """Result filters, a metric's missing condition and a built-from number's restrictions are
    joined the same way, so each brackets an OR the same way."""
    having = _item(
        unf,
        "SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id "
        "HAVING COUNT(*) > 1 OR MIN(id) = 1",
    )
    assert having["result_filters"] == ["(COUNT(*) > 1 OR MIN(id) = 1)"]
    built = _item(
        unf,
        "WITH d AS (SELECT customer_id, SUM(total_amount) AS t FROM orders "
        "WHERE status = 'a' OR status = 'b' GROUP BY customer_id) "
        "SELECT AVG(t) AS n FROM d",
    )
    assert built["built_from"]["restrictions"] == ["(status = 'a' OR status = 'b')"]
    org = _acme(
        tmp_path,
        "either",
        metrics={
            "live_orders": (
                "COUNT(*) FILTER (WHERE status = 'paid' OR status = 'open')",
                ["orders"],
            )
        },
    )
    missing = _item(org, "SELECT COUNT(*) AS n FROM orders")
    assert missing["missing_conditions"] == [
        {"metric": "live_orders", "conditions": ["(status = 'paid' OR status = 'open')"]}
    ]


def _balanced(text: str) -> bool:
    depth, quote = 0, ""
    for ch in text:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in "()":
            depth += 1 if ch == "(" else -1
            if depth < 0:
                return False
    return depth == 0 and not quote


@pytest.mark.parametrize(
    "where,expect",
    [
        (" OR ".join(f"(status = 'v{i}' AND customer_id = {i})" for i in range(30)), "(("),
        ("status IN (" + ", ".join(f"'v{i:03}'" for i in range(120)) + ")", "status IN ("),
        ("status = '" + "x" * 1000 + "'", "status = …"),
    ],
    ids=["or", "in-list", "literal"],
)
def test_a_cut_condition_never_reads_as_a_complete_one(unf, where, expect):
    """A cut inside a literal backs up to before it (a prefix of a value is another value), and every
    bracket the cut leaves open is closed after the ellipsis, so the conditions joined after it are
    not swallowed into it. Never longer than the bound plus the ellipsis."""
    item = _item(unf, f"SELECT COUNT(*) AS n FROM orders WHERE customer_id > 0 AND ({where})")
    text = item["restrictions"][1]
    assert text.startswith(expect) and "…" in text and _balanced(text), text
    assert len(text) <= rt._ECHO_MAX_CONDITION_CHARS + 1


@pytest.mark.parametrize(
    "text,dialect,kept",
    [
        ("x = 'it\\'s (", "mysql", "x = "),
        ("x = 'it''s (", "postgres", "x = "),
        ("x = 'it''s' AND (y", "postgres", "x = 'it''s' AND (y"),
        ("[weird (name] = 1 AND (y", "tsql", "[weird (name] = 1 AND (y"),
        ("x = [open (", "tsql", "x = "),
    ],
    ids=["backslash", "doubled-open", "doubled-closed", "tsql-closed", "tsql-open"],
)
def test_a_cut_knows_each_dialects_quotes(text, dialect, kept):
    """A backslash escapes a quote where the dialect writes it so, and T-SQL's `[name]` is quoted:
    a bracket inside either is not one the cut has to close."""
    assert rt._open_brackets(text, dialect)[0] == kept


def test_the_whole_line_is_bounded_link_and_join_lines_included(tmp_path):
    """The bound is on the line as shown, prefix included, not on the condition inside it."""
    sql = (
        "SELECT COUNT(*) AS n FROM assets a JOIN users u ON u.id = a.assigned_to_id WHERE "
        + " AND ".join(f"u.id <> {i}" for i in range(200))
    )
    item = _item(_audit(tmp_path), sql)
    assert item["restrictions"][0].startswith(
        "only assets rows that have a matching users row (on users.id = assets.assigned_to_id) "
        "via assets.assigned_to_id"
    )
    assert all(len(t) <= rt._ECHO_MAX_CONDITION_CHARS + 1 for t in item["restrictions"])


# --- 2. a set operation's column ------------------------------------------------------------------


def _arm_sqls(sql: str) -> list:
    tree = sqlglot.parse_one(sql)
    return [arm[0].sql() if arm else None for arm in rt._output_select_arms(tree)]


@pytest.mark.parametrize("sql", SET_OP_TRICKS)
def test_each_arm_of_a_set_operation_is_matched_on_its_own(unf, sql):
    """Every arm is its own item (`main#<n>`), matched exactly as that arm alone would be: one arm
    counting a metric and the next something else into the same column is never one claim for the
    column, and never undone into one undetermined item either. Each is keyed by the column it lands
    in, which the first arm names."""
    items = [i for i in _outputs(unf, sql) if i["aggregate"]]
    arms = [s for s in _arm_sqls(sql) if s is not None]
    assert len(items) == len(arms)
    first = _item(unf, arms[0])["column"]
    for item, arm in zip(items, arms):
        alone = _item(unf, arm)
        assert (item["status"], item["name"], item["restrictions"]) == (
            alone["status"],
            alone["name"],
            alone["restrictions"],
        ), arm
        assert item["column"] == first


@pytest.mark.parametrize("op", ["EXCEPT", "INTERSECT"])
def test_an_arm_that_adds_no_rows_is_never_matched(unf, op):
    items = [
        i
        for i in _outputs(
            unf,
            f"SELECT COUNT(*) AS n FROM orders {op} "
            "SELECT COUNT(*) FROM orders WHERE status = 'paid'",
        )
        if i["aggregate"]
    ]
    assert [(i["status"], i["reason"]) for i in items] == [
        (rt.MATCHED, None),
        (rt.UNDETERMINED, rt._WHY_ARMS),
    ]


def test_arms_counting_one_metric_keep_their_own_restrictions(unf):
    items = [
        i
        for i in _outputs(
            unf,
            "SELECT 'a' AS kind, COUNT(DISTINCT number) AS n FROM "
            "change_requests WHERE state = 'Closed' UNION ALL SELECT 'b', "
            "COUNT(DISTINCT number) FROM change_requests WHERE "
            "state = 'Work in Progress'",
        )
        if i["aggregate"]
    ]
    assert [(i["status"], i["name"], i["restrictions"]) for i in items] == [
        (rt.MATCHED, "change_count", ["state = 'Closed'"]),
        (rt.MATCHED, "change_count", ["state = 'Work in Progress'"]),
    ]


# --- 3. a filter on a step's computed column ------------------------------------------------------


@pytest.mark.parametrize("sql", list(STEP_BASE_TERMS))
def test_a_filter_on_a_step_column_is_listed_over_the_tables(unf, sql):
    item = _item(unf, sql)
    assert item["status"] == rt.MATCHED
    assert item["restrictions"] == STEP_BASE_TERMS[sql]


@pytest.mark.parametrize("sql", STEP_UNSTATED)
def test_a_filter_no_condition_over_the_tables_can_state_is_undetermined(unf, sql):
    item = _item(unf, sql)
    assert item["status"] == rt.UNDETERMINED and item["reason"] == rt._WHY_UNSTATED


def test_a_doubling_chain_of_steps_fails_closed_fast(unf):
    """Each step doubling a column grew the stated condition exponentially: 20 steps took tens of
    seconds and gigabytes. The bound makes it undetermined in well under a second."""
    import time

    start = time.perf_counter()
    item = _item(unf, STEP_UNSTATED[-1])
    assert time.perf_counter() - start < 1.0
    assert item["status"] == rt.UNDETERMINED


def test_a_whole_row_read_through_a_step_never_reaches_the_receipt(tmp_path):
    """A whole row of a table with a sensitive column, read through a step: undetermined, and its
    value is nowhere in the receipt (it was listed as `CAST(c AS TEXT) LIKE '%secret%'`)."""
    import json

    org = _acme(tmp_path, "s", metrics=UNFILTERED)
    receipt = rt.assemble_receipt(org, STEP_UNSTATED[1])
    assert "secret" not in json.dumps(receipt["columns"])


@pytest.mark.parametrize("sql,expect", GROUP_KEY_THROUGH_STEP)
def test_a_filter_on_a_grouping_steps_key_is_stated(unf, sql, expect):
    assert _item(unf, sql)["built_from"]["restrictions"] == expect


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("sql", list(STEP_BASE_TERMS)[:4])
def test_a_base_terms_restriction_agrees_with_the_numbers(unf, sql, engine):
    """The listed condition, over the step's own table, gives the statement's number."""
    item = _item(unf, sql)
    table = "change_requests" if "change_requests" in sql else "orders"
    reference = f"SELECT {item['expression']} FROM {table} WHERE " + " AND ".join(
        item["restrictions"]
    )
    assert _rows(engine, sql) == _rows(engine, reference), reference


# --- 4 and 5: the `audit` model -------------------------------------------------------------------

AUDIT_TABLES = {
    "users": [("id", "integer"), ("active", "boolean")],
    "assets": [
        ("id", "integer"),
        ("assigned_to_id", "integer"),
        ("managed_by_id", "integer"),
        ("owned_by_id", "integer"),
        ("status", "string"),
    ],
    "expense_lines": [
        ("id", "integer"),
        ("award_id", "integer"),
        ("fiscal_year", "integer"),
        ("amount", "decimal"),
        ("request_id", "integer"),
        ("submit_date_key", "integer"),
    ],
    "award_departments": [("award_id", "integer"), ("is_central", "boolean")],
    "outages": [
        ("id", "integer"),
        ("service_id", "integer"),
        ("begin_at", "date"),
        ("end_at", "date"),
        ("kind", "string"),
        ("began", "date"),
        ("ended", "date"),
        ("change_id", "integer"),
    ],
    "incidents": [
        ("id", "integer"),
        ("service_id", "integer"),
        ("opened", "date"),
        ("problem_id", "integer"),
        ("number", "string"),
    ],
    "problems": [("id", "integer"), ("state", "string"), ("number", "string")],
    "changes": [("id", "integer")],
    "dates": [("date_key", "integer"), ("full_date", "string")],
    "fiscal_periods": [
        ("period_start", "date"),
        ("period_end", "date"),
        ("adjustment_flag", "string"),
        ("fiscal_year", "integer"),
    ],
    "requests": [("id", "integer"), ("fiscal_year", "integer")],
    "rejections": [("id", "integer"), ("request_id", "integer"), ("reason", "string")],
}

AUDIT_METRICS = {
    "asset_count": ("COUNT(*)", ["assets"]),
    "expense_total": ("SUM(amount)", ["expense_lines"]),
    "incident_count": ("COUNT(DISTINCT id)", ["incidents"]),
    "problem_count": ("COUNT(DISTINCT id)", ["problems"]),
}


def _rel(ft: str, fc: str, tt: str, tc: str) -> dict:
    return {
        "from_table": ft,
        "from_column": fc,
        "to_table": tt,
        "to_column": tc,
        "from_schema": "public",
        "to_schema": "public",
        "relationship": "many_to_one",
        "confidence": "confirmed",
        "review_state": "approved",
        "signed_off_by": "you@example.com",
        "signed_off_role": "data_owner",
        "signed_off_at": "2026-01-01T00:00:00Z",
    }


AUDIT_RELATIONSHIPS = [
    _rel("assets", "assigned_to_id", "users", "id"),
    _rel("assets", "managed_by_id", "users", "id"),
    _rel("assets", "owned_by_id", "users", "id"),
    _rel("expense_lines", "award_id", "award_departments", "award_id"),
    _rel("incidents", "service_id", "outages", "service_id"),
    _rel("incidents", "problem_id", "problems", "id"),
    _rel("rejections", "request_id", "requests", "id"),
]


def _audit(
    tmp_path,
    name: str = "audit",
    metrics: "dict | None" = None,
    relationships: "list | None" = None,
):
    root = tmp_path / name
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "subject_areas" / "s" / "metrics").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "audit",
                "version": 1,
                "storage_connections": [{"name": "c", "storage_type": "PostgreSQL"}],
                "subject_areas": ["subject_areas/s"],
            }
        )
    )
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "s",
                "tables": [
                    {"storage_connection": "c", "schema": "public", "table": t}
                    for t in AUDIT_TABLES
                ],
            }
        )
    )
    (root / "subject_areas" / "s" / "relationships.yaml").write_text(
        yaml.safe_dump(
            {"relationships": AUDIT_RELATIONSHIPS if relationships is None else relationships}
        )
    )
    for name_, cols in AUDIT_TABLES.items():
        (root / "subject_areas" / "s" / "tables" / f"{name_}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": name_,
                    "schema": "public",
                    "storage_connection": "c",
                    "grain": [cols[0][0]],
                    "description": name_,
                    "columns": [{"name": c, "type": t} for c, t in cols],
                }
            )
        )
    for metric, (binding, sources) in (metrics or AUDIT_METRICS).items():
        (root / "subject_areas" / "s" / "metrics" / f"{metric}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": metric,
                    "calculation": metric,
                    "description": metric,
                    "bindings": {"PostgreSQL": binding},
                    "source_tables": sources,
                    "confidence": "confirmed",
                    "review_state": "approved",
                    "signed_off_by": "you@example.com",
                    "signed_off_role": "data_owner",
                    "signed_off_at": "2026-01-01T00:00:00Z",
                }
            )
        )
    return L.load_datasource(root)


@pytest.mark.parametrize("sql,expect", LINK_TRICKS, ids=range(len(LINK_TRICKS)))
def test_a_condition_reached_by_one_of_several_links_names_it(tmp_path, sql, expect):
    item = _item(_audit(tmp_path), sql)
    if expect is None:
        assert item["status"] == rt.UNDETERMINED and item["reason"] == rt._WHY_LINK
    else:
        assert (item["status"], item["restrictions"]) == (rt.MATCHED, expect)


def test_one_declared_link_leaves_the_condition_as_written(tmp_path):
    org = _audit(tmp_path, relationships=[_rel("assets", "assigned_to_id", "users", "id")])
    item = _item(org, LINK_TRICKS[0][0])
    assert item["restrictions"] == [
        "u.active = FALSE",
        "only assets rows that have a matching users row (on users.id = assets.assigned_to_id)",
    ]


def test_a_join_no_declared_link_fits_is_named_as_undeclared(tmp_path):
    """The other way a join can fail to be one declared link: it is named, as part 2 names it, and
    its condition carries no link."""
    item = _item(
        _audit(tmp_path),
        "SELECT COUNT(*) AS n FROM assets a JOIN users u ON u.id = a.id WHERE NOT u.active",
    )
    assert item["status"] == rt.MATCHED
    assert item["joins_not_declared"] == ["assets → users"]
    assert item["restrictions"][0] == "u.active = FALSE"


def test_a_condition_on_the_counted_table_carries_no_link(tmp_path):
    item = _item(
        _audit(tmp_path),
        "SELECT COUNT(*) AS n FROM assets a JOIN users u "
        "ON u.id = a.assigned_to_id WHERE a.status = 'x'",
    )
    assert item["restrictions"][0] == "a.status = 'x'"


@pytest.mark.parametrize("sql,expect", INNER_JOIN_TRICKS, ids=range(len(INNER_JOIN_TRICKS)))
def test_an_inner_join_on_the_counted_rows_is_listed(tmp_path, sql, expect):
    item = _item(_audit(tmp_path), sql)
    assert (item["status"], item["restrictions"]) == (rt.MATCHED, expect)


@pytest.mark.parametrize("sql,expect", REAUDIT_JOIN_PATH, ids=["left", "inner"])
def test_every_condition_on_a_multi_join_path_is_listed(tmp_path, sql, expect):
    org = _audit(tmp_path, metrics={"problem_count": ("COUNT(DISTINCT id)", ["problems"])})
    item = _item(org, sql)
    assert (item["status"], item["restrictions"]) == (rt.MATCHED, expect)


def test_a_join_the_metric_itself_spans_is_not_listed(tmp_path):
    org = _audit(
        tmp_path,
        metrics={"department_expense": ("SUM(amount)", ["expense_lines", "award_departments"])},
    )
    item = _item(org, INNER_JOIN_TRICKS[0][0])
    assert (item["name"], item["restrictions"]) == ("department_expense", ["e.fiscal_year = 2018"])


def test_a_built_from_number_lists_the_inner_join_of_its_groups(tmp_path):
    item = _item(
        _audit(tmp_path),
        "WITH g AS (SELECT e.fiscal_year, SUM(e.amount) AS t FROM "
        "expense_lines e JOIN award_departments d ON d.award_id = "
        "e.award_id GROUP BY e.fiscal_year) SELECT AVG(t) AS n FROM g",
    )
    assert item["built_from"]["restrictions"] == [
        "only expense_lines rows that have a matching award_departments row (on award_id)"
    ]


def test_the_restriction_forms_parse_back_where_they_are_conditions():
    """The harness reads the two non-condition forms back: an inner join is skipped (the reference
    keeps the join), a linked condition is read past its link."""
    assert truth._LINKED.match("users via assets.assigned_to_id → users.id: NOT u.active")
    assert sqlglot.parse_one("SELECT 1 WHERE NOT u.active") is not None
    assert truth._INNER_JOIN.match("only orders rows that have a matching customers row (on id)")


def test_the_trace_shares_no_mutable_default():
    with pytest.raises(TypeError):
        rt._Trace._field_defaults["links"]["x"] = 1
    with pytest.raises(TypeError):
        rt._Path._field_defaults["ref_joins"]["x"] = 1


# --- round 3: what a step requires, joins through it, and readable SQL ---------------------------

ROUND3_MODEL = {
    "problem_count": ("COUNT(DISTINCT id)", ["problems"]),
    "rejected": ("COUNT(DISTINCT request_id)", ["rejections"]),
}


@pytest.mark.parametrize("sql,expect", ROUND3_JOIN_PATH, ids=["step-requires", "optional-side"])
def test_what_a_joined_step_requires_is_listed_over_the_tables(tmp_path, sql, expect):
    """A step joined in narrows the counted rows by its own joins and filters: each is listed over
    the tables, and the join to the step is named by the table its key comes from, never the step."""
    item = _item(_audit(tmp_path, metrics=ROUND3_MODEL), sql)
    assert (item["status"], item["restrictions"]) == (rt.MATCHED, expect)
    assert not any("step" in line for line in item["restrictions"])


@pytest.mark.parametrize("sql,expect", READABLE_CONDITIONS, ids=range(len(READABLE_CONDITIONS)))
def test_a_condition_is_shown_as_the_most_readable_sql_it_is(tmp_path, sql, expect):
    item = _item(_audit(tmp_path), sql)
    assert (item["status"], item["restrictions"]) == (rt.MATCHED, expect)


@pytest.mark.parametrize("engine", ENGINES)
def test_a_readable_negation_counts_the_same_rows(unf, engine):
    """On the numbers: the rewritten negations keep the statement's rows."""
    sql = (
        "SELECT COUNT(*) AS n FROM orders WHERE NOT status IS NULL AND NOT status IN ('x') "
        "AND NOT (customer_id = 9) AND NOT status LIKE 'z%' AND NOT total_amount BETWEEN 3 AND 4"
    )
    item = _item(unf, sql)
    assert item["restrictions"] == [
        "status IS NOT NULL",
        "status NOT IN ('x')",
        "customer_id <> 9",
        "status NOT LIKE 'z%'",
        "total_amount NOT BETWEEN 3 AND 4",
    ]
    reference = "SELECT COUNT(*) FROM orders WHERE " + " AND ".join(item["restrictions"])
    assert _rows(engine, sql) == _rows(engine, reference)


def test_a_subquery_reading_a_step_shows_the_steps_own_select(unf):
    sql = (
        "WITH paid AS (SELECT id FROM orders WHERE status = 'paid') "
        "SELECT COUNT(*) AS n FROM orders WHERE id IN (SELECT id FROM paid)"
    )
    assert _item(unf, sql)["restrictions"] == [
        "id IN (SELECT id FROM (SELECT id FROM orders WHERE status = 'paid') AS paid)"
    ]


def test_a_condition_the_declared_link_also_states_is_still_listed(tmp_path):
    """Final round: a condition is never dropped for appearing in the declared `on:` (qualifiers
    are stripped when they are compared, so `u.status` would stand for `assets.status`). Saying it
    twice is the price."""
    rel = _rel("assets", "assigned_to_id", "users", "id")
    rel.pop("from_column"), rel.pop("to_column")
    rel["on"] = "assets.assigned_to_id = users.id AND users.active = TRUE"
    org = _audit(tmp_path, relationships=[rel, _rel("assets", "owned_by_id", "users", "id")])
    item = _item(
        org,
        "SELECT COUNT(*) AS n FROM assets a JOIN users u ON u.id = a.assigned_to_id "
        "AND u.active = TRUE WHERE u.id > 2",
    )
    assert item["status"] == rt.MATCHED
    assert any("u.active = TRUE" in line for line in item["restrictions"]), item["restrictions"]


def test_an_extra_join_key_is_never_hidden_by_the_link(tmp_path):
    """The query's own keys are shown beside the declared link they fit, extra ones included."""
    item = _item(
        _audit(tmp_path),
        "SELECT COUNT(*) AS n FROM assets a JOIN users u ON u.id = a.assigned_to_id "
        "AND u.active = a.status WHERE u.id > 2",
    )
    assert item["status"] == rt.MATCHED
    assert any(
        "(on users.id = assets.assigned_to_id" in line and "via assets.assigned_to_id" in line
        for line in item["restrictions"]
    ), item["restrictions"]


def test_a_join_after_a_grouping_step_is_a_join_line_among_the_result_filters(unf):
    item = _item(
        unf,
        "WITH g AS (SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY "
        "customer_id) SELECT g.n FROM g JOIN customers c ON c.id = g.customer_id",
    )
    assert item["result_filters"] == [
        "only orders rows that have a matching customers row (on customers.id = orders.customer_id)"
    ]


# A small DuckDB fixture over the `audit` tables these shapes read, chosen so that dropping any one
# listed condition or join changes a number: outages outside the year, of another kind, with an
# inverted window; incidents outside a window or on another service; a problem no incident names;
# requests submitted outside the year or in an adjustment period, rejections with no reason.
_AUDIT_ROWS = {
    "outages(id INTEGER, service_id INTEGER, began DATE, ended DATE, kind TEXT, change_id INTEGER)": [
        (1, 10, "2026-02-01", "2026-02-05", "Emergency", 1),
        (2, 10, "2025-06-01", "2025-06-09", "Emergency", None),
        (3, 20, "2026-03-01", "2026-03-04", " Emergency ", 2),
        (4, 20, "2026-04-05", "2026-04-01", "Emergency", None),
        (5, 30, "2026-05-01", "2026-05-03", "Normal", None),
    ],
    "incidents(id INTEGER, service_id INTEGER, opened DATE, problem_id INTEGER, number TEXT)": [
        (1, 10, "2026-02-02", 100, "I1"),
        (2, 10, "2026-02-09", 101, "I2"),
        (3, 20, "2026-03-02", 102, "I3"),
        (4, 20, "2026-04-03", 103, "I4"),
        (5, 30, "2026-05-02", 104, "I5"),
        (6, 10, "2025-06-02", 105, "I6"),
        (7, 20, "2026-03-03", 100, "I7"),
    ],
    "problems(id INTEGER, number TEXT)": [
        (100, "P0"),
        (101, "P1"),
        (102, "P2"),
        (103, "P3"),
        (104, "P4"),
        (105, "P5"),
        (106, "P6"),
    ],
    "changes(id INTEGER)": [(1,), (2,)],
    "expense_lines(request_id INTEGER, submit_date_key INTEGER)": [
        (1, 1),
        (2, 2),
        (3, 3),
        (4, 1),
        (5, 4),
        (6, 1),
    ],
    "dates(date_key INTEGER, full_date TEXT)": [
        (1, "2025-03-01"),
        (2, "2024-03-01"),
        (3, "2025-12-31"),
        (4, "2025-06-30"),
    ],
    "fiscal_periods(period_start DATE, period_end DATE, adjustment_flag TEXT, fiscal_year INTEGER)": [
        ("2025-01-01", "2025-06-29", "N", 2025),
        ("2025-06-30", "2025-06-30", "Y", 2025),
        ("2025-07-01", "2025-12-31", "N", 2025),
        ("2024-01-01", "2024-12-31", "N", 2024),
    ],
    "requests(id INTEGER, fiscal_year INTEGER)": [(1, 2025), (2, 2024), (5, 2025)],
    "rejections(request_id INTEGER, reason TEXT)": [
        (1, "late"),
        (2, "late"),
        (3, ""),
        (4, None),
        (5, "missing"),
        (6, "dup"),
        (None, "x"),
    ],
}


def _audit_duckdb():
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect()
    for table, rows in _AUDIT_ROWS.items():
        conn.execute(f"CREATE TABLE {table}")
        width = len(rows[0])
        conn.executemany(
            f"INSERT INTO {table.split('(')[0]} VALUES ({', '.join(['?'] * width)})", rows
        )
    return conn


@pytest.mark.parametrize(
    "sql,column,expect,reference",
    AUDIT_REAL_SHAPES,
    ids=["chain-step-read-twice", "chain", "optional-step"],
)
def test_a_real_shape_lists_everything_or_reads_undetermined(
    tmp_path, sql, column, expect, reference
):
    metric = {
        "problems_created": ("COUNT(DISTINCT number)", ["problems"]),
        "with_reason": ("COUNT(DISTINCT request_id)", ["rejections"]),
    }[column]
    # No relationship between the rejections and the expense lines, as in the real model.
    org = _audit(
        tmp_path,
        metrics={column: metric},
        relationships=[r for r in AUDIT_RELATIONSHIPS if r["from_table"] != "rejections"],
    )
    (item,) = [i for i in _outputs(org, sql) if i["column"] == column]
    if expect is None:
        assert item["status"] == rt.UNDETERMINED, item
        return
    assert (item["status"], item["restrictions"]) == (rt.MATCHED, expect)
    assert not any(line.startswith("only rows") for line in item["restrictions"])
    if column == "with_reason":
        # Joins it was counted through, named by their tables; the third step's join is not one.
        assert item["joins_not_declared"] == [
            "expense_lines → dates",
            "dates → fiscal_periods",
            "expense_lines → rejections",
        ]
        assert not any("requests" in line for line in item["restrictions"])
    # On the numbers: the claim, written over the tables, is the statement's number, group by
    # group (a group the claim does not keep must count nothing).
    conn = _audit_duckdb()
    try:
        got = conn.execute(sql).fetchall()
        want = dict(conn.execute(reference).fetchall()) if "AS k" in reference else None
        if want is None:
            (value,) = conn.execute(reference).fetchone()
            index = [c for c in ("submitted", "with_reason")].index(column)
            assert got[0][index] == value and value > 0, (got, value)
        else:
            for key, value in got:
                assert value == want.get(key, 0), (key, value, want)
            assert any(want.values())
    finally:
        conn.close()


# --- final round: fail closed ---------------------------------------------------------------------


@pytest.mark.parametrize("storage_type,sql,why", FAIL_CLOSED, ids=range(len(FAIL_CLOSED)))
def test_an_over_claim_the_review_found_reads_undetermined(tmp_path, storage_type, sql, why):
    org = _acme(tmp_path, "f", metrics=UNFILTERED, storage_type=storage_type)
    items = [i for i in _outputs(org, sql) if i["aggregate"]]
    assert items and all(
        (i["status"], i["reason"]) == (rt.UNDETERMINED, getattr(rt, why)) for i in items
    ), items


@pytest.mark.parametrize("sql,expect", OPTIONAL_SIDE_FILTERED)
def test_a_filter_on_an_optional_side_lists_its_join(unf, sql, expect):
    item = _item(unf, sql)
    assert (item["status"], item["restrictions"]) == (rt.MATCHED, expect)


@pytest.mark.parametrize(
    "sql", INLINE_WITHHELD, ids=["row-ref", "chain", "exists", "to-json", "recursive", "deep"]
)
def test_a_subquery_whose_step_cannot_be_shown_is_withheld_whole(tmp_path, sql):
    """No value, no step name, and a receipt every time, fast."""
    import json
    import time

    org = _acme(tmp_path, "w")
    started = time.perf_counter()
    receipt = rt.assemble_receipt(org, sql)
    assert time.perf_counter() - started < 1.0
    text = json.dumps(receipt["columns"])
    assert "SECRETVAL" not in text
    (item,) = [i for i in receipt["columns"]["items"] if i["kind"] == "output"]
    assert item["status"] == rt.MATCHED
    assert any("<withheld>" in line for line in item["restrictions"]), item["restrictions"]
    for step in ("paid", "c44", " r)", "FROM r"):
        assert all(step not in line for line in item["restrictions"]), item["restrictions"]


def test_not_of_a_column_the_model_does_not_type_boolean_stays_not(unf):
    assert _item(unf, "SELECT COUNT(*) AS n FROM orders o WHERE NOT o.status AND NOT o.is_deleted")[
        "restrictions"
    ] == ["NOT o.status", "o.is_deleted = FALSE"]
