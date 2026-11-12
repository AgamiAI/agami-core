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


def test_headers_are_short_and_unique():
    headers = re.findall(r"header `([^`]+)`", _text())
    assert set(headers) >= {"Targets", "Location", "Warehouse", "Opt-in", "Run it?"}
    assert all(len(h) <= 12 for h in headers), headers
    assert len(headers) == len(set(headers)), headers


def test_the_first_card_has_no_recommendation_on_environment_facts():
    ask = _ask()
    assert "AskUserQuestion" in ask
    for header in ("Targets", "Location", "Warehouse"):
        start = ask.index(f"header `{header}`")
        question = ask[start : ask.index("\n- header", start)]
        assert "(Recommended)" not in question, header
    # A value to type is asked for in the question text (rule 5).
    assert "Type it in Other as database.schema" in ask
    assert "Type its name in Other." in ask
    assert "Never guess a location" in ask


def test_opt_in_starts_with_none_and_every_extra_is_its_own_option():
    ask = _ask()
    opt_in = ask[ask.index("header `Opt-in`") :]
    assert _option_rows(opt_in)[0].startswith("| **None — keep all off** |")
    rows = _option_rows(ask)
    for flag in CHANGES_OR_SHOWS:
        # The flag sits in the preview column of a row whose label is in words.
        offering = [r for r in rows if flag in r.split("|")[3] and "--" not in r.split("|")[1]]
        assert offering, flag
    # Each follow-up question lets the person pick nothing.
    for header in ("Snowflake", "Catalog", "Sharing"):
        section = ask[ask.index(f"header `{header}`") :]
        following = section.find("\nheader `")
        section = section if following < 0 else section[:following]
        assert "| **None of these** |" in section, header


def test_every_target_flag_is_on_a_card_or_named_as_on_request():
    export = pytest.importorskip("semantic_model.export")
    owners = export.flag_owners()
    flags = {flag for _target, flag, _default in owners.values()}
    assert set(CHANGES_OR_SHOWS) <= flags, set(CHANGES_OR_SHOWS) - flags
    ask = _ask()
    for flag in flags:
        assert flag in ask, f"{flag} is a flag `sm export` takes but no card in the skill offers it"
    on_request = ask[ask.index("**Only on request.**") :]
    for flag in ON_REQUEST:
        assert f"`{flag}" in on_request, flag


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
