"""Static lint for plugins/agami/skills/*/SKILL.md.

Parses frontmatter, asserts required keys, and resolves every referenced file
path (markdown links, backtick code-spans for scripts/shared/, runtime
``$AGAMI_PLUGIN_ROOT/scripts/X.py`` invocations, and absolute
``plugins/agami/scripts/X.py`` mentions) back to an on-disk target.

A failure here means a SKILL.md edit has drifted from the files it references
(renamed script, deleted shared doc, broken markdown link, missing frontmatter
key) — the kind of regression that otherwise only surfaces at runtime.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "agami"
SKILLS_DIR = PLUGIN_ROOT / "skills"

REQUIRED_FRONTMATTER_KEYS = {"name", "description", "when_to_use", "argument-hint"}

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_CODESPAN_RE = re.compile(r"`((?:scripts|shared)/[A-Za-z0-9_./\-]+)`")
_RUNTIME_RE = re.compile(r"\$AGAMI_PLUGIN_ROOT/(scripts/[A-Za-z0-9_\-]+\.py)")
_ABSPATH_RE = re.compile(r"plugins/agami/(scripts/[A-Za-z0-9_\-]+\.py|shared/[A-Za-z0-9_./\-]+)")
_FILE_SUFFIXES = (".py", ".md", ".json", ".html", ".yaml", ".yml", ".svg")


def _all_skills() -> list[Path]:
    return sorted(p for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())


def _parse_frontmatter(text: str) -> dict:
    text = text.replace("\r\n", "\n")
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    end = text.find("\n---\n", 4)
    assert end != -1, "SKILL.md frontmatter unterminated"
    return yaml.safe_load(text[4:end])


def _referenced_paths(skill_dir: Path, body: str) -> set[Path]:
    refs: set[Path] = set()

    for m in _LINK_RE.finditer(body):
        href = m.group(1).split("#", 1)[0].strip()
        if not href or href.startswith(("http://", "https://", "mailto:")):
            continue
        if not href.endswith(_FILE_SUFFIXES):
            continue
        refs.add((skill_dir / href).resolve())

    for m in _CODESPAN_RE.finditer(body):
        refs.add((PLUGIN_ROOT / m.group(1)).resolve())

    for m in _RUNTIME_RE.finditer(body):
        refs.add((PLUGIN_ROOT / m.group(1)).resolve())

    for m in _ABSPATH_RE.finditer(body):
        refs.add((PLUGIN_ROOT / m.group(1)).resolve())

    return refs


@pytest.mark.parametrize("skill_dir", _all_skills(), ids=lambda p: p.name)
def test_skill_frontmatter_well_formed(skill_dir: Path) -> None:
    fm = _parse_frontmatter((skill_dir / "SKILL.md").read_text())
    missing = REQUIRED_FRONTMATTER_KEYS - set(fm)
    assert not missing, f"{skill_dir.name}: missing frontmatter keys {missing}"
    assert fm["name"] == skill_dir.name, (
        f"{skill_dir.name}: frontmatter name {fm['name']!r} != dir name"
    )
    for k in ("description", "when_to_use", "argument-hint"):
        assert isinstance(fm[k], str) and fm[k].strip(), (
            f"{skill_dir.name}: {k} empty or not a string"
        )


@pytest.mark.parametrize("skill_dir", _all_skills(), ids=lambda p: p.name)
def test_skill_references_resolve(skill_dir: Path) -> None:
    body = (skill_dir / "SKILL.md").read_text()
    missing = sorted(
        str(p.relative_to(REPO_ROOT))
        for p in _referenced_paths(skill_dir, body)
        if not p.exists()
    )
    assert not missing, (
        f"{skill_dir.name}: SKILL.md references non-existent files:\n  "
        + "\n  ".join(missing)
    )
