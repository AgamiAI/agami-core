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
              for name in ("evidence-row.md", "part-ledger.md", "statement-check.md", "plain-language.md")}


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
    assert "the four shapes as options" in phase_0
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
    assert "compare-results" in compare and "--unordered" in compare and "--golden-sql-file" not in compare
    assert "Row order is never part of this comparison" in compare and "eight claims" in compare
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
    # The three kinds of part live in three blocks, so an open part is never counted as a defect and
    # a noticed fact is never counted as a grade. The table's example rows carry no `unresolved` row.
    table = statements.split("**What couldn't be checked**")[0]
    assert "| unresolved" not in table and "| noted" not in table
    assert "**What couldn't be checked**" in statements and "**What this run noticed**" in statements
    assert "`noted` part is not a grade at all" in statements
    assert "one grade per part, `confirmed`, `model_gap`, `query_defect` or `unresolved`" in PHASE_1_5
    assert "`noted`, a fact the run states and never judges" in PHASE_1_5
    assert "| `noted` |" in REFERENCES["part-ledger.md"]
    # The semantic model's own words ride on the parts that fell short, quoted and never graded.
    assert "sm mentions" in PHASE_1_5 and "> mentions.json" in PHASE_1_5
    assert "**What the semantic model says in words**" in statements
    assert "which is right is the person's call, never the ledger's" in statements
    assert "| `mentions.json` |" in REFERENCES["part-ledger.md"]
    assert "mentions" in REFERENCES["statement-check.md"]
    # 1.5g: the one judgment in the ledger, named as such, with its four doubt signals and its file.
    fit = PHASE_1_5.split("**1.5g")[1]
    for signal in ("grain differs", "measure differs", "a filter is present the question never asked for", "time window differs"):
        assert signal in fit, signal
    assert "`question_fit.json`" in fit and "a judgment made by reading" in fit
    assert "never proves anything about the semantic model" in fit
    assert "question_fit:" in statements and "reword the question or the statement and re-run this row" in statements
    assert "For every statement row, write `question_fit.json`" in PHASE_1_5


def test_reconcile_takes_input_the_way_connect_does():
    """Nothing given: an options prompt with the four shapes, the rarer inputs named in the prompt. A CSV
    with nothing to hand: a template file written and a hand-off that ends the turn. Rows read: an
    intake page shown before anything runs, one block back, applied by a parser and never by hand."""
    preflight = SKILL.split("## Phase 0: Preflight", 1)[1].split("## Phase 1: Extract", 1)[0]
    assert "**AskUserQuestion**, the four shapes as options" in preflight
    for label in ("`A screenshot of the dashboard`", "`A CSV or an export`", "`The SQL you trust`", "`A list of questions`"):
        assert label in preflight, label
    assert "reconcile.example.csv" in preflight and "label,value,sql,question" in preflight
    assert "**End the turn.**" in preflight and "never guessed at" in preflight
    intake = _between(SKILL, "### 1n — Normalize", "## Phase 1.5")
    assert "render_reconcile_intake.py" in intake and "--intake-file" in intake
    assert "parse_reconcile_intake.py" in intake and "--rows-file" in intake
    assert "**end the turn**" in intake and "never hand-edit the rows" in intake
    assert "inbox" in preflight.split("**If they gave nothing**")[0] and "local/reconcile/inbox/" in intake
    assert 'A per-row "is this the question?" in chat is never asked' in intake
    assert "only for a statement that came alone" in PHASE_1_5
    assert "| `question_fit` |" in REFERENCES["part-ledger.md"]
    assert "question_fit.json" in REFERENCES["statement-check.md"] and "question_fit" in REFERENCES["evidence-row.md"]


