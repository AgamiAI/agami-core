"""agami-reconcile decides on question cards: what was read, each row that did not match, and the keep.

The intake page and the report page stay as the optional "Show the full view", served from
localhost, and their pasted blocks are still accepted. The default path opens no page and waits for
no paste. A card's answers are written as the same block the page would send and go through the same
parser, so a card can do exactly what the page could and nothing more.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL = (REPO_ROOT / "plugins" / "agami" / "skills" / "agami-reconcile" / "SKILL.md").read_text(encoding="utf-8")
FULL_VIEW = "**Show the full view (optional, only when asked).**"


def _between(start: str, end: str) -> str:
    i = SKILL.index(start)
    return SKILL[i: SKILL.index(end, i + len(start))]


def _default_and_full(section: str) -> tuple[str, str]:
    assert FULL_VIEW in section, "the section names its optional full view"
    default, full = section.split(FULL_VIEW, 1)
    return default, full


# Waiting on a block pasted back, in any of the ways the skill has said it. A "pasted list" of
# numbers is an input shape, not a wait, so the bare word is not the check.
WAITS_FOR_PASTE = re.compile(r"paste (it|the block)|paste\w* .{0,40}\bback\b|pasted block", re.IGNORECASE)


def _intake() -> str:
    return _between("### 1n — Normalize", "## Phase 1.5")


def _results() -> str:
    return _between("### 3a.5 — Every row in four beats", "### 3b — Mismatches")


def _keep() -> str:
    return _between("### 3e — Offer promotion", "#### A question asked relative to today")


def test_the_skill_links_the_card_conventions():
    assert "shared/question-cards.md" in SKILL


def test_the_intake_is_a_table_and_a_card_with_the_three_answers():
    default, _ = _default_and_full(_intake())
    assert "AskUserQuestion" in default
    for label in ("**Looks right, run it**", "**Fix a number**", "**Drop some rows**", "**Show the full view**"):
        assert label in default, label
    assert "| Row | Question we'll ask | Expected | Your SQL | From |" in default
    assert "at most 15 rows" in default


def test_the_intake_default_opens_no_page_and_waits_for_no_paste():
    default, full = _default_and_full(_intake())
    assert "render_reconcile_intake.py" not in default
    assert not WAITS_FOR_PASTE.search(default)
    assert "end the turn" not in default.lower()
    assert "render_reconcile_intake.py" in full and "serve_page.py" in full
    assert "parse_reconcile_intake.py" in full


def test_intake_card_answers_go_through_the_intake_parser_and_numbers_through_reconcile():
    default, _ = _default_and_full(_intake())
    # A drop or a reworded question is written as the page's own block and applied by its parser;
    # a corrected number is parsed by reconcile.py intake, never typed into the rows.
    assert "parse_reconcile_intake.py" in default and "never hand-edit the rows" in default
    assert "reconcile.py intake" in default
    assert "re-asked, never guessed" in default


def test_the_vision_confirm_step_is_the_intake_card():
    vision = _between("### Vision branch", "### CSV branch")
    assert "intake card" in vision


def test_each_chunk_ends_on_a_progress_card():
    phase_2 = _between("## Phase 2: Generate questions + execute", "### 2a")
    for label in ("**Next five**", "**Run the rest**", "**Pause here**"):
        assert label in phase_2, label
    assert "Page: `<artifacts_dir>/local/reconcile/<ts>/report.html`" not in phase_2


def test_the_results_are_a_table_then_mismatch_cards_four_rows_each():
    default, _ = _default_and_full(_results())
    assert "| Row | Question | Result | What to do |" in default
    assert "at most 15 rows" in default
    assert "up to 4 rows per card" in default
    for label, decision in (("Change the definition", "change"), ("My query is wrong", "fix"),
                            ("Reword the question", "reword"), ("Add as an example", "example"),
                            ("Nothing", "nothing")):
        assert f"| {label} | `{decision}` |" in default, label
    assert "**Suggested for all remaining**" in default
    assert "(Recommended)" in default


def test_the_results_default_opens_no_page_and_waits_for_no_paste():
    default, full = _default_and_full(_results())
    assert not WAITS_FOR_PASTE.search(default)
    assert "end the turn" not in default.lower()
    assert "serve_page.py" in full and "report.html" in full
    assert "parse_reconcile_report.py" in full


def test_card_answers_are_applied_by_the_report_parser():
    default, _ = _default_and_full(_results())
    assert "parse_reconcile_report.py" in default
    assert "re-asked, never guessed" in default
    # A stop typed into Other stops the cards; it is not a decision on that row.
    assert "asks to stop" in default


def test_the_keep_offer_is_one_card_for_the_batch():
    keep = _keep()
    for label in ("**Save all", "**Leave some out**", "**Don't save**"):
        assert label in keep, label
    assert "Never per row." in keep
    assert "AskUserQuestion" in keep


def test_no_page_is_opened_as_a_file():
    assert "file://" not in SKILL and "open_in_browser" not in SKILL


def test_every_card_header_fits_twelve_characters():
    headers = re.findall(r"header `([^`]+)`", SKILL)
    assert len(headers) >= 4
    assert [h for h in headers if len(h) > 12] == []
