"""Every agami command agami-connect shows Claude is one literal launcher call.

A live run of `/agami-connect` stopped for permission on most of agami's own commands: Claude wrote
them as it saw them, `R="…"; bash "$P/scripts/sm" steps "$R" | grep …`, and no `allowed-tools`
rule matches a line that starts with an assignment or carries a pipe. Claude copies the skill's
command forms, so the forms themselves must be the one shape that is pre-approved: the launcher,
then its arguments, every path written out. This reads agami-connect's SKILL.md, its phase files,
the shared files it links and the three agent files, and checks each command three ways: no shell
syntax, matched by one of the skill's own `allowed-tools` rules, and accepted by the real `sm`
parser. The read-only verbs the plugin hook approves must also be approved by it, so the step list,
progress, summary and wait calls never stop for permission in any turn.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from skill_docs import PLUGIN, SKILLS_DIR

CONNECT = SKILLS_DIR / "agami-connect"
REFS = CONNECT / "references"
SHARED = PLUGIN / "shared"
SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
HOOK = PLUGIN / "scripts" / "approve_own_calls.py"


def _linked_shared() -> list[Path]:
    """The shared files agami-connect links, from its SKILL.md and its phase files."""
    names: set[str] = set()
    for doc in [CONNECT / "SKILL.md", *REFS.glob("*.md")]:
        names.update(re.findall(r"shared/([a-z_-]+\.md)", doc.read_text(encoding="utf-8")))
    return sorted(SHARED / n for n in names)


DOCS = sorted([CONNECT / "SKILL.md", *REFS.glob("*.md"), *_linked_shared(),
               *(PLUGIN / "agents").glob("*.md")])
LAUNCHER = re.compile(r'scripts/(?:sm|py)"')
PLACEHOLDER = re.compile(r"<[^<>\n]+>")


def _body(doc: Path) -> str:
    text = doc.read_text(encoding="utf-8")
    if text.startswith("---\n"):  # the frontmatter's allowed-tools rules are patterns, not commands
        return text.split("\n---\n", 1)[1]
    return text


def commands() -> list[tuple[str, str]]:
    """(`file:line`, command) for every command in a bash (or unlabelled) fence or an inline code
    span that runs agami's `sm` or `py` launcher."""
    found = []
    for doc in DOCS:
        fence = None
        for n, line in enumerate(_body(doc).splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("```"):
                fence = None if fence is not None else stripped[3:].strip()
                continue
            where = f"{doc.relative_to(PLUGIN)}:{n}"
            if fence is not None:
                if fence in ("bash", "sh", "") and LAUNCHER.search(stripped):
                    found.append((where, stripped))
                continue
            for span in re.findall(r"`([^`\n]+)`", line):
                if LAUNCHER.search(span):
                    found.append((where, span))
    return found


COMMANDS = commands()


def test_the_scan_finds_the_commands():
    assert len(COMMANDS) > 60
    assert any('sm" summary "<root>"' in c for _, c in COMMANDS)
    assert any(w.startswith("agents/") for w, _ in COMMANDS) is False  # agents run no commands


def _without_placeholders(command: str) -> str:
    return PLACEHOLDER.sub("P", command.replace("${CLAUDE_PLUGIN_ROOT}", "/plugin"))


@pytest.mark.parametrize("where,command", COMMANDS)
def test_no_shell_syntax_in_a_command(where, command):
    bare = _without_placeholders(command)
    for token in ("$", "|", ";", "&&", ">", "<", "`"):
        assert token not in bare, f"{where}: {token!r} in {command}"
    assert not bare.rstrip().endswith("\\"), f"{where}: continued over lines: {command}"
    assert " #" not in bare, f"{where}: a trailing comment: {command}"


def test_no_root_or_interpreter_variable_anywhere():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for var in ("$ROOT", '"$PY"', "$PY ", "$PY`", "--full"):
            assert var not in text, f"{doc.relative_to(PLUGIN)}: {var}"


def test_no_scratch_file_outside_the_scratch_folder():
    for doc in DOCS:
        assert not re.search(r"(?<![\w.<>])/tmp/", doc.read_text(encoding="utf-8")), doc.name
        assert "cat ~/.config/agami/path" not in doc.read_text(encoding="utf-8"), doc.name


# --- placeholders filled in, as Claude fills them -----------------------------------------------

def _fill(command: str, art: Path) -> str:
    values = {
        "plugin_root": str(PLUGIN), "root": str(art / "acme"), "artifacts_dir": str(art),
        "profile": "acme", "area": "sales", "batch": "20261112-101500-ab12", "chunk": "1",
        "db_type": "postgres", "id": "build", "t1,t2,…": "orders,customers", "t1": "orders",
        "t2": "customers", "result 1": str(art / "local" / "tmp" / "agami-a.result.json"),
        "result 2": str(art / "local" / "tmp" / "agami-b.result.json"),
        "schema.table.column": "sales.orders.note", "schema": "sales",
    }

    def one(m: re.Match) -> str:
        key = m.group(0)[1:-1]
        return values.get(key, re.sub(r"[^A-Za-z0-9_.-]+", "-", key).strip("-") or "x")

    filled = command.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
    filled = PLACEHOLDER.sub(one, filled)
    return " ".join(w for w in filled.split(" ") if w != "…")


def _allowed_tools() -> list[re.Pattern]:
    head = (CONNECT / "SKILL.md").read_text(encoding="utf-8").split("\n---\n", 1)[0]
    rules = yaml.safe_load(head.removeprefix("---\n"))["allowed-tools"]
    patterns = []
    for rule in rules:
        if not rule.startswith("Bash("):
            continue
        glob = rule[len("Bash("):-1].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
        patterns.append(re.compile("".join(".*" if c == "*" else re.escape(c) for c in glob) + r"\Z",
                                   re.S))
    return patterns


ALLOWED = _allowed_tools()


@pytest.fixture
def art(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    (root / "local" / "tmp").mkdir(parents=True)
    (root / "acme").mkdir()
    return root


# A `py` call that isn't a launcher command of its own, only named inside a sentence about one.
_FRAGMENT = re.compile(r'scripts/py"$')


@pytest.mark.parametrize("where,command", COMMANDS)
def test_each_command_is_matched_by_an_allowed_tools_rule(where, command, art):
    if _FRAGMENT.search(command):
        pytest.skip("names the launcher only")
    filled = _fill(command, art)
    assert any(p.match(filled) for p in ALLOWED), f"{where}: no allowed-tools rule matches {filled}"


def _sm_argv(art: Path) -> list[tuple[str, list[str]]]:
    out = []
    for where, command in COMMANDS:
        if 'scripts/sm"' not in command:
            continue
        words = shlex.split(_fill(command, art))
        while words and re.match(r"^[A-Z_]+=", words[0]):
            words.pop(0)
        if words and words[0] == "bash":
            words.pop(0)
        assert words and words[0].endswith("/scripts/sm"), (where, command)
        argv = words[1:]
        # `install` is the launcher's own, never the parser's; `<verb>` is the rule's general form
        if argv and argv[0] not in ("install", "verb"):
            out.append((where, argv))
    return out


def test_every_sm_command_parses(art):
    """The CLI refuses abbreviated options, so a skill line with a misspelled or shortened option
    would fail at run time. Parsed in a child, so this checkout's parser is the one read."""
    pytest.importorskip("pydantic")
    calls = _sm_argv(art)
    assert len(calls) > 40
    probe = ("import json, sys; sys.path.insert(0, sys.argv[1]); from semantic_model import cli\n"
             "bad = []\n"
             "for where, argv in json.loads(sys.stdin.read()):\n"
             "    try:\n"
             "        cli.build_parser().parse_args(argv)\n"
             "    except SystemExit:\n"
             "        bad.append([where, argv])\n"
             "print(json.dumps(bad))\n")
    run = subprocess.run([sys.executable, "-c", probe, str(SRC)], input=json.dumps(calls),
                         capture_output=True, text=True, check=True)
    assert json.loads(run.stdout.strip().splitlines()[-1]) == []


_spec = importlib.util.spec_from_file_location("approve_own_calls_for_forms", HOOK)
HOOK_MOD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(HOOK_MOD)


class _Stdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def _hook_allows(command: str, art: Path) -> bool:
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command},
             "cwd": "/"}
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art), "HOME": str(art.parent)}
    saved_env, saved_in, saved_out = dict(os.environ), sys.stdin, sys.stdout
    os.environ.clear()
    os.environ.update(env)
    sys.stdin = _Stdin(json.dumps(event).encode("utf-8"))
    sys.stdout = io.StringIO()
    try:
        HOOK_MOD.main()
        out = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = saved_in, saved_out
        os.environ.clear()
        os.environ.update(saved_env)
    return bool(out.strip()) and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_the_hooks_read_only_verbs_are_approved_as_the_skill_writes_them(art):
    """The step list, progress, summary and wait calls never stop for permission, in any turn."""
    checked = set()
    for where, command in COMMANDS:
        m = re.search(r'scripts/sm" ([a-z-]+)', command)
        if not m or m.group(1) not in HOOK_MOD.VERBS or command.startswith("AGAMI_"):
            continue
        filled = _fill(command, art)
        assert _hook_allows(filled, art), f"{where}: the hook does not approve {filled}"
        checked.add(m.group(1))
    assert {"steps", "progress", "summary", "wait-files", "batch-id", "bundle"} <= checked


