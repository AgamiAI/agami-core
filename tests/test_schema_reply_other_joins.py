"""A table reply sends joins to tables the caller didn't request compactly (#477).

Those edges were most of a `dataset_names` reply. Each becomes one entry per (other table,
requested table, cardinality) with its keys as `"requested_col=other_col"`, so a statement that
reaches a further table still knows how to join it. Synthetic, generic names only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "agami-core" / "src"))

import tools  # noqa: E402


def _edge(frm, to, fc, tc, **extra):
    return {"from_table": frm, "to_table": to, "from_column": fc, "to_column": tc,
            "from_schema": "public", "to_schema": "public", "join_type": "LEFT",
            "relationship": "many_to_one", **extra}


REQ = {"orders", "customers"}
SCHEMAS = {"orders": "public", "customers": "public"}


def _split(edges):
    return tools._compact_other_joins(edges, REQ, SCHEMAS)


def test_a_join_between_two_requested_tables_stays_in_full():
    edge = _edge("orders", "customers", "customer_id", "id", description="who ordered")
    kept, other = _split([edge])
    assert kept == [edge] and other == []


def test_joins_to_one_other_table_become_one_entry_with_every_key():
    kept, other = _split([
        _edge("orders", "dates", "ordered_on", "date_key"),
        _edge("orders", "dates", column_pairs=None, fc="shipped_on", tc="date_key"),
    ])
    assert kept == []
    assert other == [{"table": "dates", "from": "orders", "relationship": "many_to_one",
                      "keys": ["ordered_on=date_key", "shipped_on=date_key"]}]


def test_merged_column_pairs_are_all_kept():
    merged = {k: v for k, v in _edge("orders", "dates", None, None).items() if "column" not in k}
    merged["column_pairs"] = [["ordered_on", "date_key"], ["shipped_on", "date_key"]]
    _, other = _split([merged])
    assert other[0]["keys"] == ["ordered_on=date_key", "shipped_on=date_key"]


def test_an_edge_pointing_at_a_requested_table_reads_from_its_side():
    """`lines -> orders` is many-to-one from lines; from orders it fans out, which is what a join
    from orders would do to the row count."""
    _, other = _split([_edge("lines", "orders", "order_id", "id")])
    assert other == [{"table": "lines", "from": "orders", "relationship": "one_to_many",
                      "keys": ["id=order_id"]}]


def test_a_join_with_its_own_condition_keeps_it_verbatim():
    cond = "orders.rep_key = reps.rep_natural_key AND reps.current_flag = 'Y'"
    _, other = _split([_edge("orders", "reps", "rep_key", "rep_natural_key", on=cond)])
    assert other[0]["on"] == [cond] and "keys" not in other[0]


def test_a_join_that_runs_elsewhere_is_left_whole():
    edge = _edge("orders", "far", "f", "id", executable="split")
    kept, other = _split([edge])
    assert kept == [edge] and other == []


def test_schema_and_join_type_appear_only_when_they_differ():
    _, other = _split([_edge("orders", "dates", "a", "k"),
                       _edge("orders", "regions", "b", "k", to_schema="ref", join_type="INNER")])
    plain, odd = other
    assert "schema" not in plain and "join_type" not in plain
    assert odd["schema"] == "ref" and odd["join_type"] == "INNER"


def test_the_shared_contract_declares_it():
    import contracts

    assert "joins_to_other_tables" in contracts.DatasourceSchemaResult.model_fields
