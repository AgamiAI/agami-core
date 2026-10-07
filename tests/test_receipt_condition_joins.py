"""The receipt and a join declared with a fixed-value condition (`on:` ... `AND d.current_flag = 'Y'`).

A versioned dimension is joined to its current row on the natural key PLUS a fixed value. The receipt
reduced every such declaration to "unreadable", so a statement that wrote the declared join exactly was
`undetermined`, and the fan-out check — which only weighs joins it can match — then called the totals
behind that join `not_multiplied`: a clean bill of health for a join it never checked. These pin both
halves: the declared condition counts toward the match (and only a statement that wrote it matches),
and a total behind a join the receipt couldn't weigh is `undetermined`, not clean.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import loader as L  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

CURRENT_JOIN = "sales.customer_ntrl = customer_d.customer_ntrl AND customer_d.current_flag = 'Y'"


@pytest.fixture
def org(tmp_path):
    root = tmp_path / "m"
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "p",
                "version": 1,
                "storage_connections": [{"name": "c", "storage_type": "PostgreSQL"}],
                "subject_areas": ["subject_areas/s"],
            }
        )
    )
    tables = {
        "sales": (["id"], ["id", "customer_ntrl", "note_id", "amount"]),
        "customer_d": (
            ["customer_key"],
            ["customer_key", "customer_ntrl", "current_flag", "region"],
        ),
        "notes": ([], ["id", "body"]),
    }
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "s",
                "tables": [
                    {"storage_connection": "c", "schema": "public", "table": t} for t in tables
                ],
            }
        )
    )
    for name, (grain, cols) in tables.items():
        (root / "subject_areas" / "s" / "tables" / f"{name}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": name,
                    "schema": "public",
                    "storage_connection": "c",
                    "grain": grain,
                    "description": name,
                    "columns": [
                        {"name": c, "type": "decimal" if c == "amount" else "string"} for c in cols
                    ],
                }
            )
        )
    (root / "subject_areas" / "s" / "relationships.yaml").write_text(
        yaml.safe_dump(
            {
                "relationships": [
                    {
                        "from_table": "sales",
                        "to_table": "customer_d",
                        "on": CURRENT_JOIN,
                        "relationship": "many_to_one",
                        "confidence": "confirmed",
                        "review_state": "approved",
                        "signed_off_by": "you@example.com",
                        "signed_off_role": "owner",
                        "signed_off_at": "2026-01-01T00:00:00Z",
                    }
                ]
            }
        )
    )
    return L.load_datasource(root)


def _receipt(org, on: str, extra_join: str = "") -> dict:
    sql = (
        f"SELECT d.region, SUM(s.amount) AS total FROM sales s "
        f"JOIN customer_d d ON {on} {extra_join} GROUP BY d.region"
    )
    return rt.assemble_receipt(org, sql)


def test_a_statement_that_wrote_the_declared_condition_matches_it(org):
    r = _receipt(org, "s.customer_ntrl = d.customer_ntrl AND d.current_flag = 'Y'")
    (join,) = r["joins"]["items"]
    assert join["status"] == rt.DECLARED and join["review_state"] == "approved"
    assert {a["status"] for a in r["aggregates"]["items"]} == {rt.NOT_MULTIPLIED}


@pytest.mark.parametrize(
    "on",
    [
        "d.customer_ntrl = s.customer_ntrl AND 'Y' = d.current_flag",  # either operand order
        "s.customer_ntrl = d.customer_ntrl AND d.current_flag = 'Y' AND d.region <> 'X'",  # extra conjuncts
    ],
)
def test_the_match_is_order_free_and_tolerates_extra_conditions(org, on):
    (join,) = _receipt(org, on)["joins"]["items"]
    assert join["status"] == rt.DECLARED


@pytest.mark.parametrize(
    "on",
    [
        "s.customer_ntrl = d.customer_ntrl",  # the filter left out: every version of the customer joins
        "s.customer_ntrl = d.customer_ntrl AND d.current_flag = 'N'",  # a different value
    ],
)
def test_a_statement_without_the_declared_condition_is_not_credited_with_it(org, on):
    r = _receipt(org, on)
    (join,) = r["joins"]["items"]
    # not `declared` (that was the guard's whole purpose) and not `undeclared` either: the model does
    # declare a join between these tables on these columns, and the filter may sit in a WHERE
    assert join["status"] == rt.UNDETERMINED
    for agg in r["aggregates"]["items"]:
        assert agg["status"] == rt.UNDETERMINED and "customer_d" in agg["reason"]


def test_a_total_behind_a_join_the_receipt_could_not_weigh_is_not_called_clean(org):
    r = _receipt(
        org,
        "s.customer_ntrl = d.customer_ntrl AND d.current_flag = 'Y'",
        extra_join="JOIN notes n ON s.note_id = n.id",
    )
    statuses = {j["from_to"]: j["status"] for j in r["joins"]["items"]}
    assert statuses["sales → notes"] == rt.UNDECLARED
    for agg in r["aggregates"]["items"]:
        assert agg["status"] == rt.UNDETERMINED and "notes" in agg["reason"]


# --- the review's findings ---------------------------------------------------------------------------

_FILTERED = "s.customer_ntrl = d.customer_ntrl AND d.current_flag = 'Y'"


def test_a_filter_on_another_instance_of_the_table_does_not_credit_this_join(org):
    # d2 joins every version of the customer; the filter it borrows is on d, not on d2
    sql = (
        f"SELECT SUM(s.amount) AS t FROM sales s JOIN customer_d d ON {_FILTERED} "
        "JOIN customer_d d2 ON d2.customer_ntrl = s.customer_ntrl AND d.current_flag = 'Y'"
    )
    r = rt.assemble_receipt(org, sql)
    first, second = r["joins"]["items"]
    assert first["status"] == rt.DECLARED and second["status"] != rt.DECLARED
    assert {a["status"] for a in r["aggregates"]["items"]} == {rt.UNDETERMINED}


@pytest.mark.parametrize(
    "on, expect",
    [(_FILTERED, rt.NOT_MULTIPLIED), ("s.customer_ntrl = d.customer_ntrl", rt.UNDETERMINED)],
)
def test_pre_flight_and_the_receipt_agree_on_the_totals(org, on, expect):
    sql = f"SELECT d.region, SUM(s.amount) AS t FROM sales s JOIN customer_d d ON {on} GROUP BY d.region"
    pre = rt.pre_flight_check(sql, org)
    assert {a.status for a in pre.aggregates} == {expect}
    assert {a["status"] for a in rt.assemble_receipt(org, sql)["aggregates"]["items"]} == {expect}


def test_join_probes_grade_a_filter_in_the_where_as_open_not_a_wrong_key(org):
    from semantic_model import probes

    sql = (
        "SELECT SUM(s.amount) AS t FROM sales s JOIN customer_d d ON s.customer_ntrl = d.customer_ntrl "
        "WHERE d.current_flag = 'Y'"
    )
    (entry,) = probes.join_probes(org, sql)["joins"]
    assert entry["status"] == rt.UNDETERMINED
    assert not any(col.startswith("=") for pair in entry["declared_pairs"] for _t, col in pair)


def test_join_probes_still_probe_an_undeclared_join_that_writes_a_filter(org):
    from semantic_model import probes

    sql = "SELECT SUM(s.amount) AS t FROM sales s JOIN notes n ON s.note_id = n.id AND n.body = 'x'"
    (entry,) = probes.join_probes(org, sql)["joins"]
    assert entry["status"] == rt.UNDECLARED and entry["probes"]["overlap"]
    assert entry["pairs"] and not any(
        col.startswith("=") for pair in entry["pairs"] for _t, col in pair
    )


@pytest.mark.parametrize(
    "sql",
    [
        # a join-free arm is not downgraded by a join in the other arm
        "SELECT SUM(amount) AS t FROM sales UNION ALL SELECT SUM(s.amount) FROM sales s JOIN notes n ON s.note_id = n.id",
        # a join inside WHERE ... IN can't repeat the outer rows
        "SELECT SUM(amount) AS t FROM sales WHERE note_id IN (SELECT n.id FROM notes n JOIN sales x ON x.note_id = n.id)",
    ],
)
def test_a_join_that_cannot_feed_the_number_does_not_downgrade_it(org, sql):
    statuses = [a["status"] for a in rt.assemble_receipt(org, sql)["aggregates"]["items"]]
    assert statuses[0] == rt.NOT_MULTIPLIED


def test_a_declaration_of_fixed_values_alone_matches_no_join(tmp_path, org):
    # without a column pair a declaration isn't pinned to two tables, so it must match nothing
    from semantic_model.models import Relationship

    sa = org.subject_areas[0]
    sa.relationships.append(
        Relationship(
            from_table="notes",
            to_table="customer_d",
            on="customer_d.current_flag = 'Y'",
            relationship="one_to_one",
            confidence="confirmed",
        )
    )
    sql = "SELECT SUM(s.amount) AS t FROM sales s JOIN customer_d d ON s.note_id = d.customer_key AND d.current_flag = 'Y'"
    (join,) = rt.assemble_receipt(org, sql)["joins"]["items"]
    assert join["status"] != rt.DECLARED and join["name"] is None


def test_the_marker_counts_unweighed_totals_under_their_own_clause(org):
    r = rt.assemble_receipt(
        org, "SELECT SUM(s.amount) AS t FROM sales s JOIN notes n ON s.note_id = n.id"
    )
    marker = r["aggregates"]["undetermined"]
    assert (
        "1 of the listed aggregate(s) sit behind a join whose cardinality isn't established"
        in marker
    )
    assert "could not be resolved to the tables they read" not in marker


def test_joins_past_the_cap_leave_a_total_undetermined(org, monkeypatch):
    monkeypatch.setattr(rt, "_RECEIPT_MAX_REFS", 1)
    sql = (
        f"SELECT SUM(s.amount) AS t FROM sales s JOIN customer_d d ON {_FILTERED} "
        "JOIN customer_d d2 ON s.customer_ntrl = d2.customer_ntrl AND d2.current_flag = 'Y'"
    )
    (agg,) = rt.assemble_receipt(org, sql)["aggregates"]["items"]
    assert agg["status"] == rt.UNDETERMINED and "past the receipt's cap" in agg["reason"]
