"""Skills name the plugin root with `${CLAUDE_PLUGIN_ROOT}`, which Claude Code fills in on load.

Nothing sets `AGAMI_PLUGIN_ROOT` in a Claude Code session's Bash environment, so a skill command
written against it broke on its first line. Claude Code substitutes `${CLAUDE_PLUGIN_ROOT}` in a
SKILL.md with the plugin's absolute path when the skill loads; files Claude reads later (the shared
docs) get no substitution, so those use the `<plugin_root>` placeholder instead.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "agami"
SKILLS = sorted(PLUGIN.glob("skills/*/SKILL.md"))
SHARED = sorted(PLUGIN.glob("shared/*.md"))

_UNSET_VAR = re.compile(r"\$\{?AGAMI_PLUGIN_ROOT\}?")
_ROOT_PATH = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./-]+)")


def test_there_are_skills_to_check():
    assert len(SKILLS) >= 9


def test_no_skill_uses_the_unset_agami_plugin_root():
    found = [
        f"{s.relative_to(REPO_ROOT)}:{n}: {line.strip()[:100]}"
        for s in SKILLS
        for n, line in enumerate(s.read_text(encoding="utf-8").splitlines(), 1)
        if _UNSET_VAR.search(line)
    ]
    assert not found, "Use ${CLAUDE_PLUGIN_ROOT}; AGAMI_PLUGIN_ROOT is never set:\n" + "\n".join(found)


def test_every_plugin_root_path_in_a_skill_exists():
    missing = []
    for s in SKILLS:
        for rel in _ROOT_PATH.findall(s.read_text(encoding="utf-8")):
            rel = rel.rstrip(".")  # a path at the end of a sentence
            if not (PLUGIN / rel).exists():
                missing.append(f"{s.relative_to(REPO_ROOT)}: ${{CLAUDE_PLUGIN_ROOT}}/{rel}")
    assert not missing, "Paths under the plugin root that do not exist:\n" + "\n".join(missing)


def test_shared_docs_use_the_placeholder_not_a_variable():
    # Claude Code does not substitute anything in a file Claude reads, so a variable there would be
    # pasted into Bash literally.
    # invocation-conventions.md is exempt: it is where the two names are explained.
    found = [
        s.name
        for s in SHARED
        if s.name != "invocation-conventions.md"
        and (_UNSET_VAR.search(s.read_text(encoding="utf-8")) or "${CLAUDE_PLUGIN_ROOT}" in s.read_text(encoding="utf-8"))
    ]
    assert not found, f"Shared docs must say <plugin_root>: {found}"


def test_the_placeholder_is_defined_once_in_the_conventions_doc():
    text = (PLUGIN / "shared" / "invocation-conventions.md").read_text(encoding="utf-8")
    assert "`<plugin_root>`" in text.split("## What works")[0]
