"""Guardrail check: the agami-query skill must route model-browsing to the deterministic surfaces
(`sm model-tree` / `/agami-model`) rather than hand-rolled `python -c` dumps. This one behavior has NO
code-level equivalent (it's purely a skill instruction), so a decidable "the skill says X" check is the
only way to keep a future edit from silently dropping it. (The other run-through determinism fixes are
locked in code — e.g. the first-time-bootstrap profile-null narration by `test_connect_resolve.py` — so
they don't need brittle prose asserts here.)
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
QUERY = (REPO / "plugins" / "agami" / "skills" / "agami-query" / "SKILL.md").read_text(encoding="utf-8")


def test_query_forbids_hand_rolled_model_dumps():
    assert "python -c" in QUERY          # the anti-pattern is named
    assert "sm model-tree" in QUERY      # …and the deterministic surface it routes to
    assert "show me the model" in QUERY  # …for the user phrasing that triggers it
