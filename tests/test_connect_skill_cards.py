"""agami-connect Phase 1.6 asks for tables on a card, not on a prune page."""

from __future__ import annotations

import re
from pathlib import Path

SKILL = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "agami"
    / "skills"
    / "agami-connect"
    / "SKILL.md"
)


def _section_16() -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("### 1.6 ")
    end = text.index("### 1.6s ")
    return text[start:end]


def test_skill_links_the_card_conventions():
    assert "shared/question-cards.md" in SKILL.read_text(encoding="utf-8")


def test_1_6_uses_the_summary_and_the_four_options():
    s = _section_16()
    assert "inventory_summary.py" in s
    for label in (
        "Keep everything",
        "Drop staging and backups",
        "Only some schemas",
        "Let me name them",
    ):
        assert label in s
    assert "AskUserQuestion" in s
    assert "--tables-file" in s


def test_1_6_default_path_opens_no_page_and_waits_for_no_paste():
    default = _section_16().split("**Show the full view")[0]
    assert not re.search(r"auto-open|end the turn|paste the block", default, re.I)
    assert "AGAMI PRUNE" not in default


def test_1_6_keeps_the_full_view_as_an_option():
    s = _section_16()
    assert "Show the full view" in s
    assert "render_prune.py" in s and "serve_page.py" in s
    assert "parse_prune_block.py" in s


def test_1_6_handles_all_noise_and_the_empty_result():
    s = _section_16()
    assert "no tables kept" in s
    assert "covers every table" in s


def test_1_6_follow_up_card_stays_within_four_options():
    s = _section_16()
    assert "fewer than 4" in s
    assert "single schema" in s.lower()


def test_1_6_lists_the_first_noise_names_before_the_card():
    s = _section_16()
    assert "first 5" in s and "and N more" in s
    assert "schema_count" in s
