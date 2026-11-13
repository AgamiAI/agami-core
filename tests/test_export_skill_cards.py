"""agami-export asks on cards, confirms the exact command, and summarises the export in the chat.

The skill drives `sm export`, whose flags that change a catalog or show content to others are off
unless passed. Which of them the person picked lives only in the skill's cards, so the checks here
read the skill's text, and the flag list is cross-checked against the export module's own registry
so a flag added to a target cannot ship without a card that offers it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "agami"
SKILL = PLUGIN / "skills" / "agami-export" / "SKILL.md"
SHARED = PLUGIN / "shared"
sys.path.insert(0, str(REPO_ROOT / "packages" / "agami-core" / "src"))

# The flags `docs/context-export.md` lists under "Flags that change your catalog or make content
# visible", less the two core ones the skill passes only on request.
CHANGES_OR_SHOWS = (
    "--snowflake-certification-tags",
    "--snowflake-mcp-server",
    "--genie-hide-sensitive",
    "--pages-publish",
    "--uc-comments",
    "--uc-constraints",
    "--uc-certify",
)
ON_REQUEST = ("--include-unreviewed", "--include-sensitive", "--area", "--ossie-version")


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _frontmatter() -> str:
    return _text().split("---")[1]


def _between(start: str, end: str) -> str:
    text = _text()
    i = text.index(start)
    return text[i : text.index(end, i + len(start))]


def _ask() -> str:
    return _between("## Phase 1:", "## Phase 2:")


def _run() -> str:
    return _between("## Phase 2:", "## Phase 3:")


def _summary() -> str:
    return _between("## Phase 3:", "## Hard rules")


def _option_rows(section: str) -> list[str]:
    """The option-table rows of a section: a bold label in the first cell."""
    return [line for line in section.splitlines() if line.startswith("| **")]


def test_skill_links_the_card_conventions_and_has_the_four_frontmatter_keys():
    assert "shared/question-cards.md" in _text()
    assert _text().startswith("---\n")
    fm = _frontmatter()
    assert "name: agami-export" in fm
    assert "description:" in fm and "when_to_use:" in fm
    assert 'argument-hint: "[snowflake | databricks | ossie]"' in fm


def test_the_routing_triggers_are_the_skills_own():
    conventions = (SHARED / "invocation-conventions.md").read_text(encoding="utf-8")
    row = next(line for line in conventions.splitlines() if line.startswith("| agami-export |"))
    triggers = re.findall(r'"([^"]+)"', row)
    assert triggers
    when = _frontmatter().split("when_to_use:")[1]
    for t in triggers:
        assert t in when, t
    assert "ships ten skills" in conventions


def test_the_skill_is_registered_where_its_neighbours_are():
    plan_mode = (SHARED / "plan-mode-check.md").read_text(encoding="utf-8")
    assert "### `agami-export`" in plan_mode
    refusal = "I can't export in plan mode"
    assert refusal in plan_mode and refusal in _text()
    assert "DO NOT** write a plan file or call `ExitPlanMode`" in _text()
    layout = (SHARED / "file-layout.md").read_text(encoding="utf-8")
    assert "`exports/context/<profile>/`" in layout
    assert "`/agami-export`" in (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "/agami-export" in (REPO_ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    assert "`/agami-export`" in (REPO_ROOT / "docs" / "context-export.md").read_text(encoding="utf-8")


def test_agami_model_only_points_here():
    model = (PLUGIN / "skills" / "agami-model" / "SKILL.md").read_text(encoding="utf-8")
    hits = [line for line in model.splitlines() if re.search(r"export", line, re.I)]
    assert len(hits) == 1 and "'export my model'" in hits[0], hits
    assert "sm\" export" not in model


# The skill's cards as the export's spec decides them: each card's questions, each question's
# options in order, and the flag each follow-up option passes. Parsed from the skill, so removing a
# card, a question, an option or a flag from it fails here.
CARDS = {
    "### 1a.": ["Targets", "Location", "Warehouse", "Opt-in"],
    "### 1b.": ["Snowflake", "Catalog", "Sharing"],
    "## Phase 2:": ["Run it?"],
}
OPTIONS = {
    "Targets": ["Snowflake", "Databricks", "Ossie"],
    "Location": ["Not needed", "Not sure yet"],
    "Warehouse": ["None", "Not sure yet"],
    "Opt-in": ["None of the extras", "Snowflake extras", "Databricks catalog changes",
               "Databricks sharing"],
    "Snowflake": ["Mark approved items certified", "Add an MCP server",
                  "Leave expected answers out", "None of these"],
    "Catalog": ["Replace comments", "Add keys", "Tag approved items certified", "None of these"],
    "Sharing": ["Publish the pages", "Hide sensitive columns in the space", "Add a page per metric",
                "None of these"],
    "Run it?": ["Write the files", "Change an answer", "Don't export"],
}
# Follow-up option -> the one flag it passes; the Opt-in group that leads to that follow-up.
FLAG_OF = {
    ("Snowflake", "Mark approved items certified"): "--snowflake-certification-tags",
    ("Snowflake", "Add an MCP server"): "--snowflake-mcp-server",
    ("Snowflake", "Leave expected answers out"): "--snowflake-no-expected-answers",
    ("Catalog", "Replace comments"): "--uc-comments",
    ("Catalog", "Add keys"): "--uc-constraints",
    ("Catalog", "Tag approved items certified"): "--uc-certify",
    ("Sharing", "Publish the pages"): "--pages-publish",
    ("Sharing", "Hide sensitive columns in the space"): "--genie-hide-sensitive",
    ("Sharing", "Add a page per metric"): "--pages-metrics",
}
GROUP_OF = {"Snowflake extras": "Snowflake", "Databricks catalog changes": "Catalog",
            "Databricks sharing": "Sharing"}
# The flag a typed answer passes, and the flags passed only on request.
TYPED = {"Warehouse": "--snowflake-warehouse", "Location": "--location"}
ON_REQUEST_TARGET_FLAGS = ("--ossie-version", "--snowflake-data-database")
HARD_RULES = [
    "Never apply the files.",
    "Never call a vendor or open a network connection.",
    "Never pass an extra the person did not pick on a card,",
    "Never guess a location or a warehouse.",
    "Never print a sensitive column's name.",
    "Pass an `export_refused` message on verbatim,",
    "Never translate SQL or suggest a binding to make a metric travel.",
    "Point to `/agami-model` only where this skill names it:",
]
_FLAG = re.compile(r"`(--[a-z-]+)")


def _question_blocks() -> dict[str, str]:
    """header -> the skill's text of that question, up to the next question or section."""
    text = _text()
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"header `([^`]+)`", text)]
    out = {}
    for i, (at, header) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        section = re.search(r"\n#{2,3} |\n\*\*None of these\*\* picked|\n\*\*Only on request"
                            r"|\nAfter the card|\n```", text[at:end])
        assert header not in out, f"two questions have the header {header}"
        out[header] = text[at:end if section is None else at + section.start()]
    return out


