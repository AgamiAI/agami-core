"""Every agami command agami-connect shows Claude is one literal launcher call.

A live run of `/agami-connect` stopped for permission on most of agami's own commands: Claude wrote
them as it saw them, `R="…"; bash "$P/scripts/sm" steps "$R" | grep …`, and no `allowed-tools`
rule matches a line that starts with an assignment or carries a pipe. Claude copies the skill's
command forms, so the forms themselves must be the one shape that is pre-approved: the launcher,
then its arguments, every path written out. This reads agami-connect's SKILL.md and the shared
files it links, and checks each command three ways: no shell syntax, matched by one of the skill's
own `allowed-tools` rules (or listed as one that deliberately asks), and accepted by the real `sm`
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

from skill_docs import PLUGIN, SKILLS_DIR, skill_docs

CONNECT = SKILLS_DIR / "agami-connect"
SHARED = PLUGIN / "shared"
SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
HOOK = PLUGIN / "scripts" / "approve_own_calls.py"


def _linked_shared() -> list[Path]:
    """The shared files agami-connect's SKILL.md links."""
    names = set(re.findall(r"shared/([a-z_-]+\.md)", (CONNECT / "SKILL.md").read_text(encoding="utf-8")))
    return sorted(SHARED / n for n in names)


DOCS = sorted([CONNECT / "SKILL.md", *_linked_shared()])
PLACEHOLDER = re.compile(r"<[^<>\n]+>")
# Any mention of the launcher as a path, quoted or not; a command must use the quoted form below.
LAUNCHER = re.compile(r"(?:\$\{CLAUDE_PLUGIN_ROOT\}|<plugin_root>)/scripts/(?:sm|py)(?![\w.-])")
QUOTED_LAUNCHER = re.compile(
    r'^(?:AGAMI_[A-Z_]+=\S+ )*bash "(?:\$\{CLAUDE_PLUGIN_ROOT\}|<plugin_root>)/scripts/(?:sm|py)"(?: |$)'
)
# Commands the skill shows that are deliberately NOT pre-approved: each asks the person, for the
# reason given. Everything else must match one of the skill's `allowed-tools` rules
# (tests/test_connect_allowed_tools.py holds what those rules may and may not approve).
DELIBERATELY_ASKS = {
    r'scripts/sm" install\b': "a network install, which the person agrees to",
    r'scripts/py" -c ': "runs code given on the line",
    r'scripts/py" -m ': "pip installs; execute_sql takes --no-safety",
    r'scripts/sm" describe-file ': "reads the person's own document, wherever it is",
    r'scripts/sm" apply-spec ': "reads the converted workbook under local/model-spec/",
    r'scripts/py" reconcile\.py ': "can call an MCP server over the network",
    r'scripts/sm" add-example .*<examples_files': "reads reconcile's output under local/reconcile/",
    r'scripts/(sm|py)" <(verb|script)>': "a generic form in prose, not a command",
    r'^bash "[^"]*scripts/py" (serve_page|render_[a-z_]+|progress_page|parse_[a-z_]+_decisions|'
    r"inventory_summary|model_spec_workbook)\.py": "writes or serves a path it is given; pre-approved "
    "only under AGAMI_SCRATCH_ONLY=1, which holds that path to agami's scratch folder",
}
SHORTHAND = re.compile(r"^sm ([a-z][a-z-]*)(?: (.*))?$")

