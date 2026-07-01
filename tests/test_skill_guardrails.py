"""OCR-034 regression: the skill-prose guardrails must stay in place — they encode determinism the
run-through found missing (#4/#6/#7) and the preflight narration fix (#3). These are decidable
"the skill says X" checks so a future edit can't silently drop them.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONNECT = (REPO / "plugins" / "agami" / "skills" / "agami-connect" / "SKILL.md").read_text(encoding="utf-8")
QUERY = (REPO / "plugins" / "agami" / "skills" / "agami-query" / "SKILL.md").read_text(encoding="utf-8")


def test_sample_6b_opens_model_explorer():
    # #7 — the "watch it build" path must open the model-explorer, not just describe in prose.
    assert "/agami-model" in CONNECT
    assert "OPEN THE MODEL-EXPLORER" in CONNECT


def test_sample_6b_codifies_carve_outs():
    # #6 — the sample skips are spelled out (not left to the model's discretion).
    assert "Sample carve-outs" in CONNECT
    for skip in ("prune", "org-description", "doc/metrics intake"):
        assert skip in CONNECT, skip


def test_sample_path_skip_contradiction_resolved():
    # #6 — the blanket "sample path skips introspect/enrich/seed" (which contradicted 6B) is gone;
    # the skip is now scoped to the 6A copy path.
    assert "sample path skips introspect/enrich/seed" not in CONNECT


def test_bootstrap_narration_has_no_invented_profile():
    # #3 — first-time bootstrap narrates "first-time setup" off profile_source, never a fake "main".
    assert "first-time setup" in CONNECT
    assert "profile_source" in CONNECT


def test_query_forbids_hand_rolled_model_dumps():
    # #4 — no ad-hoc python to walk the model; route "show me the model" to sm model-tree / /agami-model.
    assert "python -c" in QUERY
    assert "sm model-tree" in QUERY
    assert "show me the model" in QUERY
