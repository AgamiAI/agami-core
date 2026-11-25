"""Flagged and suspected personal-data columns are counted and said apart, and nothing claims a flag
hides values.

A flagged column carries a stored decision (`sensitive: true`). A suspected one is only a guess from
its name or a sampled value, recomputed on every read. The overview line used to add the two into
one "<n> columns look like personal data", which claimed more than anyone knew. And since the
sensitive-projection gate went (ACE-094), a flag guides answers toward counts and filters but does
not stop a query returning the values, so "a raw value is never shown" was untrue too.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "plugins" / "agami" / "skills"
SHARED = REPO / "plugins" / "agami" / "shared"
MODEL_SKILL = SKILLS / "agami-model" / "SKILL.md"
SAMPLE = REPO / "plugins" / "agami" / "samples" / "store" / "model"


def _docs() -> dict[str, str]:
    files = [*SKILLS.glob("*/SKILL.md"), *SHARED.glob("*.md"), *SHARED.glob("*.html")]
    return {str(p.relative_to(REPO)): p.read_text(encoding="utf-8") for p in files}


def _overview_step() -> str:
    text = MODEL_SKILL.read_text(encoding="utf-8")
    i = text.index("3. When `count` or `suspected_count` > 0")
    return text[i : text.index("\n4. ", i)]


def test_the_overview_says_flagged_and_suspected_apart():
    step = _overview_step()
    assert "each count said apart and a clause left out when its count is 0" in step
    flagged = '*"<count> columns are flagged as personal data and are still queryable'
    suspected = (
        '*"<suspected_count> more have names or sampled values that suggest personal data '
        "but aren't flagged"
    )
    assert flagged in step and suspected in step
    # each clause is gated on its own count, never on their sum
    assert "- `count` > 0:" in step and "- `suspected_count` > 0:" in step
    assert "PII tab" in step.split("- `suspected_count` > 0:")[1]


def test_the_overview_never_adds_the_two_counts():
    text = MODEL_SKILL.read_text(encoding="utf-8")
    assert "Never add the two." in text


def test_the_combined_claim_is_gone_from_skills_and_shared_docs():
    for name, text in _docs().items():
        assert "look like personal data" not in text.lower(), name


def test_nothing_claims_a_flag_hides_values():
    # ACE-094 removed the gate; only hiding a column keeps its values out of answers.
    for claim in (
        "raw value is never shown",
        "raw values never shown",
        "never shows the raw values",
        "never shows a sensitive column",
        "agami hides the raw values",
        "values never returned row by row",
    ):
        for name, text in _docs().items():
            assert claim not in text, (claim, name)
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "enforced at runtime" not in readme


def test_sm_sensitive_gives_the_two_counts_separately(tmp_path, capsys):
    from semantic_model import cli

    root = tmp_path / "store"
    shutil.copytree(SAMPLE, root)
    assert cli.main(["sensitive", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    flagged = {(c["table"], c["column"]) for c in out["columns"]}
    suspected = {(c["table"], c["column"]) for c in out["suspected"]}
    assert out["count"] == len(flagged) == 2
    assert out["suspected_count"] == len(suspected) == 1
    # a column is in one tier or the other, so the skill can say both without counting one twice
    assert not flagged & suspected
    assert suspected == {("customers", "full_name")}
