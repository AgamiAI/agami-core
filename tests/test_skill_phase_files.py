"""agami-connect keeps a short SKILL.md and reads each phase from references/ when it starts.

The whole skill was 1,460 lines, all loaded on every run, against a 500-line guideline. Each phase
now lives in its own file. These tests keep that split honest: the main file stays short, every
phase file is reachable from its index, the phase files name the plugin root the way a file Claude
reads must, and no link in the plugin points at a heading that is not there.
"""

from __future__ import annotations

import re
from pathlib import Path

from skill_docs import PLUGIN, SKILLS_DIR, label, skill_docs, skill_text

CONNECT = SKILLS_DIR / "agami-connect"
_ROOT_PATH = re.compile(r"<plugin_root>/([A-Za-z0-9_./-]+)")
_LINK = re.compile(r"\]\(([^)\s#]*\.md)?#([^)\s]+)\)")


def _slug(heading: str) -> str:
    text = re.sub(r"^#+\s*", "", heading.strip()).lower()
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _anchors(doc: Path) -> set[str]:
    text = doc.read_text(encoding="utf-8")
    found = {_slug(line) for line in text.splitlines() if line.startswith("#")}
    return found | set(re.findall(r'<a id="([^"]+)"', text))


def test_the_main_file_is_short_and_indexes_every_phase_file():
    main = (CONNECT / "SKILL.md").read_text(encoding="utf-8")
    assert main.count("\n") < 500
    phase_files = sorted(p.name for p in (CONNECT / "references").glob("*.md"))
    assert len(phase_files) == 8
    for name in phase_files:
        assert f"](references/{name})" in main, name
        assert f"${{CLAUDE_PLUGIN_ROOT}}/skills/agami-connect/references/{name}" in main, name


def test_the_main_file_says_to_read_a_phase_before_starting_it():
    main = (CONNECT / "SKILL.md").read_text(encoding="utf-8")
    assert "**Before starting a\nphase, `Read` its file; never work a phase from memory" in main


def test_phase_files_name_the_plugin_root_as_a_placeholder():
    # Claude Code fills ${CLAUDE_PLUGIN_ROOT} in only in the SKILL.md it loads; a phase file is read
    # later, so a variable there would reach Bash unset.
    for doc in (CONNECT / "references").glob("*.md"):
        text = doc.read_text(encoding="utf-8")
        assert "${CLAUDE_PLUGIN_ROOT}" not in text and "$CLAUDE_PLUGIN_ROOT" not in text, doc.name
        assert "`<plugin_root>` in this file is the plugin's root directory" in text, doc.name


def test_every_plugin_root_path_in_a_phase_file_exists():
    missing = []
    for doc in (CONNECT / "references").glob("*.md"):
        for rel in _ROOT_PATH.findall(doc.read_text(encoding="utf-8")):
            rel = rel.rstrip(".")
            if not (PLUGIN / rel).exists():
                missing.append(f"{doc.name}: <plugin_root>/{rel}")
    assert not missing, missing


def test_no_link_points_at_a_missing_heading():
    docs = [*skill_docs(), *PLUGIN.glob("shared/*.md")]
    dead = []
    for doc in docs:
        for target, anchor in _LINK.findall(doc.read_text(encoding="utf-8")):
            path = (doc.parent / target).resolve() if target else doc
            if not path.is_file():
                dead.append(f"{doc.relative_to(PLUGIN)}: {target}#{anchor} (no such file)")
            elif anchor not in _anchors(path):
                dead.append(f"{doc.relative_to(PLUGIN)}: {target or ''}#{anchor}")
    assert not dead, "\n".join(dead)


def test_the_text_helper_reads_phases_in_order():
    text = skill_text("agami-connect")
    order = ["## Phase 0: Preflight", "## Phase 0a:", "## Phase 1:", "### 1.7 ", "## Phase 2:",
             "## Phase 4:", "## Phase 6:", "## Phase 8.5:", "## Error handling"]
    positions = [text.index(h) for h in order]
    assert positions == sorted(positions)


def test_every_phase_file_is_scanned_by_the_skill_guards():
    assert {label(p) for p in skill_docs()} >= {
        f"agami-connect/references/{p.name}" for p in (CONNECT / "references").glob("*.md")
    }
