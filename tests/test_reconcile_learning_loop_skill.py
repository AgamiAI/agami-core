"""ACE-116 — `agami-reconcile` grades a statement the person supplied, and the pins that hold it.

Everything here asserts on Markdown, for the reason the ah111 pins give about themselves: the
helpers cannot make the AI run a person's statement through the same tier it runs its own, refuse
to promote a match nobody could verify, or write a defect in the person's query down as the
person's rather than as agami's. The skill is the only place those live, so "the skill says X" is
the only decidable check there is.

The RED baseline in `tests/fixtures/reconcile/RED.md` is the other half: what the skill did with
these inputs before this slice, recorded before a word was edited.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS = REPO_ROOT / "plugins" / "agami" / "skills"
SHARED = REPO_ROOT / "plugins" / "agami" / "shared"

SKILL = (SKILLS / "agami-reconcile" / "SKILL.md").read_text(encoding="utf-8")
FRONTMATTER = SKILL.split("---")[1]
WHEN_TO_USE = re.search(r"^when_to_use: \"(.*)\"$", FRONTMATTER, re.MULTILINE).group(1)
CONVENTIONS = (SHARED / "invocation-conventions.md").read_text(encoding="utf-8")
PLAN_MODE = (SHARED / "plan-mode-check.md").read_text(encoding="utf-8")
SAVE_CORRECTION = (SKILLS / "agami-save-correction" / "SKILL.md").read_text(encoding="utf-8")
REFERENCES = {name: (SHARED / name).read_text(encoding="utf-8")
              for name in ("evidence-row.md", "part-ledger.md", "statement-check.md")}


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


PHASE_1_5 = _between(SKILL, "## Phase 1.5:", "## Phase 2:")
RECORD = _between(SKILL, "### 2d — Build the row record", "## Phase 3: Present")
HARD_RULE = _between(SKILL, "## Hard rule for a statement the person supplies", "## Roadmap")
HARD_RULE_3 = _between(SKILL, "3. **Don't write to the semantic model from this skill.**", "\n4. ")
NEW_PROSE = PHASE_1_5 + RECORD + _between(SKILL, "### 3b.5", "### 3c") + HARD_RULE + HARD_RULE_3

BARE_MODEL = re.compile(r"(?<!semantic )(?<!Semantic )\bthe model\b", re.IGNORECASE)


# --- the baseline -----------------------------------------------------------


def test_the_red_baseline_was_recorded_before_the_edit():
    red = (REPO_ROOT / "tests" / "fixtures" / "reconcile" / "RED.md").read_text(encoding="utf-8")
    assert "2026-09-11" in red
    # The two behaviours the slice exists to change, seen and written down first.
    assert "parse" in red and "3e" in red
    assert not BARE_MODEL.search(red)


# --- the front door ---------------------------------------------------------------


def test_four_shapes_are_detected_and_never_asked_for():
    phase_0 = _between(SKILL, "## Phase 0: Preflight", "## Phase 1:")
    assert "accept any of four shapes, or a mix" in phase_0
    assert "**statement branch**" in phase_0 and "**questions branch**" in phase_0
    assert "welcoming all four" in phase_0
    phase_1 = _between(SKILL, "## Phase 1:", "## Phase 1.5:")
    for heading in ("### Statement branch", "### Questions branch", "### 1n — Normalize"):
        assert heading in phase_1, heading
    assert 'reconcile.py" intake --file' in phase_1
    assert "**Do not run the statement here.**" in phase_1


def test_the_routing_phrases_are_the_skills_own_on_both_surfaces():
    for phrase in ("here is the SQL behind each tile", "validate this query part by part",
                   "audit this SQL", "check these questions"):
        assert phrase in WHEN_TO_USE, phrase
        assert phrase in CONVENTIONS, phrase


def test_the_plan_mode_refusal_no_longer_assumes_a_csv():
    section = _between(PLAN_MODE, "### `agami-reconcile`", "### `agami-eval`")
    assert "CSV path" not in section
    assert "re-invoke me with the same input" in section
    # And the skill's own Phase 0 quotes the same sentence, so the two cannot drift apart.
    assert "re-invoke me with the same input" in _between(SKILL, "## Phase 0: Preflight", "## Phase 1:")


# --- the statement is evidence ---------------------------------------------------------


def test_the_statement_is_evidence_and_the_rule_names_the_worked_case():
    assert "**The person's query is evidence, never the answer.**" in SKILL
    assert SKILL.index("## Hard rule for screenshots") < SKILL.index(
        "## Hard rule for a statement the person supplies") < SKILL.index("## Roadmap")
    assert "**The person's query is not ground truth.**" in HARD_RULE
    assert "'Hold'" in HARD_RULE and "query_defect" in HARD_RULE
    assert "a part reaches `model_gap` only by measurement" in HARD_RULE
    assert "match_unverified" in HARD_RULE


def test_phase_1_5_runs_the_statement_the_way_agami_runs_its_own():
    assert "**The person's statement runs on the road agami's own SQL runs, with the same guards.**" in PHASE_1_5
    assert "`sm prepare` first" in PHASE_1_5
    assert "never `--no-safety`" in PHASE_1_5
    assert "**A refusal is a finding, not a crash**" in PHASE_1_5
    assert "Never rewrite the statement and never retry." in PHASE_1_5
    assert "Nothing in this phase writes `query_log.jsonl`" in PHASE_1_5
    for ref in ("shared/statement-check.md", "shared/part-ledger.md", "shared/sql-generation-rules.md",
                "shared/db_error_classifier.md"):
        assert ref in PHASE_1_5, ref
    assert 'reconcile.py" ledger --row-dir' in PHASE_1_5
    assert "A part reaches `model_gap` only by measurement" in PHASE_1_5


def test_the_run_never_touches_the_persons_statement_before_grading():
    # Phase 1 reads and confirms; 1.5 runs and grades; only then does 2a ask agami the question.
    assert SKILL.index("### Statement branch") < SKILL.index("## Phase 1.5:") < SKILL.index("### 2a —")


# --- the record and the statuses -----------------------------------------------------------


def test_the_row_record_gains_its_keys_after_error_and_its_two_statuses():
    assert ('"status":       "match" | "match_unverified" | "mismatch" | "expected_doubtful" | "error",'
            in RECORD)
    error_at = RECORD.index('"error":')
    for key in ("provenance", "statement", "statement_recorded", "statement_receipt_path", "receipt_path",
                "ledger", "ledger_verdict", "comparison", "claims", "finding_keys"):
        assert RECORD.index(f'"{key}":') > error_at, key
    # The rules are applied by code, and the record says so.
    assert 'reconcile.py" status --match' in RECORD
    for status in ("`match_unverified`", "`expected_doubtful`"):
        assert status in RECORD


def test_compare_and_findings_sit_between_the_record_and_present():
    assert (SKILL.index("### 2d — Build the row record") < SKILL.index("### 2e — Compare")
            < SKILL.index("### 2f — Write the findings") < SKILL.index("## Phase 3: Present"))
    compare = _between(SKILL, "### 2e — Compare", "### 2f")
    assert "compare-results" in compare and "--golden-sql-file" in compare
    assert 'sm" claims' in compare and "--with-claims" in compare
    assert "they never say who is right" in compare
    findings = _between(SKILL, "### 2f — Write the findings", "## Phase 3: Present")
    assert 'reconcile.py" findings --run-dir' in findings
    assert "findings.json" in findings and "query_defects.json" in findings
    assert "never result rows beyond the one recorded cell" in findings


# --- presenting -------------------------------------------------------------------------


def test_the_summary_gains_a_second_line_and_the_statements_get_their_own_table():
    summary = _between(SKILL, "### 3a — Summary line first", "### 3b — Mismatches")
    assert "match_unverified" in summary and "expected_doubtful" in summary
    assert SKILL.index("### 3b — Mismatches") < SKILL.index("### 3b.5 — Your statements") < SKILL.index("### 3c —")
    statements = _between(SKILL, "### 3b.5 — Your statements", "### 3c")
    assert "no SQL in chat" in statements
    for grade in ("query_defect", "model_gap", "unresolved"):
        assert grade in statements, grade
    assert "/agami-save-correction" in statements


def test_hard_rule_3_gains_the_findings_file_and_keeps_every_pin():
    rule = SKILL.split("3. **Don't write to the semantic model from this skill.**")[1].split("\n4. ")[0]
    assert "findings file" in rule
    assert "never a metric, a join, a column or a default filter" in rule
    assert "A `query_defect` in the person's statement never becomes a finding" in rule
    # The pins the ah111 tests hold, restated so a later edit of this rule fails here too.
    for pinned in ("never mutates a metric, a join, a column", "golden_author.py save", "sm add-example",
                   "No row goes to both"):
        assert pinned in rule, pinned


def test_the_cheat_sheet_covers_the_new_failures():
    sheet = _between(SKILL, "## Error handling cheat sheet", "## Hard rule for screenshots")
    for symptom in ("exits `4`", "refused by the scope gate", "fails the zero-row check",
                    "`sensitive` or unquotable", "A probe's CSV is empty"):
        assert symptom in sheet, symptom
    assert "Never rewrite or retry the statement" in sheet


# --- the neighbours -----------------------------------------------------------------------


def test_save_correction_grades_a_pasted_statement_with_the_same_ledger():
    phase_1d = _between(SAVE_CORRECTION, "### 1d —", "## Phase 2")
    assert "shared/part-ledger.md" in phase_1d
    assert "a pasted statement is evidence" in phase_1d


def test_no_bare_the_model_in_the_new_prose_or_the_references():
    assert not BARE_MODEL.search(NEW_PROSE), BARE_MODEL.search(NEW_PROSE).group(0)
    for name, text in REFERENCES.items():
        assert not BARE_MODEL.search(text), (name, BARE_MODEL.search(text).group(0))
