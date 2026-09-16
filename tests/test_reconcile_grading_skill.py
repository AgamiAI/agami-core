"""ACE-150 — Phase 2.5 of `agami-reconcile`: the questions branch, where agami answered and there is
nothing to compare against. Prose pins for the one phase that has a query to check and no answer key,
so it must both measure what it can and leave to a person what it cannot. Supersedes ACE-117, whose
separate grading page this phase replaced with the reconciliation report."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL = (REPO_ROOT / "plugins" / "agami" / "skills" / "agami-reconcile" / "SKILL.md").read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


PHASE = _between(SKILL, "### 2.5 — Check agami's query", "### 2c — Diff")


def test_the_phase_sits_after_the_run_and_before_the_diff():
    assert SKILL.index("### 2b — Run via the agami-query pipeline") < SKILL.index("### 2.5 — Check agami's query") \
        < SKILL.index("### 2c — Diff")


def test_the_query_is_checked_by_the_same_ledger_as_a_supplied_statement():
    assert "Take agami's statement from 2b through **Phase 1.5**" in PHASE
    assert "`statement.sql`" in PHASE


def test_agamis_own_result_never_becomes_the_expected_value():
    """The never-ground-truth rule at its sharpest: the side under test cannot supply the answer key."""
    assert "**1.5f does not apply.**" in PHASE
    assert "Agami's own result never becomes `expected`" in PHASE
    assert "compares agami against agami" in PHASE


def test_claims_do_not_run_with_one_statement():
    assert "**1.5e runs without `--with-claims`.** Claims compare two statements and there is only one." in PHASE


def test_the_fit_check_still_runs():
    assert "**1.5g still runs**" in PHASE


def test_a_failing_check_names_agami_not_the_person():
    assert "`query_defect` on this row means agami wrote a query with a mistake in it" in PHASE
    assert "`model_gap` is a gap in the semantic model whoever tripped on it" in PHASE


def test_the_row_lands_on_the_reconciliation_report_not_a_page_of_its_own():
    assert "Report it on the reconciliation report" in PHASE
    assert "render_reconcile_grades.py" not in PHASE and "parse_reconcile_grades.py" not in PHASE
    assert "grade.html" not in PHASE


def test_the_data_section_shows_the_answer_being_judged():
    assert "shows up to five of agami's rows" in PHASE
    assert "an answer nobody can see cannot be judged" in PHASE


def test_the_decisions_are_the_same_six():
    assert "The decisions are the same six" in PHASE
    for word in ("`example`", "`change`", "`fix`", "`reword`", "`nothing`"):
        assert word in PHASE


def test_no_bare_the_model_in_the_phase():
    assert not re.search(r"(?<!semantic )\bthe model\b", PHASE, re.IGNORECASE)
