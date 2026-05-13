"""Soft coverage check: every helper script invoked in a SKILL.md bash fence
must have at least one allowlist entry mentioning its basename in
``.claude/settings.json#permissions.allow``.

Strict path matching is intentionally avoided — the allowlist uses the
installed path (``~/.claude/plugins/litebi/agami/scripts/X.py``) while
SKILL.md fences use the runtime form (``$AGAMI_PLUGIN_ROOT/scripts/X.py``)
or absolute (``plugins/agami/scripts/X.py``). Basename coverage catches
the real failure mode (renamed script, forgot to update the allowlist)
without false positives on path-form differences.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "plugins" / "agami" / "skills"
SETTINGS = REPO_ROOT / ".claude" / "settings.json"

_BASH_FENCE = re.compile(r"```bash\n(.*?)\n```", re.DOTALL)
_HELPER_INVOKE = re.compile(r"scripts/([A-Za-z0-9_\-]+\.py)")


def _allowlist_helper_basenames() -> set[str]:
    cfg = json.loads(SETTINGS.read_text())
    patterns = cfg.get("permissions", {}).get("allow", [])
    names: set[str] = set()
    for p in patterns:
        for m in re.finditer(r"scripts/([A-Za-z0-9_\-]+\.py)", p):
            names.add(m.group(1))
    return names


def _all_skills() -> list[Path]:
    return sorted(p for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())


def _invocations_in(skill_dir: Path) -> set[str]:
    body = (skill_dir / "SKILL.md").read_text()
    invoked: set[str] = set()
    for fence in _BASH_FENCE.finditer(body):
        for m in _HELPER_INVOKE.finditer(fence.group(1)):
            invoked.add(m.group(1))
    return invoked


@pytest.mark.parametrize("skill_dir", _all_skills(), ids=lambda p: p.name)
def test_helper_invocations_are_allowlisted(skill_dir: Path) -> None:
    allowlisted = _allowlist_helper_basenames()
    missing = sorted(_invocations_in(skill_dir) - allowlisted)
    assert not missing, (
        f"{skill_dir.name}: bash fences invoke helper scripts not in "
        f".claude/settings.json#permissions.allow (basename match): {missing}\n"
        "Add Bash(python3 ~/.claude/plugins/litebi/agami/scripts/<name>.py) "
        "entries to fix."
    )
