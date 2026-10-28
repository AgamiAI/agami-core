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
         storage_type: str = "PostgreSQL",
         calculation: str = "Average staff count per operation.") -> m.Datasource:
    t = m.Table(name="operations", schema="s", storage_connection="c", grain=["op_id"],
                description="One row per operation.", columns=[
                    m.Column(name="op_id", type="integer", primary_key=True),
                    m.Column(name="staff_count", type="integer", description=staff_desc,
                             caveats=staff_caveats or []),
                    m.Column(name="volunteers", type="integer", description="Volunteers on it.")])
    met = m.Metric(name="staff_per_op", description="Average staff per operation",
                   calculation=calculation, source_tables=["operations"],
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


@pytest.mark.parametrize("calculation", [
    "The KB formula counts a missing staff count as 0, so this binding does the same.",
    "Per the business definition, which treats a missing count as zero.",
    "Average staff per operation; a missing staff count means none were assigned.",
])
def test_a_metric_that_names_the_definition_behind_its_fill_is_left_alone(calculation):
    # a definition the author was given can write the fill into its formula; saying so, with
    # where it comes from, is a reviewable reason, not an invented zero
    assert _warnings(_org("AVG(COALESCE(staff_count, 0))", calculation=calculation)) == []


@pytest.mark.parametrize("calculation", [
    "Total personnel = staff_counts.total (null as 0).",  # names no source
    "Missing staff counts are filled so every operation gets a score.",  # a reason, not a rule
])
def test_a_metric_that_fills_without_naming_a_source_still_warns(calculation):
    assert len(_warnings(_org("AVG(COALESCE(staff_count, 0))", calculation=calculation))) == 1


SKILL = (REPO_ROOT / "plugins" / "agami" / "skills" / "agami-connect" / "SKILL.md").read_text(
    encoding="utf-8")


def test_the_skill_tells_a_missing_value_from_no_related_rows():
    # a staff count nobody entered stays empty; a hub with no supply records serves no disasters,
    # which is often a real zero — the rule must not blank the second case along with the first
    assert "**a recorded value that is missing**" in SKILL
    assert "**no related rows at all**" in SKILL
    assert "decide it per metric from what the formula means" in SKILL
    assert '("the KB formula counts a missing percentage as 0")' in SKILL


@pytest.mark.parametrize("desc,calculation", [
    ("Staff on the operation. NULL is not zero.", "Average staff count per operation."),
    ("Staff on the operation. NULL means no measurement was recorded.", "Average staff count."),
    (NOT_RECORDED, "The business definition never treats a missing staff count as 0."),
    (NOT_RECORDED, "The KB formula does not count a missing staff count as 0."),
])
def test_a_statement_that_denies_the_zero_does_not_silence_the_warning(desc, calculation):
    org = _org("AVG(COALESCE(staff_count, 0))", staff_desc=desc, calculation=calculation)
    assert len(_warnings(org)) == 1


def _qualified_org(source_ref: str, *, other_schema_desc: str | None = None) -> m.Datasource:
    org = _org("AVG(COALESCE(staff_count, 0))")
    org.subject_areas[0].metrics[0].source_tables = [source_ref]
    if other_schema_desc is not None:
        # a same-named table in another schema, whose column does say missing means zero
        twin = m.Table(name="operations", schema="archive", storage_connection="c", grain=["op_id"],
                       description="Archived operations.", columns=[
                           m.Column(name="op_id", type="integer", primary_key=True),
                           m.Column(name="staff_count", type="integer",
                                    description=other_schema_desc)])
        org.subject_areas[0].tables_defined.append(twin)
    return org


def test_a_schema_qualified_source_table_is_resolved():
    assert len(_warnings(_qualified_org("s.operations"))) == 1


def test_same_named_tables_in_other_schemas_do_not_lend_their_text():
    # `archive.operations` says NULL means none; the metric reads `s.operations`, which does not
    org = _qualified_org("s.operations", other_schema_desc="NULL means none were assigned.")
    assert len(_warnings(org)) == 1


def test_a_qualifier_in_the_sql_picks_its_table():
    org = _org("AVG(COALESCE(operations.staff_count, 0))")
    assert len(_warnings(org)) == 1


def test_the_query_time_rule_carries_the_sourced_definition_exception():
    text = OD.derived_context(_org("AVG(staff_count)"))
    assert "or the metric's calculation says the definition it follows counts it as 0" in text


# --- An average of the values that exist (#519) ---------------------------------------------


@pytest.mark.parametrize("binding", [
    "SUM(COALESCE(staff_count, 0)) / NULLIF(COUNT(staff_count), 0)",
    "SUM(COALESCE(staff_count, 0) + COALESCE(volunteers, 0)) "
    "/ NULLIF(COUNT(staff_count) + COUNT(volunteers), 0)",
    "SUM(COALESCE(staff_count, 0)) "
    "/ NULLIF(SUM(CASE WHEN staff_count IS NOT NULL THEN 1 ELSE 0 END), 0)",
    "SUM(COALESCE(staff_count, 0)) * 1.0 / NULLIF(COUNT(staff_count), 0)",
    "SUM(COALESCE(operations.staff_count, 0)) / NULLIF(COUNT(operations.staff_count), 0)",
    "CAST(SUM(COALESCE(staff_count, 0)) AS NUMERIC) "
    "/ NULLIF(COUNT(CASE WHEN staff_count IS NOT NULL THEN 1 END), 0)",
    "1.0 * SUM(COALESCE(staff_count, 0)) "
    "/ NULLIF(SUM(CASE WHEN NOT staff_count IS NULL THEN 1 ELSE 0 END), 0)",
])
def test_a_fill_divided_by_the_count_of_present_values_is_left_alone(binding):
    # the zero adds to neither side, so the result is the average of the values that exist
    assert _warnings(_org(binding)) == []


@pytest.mark.parametrize("binding", [
    "SUM(COALESCE(staff_count, 0)) / NULLIF(COUNT(*), 0)",  # counts the missing rows too
    "SUM(COALESCE(staff_count, 0)) / NULLIF(COUNT(DISTINCT staff_count), 0)",
    "SUM(COALESCE(staff_count, 0) + COALESCE(volunteers, 0)) / NULLIF(COUNT(volunteers), 0)",
    "COUNT(staff_count) / SUM(COALESCE(staff_count, 0))",  # the fill is the denominator
    # another table's same-named column, or one whose table cannot be matched
    "SUM(COALESCE(a.staff_count, 0)) / NULLIF(COUNT(b.staff_count), 0)",
    "SUM(COALESCE(a.staff_count, 0)) / NULLIF(COUNT(staff_count), 0)",
    # a term that counts the missing rows, beside one that does not
    "SUM(COALESCE(staff_count, 0)) / NULLIF(COUNT(staff_count) + COUNT(*), 0)",
    "SUM(COALESCE(staff_count, 0)) / NULLIF(COUNT(staff_count) + 1, 0)",
    "SUM(COALESCE(staff_count, 0)) "
    "/ NULLIF(COUNT(CASE WHEN staff_count IS NOT NULL THEN 1 ELSE 0 END), 0)",  # counts every row
    # a fallback other than zero changes the total; another aggregate counts the filled rows
    "SUM(COALESCE(staff_count, 100)) / NULLIF(COUNT(staff_count), 0)",
    "AVG(COALESCE(staff_count, 0)) / NULLIF(COUNT(staff_count), 0)",
    "MAX(COALESCE(staff_count, 0)) / NULLIF(COUNT(staff_count), 0)",
    "SUM(COALESCE(staff_count, 0) * volunteers) / NULLIF(COUNT(staff_count), 0)",
    "SUM(COALESCE(staff_count + volunteers, 0)) / NULLIF(COUNT(staff_count), 0)",
    "SUM(COALESCE(staff_count, 0)) "
    "/ NULLIF(SUM(CASE WHEN staff_count IS NOT NULL THEN 2 ELSE 0 END), 0)",
    "SUM(COALESCE(staff_count, 0)) / NULLIF(SUM(CASE WHEN staff_count IS NULL THEN 1 ELSE 0 END), 0)",
    "SUM(COALESCE(staff_count, 0)) / NULLIF(SUM(CASE WHEN staff_count > 0 THEN 1 ELSE 0 END), 0)",
])
def test_a_denominator_that_does_not_count_only_present_values_still_warns(binding):
    assert len(_warnings(_org(binding))) == 1


def test_the_skill_and_the_shape_doc_show_the_present_values_form():
    shape = (REPO_ROOT / "plugins" / "agami" / "shared" / "metric-entity-shape.md").read_text(
        encoding="utf-8")
    for text in (SKILL, shape):
        assert "COUNT(a) + COUNT(b)" in text or "COUNT(score_a) + COUNT(score_b)" in text
        assert "`VALUES` list" in text


def test_the_documented_present_values_form_is_not_integer_division():
    # PostgreSQL and SQLite divide an integer sum by a count as integers: scores 1 and 2 gave 1.
    import re
    import sqlite3

    shape = (REPO_ROOT / "plugins" / "agami" / "shared" / "metric-entity-shape.md").read_text(
        encoding="utf-8")
    binding = re.search(r'PostgreSQL: "(SUM\(COALESCE\(score_a.*?)"', shape).group(1)
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (score_a INTEGER, score_b INTEGER)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [(1, None), (2, None)])
    assert con.execute(f"SELECT {binding} FROM t").fetchone()[0] == 1.5
    assert _warnings(_org(binding.replace("score_a", "staff_count").replace("score_b", "volunteers"))) == []
    skill_form = re.search(r"`(SUM\(COALESCE\(a, 0\).*?)`", SKILL).group(1)
    con.execute("CREATE TABLE u (a INTEGER, b INTEGER)")
    con.executemany("INSERT INTO u VALUES (?, ?)", [(1, None), (None, 2)])
    assert con.execute(f"SELECT {skill_form} FROM u").fetchone()[0] == 1.5
