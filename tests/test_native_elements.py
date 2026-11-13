"""One written rule for which native element each agami skill uses, with a fallback for each
(`shared/native-elements.md`), and every skill that picks an element links it."""

from __future__ import annotations

import re

from skill_docs import PLUGIN, SKILLS_DIR, label, skill_docs, skill_of

SHARED = PLUGIN / "shared"
LADDER = SHARED / "native-elements.md"
NEEDS = (
    "A decision",
    "Progress through onboarding",
    "A wait over 10 seconds",
    "A result of up to 15 rows",
    "A summary of 4 to 5 numbers",
    "Anything longer",
)
# Already over the 500-line guideline before this rule (their split is separate work): capped at
# their length then, so they can only shrink. Every other SKILL.md stays under 500 lines.
ALREADY_LONG = {"agami-query": 1019, "agami-reconcile": 779}
TRIGGERS = ('sm" overview', "serve_page.py", "run_in_background", "overview-card.md",
            "Show the full view")


def _ladder() -> str:
    return LADDER.read_text(encoding="utf-8")


def test_ladder_wording_agrees_with_the_phase_files():
    text = _ladder()
    assert "every 30 seconds" not in text
    assert "as the phase file's wait loop says" in text
    assert "say what is running before it starts and the result when it ends" in text
    assert "(overview, review groups)" in text
    assert "Each tile: at most 6 numbers" in text
    assert "question-cards.md#6-" in text


def test_ladder_table_has_every_need_and_its_links_resolve():
    text = _ladder()
    rows = [line for line in text.splitlines() if line.startswith("| ")]
    for need in NEEDS:
        assert any(row.startswith(f"| {need}") for row in rows), need
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", text):
        assert (SHARED / target).exists(), target
    assert len(text.splitlines()) < 70


def test_show_widget_is_named_only_in_the_ladder_and_the_overview_card():
    allowed = {LADDER, SHARED / "overview-card.md"}
    hits = [p for p in PLUGIN.rglob("*") if p.is_file() and p not in allowed
            and "show_widget" in p.read_text(encoding="utf-8", errors="ignore")]
    assert hits == []


def test_every_skill_that_picks_an_element_links_the_ladder():
    """A skill's SKILL.md links the ladder, or else every one of its files that picks an element
    does: a link in one phase file must not stand in for another phase file's."""
    missing = []
    for doc in skill_docs():
        text = doc.read_text(encoding="utf-8")
        if not any(t in text for t in TRIGGERS) or "native-elements.md" in text:
            continue
        main = SKILLS_DIR / skill_of(doc) / "SKILL.md"
        if "native-elements.md" not in main.read_text(encoding="utf-8"):
            missing.append(label(doc))
    assert missing == []


def test_links_to_the_ladder_resolve():
    for doc in skill_docs():
        for target in re.findall(r"\]\(([^)#]*native-elements\.md)", doc.read_text(encoding="utf-8")):
            assert (doc.parent / target).resolve() == LADDER, label(doc)


def test_summary_shows_the_measured_setup_time():
    # the closing summary, "Set up in …" line included, is the `say` of the last step's start mark;
    # live runs ran `sm summary` and `sm timing` and never posted either
    text = (SKILLS_DIR / "agami-connect" / "references" / "summary.md").read_text(encoding="utf-8")
    assert 'sm" steps "<root>" --mark first_question=in_progress' in text
    assert "Set up in 6 min 40 s" in text
    assert "native-elements.md" in text


def test_question_cards_rule_6_links_the_ladder():
    text = (SHARED / "question-cards.md").read_text(encoding="utf-8")
    rule6 = text.split("## 6.", 1)[1].split("## 7.", 1)[0]
    assert "](native-elements.md" in rule6


def test_overview_card_is_the_summary_row():
    text = (SHARED / "overview-card.md").read_text(encoding="utf-8")
    assert "](native-elements.md" in text
    assert "summary of 4 to 5 numbers" in text


def test_skill_files_stay_under_500_lines():
    for skill in SKILLS_DIR.glob("*/SKILL.md"):
        limit = ALREADY_LONG.get(skill.parent.name, 499)
        assert len(skill.read_text(encoding="utf-8").splitlines()) <= limit, label(skill)