# Commands other than agami's launchers that a fence may still show, each with the reason it stays.
# A fenced command whose first word is not here (and not the launcher) fails the allowlist test.
ALLOWED_HELPERS = {
    "psql": "the native-CLI query tier: password-free through PGPASSFILE, SQL read from a scratch file",
    "PGPASSFILE": "the env prefix of that psql call (the auth file, never the password)",
    "mysql": "the native-CLI query tier: password-free through --defaults-file, SQL read with `source`",
    "snowsql": "the native-CLI query tier: password-free through its config, SQL read with -f",
    "sqlite3": "the native-CLI query tier for a file database, SQL read with `.read`",
    "duckdb": "the native-CLI query tier, credentials in an init file, never on the line",
    "pip": "the driver package a card names; installed only after the person agrees",
    "gcloud": "a read-only grant for the person's admin to run, never run by Claude",
    "bq": "a read-only grant for the person's admin to run, never run by Claude",
    "AGAMI_PROFILE": "a setting the person types in their own shell to switch databases",
}
# Fences with these labels hold data, usage lines or pseudocode, never a command to run.
NOT_COMMAND_LABELS = {"text", "json", "yaml", "ini", "sql", "markdown", "md", "html", "csv", "tsv"}
# Whole fences left for a follow-up (not agami launcher calls; replacing them needs a new entry point).
FOLLOW_UP_FENCES = {
    (
        "shared/version-check.md",
        "# Read the installed plugin's own version",
    ): "a curl probe; follow-up",
    (
        "shared/connection-reference.md",
        "# Refuse to read if too permissive",
    ): "the perms check; follow-up",
    (
        "shared/connection-reference.md",
        "# Postgres",
    ): "a DuckDB heredoc; needs a one-profile attach",
    ("shared/connection-reference.md", "# MySQL"): "a DuckDB heredoc; needs a one-profile attach",
    # reconcile.py intake and parse_reconcile_report.py print to stdout only, so writing their
    # output to a file still needs a redirect until they take --out
    (
        "shared/own-examples.md",
        'mkdir -p "<artifacts_dir>/local/tmp"',
    ): "a redirect; needs --out on intake",
    (
        "shared/own-examples.md",
        'bash "<plugin_root>/scripts/py" parse_reconcile_report.py',
    ): "a redirect; needs --out on parse_reconcile_report.py",
}
# First words that mark a line in an unlabelled fence as a command (which must then be labelled).
SHELL_WORDS = {
    "bash",
    "sh",
    "zsh",
    "sm",
    "python",
    "python3",
    "pip",
    "pip3",
    "mkdir",
    "chmod",
    "cp",
    "mv",
    "rm",
    "ls",
    "cat",
    "which",
    "uname",
    "date",
    "printf",
    "echo",
    "grep",
    "sed",
    "awk",
    "head",
    "tail",
    "jq",
    "tr",
    "curl",
    "split",
    "for",
    "if",
    "export",
    "cd",
    "kill",
    "stat",
    "case",
    "open",
    "xdg-open",
    "psql",
    "mysql",
    "sqlite3",
    "snowsql",
    "duckdb",
    "gcloud",
    "bq",
    "git",
    "touch",
    "sudo",
    "node",
    "npm",
    "npx",
    "env",
    "source",
    "exec",
    "PGPASSFILE",
    "AGAMI_SQL_MAX_ROWS",
    "AGAMI_PROFILE",
    "AGAMI_PYTHON",
}
# Every line of a fence with one of these labels is a command (a `console` fence: every `$ ` line).
SHELL_LABELS = {"bash", "sh", "zsh", "shell", "console"}
# A code span in prose that starts with one of these words and has an argument is a helper command;
# each such span must be one of HELPER_SPANS, with its reason.
HELPER_SPAN_WORDS = {
    "chmod",
    "rm",
    "mkdir",
    "cp",
    "mv",
    "kill",
    "cat",
    "ls",
    "touch",
    "curl",
    "python3",
    "python",
    "pip",
    "pip3",
    "which",
    "uname",
    "date",
    "sudo",
    "git",
}
_CREDENTIAL = "protects a file that holds a credential (the credentials file, a key or token file)"
_DRIVER = "the driver package the table names; installed only after the person agrees on a card"
_NEVER = "named in a rule that forbids it"
HELPER_SPANS = {
    "chmod 600": _CREDENTIAL,
    "chmod 600 <artifacts_dir>/local/credentials": _CREDENTIAL,
    "ls /Applications": _NEVER,
    "which <tool>": "the one probe the hard rule allows; the resolve step already runs it, so the skill does not",
    "python3 -c '...'": _NEVER,
    "python3 -c": _NEVER,
    "python -m execute_sql": "names the module; every command runs it through the `py` launcher",
    "python -m semantic_model.cli": "names the module; every command runs it through the `sm` launcher",
    "pip install psycopg2-binary": _DRIVER,
    "pip install pymysql": _DRIVER,
    "pip install snowflake-connector-python": _DRIVER,
    "pip install duckdb pytz": _DRIVER,
    "pip install": _DRIVER,
    "pip install --user": _DRIVER,
    'mkdir -p "<artifacts_dir>/local/model-spec/<profile>"': "only with a spec workbook: its parser does not create its output folder",
    'mv "<artifacts_dir>/<profile>" "<artifacts_dir>/<profile>.partial-<timestamp>"': "only after a failed spec apply: moves a partial model aside, never deletes it",
    "kill <pid>": "only where the session has no stop tool, to stop the password helper",
    'cp -R "<plugin_root>/samples/store/model/." "<artifacts_dir>/agami-example/"': "the sample's ready-made model; no sm verb copies a model",
    'cp -R "${CLAUDE_PLUGIN_ROOT}/samples/store/model/." "<artifacts_dir>/agami-example/"': "the sample's ready-made model, as SKILL.md writes it; no sm verb copies a model",
}


