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
    INNER_JOIN_TRICKS,
    LINK_TRICKS,
    OR_GROUPS,
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
        ("status IN (" + ", ".join(f"'v{i:03}'" for i in range(60)) + ")", "status IN ("),
        ("status = '" + "x" * 400 + "'", "status = …"),
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
    assert len(text) <= rt._ECHO_MAX_EXPR_CHARS + 1


# --- 2. a set operation's column ------------------------------------------------------------------


@pytest.mark.parametrize("sql", SET_OP_TRICKS)
def test_no_arm_of_a_column_counting_different_things_reads_matched(unf, sql):
    items = [i for i in _outputs(unf, sql) if i["aggregate"]]
    assert items and all(i["status"] != rt.MATCHED for i in items), items
    assert any(i["reason"] == rt._WHY_ARMS for i in items), items


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


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("sql", list(STEP_BASE_TERMS)[:4])
def test_a_base_terms_restriction_agrees_with_the_numbers(unf, sql, engine):
    """The listed condition, over the step's own table, gives the statement's number."""
    item = _item(unf, sql)
    table = "change_requests c" if "change_requests" in sql else "orders o"
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
    ],
    "award_departments": [("award_id", "integer"), ("is_central", "boolean")],
    "outages": [
        ("id", "integer"),
        ("service_id", "integer"),
        ("begin_at", "date"),
        ("end_at", "date"),
    ],
    "incidents": [("id", "integer"), ("service_id", "integer"), ("opened", "date")],
}

AUDIT_METRICS = {
    "asset_count": ("COUNT(*)", ["assets"]),
    "expense_total": ("SUM(amount)", ["expense_lines"]),
    "incident_count": ("COUNT(DISTINCT id)", ["incidents"]),
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
        "NOT u.active",
        "only rows with a match in users (u.id = a.assigned_to_id)",
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
    assert item["restrictions"][0] == "NOT u.active"


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
        "only rows with a match in award_departments (d.award_id = e.award_id)"
    ]


def test_the_restriction_forms_parse_back_where_they_are_conditions():
    """The harness reads the two non-condition forms back: an inner join is skipped (the reference
    keeps the join), a linked condition is read past its link."""
    assert truth._LINKED.match("users via assets.assigned_to_id → users.id: NOT u.active")
    assert sqlglot.parse_one("SELECT 1 WHERE NOT u.active") is not None
    assert "only rows with a match in x".startswith(truth._INNER_JOIN)
