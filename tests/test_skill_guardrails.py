"""Guardrail check: the agami-query skill must route model-browsing to the deterministic surfaces
(`sm model-tree` / `/agami-model`) rather than hand-rolled `python -c` dumps. This one behavior has NO
code-level equivalent (it's purely a skill instruction), so a decidable "the skill says X" check is the
only way to keep a future edit from silently dropping it. (The other run-through determinism fixes are
locked in code — e.g. the first-time-bootstrap profile-null narration by `test_connect_resolve.py` — so
they don't need brittle prose asserts here.)
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
QUERY = (REPO / "plugins" / "agami" / "skills" / "agami-query" / "SKILL.md").read_text(encoding="utf-8")


def test_query_forbids_hand_rolled_model_dumps():
    assert "python -c" in QUERY          # the anti-pattern is named
    assert "sm model-tree" in QUERY      # …and the deterministic surface it routes to
    assert "show me the model" in QUERY  # …for the user phrasing that triggers it


# A projected star in a statement the reader is told to write: `SELECT *` / `SELECT t.*` /
# `SELECT DISTINCT ON (…) *` followed by FROM, a comma or a star modifier. Naming the gate itself
# ("the `SELECT *` gate") is not an instruction and does not match.
_STAR_INSTRUCTION = re.compile(
    r"\bSELECT\s+(?:DISTINCT\s+(?:ON\s*\([^)]*\)\s*)?)?(?:\w+\.)?\*\s*(?:,|FROM\b|EXCEPT\b|REPLACE\b)",
    re.IGNORECASE,
)


def test_no_skill_or_shared_doc_tells_the_reader_to_select_star():
    """execute_sql refuses a projected star, so an instruction to write one only produces refusals
    (#523: agami-connect's sampling step said `SELECT * FROM <t> LIMIT 5`)."""
    docs = [*(REPO / "plugins" / "agami" / "skills").glob("*/SKILL.md"),
            *(REPO / "plugins" / "agami" / "shared").glob("*.md")]
    assert docs
    offenders = [f"{p.relative_to(REPO)}:{n}" for p in docs
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if _STAR_INSTRUCTION.search(line)]
    assert offenders == []


def test_the_star_check_reads_the_shapes_it_is_for():
    for line in ("`SELECT * FROM <t> LIMIT 5`", "SELECT o.* FROM orders o",
                 "SELECT DISTINCT ON (a) * FROM t", "SELECT *, ROW_NUMBER() OVER () AS rn FROM t"):
        assert _STAR_INSTRUCTION.search(line), line
    for line in ("the `SELECT *` gate", "SELECT COUNT(*) FROM t"):
        assert not _STAR_INSTRUCTION.search(line), line


def test_connect_profiles_with_the_build_profile_not_no_safety():
    connect = (REPO / "plugins" / "agami" / "skills" / "agami-connect" / "SKILL.md").read_text(encoding="utf-8")
    assert "--build-profile" in connect
    assert "SELECT c1, c2, … FROM <t> LIMIT 5" in connect


def test_connect_profiles_under_the_resolved_interpreter():
    # A bare `python` can be a different interpreter from Phase 0a.5's `$PY`, one without the package or driver.
    connect = (REPO / "plugins" / "agami" / "skills" / "agami-connect" / "SKILL.md").read_text(encoding="utf-8")
    commands = [c for c in re.findall(r"`([^`\n]*-m execute_sql[^`\n]*)`", connect) if "--build-profile" in c]
    assert commands and all(c.startswith('"$PY" -m execute_sql') for c in commands), commands