def _body(doc: Path) -> str:
    text = doc.read_text(encoding="utf-8")
    if text.startswith("---\n"):  # the frontmatter's allowed-tools rules are patterns, not commands
        return text.split("\n---\n", 1)[1]
    return text


def _first_word(line: str) -> str:
    word = line.split(" ", 1)[0].strip('"')
    return word.split("=", 1)[0] if "=" in word else word


def _is_shell_line(line: str) -> bool:
    """Whether a line in an unlabelled fence reads as a command: a known program, the launcher, an
    assignment, a path, or a bare lowercase word followed by an option. Display text (a table row, a
    `✓` line, a sentence) does not."""
    word = _first_word(line)
    return (
        word in SHELL_WORDS
        or bool(LAUNCHER.search(line.split(" ", 1)[0]))
        or word.startswith(("<python3>", "./", "/"))
        or bool(re.match(r"^[A-Za-z_]\w*=\S", line))
        or bool(re.match(r"^[a-z][\w.-]* -{1,2}[a-z]", line))
    )


def _fence_lines(lines: list[tuple[int, str]], label: str) -> list[tuple[int, str]]:
    """The commands in a shell fence: comments and blank lines dropped, `\\` continuations joined, and
    in a `console` fence only the `$ ` lines (the rest is output)."""
    out: list[tuple[int, str]] = []
    pending: tuple[int, str] | None = None
    for n, raw in lines:
        line = raw.strip()
        if pending:
            pending = (pending[0], pending[1] + " " + line.rstrip("\\").strip())
            if not line.endswith("\\"):
                out.append(pending)
                pending = None
            continue
        if not line or line.startswith("#"):
            continue
        if label == "console":
            if not line.startswith("$ "):
                continue
            line = line[2:]
        if line.endswith("\\"):
            pending = (n, line.rstrip("\\").strip())
        else:
            out.append((n, line))
    if pending:
        out.append(pending)
    return out


