"""`metric_null_filled` and the row-keeping convention: a missing value is not a zero (#478).

A binding like COALESCE(staff_count, 0) on a column whose description says NULL means "not
recorded" invents a value: the row gets a result it should not have, averages and rates are
pulled toward zero, and sessions copy the binding verbatim. These pin what the warning catches,
what it deliberately leaves alone, and that every model's derived context carries the rule on
rows whose value cannot be calculated.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import models as m  # noqa: E402
from semantic_model import org_draft as OD  # noqa: E402
from semantic_model import validator as V  # noqa: E402

NOT_RECORDED = "Staff on the operation. NULL means the count was not recorded."


def _org(binding: str, *, staff_desc: str = NOT_RECORDED, staff_caveats: list[str] | None = None,
         storage_type: str = "PostgreSQL") -> m.Datasource:
    t = m.Table(name="operations", schema="s", storage_connection="c", grain=["op_id"],
                description="One row per operation.", columns=[
                    m.Column(name="op_id", type="integer", primary_key=True),
                    m.Column(name="staff_count", type="integer", description=staff_desc,
                             caveats=staff_caveats or []),
                    m.Column(name="volunteers", type="integer", description="Volunteers on it.")])
    met = m.Metric(name="staff_per_op", description="Average staff per operation",
                   calculation="Average staff count per operation.", source_tables=["operations"],
                   bindings={storage_type: binding})
    sa = m.SubjectArea(name="s", tables_defined=[t], metrics=[met])
    return m.Datasource(datasource="demo", subject_areas=[sa])


def _warnings(org: m.Datasource) -> list[str]:
    res = V.ValidationResult()
    V._check_metric_null_fill(org, res)
    return [f.message for f in res.findings if f.code == "metric_null_filled"]


@pytest.mark.parametrize("storage_type,binding", [
    ("PostgreSQL", "AVG(COALESCE(staff_count, 0))"),
    ("PostgreSQL", "AVG(COALESCE(operations.staff_count, 0) + volunteers)"),
    ("PostgreSQL", "AVG(COALESCE(staff_count, -1))"),
    ("PostgreSQL", "AVG(COALESCE(CAST(staff_count AS NUMERIC), 0.0))"),
    ("MySQL", "AVG(IFNULL(staff_count, 0))"),
    ("Oracle", "AVG(NVL(staff_count, 0))"),
    ("SQLServer", "AVG(ISNULL(staff_count, 0))"),
])
def test_a_constant_fill_on_a_not_recorded_column_warns(storage_type, binding):
    msgs = _warnings(_org(binding, storage_type=storage_type))
    assert len(msgs) == 1
    assert "staff_per_op" in msgs[0] and "staff_count" in msgs[0]


@pytest.mark.parametrize("desc,caveats", [
    ("Staff on the operation. NULL means none were assigned.", None),
    ("Staff on the operation.", ["A missing value is treated as 0."]),
    ("Staff count; empty counts as zero.", None),
])
def test_a_column_that_says_missing_means_zero_is_left_alone(desc, caveats):
    assert _warnings(_org("AVG(COALESCE(staff_count, 0))", staff_desc=desc,
                          staff_caveats=caveats)) == []


@pytest.mark.parametrize("binding", [
    "AVG(staff_count)",  # NULL propagates: what it should be
    "COALESCE(SUM(staff_count), 0)",  # a total over no rows, not a value invented for a row
    "AVG(COALESCE(staff_count, volunteers))",  # a fallback column, not a constant
    "MAX(COALESCE(label_text, 'none'))",  # a text label is not a numeric fill
    "AVG(COALESCE(unlisted_col, 0))",  # not a column of the source table: another check's job
    "AVG(COALESCE(staff_count, 0",  # does not parse: no evidence
])
def test_what_is_not_a_constant_fill_of_a_known_column_is_left_alone(binding):
    assert _warnings(_org(binding)) == []


def test_validate_runs_the_check():
    res = V.validate(_org("AVG(COALESCE(staff_count, 0))"))
    assert any(f.code == "metric_null_filled" for f in res.findings)


def test_every_derived_context_carries_the_row_keeping_rule():
    text = OD.derived_context(_org("AVG(staff_count)"))
    assert "### Conventions" in text
    assert "placed after the rest" in text
    assert "counts in neither the numerator nor the denominator" in text
    assert "not counted as 0 unless its column says that missing means zero" in text


def test_units_still_render_beside_the_rule():
    org = _org("AVG(staff_count)")
    org.subject_areas[0].tables_defined[0].columns[1].unit = "USD"
    text = OD.derived_context(org)
    assert "- Units / currency in use: USD." in text
    assert text.index("Units / currency") < text.index("placed after the rest")
