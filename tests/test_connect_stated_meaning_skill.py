"""agami-connect keeps a value's stated meaning even when the data looks implausible (#481).

Like `test_ah109_save_golden_skill.py`, this asserts on Markdown because the behavior has no
code-level equivalent: no helper can stop the model deciding that a '%' label is wrong because
every value is below 1, or swapping a documented column for a neighbour whose values look more
plausible. The skill is the only place the rule lives, so "the skill says X" is the decidable check.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL = (REPO_ROOT / "plugins" / "agami" / "skills" / "agami-connect" / "SKILL.md").read_text(
    encoding="utf-8")
ENRICH = SKILL.split("### 2d — Caveats")[1].split("### 2e")[0]


def test_enrichment_binds_a_value_as_its_label_and_unit_say():
    assert "Bind a value as its label, unit and description say" in ENRICH
    assert "never overrule them because the data looks implausible" in ENRICH


def test_both_ways_of_overruling_a_stated_meaning_are_named():
    # reinterpreting the unit from the value range, and switching to a look-alike column
    assert "means 0.73 percent (0.0073 as a ratio) even when every value is below 1" in ENRICH
    assert "no switching to a neighbouring column with a similar name" in ENRICH
    assert "no rescaling" in ENRICH


def test_the_doubt_goes_to_a_caveat_and_the_user_not_into_the_binding():
    assert "write what you saw as a caveat" in ENRICH
    assert "ask the user in plain words which reading is right" in ENRICH
    assert "Change the binding only on their answer" in ENRICH


def test_the_data_quality_section_points_to_the_same_rule():
    dq = SKILL.split('do NOT clip to a "textbook" range')[1][:1200]
    assert "never a reason to reinterpret the column (2d)" in dq