def scan(docs: list[Path] | None = None) -> tuple[list, list, list, list, list]:
    """(launcher commands, `sm` shorthand spans, other fenced commands, helper spans in prose, command
    lines in unlabelled fences), each with `file:line`. Every line of a bash, sh, zsh, shell or console
    fence is a command; an unlabelled fence may hold none."""
    launchers, shorthand, others, helpers, unlabelled = [], [], [], [], []
    for doc in docs or DOCS:
        rel = str(doc.relative_to(PLUGIN)) if doc.is_relative_to(PLUGIN) else doc.name
        fence, block = None, []
        for n, line in enumerate(_body(doc).splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("```"):
                if fence is None:
                    fence, block = stripped[3:].strip(), []
                    continue
                head = next((t.strip() for _, t in block if t.strip()), "")
                follow_up = any(rel == f and head.startswith(h) for f, h in FOLLOW_UP_FENCES)
                if fence in SHELL_LABELS and not follow_up:
                    for m, cmd in _fence_lines(block, fence):
                        (launchers if LAUNCHER.search(cmd) else others).append((f"{rel}:{m}", cmd))
                elif fence == "":
                    unlabelled += [
                        (f"{rel}:{m}", t.strip())
                        for m, t in block
                        if t.strip() and not t.strip().startswith("#") and _is_shell_line(t.strip())
                    ]
                fence = None
                continue
            if fence is not None:
                block.append((n, line))
                continue
            where = f"{rel}:{n}"
            for span in re.findall(r"`([^`\n]+)`", line):
                if LAUNCHER.search(span):
                    launchers.append((where, span))
                elif SHORTHAND.match(span):
                    shorthand.append((where, span))
                elif span.split(" ", 1)[0] in HELPER_SPAN_WORDS and " " in span.strip():
                    helpers.append((where, span))
    return launchers, shorthand, others, helpers, unlabelled


COMMANDS, SHORTHANDS, OTHERS, HELPER_USES, UNLABELLED = scan()


def test_the_scan_finds_the_commands():
    assert len(COMMANDS) > 60 and len(SHORTHANDS) > 40
    assert any('sm" steps "<root>" --mark first_question=in_progress' in c for _, c in COMMANDS)
    # local/ is made by `sm init-local`, never by a mkdir of Claude's own
    assert any('sm" init-local "<artifacts_dir>"' in c for _, c in COMMANDS)
    assert not any(c.startswith("mkdir") for _, c in OTHERS) and HELPER_USES


def _without_placeholders(command: str) -> str:
    return PLACEHOLDER.sub("P", command.replace("${CLAUDE_PLUGIN_ROOT}", "/plugin"))


def _check_syntax(where: str, command: str) -> None:
    bare = _without_placeholders(command)
    for token in ("$", "|", ";", "&&", ">", "<", "`"):
        assert token not in bare, f"{where}: {token!r} in {command}"
    assert not bare.rstrip().endswith("\\"), f"{where}: continued over lines: {command}"
    assert " #" not in bare, f"{where}: a trailing comment: {command}"


def _expand(span: str) -> str:
    return 'bash "<plugin_root>/scripts/sm" ' + span[len("sm ") :]


@pytest.mark.parametrize("where,command", COMMANDS)
def test_no_shell_syntax_in_a_command(where, command):
    _check_syntax(where, command)
    assert QUOTED_LAUNCHER.match(command), (
        f'{where}: write the launcher as bash "<plugin_root>/scripts/…": {command}'
    )


@pytest.mark.parametrize("where,span", SHORTHANDS)
def test_no_shell_syntax_in_an_sm_shorthand(where, span):
    _check_syntax(where, _expand(span))


@pytest.mark.parametrize("where,span", HELPER_USES)
def test_helper_spans_in_prose_are_allowlisted(where, span):
    """A `chmod …`, `rm …`, `mkdir …` or other helper named in a sentence is copied like a fenced one:
    each that stays is listed with its reason; a leftover (`chmod 600 .config`) fails."""
    assert span in HELPER_SPANS, f"{where}: {span!r} is not an allowed helper"


def test_no_unlabelled_fence_holds_a_command():
    """A command goes in a `bash` fence, where every line is checked; an unlabelled fence is display
    text only."""
    assert UNLABELLED == []


@pytest.mark.parametrize("where,command", OTHERS)
def test_other_fenced_commands_are_allowlisted(where, command):
    """Each command a fence shows that is not agami's launcher is named here with its reason; a
    `python3 …`, `"<python3>" -c`, `chmod`, `cp`, `which`, `uname` or `date` line fails."""
    word = _first_word(command)
    assert word in ALLOWED_HELPERS, f"{where}: {word!r} is not an allowed helper: {command}"
    if word not in ("gcloud", "bq"):  # an admin's multi-line grant, shown for the person to copy
        _check_syntax(where, command)


def test_no_root_or_interpreter_variable_anywhere():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for var in ("$ROOT", '"$PY"', "$PY ", "$PY`", "--full"):
            assert var not in text, f"{doc.relative_to(PLUGIN)}: {var}"


INTERPRETER_VAR = re.compile(r"\$\{?PY\b")


@pytest.mark.parametrize(
    "doc",
    [*skill_docs(), *sorted(SHARED.glob("*.md"))],
    ids=lambda d: str(d.relative_to(PLUGIN)),
)
def test_no_skill_or_shared_file_names_the_interpreter_variable(doc):
    """`connection-reference.md` no longer defines `$PY`, and nothing carries a shell variable from
    one call to the next, so a `"$PY" …` line runs an empty command. Every skill and every shared
    file, not only the ones agami-connect links, names the launcher instead."""
    hits = [line for line in doc.read_text(encoding="utf-8").splitlines() if INTERPRETER_VAR.search(line)]
    assert hits == [], f"{doc.relative_to(PLUGIN)}: {hits}"


def test_no_scratch_file_outside_the_scratch_folder():
    for doc in DOCS:
        assert not re.search(r"(?<![\w.<>])/tmp/", doc.read_text(encoding="utf-8")), doc.name
        assert "cat ~/.config/agami/path" not in doc.read_text(encoding="utf-8"), doc.name


# --- placeholders filled in, as Claude fills them -----------------------------------------------


def _fill(command: str, art: Path) -> str:
    values = {
        "plugin_root": str(PLUGIN),
        "root": str(art / "acme"),
        "artifacts_dir": str(art),
        "profile": "acme",
        "area": "sales",
        "batch": "20261112-101500-ab12",
        "chunk": "1",
        "db_type": "postgres",
        "id": "build",
        "t1,t2,…": "orders,customers",
        "t1": "orders",
        "t2": "customers",
        "result 1": str(art / "local" / "tmp" / "agami-a.result.json"),
        "result 2": str(art / "local" / "tmp" / "agami-b.result.json"),
        "schema.table.column": "sales.orders.note",
        "schema": "sales",
        "month": "7",
        "dictionary_file": str(art / "local" / "tmp" / "agami-dictionary.csv"),
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
        glob = rule[len("Bash(") : -1].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
        patterns.append(
            re.compile("".join(".*" if c == "*" else re.escape(c) for c in glob) + r"\Z", re.S)
        )
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
    asks = [why for pat, why in DELIBERATELY_ASKS.items() if re.search(pat, command)]
    filled = _fill(command, art)
    approved = any(p.match(filled) for p in ALLOWED)
    if asks:
        assert not approved, f"{where}: pre-approved, but it should ask ({asks[0]}): {filled}"
        return
    assert approved, f"{where}: no allowed-tools rule matches {filled}"


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
    probe = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); from semantic_model import cli\n"
        "bad = []\n"
        "for where, argv in json.loads(sys.stdin.read()):\n"
        "    try:\n"
        "        cli.build_parser().parse_args(argv)\n"
        "    except SystemExit:\n"
        "        bad.append([where, argv])\n"
        "print(json.dumps(bad))\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", probe, str(SRC)],
        input=json.dumps(calls),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(run.stdout.strip().splitlines()[-1]) == []