def test_phase_three_speaks_plainly_and_names_four_actors():
    """Every sentence about a difference names who did what, and the AI is never one of them; the word
    "model" alone is never written; the ledger's mechanism words stay in tables and files. The pointer
    sits before 3a, and 3a is untouched."""
    opening = SKILL.split("## Phase 3: Present", 1)[1].split("### 3a — Summary line first", 1)[0]
    assert "shared/plain-language.md" in opening and "never the mechanism" in opening
    assert "steering, front-running or deciding" in opening
    plain = REFERENCES["plain-language.md"]
    for actor in ("**you**", "**agami**", "**the semantic model**", "**the prompt examples**", "**the data**"):
        assert actor in plain, actor
    # The bare word is never written; the reference says which of four things each name stands for.
    assert '## The word "model" on its own is never written' in plain
    for meaning in ("the semantic model", "the large language model", "the prompt examples", "the data model"):
        assert any(line.startswith("| " + meaning) for line in plain.splitlines()), meaning
    assert 'never write the word "model" on its own' in opening
    for machinery, said in (("fan-out", "the join repeats rows"), ("anti-join", "also holds"),
                            ("predicate", "filter"), ("query_defect", "a mistake in your query"),
                            ("model_gap", "missing"), ("noted", "not a problem"),
                            ("unresolved", "could not be checked")):
        row = next(line for line in plain.splitlines() if line.startswith("| ") and machinery in line.split("|")[1])
        assert said in row, (machinery, row)
    assert "## A worked example" in plain and "front-running" in plain
    statements = _between(SKILL, "### 3b.5 — Your statements", "### 3c")
    assert "shared/plain-language.md" in statements and 'never "fan-out"' in statements


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


def test_phase_three_is_told_in_four_beats_and_the_ledger_runs_once():
    """The beats lead and the pinned sections follow: the opener names the four beats and where each
    comes from, 3a.5 sits between 3a and 3b, beat 4 has both halves and keep is 3e's one offer. The
    ledger runs once per row, in 2e, and every reference says so."""
    opening = SKILL.split("## Phase 3: Present", 1)[1].split("### 3a — Summary line first", 1)[0]
    assert "**Tell every row in four beats, in the reader's order.**" in opening
    for beat in ("**how we read your input and how we checked it**", "**what agami did with the question and what it answered**",
                 "**how agami got there**", "**what to change so the output matches, on whichever side the mistake is**",
                 "**what to keep**"):
        assert beat in opening, beat
    assert "keep is Phase 3e's offer, made once for the batch and never per row" in opening
    assert SKILL.index("### 3a — Summary line first") < SKILL.index("### 3a.5 — Every row in four beats") \
        < SKILL.index("### 3b — Mismatches")
    story = _between(SKILL, "### 3a.5 — Every row in four beats", "### 3b — Mismatches")
    for line in ("1. What you gave us:", "2. What agami did:", "3. How it got there:", "4. What to change:", "4. Keep it:"):
        assert line in story, line
    assert "Beat 4 never asks anything per row" in story
    assert SKILL.split("### 3e — Offer promotion", 1)[1].lstrip().startswith("This is beat 4's keep half, made once for the batch.")
    # One ledger run per row.
    assert "**Run it once per row, in Phase 2e, after the comparison**" in PHASE_1_5
    compare = _between(SKILL, "### 2e", "### 2f")
    assert "**This is the row's one ledger run.**" in compare and "Never run it twice." in compare
    assert "once per row and after the comparison" in REFERENCES["statement-check.md"]
    assert "and again with `--with-claims`" not in REFERENCES["statement-check.md"]
    assert "once per row after the" in REFERENCES["part-ledger.md"]


def test_the_four_beats_are_also_a_page_and_a_keep_is_never_the_pages_to_grant():
    story = _between(SKILL, "### 3a.5 — Every row in four beats", "### 3b — Mismatches")
    assert "render_reconcile_report.py" in story and "parse_reconcile_report.py" in story
    assert "--run-dir" in story and "--match-rows" not in story
    assert "offers `keep` only on rows whose status is `match` with a single recorded cell" in story
    assert "never from anything typed" in story
    assert "the offer's predicate is the ledger's and never the page's" in story
    assert "No decision writes anything this skill does not already write." in story
    assert "The chat keeps 3a, the link and one line of next steps" in story
    assert "one card per row for a batch and a checklist with a rail for a single audited query" in story
    assert "--layout cards|audit" in story and "report-items --run-dir" in story
    assert "Build the report items file with the verb, never by hand" in story
    assert "rewrite only `sentence` and `change`" in story and "never touch `diff`" in story
    assert "When the page cannot be written, the four beats below are said in chat instead" in story


