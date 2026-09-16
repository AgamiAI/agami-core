"""ACE-117 — Phase 2.5 of `agami-reconcile`: grading the answers when there is nothing to compare
against. Prose pins, in the ah111 style, for the one phase that talks to a person about many rows at
once and must never turn into one prompt per answer."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL = (REPO_ROOT / "plugins" / "agami" / "skills" / "agami-reconcile" / "SKILL.md").read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


PHASE = _between(SKILL, "### 2.5 — Grade the answers", "### 2c — Diff")


def test_the_phase_sits_after_the_run_and_before_the_diff():
    assert SKILL.index("### 2b — Run via the agami-query pipeline") < SKILL.index("### 2.5 — Grade the answers") \
        < SKILL.index("### 2c — Diff")


def test_one_page_and_one_block_never_one_prompt_per_answer():
    assert "**One page, one block back. Never one prompt per answer**" in PHASE
    assert "render_reconcile_grades.py" in PHASE and "parse_reconcile_grades.py" in PHASE
    assert "End the turn." in PHASE


def test_the_page_never_shows_a_result_row():
    assert "Never a result row: the answer is one cell or a shape." in PHASE


def test_each_grade_has_one_consequence_and_it_is_named():
    assert "**`right`** → the answer becomes the row's `expected`" in PHASE
    assert "**`wrong` with `sql`** → the SQL is a statement the person supplies, graded like any other" in PHASE
    assert "take that row through Phase 1.5" in PHASE
    assert "**`wrong` with `words`**" in PHASE and "a finding of kind `description` carrying them" in PHASE
    assert "**`unsure`**" in PHASE and "Nothing else happens." in PHASE


def test_a_block_that_does_not_parse_applies_nothing():
    assert ("A `needs_judgment` means the block did not parse, or a grade in it could not be applied as written "
            "(a misspelt grade, a row graded twice, SQL beside a `right`): ask for it again, apply nothing.") in PHASE


def test_no_bare_the_model_in_the_phase():
    import re
    assert not re.search(r"(?<!semantic )\bthe model\b", PHASE, re.IGNORECASE)
