"""The comparator names which golden column paired with which generated column, by values, and which
generated columns paired with none; a renamed column is the same column, not a missing one."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from semantic_model.comparator import ExecResult, compare_result_sets


def test_renamed_and_extra_columns_are_reported_as_pairs_and_extras():
    golden = ExecResult(columns=["number", "status", "channel"], rows=[("A1", "paid", "web"), ("A2", "open", "shop")])
    generated = ExecResult(columns=["o.number", "o.status", "region"], rows=[("A1", "paid", "EU"), ("A2", "open", "US")])
    score = compare_result_sets(golden, generated, match="values")
    assert score.accuracy == 0.0 and score.unmatched_golden_columns == ("channel",)
    assert score.column_pairs == (("number", "o.number"), ("status", "o.status")) and score.unmatched_generated_columns == ("region",)
    generated2 = ExecResult(columns=["o.number", "o.status", "o.channel", "region"], rows=[("A1", "paid", "web", "EU"), ("A2", "open", "shop", "US")])
    score2 = compare_result_sets(golden, generated2, match="values")
    assert score2.accuracy == 1.0 and score2.column_pairs == (("number", "o.number"), ("status", "o.status"), ("channel", "o.channel"))
    assert score2.unmatched_generated_columns == ("region",) and score2.unmatched_golden_columns == ()


def test_a_scalar_pair_serialises_and_other_levels_report_nothing():
    score = compare_result_sets(ExecResult(columns=["revenue"], rows=[(Decimal("10"),)]), ExecResult(columns=["total"], rows=[(Decimal("10"),)]), match="values")
    d = dataclasses.asdict(score)
    assert list(map(list, d["column_pairs"])) == [["revenue", "total"]] and d["unmatched_generated_columns"] == ()
    shape = compare_result_sets(ExecResult(columns=["a"], rows=[(1,)]), ExecResult(columns=["b"], rows=[(2,)]), match="shape")
    assert shape.column_pairs == () and shape.unmatched_generated_columns == ()