def test_every_sm_shorthand_names_a_real_verb_and_real_options():
    """`sm suggest-metrics … --flag-rates` in a sentence is copied as much as a fenced command: its
    verb must exist and every option it names must be one of that verb's, in full (no abbreviation)."""
    pytest.importorskip("pydantic")
    probe = (
        "import argparse, json, sys; sys.path.insert(0, sys.argv[1]); from semantic_model import cli\n"
        "sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))\n"
        "print(json.dumps({v: sorted(o for a in p._actions for o in a.option_strings)"
        " for v, p in sub.choices.items()}))\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", probe, str(SRC)], capture_output=True, text=True, check=True
    )
    options = json.loads(run.stdout.strip().splitlines()[-1])
    bad = []
    for where, span in SHORTHANDS:
        verb, rest = SHORTHAND.match(span).groups()
        if verb == "install":
            continue
        if verb not in options:
            bad.append((where, span, "no such verb"))
            continue
        for opt in re.findall(r"(?<![\w-])--[a-z][a-z-]*", PLACEHOLDER.sub("", rest or "")):
            if opt not in options[verb]:
                bad.append((where, span, opt))
    assert bad == []


_spec = importlib.util.spec_from_file_location("approve_own_calls_for_forms", HOOK)
HOOK_MOD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(HOOK_MOD)


