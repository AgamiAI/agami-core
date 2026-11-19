"""`signoff_counts` calls a definition `signed` only when a person approved it, and, given a run's
start, only when the approval is dated at or after that start."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import curate  # noqa: E402
from semantic_model import models as m  # noqa: E402

RUN_START = 1_800_000_000.0  # 2027-01-15, epoch seconds
BEFORE = "2027-01-01T00:00:00+00:00"
AFTER = "2027-02-01T00:00:00+00:00"


def _metric(name: str, state: str, at: str | None, by: str = "you@example.com") -> m.Metric:
    return m.Metric(name=name, calculation="count of rows", review_state=state,
                    signed_off_by=by, signed_off_at=at)


def _org(*metrics: m.Metric) -> m.Datasource:
    return m.Datasource(datasource="o", subject_areas=[m.SubjectArea(name="a", metrics=list(metrics))])


def test_a_not_applicable_sign_off_is_not_signed():
    org = _org(_metric("kept", "approved", AFTER), _metric("skipped", "not_applicable", AFTER))
    assert curate.signoff_counts(org)["signed"] == 1
    assert curate.signoff_counts(org, RUN_START)["signed"] == 1


def test_an_undated_approval_is_not_signed_this_run():
    org = _org(_metric("dated", "approved", AFTER), _metric("undated", "approved", None))
    out = curate.signoff_counts(org, RUN_START)
    assert (out["signed"], out["earlier"]) == (1, 1)


def test_without_a_run_start_every_persons_approval_counts_as_before():
    org = _org(_metric("a1", "approved", BEFORE), _metric("a2", "approved", None))
    assert curate.signoff_counts(org)["signed"] == 2
