"""Large-table filters are a seed the person building the model confirms (#451).

Introspection seeds `recommended_filters` with every date column of a large table. On a warehouse
many of those are load stamps nobody filters a question on, so the report lists each large table
with its seed, and curate edits the list alone without restating the row count beside it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate as C  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model.loader import load_datasource  # noqa: E402

from catalog_helpers import col, make_catalog_runner  # noqa: E402

_runner = make_catalog_runner(
    tables=["orders"],
    columns={
        "orders": [
            col("id", "integer", nullable=False),
            col("order_date", "date"),
            col("row_loaded_at", "timestamp"),
        ],
    },
    estimate="5000000",
)


def test_the_report_lists_each_large_table_with_its_seeded_filters(tmp_path):
    _, report = I.introspect(
        "shop", "postgres", runner=_runner, artifacts_dir=tmp_path, dry_run=True
    )
    assert report.large_tables == {"orders": (5_000_000, ["order_date", "row_loaded_at"])}
    assert (
        "large table orders: about 5,000,000 rows; suggested filters: order_date, row_loaded_at"
        in report.render()
    )


def test_curate_edits_the_filters_and_keeps_the_row_count(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_runner, artifacts_dir=tmp_path)
    root = tmp_path / "shop"
    area = org.subject_areas[0].name
    res = C.apply(
        root,
        [
            {
                "op": "edit",
                "kind": "table",
                "area": area,
                "name": "orders",
                "field": "recommended_filters",
                "value": ["order_date"],
            }
        ],
    )
    assert not res.errors and not res.skipped, (res.errors, res.skipped)
    hints = load_datasource(root).subject_areas[0].defined_table("orders").performance_hints
    assert hints.recommended_filters == ["order_date"]
    assert hints.estimated_row_count == 5_000_000

    # "None of these": the table is narrowed through a joined table, not a column of its own.
    C.apply(
        root,
        [
            {
                "op": "edit",
                "kind": "table",
                "area": area,
                "name": "orders",
                "field": "recommended_filters",
                "value": [],
            }
        ],
    )
    hints = load_datasource(root).subject_areas[0].defined_table("orders").performance_hints
    assert hints.recommended_filters == [] and hints.estimated_row_count == 5_000_000


def _runner_with(rows: str):
    return make_catalog_runner(
        tables=["orders"],
        columns={
            "orders": [
                col("id", "integer", nullable=False),
                col("order_date", "date"),
                col("row_loaded_at", "timestamp"),
            ]
        },
        estimate=rows,
    )


def _filters(root):
    return (
        load_datasource(root)
        .subject_areas[0]
        .defined_table("orders")
        .performance_hints.recommended_filters
    )


def test_a_refresh_keeps_the_answer_and_does_not_ask_again(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_runner, artifacts_dir=tmp_path)
    root, area = tmp_path / "shop", org.subject_areas[0].name
    C.apply(
        root,
        [
            {
                "op": "edit",
                "kind": "table",
                "area": area,
                "name": "orders",
                "field": "recommended_filters",
                "value": [],
            }
        ],
    )
    _, report = I.introspect(
        "shop", "postgres", runner=_runner, artifacts_dir=tmp_path, append=True
    )
    assert _filters(root) == []  # "None of these" survives the refresh
    assert report.large_tables == {}  # and the refresh does not ask about it again


def test_a_table_that_grows_large_is_seeded_and_listed(tmp_path):
    I.introspect("shop", "postgres", runner=_runner_with("500"), artifacts_dir=tmp_path)
    _, report = I.introspect(
        "shop", "postgres", runner=_runner_with("5000000"), artifacts_dir=tmp_path, append=True
    )
    assert _filters(tmp_path / "shop") == ["order_date", "row_loaded_at"]
    assert list(report.large_tables) == ["orders"]


@pytest.mark.parametrize("value", ["order_date", ["nope"], [1]])
def test_a_bad_filter_value_is_refused(tmp_path, value):
    org, _ = I.introspect("shop", "postgres", runner=_runner, artifacts_dir=tmp_path)
    root = tmp_path / "shop"
    res = C.apply(
        root,
        [
            {
                "op": "edit",
                "kind": "table",
                "area": org.subject_areas[0].name,
                "name": "orders",
                "field": "recommended_filters",
                "value": value,
            }
        ],
    )
    assert res.skipped and not res.applied
    assert _filters(root) == ["order_date", "row_loaded_at"]


def test_a_filter_is_stored_in_the_columns_own_spelling(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_runner, artifacts_dir=tmp_path)
    root = tmp_path / "shop"
    C.apply(
        root,
        [
            {
                "op": "edit",
                "kind": "table",
                "area": org.subject_areas[0].name,
                "name": "orders",
                "field": "recommended_filters",
                "value": ["ORDER_DATE"],
            }
        ],
    )
    assert _filters(root) == ["order_date"]
