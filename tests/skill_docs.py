"""Where the plugin's skills live, for the tests that read their words.

Each skill is one `plugins/agami/skills/<name>/SKILL.md`; a test that checks a skill's text, or scans
every skill, goes through here so it names them one way.
"""

from __future__ import annotations

from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "agami"
SKILLS_DIR = PLUGIN / "skills"


def skill_docs() -> list[Path]:
    """Every skill's SKILL.md."""
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def skill_text(name: str) -> str:
    """A skill's SKILL.md."""
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