# --- the instructions this task added ----------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_hard_rule_is_stated_in_the_skill_and_the_conventions():
    rule = ("Never put a variable assignment, a pipe, `;`, `&&`, a redirect or a filter on the line, "
            "and never run two agami commands in one call. A line written any other way stops to ask "
            "the person for permission; written this way it is pre-approved while the skill is "
            "running. Read the whole output: the commands print only what you need.")
    assert rule in _flat(_read(CONNECT / "SKILL.md"))
    assert rule in _flat(_read(SHARED / "invocation-conventions.md"))
    hard_rules = _read(CONNECT / "SKILL.md").split("### HARD RULES", 1)[1].split("### Preflight", 1)[0]
    assert "6. **Run each agami command as its own Bash call" in hard_rules


def test_root_is_defined_where_plugin_root_is():
    assert "`<root>` is the database's model folder, `<artifacts_dir>/<profile>`" in \
        _flat(_read(SHARED / "invocation-conventions.md"))
    for doc in REFS.glob("*.md"):
        head = _read(doc).split("\n## ", 1)[0]
        assert "`<root>` is the database's model folder" in head, doc.name


# Section 2: the task list
def test_the_task_list_is_updated_after_every_steps_and_progress_call():
    text = _flat(_read(SHARED / "task-list.md"))
    assert ("After every `sm steps` call and every `sm progress --wait` call, the very next thing is "
            "the task-list update.") in text
    assert "Never filter or shorten the output" in text
    assert "`✓` is done, `▶` is in progress, `·` is not started" in text