def _options(block: str) -> list[tuple[str, str]]:
    """(label, preview) of each option: a table row's bold label and its third cell, or a
    bullet's bold label and its text."""
    rows = [(m.group(1), m.group(2)) for m in re.finditer(
        r"^\| \*\*(.+?)\*\* \|[^|]*\|(.*)\|$", block, re.M)]
    bullets = [(m.group(1), m.group(2)) for m in re.finditer(
        r"^\s*- \*\*(.+?)\*\*:(.*)$", block, re.M)]
    assert not (rows and bullets), "a question mixes a table and bullets"
    return rows or bullets


def test_every_card_holds_exactly_its_questions_within_the_card_limits():
    blocks = _question_blocks()
    text = _text()
    for start, headers in CARDS.items():
        i = text.index(start)
        nxt = min(j for j in (text.find("\n## ", i + 1), text.find("\n### ", i + 1)) if j > 0)
        found = re.findall(r"header `([^`]+)`", text[i:nxt])
        assert found == headers, (start, found)
        assert 1 <= len(found) <= 4, start          # AskUserQuestion: 1 to 4 questions a card
    assert set(blocks) == {h for hs in CARDS.values() for h in hs}
    for header in blocks:
        assert len(header) <= 12, header            # a chip holds at most 12 characters


def test_every_question_offers_exactly_its_options_within_the_limits():
    for header, block in _question_blocks().items():
        labels = [label for label, _ in _options(block)]
        assert labels == OPTIONS[header], header
        assert 2 <= len(labels) <= 4, header        # 2 to 4 options a question
        assert len(set(labels)) == len(labels), header


def test_each_extra_passes_exactly_its_flag_and_each_group_leads_to_its_follow_up():
    blocks = _question_blocks()
    for header in ("Snowflake", "Catalog", "Sharing"):
        for label, preview in _options(blocks[header]):
            flags = _FLAG.findall(preview)
            if label == "None of these":
                assert flags == [], header
            else:
                assert flags == [FLAG_OF[(header, label)]], (header, label, flags)
    for label, preview in _options(blocks["Opt-in"]):
        flags = set(_FLAG.findall(preview))
        if label == "None of the extras":
            assert flags == set()
            continue
        follow = GROUP_OF[label]
        assert flags == {f for (h, _), f in FLAG_OF.items() if h == follow}, label
    for header, flag in TYPED.items():
        assert f"`{flag}`" in blocks[header], header


def test_none_of_the_extras_says_expected_answers_stay_on():
    (label, preview), *_ = _options(_question_blocks()["Opt-in"])
    row = next(line for line in _ask().splitlines() if line.startswith(f"| **{label}** |"))
    assert "20 result rows" in row and "stay on" in preview
    assert "Leave expected answers out** turns it off" in _ask()