def test_the_run_works_five_rows_at_a_time_and_resumes_from_its_checkpoint():
    """Phase 2 reads the next rows from rows.jsonl through next-chunk, ends the turn after each chunk
    with a progress line, runs through on `continue all`, never runs a row twice, and never skips a
    checkpoint line it cannot read. The pages filter what is shown, never what the block sends."""
    phase_2 = _between(SKILL, "## Phase 2: Generate questions + execute", "### 2a")
    assert "**Work five rows at a time.**" in phase_2
    assert "next-chunk --run-dir" in phase_2 and "--rows-file" in phase_2
    assert "never run twice" in phase_2 and "**continue all**" in phase_2 and "**end the turn**" in phase_2
    assert "Exit `4` means every row is in the checkpoint" in phase_2
    assert "never skip it" in phase_2 and "The keep-offer stays one per run (3e), never one per chunk." in phase_2
    intake = _between(SKILL, "### 1n — Normalize", "## Phase 1.5")
    assert '--out "<artifacts_dir>/local/reconcile/<ts>/intake.json"' in intake
    beats = _between(SKILL, "### 3a.5", "### 3b")
    assert "Render it after every chunk" in beats and "never what the block sends" in beats
    grading = _between(SKILL, "### 2.5 —", "## Phase 3")
    assert "filters by grade state" in grading


def test_the_items_file_step_1_writes_is_the_one_step_2_reads_and_the_run_goes_through_intake():
    story = _between(SKILL, "### 3a.5", "### 3b")
    assert 'report-items --run-dir "<artifacts_dir>/local/reconcile/<ts>"' in story
    assert '--items-file "<artifacts_dir>/local/reconcile/<ts>/report-items.json"' in story
    assert "/tmp/agami-reconcile-report-items" not in story
    csv_branch = _between(SKILL, "### CSV branch", "### ")
    assert "reconcile.py\" intake --file" in csv_branch and "parse --csv" not in csv_branch
    phase_2 = _between(SKILL, "## Phase 2: Generate questions + execute", "### 2a")
    assert 'resume --reconcile-dir' in phase_2 and "Exit `2` is a refusal and stderr says which" in phase_2


def test_phase_3_keeps_to_plain_words():
    phase_3 = SKILL[SKILL.index("## Phase 3: Present"):]
    for phrase in ("the numbers meet", "the numbers agree", "held on every part", "the evidence points to"):
        assert phrase not in phase_3, phrase
    layout = (Path(__file__).resolve().parent.parent / "plugins" / "agami" / "shared" / "file-layout.md").read_text()
    assert "report-items.json" in layout and "intake.html" in layout


def test_the_items_file_carries_the_result_and_the_fix():
    story = _between(SKILL, "### 3a.5", "### 3b")
    assert "`result` (`data`: matches, partly, differs or could_not_compare" in story and "`fix` (your query, the semantic model, the examples, the question, agami again, or nothing)" in story
    table = _between(SKILL, "### 2d", "### 2e")
    assert 'A query written differently from agami\'s is not this' in table and '"same answer, different query"' in table


def test_the_example_decision_has_a_route():
    story = _between(SKILL, "### 3a.5", "### 3b")
    assert "**`example`** takes the person's statement and its question to `/agami-save-correction` as a prompt example" in story


def test_agamis_answer_comes_from_a_cold_client_never_from_the_session():
    phase_2b = _between(SKILL, "### 2b", "### 2.5")
    assert "--ask-file /tmp/agami-reconcile-chunk-<ts>.json" in phase_2b and "--parallel 4" in phase_2b and "--out rows/<n>/agami-answer.json" in phase_2b
    assert "fetches the model context once for the batch" in phase_2b and "the session itself is never reused" in phase_2b
    assert "**Agami's answer comes from a cold client, never from this session.**" in phase_2b
    assert "**Never write agami's SQL yourself, and never retry with your own wording**" in phase_2b
    assert "four fixed sentences" in phase_2b and "the batch exits `3` when any row is like that" in phase_2b
    assert "Invoke the same SQL-generation + execution path agami-query uses" not in phase_2b

