"""What agami-connect's `allowed-tools` pre-approves, and that nothing hostile fits under it.

Onboarding reads rows and documents nobody vetted, and that text steers the model. A rule that ended
in a free tail (`sm *`, `py *`, `VAR=* bash …`) pre-approved `sm install` (a network install),
`py -c '<code>'`, `py -m pip install …`, `py -m execute_sql --no-safety`, and, through the
assignment, `BASH_ENV=<file> bash …`, which runs <file> as shell. Every rule now names one command,
and the commands that read a file the call names carry the scratch-only guard.

Claude Code fills `${CLAUDE_PLUGIN_ROOT}` in before matching, and a `*` matches any characters,
spaces included; the rules are compiled here the same way.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from skill_docs import PLUGIN, SKILLS_DIR

SKILL = SKILLS_DIR / "agami-connect" / "SKILL.md"
ROOT = str(PLUGIN)
SM = f'bash "{ROOT}/scripts/sm"'
PY = f'bash "{ROOT}/scripts/py"'
ART = "/Users/you/agami-artifacts"


def _rules() -> list[str]:
    head = SKILL.read_text(encoding="utf-8").split("\n---\n", 1)[0]
    return yaml.safe_load(head.removeprefix("---\n"))["allowed-tools"]


def _compile(rule: str) -> re.Pattern:
    glob = rule[len("Bash(") : -1].replace("${CLAUDE_PLUGIN_ROOT}", ROOT)
    return re.compile("".join(".*" if c == "*" else re.escape(c) for c in glob) + r"\Z", re.S)


BASH_RULES = [r for r in _rules() if r.startswith("Bash(")]
PATTERNS = [_compile(r) for r in BASH_RULES]


def _approved(command: str) -> list[str]:
    return [r for r, p in zip(BASH_RULES, PATTERNS) if p.match(command)]


HOSTILE = [
    f"{SM} install",
    f"{SM} install --upgrade",
    f"{PY} -c 'import os; os.system(\"id\")'",
    f"{PY} -m pip install requests",
    f"{PY} -m execute_sql --sql 'DELETE FROM t' --no-safety",
    f"{PY} /tmp/planted.py",
    f"{PY} reconcile.py run --via mcp --url https://example.com/mcp",
    f"BASH_ENV={ART}/local/tmp/agami-x.txt {SM} steps {ART}/acme",
    f"AGAMI_SQL_MAX_ROWS=1 BASH_ENV={ART}/local/tmp/agami-x.txt AGAMI_SCRATCH_ONLY=1 {SM} introspect",
    f"AGAMI_PROFILE=acme BASH_ENV=/tmp/x {PY} connect_resolve.py",
    f"PYTHONPATH=/tmp/x {SM} steps {ART}/acme",
    f"{SM} curate {ART}/acme --ops-file /Users/you/.aws/credentials",
    f"{SM} add {ART}/acme --kind metric --area a --file /Users/you/.ssh/id_rsa",
    f"{SM} seed-examples {ART}/acme --profile acme --file /etc/passwd",
    f"{SM} introspect --profile acme --tables-file /etc/passwd",
    f"{SM} describe-file {ART}/acme --file /Users/you/.netrc",
    f"{SM} apply-spec {ART}/acme --file /Users/you/spec.json",
    # A script that writes or serves a path it is given, without the guard that holds the path.
    f"{PY} render_table_picker.py --inventory x --save-to-helper --out /Users/you/.zshrc",
    f"{PY} serve_page.py /Users/you/.ssh/id_rsa.html",
    f'{PY} "{ROOT}/samples/store/build_sample.py" --out /Users/you/thesis.db',
    f"{PY} model_spec_workbook.py parse --file x.xlsx --out /Users/you/.bashrc",
    f"{PY} check_statement.py --profile acme --area a --row-dir /Users/you/project",
    f"{PY} promote_credentials.py --agami-dir /Users/you/elsewhere",
]


@pytest.mark.parametrize("command", HOSTILE)
def test_no_rule_approves_a_hostile_command(command):
    assert _approved(command) == []


def test_no_rule_has_a_free_tail_or_a_wildcard_before_the_launcher():
    for rule in BASH_RULES:
        body = rule[len("Bash(") : -1]
        launcher = re.search(r'bash "\$\{CLAUDE_PLUGIN_ROOT\}/scripts/(sm|py)"', body)
        assert launcher, rule
        assert "*" not in body[: launcher.start()], f"wildcard before the launcher: {rule}"
        rest = body[launcher.end() :]
        # one fixed word (a verb or a script) before any wildcard
        assert re.match(r' ("[^"*]+"|[a-z][a-z0-9_.-]*)( \*)?$', rest), f"free tail: {rule}"


def test_every_py_rule_names_a_script_the_plugin_has():
    for rule in BASH_RULES:
        m = re.search(r'scripts/py" (?:"([^"]+)"|([^" )]+))', rule)
        if m:
            path = (m.group(1) or m.group(2)).replace("${CLAUDE_PLUGIN_ROOT}", ROOT)
            target = Path(path) if path.startswith("/") else PLUGIN / "scripts" / path
            assert target.is_file(), rule
            assert target.suffix == ".py", rule


# The verbs that read a JSON or list file the call names: pre-approved only under the guard.
FILE_READERS = {
    "curate",
    "add",
    "add-example",
    "seed-examples",
    "set-terminology",
    "introspect",
    "discover",
}
NEVER = {"install", "describe-file", "apply-spec"}


def test_sm_rules_name_one_verb_and_file_readers_carry_the_guard():
    verbs = []
    for rule in BASH_RULES:
        m = re.search(r'scripts/sm" ([a-z-]+)', rule)
        if not m:
            continue
        verb = m.group(1)
        verbs.append(verb)
        assert verb not in NEVER, rule
        if verb in FILE_READERS:
            assert "AGAMI_SCRATCH_ONLY=1 bash" in rule, rule
    assert len(verbs) == len(set(verbs)), "one rule per verb"


# The `py` scripts that write or serve a path the call names (scripts/_scratch_out.py holds it).
PATH_WRITERS = {
    "inventory_summary.py",
    "serve_page.py",
    "render_table_picker.py",
    "render_open_questions_page.py",
    "render_examples_validation.py",
    "render_model_explorer.py",
    "progress_page.py",
    "parse_open_questions_decisions.py",
    "parse_examples_decisions.py",
    "model_spec_workbook.py",
    "build_sample.py",
}


def test_py_rules_that_write_a_named_path_carry_the_guard():
    seen = set()
    for rule in BASH_RULES:
        m = re.search(
            r'scripts/py" "?(?:\$\{CLAUDE_PLUGIN_ROOT\}/samples/store/)?([a-z_]+\.py)', rule
        )
        if m and m.group(1) in PATH_WRITERS:
            seen.add(m.group(1))
            assert rule.startswith("Bash(AGAMI_SCRATCH_ONLY=1 bash "), rule
    assert seen == PATH_WRITERS
    assert not any("check_statement.py" in r for r in BASH_RULES)
    assert not any("promote_credentials.py *" in r for r in BASH_RULES)


def test_the_skill_runs_every_path_writer_under_the_guard():
    body = SKILL.read_text(encoding="utf-8").split("\n---\n", 1)[1]
    for line in body.splitlines():
        for script in PATH_WRITERS:
            call = re.search(
                r'bash "\$\{CLAUDE_PLUGIN_ROOT\}/scripts/py" "?\S*' + re.escape(script), line
            )
            if call:
                assert line[: call.start()].endswith("AGAMI_SCRATCH_ONLY=1 "), line


def test_the_skills_own_commands_are_still_approved():
    commands = [
        f"{SM} steps {ART}/acme --mark build=in_progress",
        f"{SM} progress {ART}/acme --wait 20",
        f"{SM} batch-id",
        f"AGAMI_SCRATCH_ONLY=1 {SM} curate {ART}/acme --ops-file {ART}/local/tmp/agami-a.json",
        f"AGAMI_SQL_MAX_ROWS=200000 AGAMI_SCRATCH_ONLY=1 {SM} introspect --profile acme "
        f"--db-type sqlite --artifacts {ART} --tables-file {ART}/local/tmp/agami-keep.txt",
        f"{PY} connect_resolve.py --db-type sqlite --profile agami-example",
        f'AGAMI_SCRATCH_ONLY=1 {PY} "{ROOT}/samples/store/build_sample.py" --out "{ART}/local/samples/store.db"',
        f"AGAMI_SCRATCH_ONLY=1 {PY} serve_page.py {ART}/local/tmp/agami-q-1.html "
        f"--decisions {ART}/local/tmp/agami-page-1.json --minutes 30",
        f"{PY} promote_credentials.py",
        f"{SM} init-local {ART}",
        f"{SM} init-local {ART} --sample",
        f"{SM} set-config {ART} --tier cli --active-profile acme --python3 /usr/bin/python3",
    ]
    for c in commands:
        assert _approved(c), c


def test_the_rules_are_explained_where_they_are_listed():
    head = SKILL.read_text(encoding="utf-8").split("\n---\n", 1)[0]
    for word in ("sm install", "py -c", "AGAMI_SCRATCH_ONLY=1", "BASH_ENV"):
        assert word in head, word


def test_every_sm_verb_the_skill_names_has_a_rule_or_asks_on_purpose():
    # A live run hit three prompts on `suggest-metrics` and `suggest-units`: the skill names them
    # only as `sm <verb>` in prose, and the rule list had been drawn from the full commands.
    body = SKILL.read_text(encoding="utf-8").split("\n---\n", 1)[1]
    named = set(re.findall(r'(?:scripts/sm" |`sm )([a-z][a-z-]+)', body))
    ruled = {m for r in BASH_RULES for m in re.findall(r'scripts/sm" ([a-z-]+)', r)}
    assert named - ruled - NEVER == set()