def test_the_hide_sensitive_option_says_what_it_writes_and_that_it_is_no_access_control():
    _, preview = next(o for o in _options(_question_blocks()["Sharing"])
                      if o[0] == "Hide sensitive columns in the space")
    assert "names into the Genie space file" in preview and "not access control" in preview
    assert "column mask" in preview
    row = next(line for line in _ask().splitlines() if "**Hide sensitive columns" in line)
    assert "exclude: true" in row


def test_every_target_flag_is_on_a_card_or_named_as_on_request():
    export = pytest.importorskip("semantic_model.export")
    flags = {flag for _target, flag, _default in export.flag_owners().values()}
    on_cards = set(FLAG_OF.values()) | {TYPED["Warehouse"]}
    assert flags == on_cards | set(ON_REQUEST_TARGET_FLAGS), flags ^ (on_cards | set(ON_REQUEST_TARGET_FLAGS))
    assert set(CHANGES_OR_SHOWS) <= on_cards
    on_request = _ask()[_ask().index("**Only on request.**"):]
    for flag in (*ON_REQUEST, *ON_REQUEST_TARGET_FLAGS):
        assert f"`{flag}" in on_request, flag


def test_the_hard_rules_are_exactly_these_and_consistent_with_the_pointers():
    rules = _between("## Hard rules", "## Error handling cheat sheet")
    leads = re.findall(r"^\d+\. \*\*(.+?)\*\*", rules, re.M)
    assert leads == HARD_RULES
    assert re.findall(r"^(\d+)\. ", rules, re.M) == [str(i) for i in range(1, len(HARD_RULES) + 1)]
    # Rule 8 names every place the skill sends the person to /agami-model, so no other line
    # contradicts it: the summary's binding and validation lines and the refusals that need a
    # model change.
    rule8 = rules.split("8. ", 1)[1]
    for place in ("no SQL for a target's engine", "validation errors", "does not load",
                  "named alike", "name too long"):
        assert place in rule8, place
    pointers = [line for line in _text().splitlines() if "/agami-model" in line]
    assert len(pointers) >= 5


def test_the_confirm_card_comes_before_the_command_and_names_the_output():
    run = _run()
    card = run.index("header `Run it?`")
    command = run.index('"${CLAUDE_PLUGIN_ROOT}/scripts/sm" export "$ROOT"')
    assert card < command
    assert "the exact command" in run[card:command]
    assert '--out "<artifacts_dir>/local/exports/context/<profile>"' in run
    assert "export_refused" in run and "verbatim" in run


def test_the_summary_is_grouped_capped_and_names_what_does_not_travel():
    s = _summary()
    assert "at most 15 rows" in s
    assert "grouped by reason" in s.lower() or "group `targets.<target>.losses` by `reason`" in s
    assert "Never one row per item" in s
    assert "`no_binding_for_target`" in s and "did not travel" in s
    assert "does_not_travel" in s
    assert "export-report.json" in s
    assert "docs/context-export.md" in s and "Applying the files" in s
    reasons = pytest.importorskip("semantic_model.export").REASONS
    for reason in reasons:
        assert f"| `{reason}` |" in s, f"the summary has no words for {reason}"


def test_sensitive_columns_are_counted_never_named():
    text = _text()
    assert "never print its `columns`" in text
    assert "Never print a name or an export id of a sensitive item" in text
    assert "written into the Genie space file" in _summary()


def test_hard_rules_cover_applying_vendors_guessing_and_sql():
    rules = _between("## Hard rules", "## Error handling cheat sheet")
    for phrase in (
        "Never apply the files",
        "Never call a vendor",
        "Never guess a location or a warehouse",
        "Never translate SQL",
        "verbatim",
    ):
        assert phrase in rules, phrase


def test_every_refusal_the_command_raises_has_a_row():
    cheat = _text()[_text().index("## Error handling cheat sheet") :]
    src = REPO_ROOT / "packages" / "agami-core" / "src" / "semantic_model"
    raised = []
    for path in sorted(src.glob("export*.py")):
        raised += re.findall(r'raise ExportRefused\(\s*f?"([^"{]{6,})', path.read_text(encoding="utf-8"))
    assert len(raised) >= 10
    for start in raised:
        # The table keys each row on the message's first words; a message that opens with a value
        # (`{flag} belongs to …`) is matched by hand in its row.
        words = " ".join(w.rstrip(",;:") for w in start.split()[:3])
        assert words in cheat, start


_PAGE_OR_PASTE = re.compile(
    r"open_in_browser|serve_page|render_\w+\.py|file://|auto-open|\bOpen it\b|\bOpen the page\b"
    r"|(?i:\bpast(?:e|es|ed|ing)\b[^.\n]{0,80}?\bback\b)"
)


def test_no_page_is_opened_or_served_and_nothing_is_pasted_back():
    assert not _PAGE_OR_PASTE.search(_text())


def test_commands_use_the_braced_plugin_root():
    text = _text()
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/sm" in text
    assert not re.search(r"\$\{?AGAMI_PLUGIN_ROOT\}?|\$CLAUDE_PLUGIN_ROOT\b", text)