class _Stdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def _hook_allows(command: str, art: Path) -> bool:
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": "/",
    }
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
    return (
        bool(out.strip()) and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "allow"
    )


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
    assert {"steps", "progress", "summary", "batch-id", "review-items", "model-tree"} <= checked


# --- the instructions this task added ----------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_hard_rule_is_stated_in_the_skill_and_the_conventions():
    rule = (
        "Never put a variable assignment, a pipe, `;`, `&&`, a redirect or a filter on the line, "
        "and never run two agami commands in one call. A line written any other way stops to ask "
        "the person for permission; written this way it is pre-approved while the skill is "
        "running. Read the whole output: the commands print only what you need."
    )
    assert rule in _flat(_read(CONNECT / "SKILL.md"))
    assert rule in _flat(_read(SHARED / "invocation-conventions.md"))
    hard_rules = (
        _read(CONNECT / "SKILL.md").split("### HARD RULES", 1)[1].split("### Preflight", 1)[0]
    )
    assert "6. **Run each agami command as its own Bash call" in hard_rules


def test_root_is_defined_where_plugin_root_is():
    assert "`<root>` is the database's model folder, `<artifacts_dir>/<profile>`" in _flat(
        _read(SHARED / "invocation-conventions.md")
    )
    hard_rules = (
        _read(CONNECT / "SKILL.md").split("### HARD RULES", 1)[1].split("### Preflight", 1)[0]
    )
    assert "`<root>` is the database's model folder" in hard_rules


# Section 2: the task list
def test_the_task_list_is_updated_when_then_says_so():
    text = _flat(_read(SHARED / "task-list.md"))
    assert (
        "After a `sm steps` or `sm progress` call, update the task list from `list` when its `then` "
        "says to, before anything else; otherwise there is nothing to do."
    ) in text
    assert "Never filter or shorten the output" in text
    assert "`✓` is done, `▶` is in progress, `·` is not started" in text


def _section(text: str, heading: str) -> str:
    return text.split(heading, 1)[1].split("\n### ", 1)[0]


def test_the_build_loop_is_one_progress_call_per_pass():
    text = _section(_read(CONNECT / "SKILL.md"), "### 1.7 ")
    assert 'bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" progress "<root>" --wait 20' in text
    loop = text.split("3. **Keep the turn open", 1)[1].split("\n4. ", 1)[0]
    assert "No separate `sm steps` call per pass" in loop
    assert '```bash\n   bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" steps' not in loop


def test_the_checks_catch_the_live_runs_forms(art):
    """The forms the live run wrote fail here: a redirect, a variable, a pipe, a chain."""
    for bad in (
        'bash "<plugin_root>/scripts/sm" seed-validate "<root>" --profile <profile> > "<artifacts_dir>/local/tmp/agami-x.json"',
        'bash "<plugin_root>/scripts/sm" steps "$ROOT"',
        'bash "<plugin_root>/scripts/sm" steps "<root>" | grep text',
        'bash "<plugin_root>/scripts/sm" steps "<root>"; bash "<plugin_root>/scripts/sm" summary "<root>"',
    ):
        with pytest.raises(AssertionError):
            test_no_shell_syntax_in_a_command("example", bad)
    assert not any(
        p.match(_fill('R=x bash "<plugin_root>/scripts/sm" steps "<root>"', art)) for p in ALLOWED
    )
    assert not _hook_allows(
        _fill('bash "<plugin_root>/scripts/sm" steps "/elsewhere/acme"', art), art
    )


