"""Phase 2 (scorecard #3): additivity constraints on metrics.

A semi-additive metric declares `non_additive_dimensions` (the dimensions it can't
be summed over — usually time) and optionally `semi_additive_agg` (how to collapse
over them). The declaration lands + validates here; enforcement is Phase 4 (#4).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import models as m  # noqa: E402


def _metric(**kw):
    base = dict(name="total balance", calculation="sum of balances at period end",
                bindings={"PostgreSQL": "SUM(balance)"}, source_tables=["daily_balances"])
    base.update(kw)
    return m.Metric(**base)


def test_fully_additive_metric_leaves_fields_empty():
    mm = _metric(name="revenue", calculation="sum of line totals")
    assert mm.non_additive_dimensions == []
    assert mm.semi_additive_agg is None


def test_semi_additive_metric_declares_dims_and_agg():
    mm = _metric(non_additive_dimensions=["time"], semi_additive_agg="last")
    assert mm.non_additive_dimensions == ["time"]
    assert mm.semi_additive_agg == "last"


def test_non_additive_dims_without_agg_is_allowed():
    # dims-only = "refuse to sum over these" (Phase 4 blocks rather than auto-collapses)
    mm = _metric(non_additive_dimensions=["time"])
    assert mm.semi_additive_agg is None


def test_agg_without_dims_is_rejected():
    # "how to collapse" is meaningless without naming what to collapse over
    with pytest.raises(Exception):
        _metric(semi_additive_agg="last")


def test_bad_semi_additive_agg_value_rejected():
    with pytest.raises(Exception):
        _metric(non_additive_dimensions=["time"], semi_additive_agg="sum")


def test_round_trip_through_dump_and_load():
    mm = _metric(non_additive_dimensions=["snapshot_date"], semi_additive_agg="average")
    dumped = mm.model_dump()
    assert dumped["non_additive_dimensions"] == ["snapshot_date"]
    assert dumped["semi_additive_agg"] == "average"
    again = m.Metric(**dumped)
    assert again.non_additive_dimensions == ["snapshot_date"]
    assert again.semi_additive_agg == "average"
