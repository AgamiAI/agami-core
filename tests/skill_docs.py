"""The text of a skill, wherever it lives: its SKILL.md and the phase files under references/.

A skill can keep its phases in `skills/<name>/references/*.md`, read by Claude when a phase starts
(agami-connect does). A test that read only SKILL.md would then pass because the text it checks had
moved, not because it was right. So every test that reads a skill's words, or scans every skill,
goes through here.
"""

from __future__ import annotations

from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "agami"
SKILLS_DIR = PLUGIN / "skills"


def skill_docs() -> list[Path]:
    """Every skill's SKILL.md and every phase file, so a guard that scans skills misses none."""
    return sorted([*SKILLS_DIR.glob("*/SKILL.md"), *SKILLS_DIR.glob("*/references/*.md")])


def skill_of(doc: Path) -> str:
    """The skill a doc belongs to: `agami-connect` for its SKILL.md and for its phase files."""
    return doc.parent.parent.name if doc.parent.name == "references" else doc.parent.name


def label(doc: Path) -> str:
    """`agami-connect/SKILL.md` or `agami-connect/references/build.md`, for a failure message."""
    return str(doc.relative_to(SKILLS_DIR))


def skill_text(name: str) -> str:
    """A skill's words in reading order: SKILL.md up to its error-handling section, the phase
    files in the order its index lists them, then the rest of SKILL.md.

    A phase file writes the plugin root as `<plugin_root>` (Claude Code fills `${CLAUDE_PLUGIN_ROOT}`
    in only in the SKILL.md it loads); here it reads as `${CLAUDE_PLUGIN_ROOT}` again, so a test
    can name a command one way whichever file holds it.
    """
    main = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    refs = SKILLS_DIR / name / "references"
    if not refs.is_dir():
        return main
    order = [line.split("](references/", 1)[1].split(")", 1)[0]
             for line in main.splitlines() if line.startswith("|") and "](references/" in line]
    phases = "".join(
        (refs / f).read_text(encoding="utf-8").replace("<plugin_root>", "${CLAUDE_PLUGIN_ROOT}")
        for f in dict.fromkeys(order)
    )
    head, sep, tail = main.partition("## Error handling")
    return head + phases + sep + tail