def test_no_skill_file_tells_claude_to_change_the_persons_characters():
    """The person's text goes to `sm` through a file, as written: no instruction to swap, replace or
    escape an apostrophe or a quote (a company called "Macy's" must be stored as typed)."""
    pattern = re.compile(
        r"`'`[^.\n]{0,60}(?:as|into|with|to) `[’‘\\]"
        r"|(?:replace|swap|escape|turn)[^.\n]{0,40}(?:apostrophe|`'`|quote)",
        re.I,
    )
    docs = [*SKILLS_DIR.glob("*/SKILL.md"), *SHARED.glob("*.md")]
    found = [
        f"{d.relative_to(PLUGIN)}: {m.group(0)}"
        for d in docs
        for m in pattern.finditer(d.read_text(encoding="utf-8"))
    ]
    assert found == []
    # and the check catches the instruction it exists for
    assert pattern.search("each value in single quotes (a `'` inside it written as `’`)")
    assert pattern.search("with every `'` in it written as `’`")


def test_the_new_checks_catch_what_they_claim():
    # an unquoted launcher path, which the hook and the shell treat differently
    with pytest.raises(AssertionError):
        test_no_shell_syntax_in_a_command("example", 'bash <plugin_root>/scripts/sm steps "<root>"')
    # helpers that are not on the allowlist, whatever the fence's label
    for line in (
        'python3 "<plugin_root>/scripts/x.py"',
        "\"<python3>\" -c 'import yaml'",
        'chmod 700 "<artifacts_dir>/local"',
        "uname -s",
        "date -u",
        "which sqlite3",
        'cp -R "<a>" "<b>"',
    ):
        assert _is_shell_line(line), line
        with pytest.raises(AssertionError):
            test_other_fenced_commands_are_allowlisted("example", line)
    # a shorthand span with a shell variable or a redirect
    with pytest.raises(AssertionError):
        test_no_shell_syntax_in_an_sm_shorthand("example", 'sm steps "$ROOT"')
    with pytest.raises(AssertionError):
        test_no_shell_syntax_in_an_sm_shorthand("example", "sm bundle x --area a > out.json")


def test_every_fence_label_is_scanned(tmp_path):
    doc = tmp_path / "x.md"
    doc.write_text(
        "```zsh\nchmod 700 x\ngit status\n```\n"
        '```console\n$ bash "<plugin_root>/scripts/sm" steps "$ROOT"\n{"list": []}\n```\n'
        "```shell\ntouch a \\\n  b\nnode x.js\n```\n"
        "```\n| a | b |\n✓ Done\nsudo rm -rf x\n```\n"
        '```json\n{"a": 1}\n```\nRun `sm steps "<root>"` now, never `chmod 600 x`.\n',
        encoding="utf-8",
    )
    launchers, shorthand, others, helpers, unlabelled = scan([doc])
    assert [c for _, c in others] == ["chmod 700 x", "git status", "touch a b", "node x.js"]
    assert [c for _, c in launchers] == ['bash "<plugin_root>/scripts/sm" steps "$ROOT"']
    assert [c for _, c in shorthand] == ['sm steps "<root>"']
    assert [c for _, c in helpers] == ["chmod 600 x"]
    assert [c for _, c in unlabelled] == ["sudo rm -rf x"]
    for _, c in others:
        with pytest.raises(AssertionError):
            test_other_fenced_commands_are_allowlisted("example", c)
    with pytest.raises(AssertionError):
        test_helper_spans_in_prose_are_allowlisted("example", "chmod 600 x")
