"""No agami skill pre-approves a command through `allowed-tools`; the plugin's hook is the only
auto-approval.

agami-connect's list (one rule per `sm` verb and plugin script) kept leaking: a pre-approved script
printed any file it was pointed at, verbs wrote under any `--artifacts`, `set-config --python3` chose
what later calls run, and `approve-queue`/`curate` signed off under any name, all with no prompt,
steered by rows and documents nobody vetted. So for 0.9.25 the list is gone: the PreToolUse hook
(`scripts/approve_own_calls.py`, tests/test_approve_own_calls.py) approves only read-only verbs and
agami's scratch files, and every other command asks. Pre-approval may return with per-argument
checks. The commands that must always ask are checked against the hook here too.
"""

from __future__ import annotations

import pytest
import yaml

from skill_docs import PLUGIN, SKILLS_DIR
from test_agami_command_forms import _hook_allows

SKILLS = sorted(SKILLS_DIR.glob("*/SKILL.md"))
SM = f'bash "{PLUGIN}/scripts/sm"'
PY = f'bash "{PLUGIN}/scripts/py"'


def _frontmatter(path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    return yaml.safe_load(text.split("\n---\n", 1)[0].removeprefix("---\n")) or {}


def test_there_are_skills_to_check():
    assert any(p.parent.name == "agami-connect" for p in SKILLS)


@pytest.mark.parametrize("skill", SKILLS, ids=[p.parent.name for p in SKILLS])
def test_no_skill_declares_allowed_tools(skill):
    head = _frontmatter(skill)
    assert "allowed-tools" not in head and "allowed_tools" not in head


@pytest.fixture
def art(tmp_path):
    root = tmp_path / "artifacts"
    (root / "local" / "tmp").mkdir(parents=True)
    (root / "acme").mkdir()
    return root


def _hostile(art) -> list[str]:
    a = str(art)
    return [
        f"{SM} install",
        f"{PY} -c 'import os; os.system(\"id\")'",
        f"{PY} -m pip install requests",
        f"{PY} -m execute_sql --sql 'DELETE FROM t' --no-safety",
        f"AGAMI_SCRATCH_ONLY=1 {PY} inventory_summary.py --check-columns {a}/../.aws/credentials",
        f"AGAMI_SQL_MAX_ROWS=200000 AGAMI_SCRATCH_ONLY=1 {SM} introspect --profile ../x --db-type "
        f"sqlite --artifacts {a}",
        f"{SM} set-config {a} --python3 /tmp/planted/python3",
        f"{SM} approve-queue {a}/acme --signer you@example.com",
        f"AGAMI_SCRATCH_ONLY=1 {SM} curate {a}/acme --ops-file {a}/local/tmp/agami-x.json",
        f"BASH_ENV={a}/local/tmp/agami-x.txt {SM} steps {a}/acme",
        f"{SM} describe-file {a}/acme --file /Users/you/.netrc",
    ]


def test_the_hook_approves_none_of_the_commands_that_leaked(art):
    approved = [c for c in _hostile(art) if _hook_allows(c, art)]
    assert approved == []


def test_the_hook_still_approves_the_read_only_step_list(art):
    assert _hook_allows(f"{SM} steps {art}/acme", art)