def test_the_build_loop_is_one_progress_call_per_pass():
    text = _read(REFS / "build.md")
    assert 'bash "<plugin_root>/scripts/sm" progress "<root>" --wait 20' in text
    loop = text.split("3. **Keep the turn open", 1)[1].split("\n4. ", 1)[0]
    assert "No separate `sm steps` call per pass" in loop
    assert "```bash\n   bash \"<plugin_root>/scripts/sm\" steps" not in loop


# Section 3: the closing summary
@pytest.mark.parametrize("name,card", [("summary.md", "Then **AskUserQuestion**: `Review what needs sign-off`"),
                                       ("sample.md", "**A card of starter questions")])
def test_the_closing_card_comes_after_the_summary(name, card):
    text = _read(REFS / name)
    call = text.index('scripts/sm" summary "<root>"')
    rule = text.index("The closing card is never asked in a message that does not begin with this summary.")
    assert call < rule < text.index(card)
    assert "Post its output exactly as printed" in text


def test_the_summary_is_not_assembled_by_hand():
    summary = _read(REFS / "summary.md")
    assert "agami-connect just ran. Here's what we found" not in summary
    assert 'sm" timing' not in summary
    sample = _read(REFS / "sample.md")
    assert "`Subject area | Tables | Hidden` table" not in sample
    assert "as well as the summary, not instead of it" in _flat(summary)
    assert "as well as the summary, not instead of it" in _flat(sample)


