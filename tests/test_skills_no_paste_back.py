"""Guard: no skill's default path opens a page or waits for a pasted block.

Every agami decision is a question card in the chat (`plugins/agami/shared/question-cards.md`, rule
6). The HTML pages stay only as an opt-in "full view". So in every `SKILL.md`, a line that tells
Claude to open a page or to wait for a pasted block must sit where the text says it is the full view:

- its paragraph names "full view" (a paragraph is a run of non-blank lines; a top-level list item
  starts a new one, so one bullet that names the full view does not cover its neighbours), or
- its section names "full view": the heading above it (any level, or a parent heading), or the
  bold-led paragraph (`**Show the full view …**`) that opened the current run of paragraphs. A new
  heading or a new bold-led paragraph ends that run.

What counts as such a line (`_PAGE_OR_PASTE`): `open_in_browser`, "auto-open", a `file://`, the
imperatives "Open it" / "Open the page" (case-sensitive, so "reopen it" or "do not open it with
the Read tool" are not page instructions), "paste … back", and the page buttons that produce a
block to paste: "Generate feedback for Claude", "Generate for Claude", "Generate decisions".
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "plugins" / "agami" / "skills"

_PAGE_OR_PASTE = re.compile(
    r"open_in_browser"
    r"|(?i:auto-open)"
    r"|file://"
    r"|\bOpen it\b"
    r"|\bOpen the page\b"
    r"|(?i:\bpast(?:e|es|ed|ing)\b[^.\n]{0,80}?\bback\b)"
    r"|(?i:generate feedback for claude|generate for claude|generate decisions)"
)
_FULL_VIEW = re.compile(r"full view", re.I)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_ITEM = re.compile(r"^(?:[-*+]|\d+\.)\s")

# (skill, a substring of the line) pairs that are allowed outside a full view, each with its reason.
_ALLOWED = {
    # The one-time GitHub-star ask opens github.com only after the person picks "Yes, open GitHub now"
    # on a card. It is a link the person asked to follow, not an agami page or a paste-back.
    ("agami-query", "github.com/AgamiAI/agami-core"),
}


def _blocks(lines: list[str]) -> list[tuple[int, int]]:
    """(start, end) line spans of paragraphs: non-blank runs, split at headings and top-level list items."""
    spans, start = [], None
    for i, line in enumerate(lines):
        new_block = not line.strip() or _HEADING.match(line) or _LIST_ITEM.match(line)
        if new_block and start is not None:
            spans.append((start, i))
            start = None
        if line.strip() and start is None:
            start = i
        if _HEADING.match(line):
            spans.append((i, i + 1))
            start = None
    if start is not None:
        spans.append((start, len(lines)))
    return spans


def violations_in(text: str) -> list[tuple[int, str]]:
    """(1-based line number, line) for every page-or-paste instruction outside a full view."""
    lines = text.splitlines()
    block_of = {}
    for start, end in _blocks(lines):
        for i in range(start, end):
            block_of[i] = (start, end)

    out = []
    headings: dict[int, str] = {}  # level → text of the heading currently open at that level
    section_lead = ""  # the bold-led paragraph that opened the current run, if any
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            headings = {lv: t for lv, t in headings.items() if lv < level}
            headings[level] = heading.group(2)
            section_lead = ""
        elif not in_fence and line.startswith("**") and (i == 0 or not lines[i - 1].strip()):
            start, end = block_of[i]
            section_lead = " ".join(lines[start:end])
        if not _PAGE_OR_PASTE.search(line):
            continue
        start, end = block_of.get(i, (i, i + 1))
        context = [" ".join(lines[start:end]), section_lead, *headings.values()]
        if not any(_FULL_VIEW.search(c) for c in context):
            out.append((i + 1, line.strip()))
    return out


def find_violations(skills_dir: Path) -> list[str]:
    """`<skill>/SKILL.md:<line>: <text>` for every violation under `skills_dir`, allow-list applied."""
    found = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        skill = skill_md.parent.name
        for number, line in violations_in(skill_md.read_text(encoding="utf-8")):
            if any(skill == s and needle in line for s, needle in _ALLOWED):
                continue
            found.append(f"{skill}/SKILL.md:{number}: {line[:160]}")
    return found


def test_no_skill_opens_a_page_or_waits_for_a_paste_outside_the_full_view():
    found = find_violations(SKILLS_DIR)
    assert not found, (
        "A skill opens a page or waits for a pasted block on its default path. Move it under the "
        "full view (rule 6 of plugins/agami/shared/question-cards.md), or say why not:\n  "
        + "\n  ".join(found)
    )


def test_the_guard_reads_every_skill():
    assert len(list(SKILLS_DIR.glob("*/SKILL.md"))) >= 9


def test_a_bare_page_instruction_is_caught():
    text = "## Phase 2\n\nRender the page. Open it with `open_in_browser`.\n"
    assert [n for n, _ in violations_in(text)] == [3]


def test_a_paste_back_wait_is_caught():
    text = (
        'Say: *"Generate feedback for Claude, then paste the block back here."* and end the turn.\n'
    )
    assert len(violations_in(text)) == 1


def test_a_paragraph_that_names_the_full_view_is_allowed():
    text = "**Show the full view (only when asked).** Serve it; never as a `file://` page.\n"
    assert violations_in(text) == []


def test_the_section_opened_by_a_full_view_lead_covers_its_later_paragraphs():
    text = (
        "**Show the full view (only when asked).** Serve the page from localhost.\n\n"
        "```bash\nserve_page.py page.html\n```\n\n"
        "Its **Generate decisions** button gives a block; when they paste it back, parse it.\n\n"
        "**On re-entry** a pasted block is parsed again; paste it back here.\n"
    )
    assert [n for n, _ in violations_in(text)] == [9]


def test_a_full_view_heading_covers_its_subsections():
    text = "## 4e: Show as a chart (the full view)\n\n### 4e.vi: serve it\n\nNever as a `file://` page.\n"
    assert violations_in(text) == []
    text_next = text + "\n## 5: Log\n\nThen open_in_browser the log.\n"
    assert [n for n, _ in violations_in(text_next)] == [9]


def test_one_bullet_naming_the_full_view_does_not_cover_its_neighbour():
    text = "- **Show the full view**: the page.\n- Open the page and wait.\n"
    assert [n for n, _ in violations_in(text)] == [2]


def test_reopening_an_app_or_a_workbook_is_not_a_page_instruction():
    text = "Fully quit the app, then reopen it. Do not open it with the Read tool.\n"
    assert violations_in(text) == []