# Section 4: no silent waits
@pytest.mark.parametrize("name,line", [
    ("build.md", 'After the last "Checking joins" line'),
    ("enrich.md", "Inline, describe 4 tables at a time."),
    ("enrich.md", "A model described inline is never silent for more than one batch."),
    ("enrich.md", "Proposing metrics from the described tables…"),
    ("enrich.md", "Collect** the result files with `sm wait-files --wait 30`"),
    ("enrich.md", "post one line naming what is still running"),
    ("examples.md", 'wait-files --file "<artifacts_dir>/local/tmp/agami-examples-items-<batch>.json" --wait 30'),
    ("examples.md", "Collecting the example questions drafted during sign-off…"),
    ("review.md", "Gathering the definitions that need your sign-off…"),
])
def test_each_wait_posts_a_line(name, line):
    assert line in _flat(_read(REFS / name))


def test_subagent_passes_are_30_seconds():
    text = _flat(_read(SHARED / "parallel-work.md"))
    assert "--wait 30" in text and "--wait 60" not in text
    assert "10 minutes of `--wait 30` passes (20 passes)" in text


# Section 5: currency on the decoding card
def test_the_currency_question_rides_on_the_decoding_card():
    text = _flat(_read(REFS / "enrich.md"))
    step6 = text.split("6. **After the last chunk:**", 1)[1].split("For each table fetch", 1)[0]
    assert "**The currency question rides on these cards:**" in step6
    assert 'sm" suggest-units "<root>"' in step6
    assert "while enrichers are still running it waits for the card that asks their `questions`" in step6
    assert "not on a card of its own" in text


# Section 6: chunks of 8
def test_chunks_are_8_tables_everywhere():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        assert not re.search(r"up to 12 tables|at most 12 tables|12 tables or fewer|tables \(12\)", text), \
            doc.name
    assert "one per chunk of up to 8 tables" in _read(CONNECT / "SKILL.md")
    assert "(up to 8 tables of one subject area)" in _read(PLUGIN / "agents" / "agami-enricher.md")


def test_the_checks_catch_the_live_runs_forms(art):
    """The forms the live run wrote fail here: a redirect, a variable, a pipe, a chain."""
    for bad in ('bash "<plugin_root>/scripts/sm" seed-validate "<root>" --profile <profile> > "<artifacts_dir>/local/tmp/agami-x.json"',
                'bash "<plugin_root>/scripts/sm" steps "$ROOT"',
                'bash "<plugin_root>/scripts/sm" steps "<root>" | grep text',
                'bash "<plugin_root>/scripts/sm" steps "<root>"; bash "<plugin_root>/scripts/sm" timing "<root>"'):
        with pytest.raises(AssertionError):
            test_no_shell_syntax_in_a_command("example", bad)
    assert not any(p.match(_fill('R=x bash "<plugin_root>/scripts/sm" steps "<root>"', art)) for p in ALLOWED)
    assert not _hook_allows(_fill('bash "<plugin_root>/scripts/sm" steps "/elsewhere/acme"', art), art)
