"""The PreToolUse hook that approves a few read-only agami calls and agami's scratch files.

It grants permission in every session, turn and subagent, so it approves only what is harmless even
if a hostile document is steering the model, and the refusals are tested harder than the approvals:
anything but the table's exact shapes prints nothing (the normal prompt then applies). Each case
drives the script as Claude Code does, with the event as JSON on stdin.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from skill_docs import PLUGIN, SKILLS_DIR

HOOK = PLUGIN / "scripts" / "approve_own_calls.py"
SM = str(PLUGIN / "scripts" / "sm")
PY = str(PLUGIN / "scripts" / "py")
_spec = importlib.util.spec_from_file_location("approve_own_calls_under_test", HOOK)
HOOK_MOD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(HOOK_MOD)
# The package from this checkout, not an older one installed elsewhere: put at collection time,
# before the suite's fixtures import it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))
SYMLINKS = pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32",
                              reason="symlinks")


@pytest.fixture
def art(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    (root / "local" / "tmp").mkdir(parents=True)
    (root / "acme").mkdir()
    return root


def _run(event: object, art: Path, raw: bytes | None = None,
         env: dict | None = None) -> subprocess.CompletedProcess:
    env = env or {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art), "HOME": str(art.parent)}
    data = raw if raw is not None else json.dumps(event).encode("utf-8")
    return subprocess.run([sys.executable, "-I", str(HOOK)], input=data, capture_output=True,
                          env=env, timeout=30)


class _Stdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def _in_process(event: object, env: dict) -> str:
    """The hook's main() in this process, with stdin and the environment swapped in: the same
    JSON-in, JSON-out contract as a subprocess, and its lines are traced for coverage. A few tests
    below still start it as a real process, the way Claude Code does."""
    saved_env, saved_in, saved_out = dict(os.environ), sys.stdin, sys.stdout
    os.environ.clear()
    os.environ.update(env)
    sys.stdin = _Stdin(json.dumps(event).encode("utf-8"))
    sys.stdout = io.StringIO()
    try:
        assert HOOK_MOD.main() == 0
        return sys.stdout.getvalue()
    finally:
        out = sys.stdout
        sys.stdin, sys.stdout = saved_in, saved_out
        os.environ.clear()
        os.environ.update(saved_env)
        del out


def _decision(event: object, art: Path, env: dict | None = None) -> str | None:
    env = env or {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art), "HOME": str(art.parent)}
    out = _in_process(event, env)
    if not out.strip():
        return None
    spec = json.loads(out)["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "allow"  # the only decision it ever makes
    assert spec["permissionDecisionReason"].startswith("agami: ")
    return spec["permissionDecision"]


def _bash(command: str, **extra: object) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": command, **extra}, "cwd": "/"}


def _file(tool: str, path: str, **extra: object) -> dict:
    tool_input: dict = {"file_path": path, **extra}
    if tool == "Write":
        tool_input.setdefault("content", "{}")
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}


def _sm(art: Path, args: str, prefix: str = "bash ") -> str:
    """`bash "<sm>" <args>` with {R} for the model root and {T} for the scratch folder."""
    filled = args.format(R=str(art / "acme"), T=str(art / "local" / "tmp"), A=str(art))
    return f'{prefix}"{SM}" {filled}'


# --- A. approved sm forms ----------------------------------------------------------------------------

APPROVED = [
    "steps {R}",
    "steps {R} --mark connect=in_progress",
    "steps {R} --mark connect=done --mark reports=skipped --mark build=in_progress",
    "steps {R} --full",
    "steps --full {R} --mark describe=done",
    "progress {R}",
    "progress {R} --wait 20",
    "progress {R} --wait 0",
    "progress {R} --wait 600",
    "timing {R}",
    "summary {R}",
    "summary {R} --json",
    "overview {R}",
    "areas {R}",
    "coverage {R}",
    "review-queue {R}",
    "review-items {R}",
    "review-items {R} --scope preseed",
    "model-tree {R}",
    "model-tree {R} --out {T}/agami-tree.json",
    "bundle {R} --area sales",
    "bundle {R} --area agami-example --out {T}/agami-examples-1-sales.input.json",
    "batch-id",
    "wait-files --file {T}/agami-a.result.json",
    "wait-files --file {T}/agami-a.json --file {T}/agami-b.json --wait 60",
]


@pytest.mark.parametrize("args", APPROVED)
def test_an_approved_form_is_allowed(art, args):
    assert _decision(_bash(_sm(art, args)), art) == "allow"


@pytest.mark.parametrize("prefix", ["bash ", ""])
def test_with_or_without_bash_and_quotes(art, prefix):
    root = art / "acme"
    for command in (f'{prefix}"{SM}" steps "{root}"', f"{prefix}{SM} steps {root}",
                    f"{prefix}'{SM}' steps '{root}'"):
        assert _decision(_bash(command), art) == "allow", command


def test_an_escaped_ordinary_character_is_read_as_the_shell_reads_it(art):
    # `\a` is `a` to the shell, so the root is the same model folder
    assert _decision(_bash(f'bash "{SM}" steps {art}/\\acme'), art) == "allow"


def test_with_no_setting_the_default_artifacts_folder_is_used(tmp_path):
    art = tmp_path / "agami-artifacts"
    (art / "acme").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k != "AGAMI_ARTIFACTS_DIR"}
    env["HOME"] = env["USERPROFILE"] = str(tmp_path)  # no ~/.config/agami/path here
    assert _decision(_bash(f'bash "{SM}" steps {art / "acme"}'), art, env=env) == "allow"


def test_a_model_root_that_does_not_exist_yet_is_allowed(art):
    # onboarding marks its first step before the build creates the folder
    assert _decision(_bash(f'bash "{SM}" steps {art / "newdb"} --mark connect=in_progress'),
                     art) == "allow"


@pytest.mark.parametrize("args", ["steps {R}", "steps {R} --mark describe=in_progress",
                                  "steps {R} --full", "progress {R}", "progress {R} --wait 0"])
def test_an_approved_steps_or_progress_call_writes_only_inside_introspect(art, args):
    # `steps` and `progress` keep the list they last printed in <root>/.introspect/steps.last; the
    # hook approves them because that stays inside the model's own .introspect/ folder.
    pytest.importorskip("pydantic")
    from semantic_model import cli

    assert _decision(_bash(_sm(art, args)), art) == "allow"
    shutil.rmtree(art / "acme")
    shutil.copytree(PLUGIN / "samples" / "store" / "model", art / "acme")

    def snapshot() -> dict[Path, bytes]:
        return {p: p.read_bytes() for p in art.parent.rglob("*") if p.is_file()}

    before = snapshot()
    filled = args.format(R=str(art / "acme")).split()
    assert cli.main(filled) == 0
    after = snapshot()
    touched = {p for p in after if before.get(p) != after[p]} | (set(before) - set(after))
    assert touched and all(p.parent == art / "acme" / ".introspect" for p in touched), touched
    assert (art / "acme" / ".introspect" / "steps.last").is_file()


def test_the_description_field_is_fine(art):
    assert _decision(_bash(_sm(art, "steps {R}"), description="Show the steps"), art) == "allow"


# --- A. refused sm forms: the reviewer's inputs and more ----------------------------------------------

REFUSED = {
    # abbreviated or `=` options, on every verb that has --out
    "abbrev --o": "org-draft {R} --o {A}/../.zshrc",
    "abbrev --o model-tree": "model-tree {R} --o {T}/agami-x.json",
    "abbrev --ou model-tree": "model-tree {R} --ou {T}/agami-x.json",
    "abbrev --o= model-tree": "model-tree {R} --o={T}/agami-x.json",
    "abbrev --ou= model-tree": "model-tree {R} --ou={T}/agami-x.json",
    "--out= model-tree": "model-tree {R} --out={T}/agami-x.json",
    "abbrev --o bundle": "bundle {R} --area sales --o {T}/agami-x.json",
    "abbrev --ou bundle": "bundle {R} --area sales --ou {T}/agami-x.json",
    "abbrev --o= bundle": "bundle {R} --area sales --o={T}/agami-x.json",
    "abbrev --ar bundle": "bundle {R} --ar sales",
    "abbrev --f wait-files": "wait-files --f {T}/agami-a.json",
    "abbrev --m steps": "steps {R} --m build=done",
    "abbrev --fu steps": "steps {R} --fu",
    "abbrev --wa progress": "progress {R} --wa 5",
    # --out outside scratch
    "--out home": "model-tree {R} --out {A}/../.zshrc",
    "--out new dirs": "model-tree {R} --out {T}/new/dirs/agami-settings.json",
    "--out not agami-": "model-tree {R} --out {T}/settings.json",
    "--out local": "model-tree {R} --out {A}/local/agami-x.json",
    "--out model": "bundle {R} --area sales --out {R}/agami-x.json",
    "--out relative": "model-tree {R} --out agami-x.json",
    "--out missing value": "model-tree {R} --out",
    "--out twice": "model-tree {R} --out {T}/agami-a.json --out {T}/agami-b.json",
    # roots
    "root outside artifacts": "steps /tmp/elsewhere",
    "root outside, mark": "steps /tmp/elsewhere --mark build=done",
    "root is artifacts": "steps {A}",
    "root is local": "steps {A}/local",
    "root is Local": "steps {A}/Local",
    "root under local": "steps {A}/local/tmp",
    "root nested": "steps {R}/sub",
    "root hidden": "steps {A}/.git",
    "root 'local '": "steps '{A}/local '",
    "root local.": "steps {A}/local.",
    "root full-width local": "steps {A}/\uff4cocal",
    "root with a dot": "steps {A}/acme.yaml",
    "root leading dash": "steps {A}/-x",
    "root leading underscore": "steps {A}/_x",
    "root nbsp": "steps '{A}/ac\u00a0me'",
    "root space": "steps '{A}/ac me'",
    "root LOCAL": "steps {A}/LOCAL",
    "root dotdot": "steps {R}/../acme",
    "root relative": "steps acme",
    "root tilde": "steps ~/agami-artifacts/acme",
    "root missing": "steps",
    "two roots": "steps {R} {R}",
    # option values
    "mark bad status": "steps {R} --mark build=finished",
    "mark bad id": "steps {R} --mark Build=done",
    "mark no value": "steps {R} --mark",
    "mark option value": "steps {R} --mark --full",
    "full twice": "steps {R} --full --full",
    "wait negative": "progress {R} --wait -1",
    "wait float": "progress {R} --wait 2.5",
    "wait too long": "progress {R} --wait 601",
    "wait twice": "progress {R} --wait 1 --wait 2",
    "scope bad": "review-items {R} --scope everything",
    "area slash": "bundle {R} --area ../x",
    "area dots": "bundle {R} --area ..",
    "area missing": "bundle {R}",
    "area twice": "bundle {R} --area a --area b",
    "wait-files outside": "wait-files --file /tmp/agami-a.json",
    "wait-files none": "wait-files --wait 5",
    "batch-id arg": "batch-id {R}",
    "unknown option": "steps {R} --verbose",
    "double dash": "steps -- {R}",
    "help": "steps {R} -h",
    "json on steps": "steps {R} --json",
    # verbs that are not approved
    "describe-file": "describe-file {R} --file /etc/passwd",
    "curate": "curate {R} --ops-file {T}/agami-ops.json",
    "claims": "claims {R} --sql-file /etc/passwd --against-sql-file /etc/hosts",
    "introspect": "introspect --profile prod --db-type postgres --artifacts /tmp/x --progress /tmp/p",
    "seed-examples": "seed-examples {R} --profile prod --file {T}/agami-seeds.json",
    "seed-validate": "seed-validate {R} --profile prod",
    "install": "install",
    "bare sm": "",
    "org-draft": "org-draft {R}",
    "enrich-input": "enrich-input {R} --chunks",
    "apply-enrichment": "apply-enrichment {R} --file {T}/agami-x.json --area sales",
    "context": "context {R} --tables orders",
    "unknown verb": "frobnicate {R}",
}


@pytest.mark.parametrize("args", REFUSED.values(), ids=REFUSED.keys())
def test_a_refused_form_says_nothing(art, args):
    assert _decision(_bash(_sm(art, args)), art) is None


SHELL_REFUSED = [
    # chained, piped, redirected, substituted; with and without spaces around the operator
    '{L} steps {R};id', '{L} steps {R}|id', '{L} steps {R}&id', '{L} steps {R}>y', '{L} steps {R}<y',
    '{L} steps {R}; rm -rf x', '{L} steps {R} | grep text', '{L} steps {R} && echo done',
    '{L} steps {R} & echo bg', '{L} steps {R} || true', '{L} steps {R} > out.json',
    '{L} steps {R} 2>/dev/null', '{L} steps < /etc/passwd', '{L} steps "$(whoami)"',
    '{L} steps `whoami`', '{L} steps {R}\nrm -rf x', '{L} steps {R}\rrm -rf x',
    # variables in any quoting the shell still expands
    '{L} steps $HOME', '{L} steps "$HOME"', '{L} steps "${{HOME}}"', '{L} steps "a\\$b"',
    # env prefixes are not approved any more
    'AGAMI_SQL_MAX_ROWS=50 {L} steps {R}', 'AGAMI_PROFILE=acme {L} steps {R}',
    'A=1; {L} steps {R}', 'A=1 {L} steps {R}', 'PATH=/tmp {L} steps {R}',
    # other runners
    'env {L} steps {R}', 'sudo {L} steps {R}', 'bash -x "{S}" steps {R}', 'sh "{S}" steps {R}',
    'bash bash "{S}" steps {R}', 'exec "{S}" steps {R}', 'bash -c "{S} steps {R}"',
    # grouping, globbing, comments
    '( {L} steps {R} )', '{L} steps {R}*', '{L} steps {R} # c', '{L} steps "unclosed',
    "{L} steps 'unclosed", '{L} steps {R}\\ x', '{L} steps {R}\\', '{L} steps {R}\\$x',
]


@pytest.mark.parametrize("template", SHELL_REFUSED)
def test_shell_syntax_says_nothing(art, template):
    command = template.format(L=f'bash "{SM}"', S=SM, R=str(art / "acme"))
    assert _decision(_bash(command), art) is None


@pytest.mark.parametrize("launcher", [
    f"{PLUGIN}/scripts/py",
    f"{PLUGIN}/scripts/connect_helper.py",
    f"{PLUGIN}/scripts/_python.sh",
    f"{PLUGIN}-evil/scripts/sm",
    f"{PLUGIN}/scripts/sm-evil",
    f"{PLUGIN}/scripts/../scripts/sm",
    f"{PLUGIN}/scripts/./sm",
    f"{PLUGIN}//scripts/sm",
    f"{PLUGIN}/scripts/SM",
    "scripts/sm",
    "~/agami/scripts/sm",
])
def test_anything_but_this_plugins_sm_says_nothing(art, launcher):
    assert _decision(_bash(f'bash "{launcher}" steps {art / "acme"}'), art) is None


@pytest.mark.parametrize("args", ["connect_resolve.py --db-type postgres", "-c 'import os'",
                                  "-m execute_sql --profile acme", "setup_desktop_mcp.py --dry-run",
                                  "prepare_deploy.py --target /tmp/x", "serve_page.py x --open"])
def test_no_py_call_is_approved(art, args):
    assert _decision(_bash(f'bash "{PY}" {args}'), art) is None


def test_the_forbidden_set_is_exactly_this():
    assert set(HOOK_MOD.FORBIDDEN_ANYWHERE) == {";", "&", "|", "`", "$(", "<", ">", "\n", "\r"}


@pytest.mark.parametrize("seq", sorted(HOOK_MOD.FORBIDDEN_ANYWHERE))
def test_each_forbidden_sequence_alone_refuses_a_call(seq, monkeypatch):
    """Mutation guard: with every later check made to pass, a command holding `seq` inside single
    quotes (which the tokenizer accepts whatever they hold) is refused by FORBIDDEN_ANYWHERE alone,
    and allowed once `seq` is dropped from it. So no entry can be removed with the suite still green."""
    monkeypatch.setattr(HOOK_MOD, "args_allowed", lambda args: True)
    command = f"bash '{SM}' 'x{seq}y'"
    assert HOOK_MOD.bash_allowed(command) is False
    monkeypatch.setattr(HOOK_MOD, "FORBIDDEN_ANYWHERE",
                        tuple(c for c in HOOK_MOD.FORBIDDEN_ANYWHERE if c != seq))
    assert HOOK_MOD.bash_allowed(command) is True


def test_a_copy_or_link_of_the_launcher_says_nothing(art, tmp_path):
    copy = tmp_path / "elsewhere" / "scripts"
    copy.mkdir(parents=True)
    shutil.copy(SM, copy / "sm")
    assert _decision(_bash(f'bash "{copy / "sm"}" steps {art / "acme"}'), art) is None
    if hasattr(os, "symlink") and sys.platform != "win32":
        (tmp_path / "sm").symlink_to(SM)
        assert _decision(_bash(f'bash "{tmp_path / "sm"}" steps {art / "acme"}'), art) is None


@pytest.mark.parametrize("extra", [{"run_in_background": True}, {"timeout": 600000},
                                   {"run_in_background": False}, {"dangerouslyDisableSandbox": True}])
def test_a_bash_field_beyond_command_and_description_says_nothing(art, extra):
    assert _decision(_bash(_sm(art, "steps {R}"), **extra), art) is None


@SYMLINKS
def test_a_root_that_is_a_link_out_says_nothing(art, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (art / "linked").symlink_to(outside)
    assert _decision(_bash(f'bash "{SM}" steps {art / "linked"} --mark build=done'), art) is None
    (art / "tolocal").symlink_to(art / "local")
    assert _decision(_bash(f'bash "{SM}" steps {art / "tolocal"}'), art) is None


def test_no_artifacts_folder_says_nothing(tmp_path):
    art = tmp_path / "nothing-here"
    assert _decision(_bash(f'bash "{SM}" steps {art / "acme"}'), art) is None


def test_a_relative_artifacts_setting_says_nothing(art):
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": "artifacts", "HOME": str(art.parent)}
    assert _decision(_bash(f'bash "{SM}" steps {art / "acme"}'), art, env=env) is None


def test_every_other_cli_verb_is_refused():
    """A verb added to cli.py later is not approved by accident: a plain call of every verb the
    hook's table doesn't list says nothing, and the table names only real verbs."""
    pytest.importorskip("pydantic")
    # In a child, so this checkout's cli.py is the one read, whatever another test imported first.
    src = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
    probe = ("import argparse, json, sys; sys.path.insert(0, sys.argv[1]); "
             "from semantic_model import cli; "
             "sub = next(a for a in cli.build_parser()._actions "
             "if isinstance(a, argparse._SubParsersAction)); "
             "print(json.dumps(sorted(sub.choices)))")
    verbs = set(json.loads(subprocess.run([sys.executable, "-c", probe, str(src)], check=True,
                                          capture_output=True, text=True).stdout))
    table = set(HOOK_MOD.VERBS)
    assert table <= verbs
    others = sorted(verbs - table)
    assert {"describe-file", "curate", "introspect", "seed-examples", "claims", "org-draft"} <= set(others)
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        art = Path(d) / "artifacts"
        (art / "local" / "tmp").mkdir(parents=True)
        (art / "acme").mkdir()
        for verb in [*others, "install"]:
            assert _decision(_bash(f'bash "{SM}" {verb} {art / "acme"}'), art) is None, verb
            assert _decision(_bash(f'bash "{SM}" {verb}'), art) is None, verb


# --- B. Write / Read of agami's scratch files ----------------------------------------------------------


@pytest.mark.parametrize("name", ["agami-x.json", "agami-enrich-20261112-ab12-sales-1.result.json",
                                  "agami-notes.md", "agami-cols.tsv", "agami-a_b.txt"])
@pytest.mark.parametrize("tool", ["Write", "Read"])
def test_a_scratch_file_is_allowed(art, tool, name):
    assert _decision(_file(tool, str(art / "local" / "tmp" / name)), art) == "allow"


def test_an_existing_scratch_file_is_allowed(art):
    target = art / "local" / "tmp" / "agami-x.json"
    target.write_text("{}")
    assert _decision(_file("Write", str(target)), art) == "allow"
    assert _decision(_file("Read", str(target), offset=1, limit=10), art) == "allow"


def test_the_scratch_folder_comes_from_the_pointer_file(tmp_path):
    other = tmp_path / "chosen"
    (other / "local" / "tmp").mkdir(parents=True)
    (tmp_path / ".config" / "agami").mkdir(parents=True)
    (tmp_path / ".config" / "agami" / "path").write_text(f"{other}\n")
    env = {k: v for k, v in os.environ.items() if k != "AGAMI_ARTIFACTS_DIR"}
    env["HOME"] = env["USERPROFILE"] = str(tmp_path)
    event = _file("Write", str(other / "local" / "tmp" / "agami-x.json"))
    assert _decision(event, other, env=env) == "allow"


@pytest.mark.parametrize("rel", [
    "local/tmp/../../x.json", "local/tmp/../agami-x.json", "local/tmp/./agami-x.json",
    "local/tmp/sub/agami-x.json", "local/tmp/x.json", "local/tmp/agami-.json",
    "local/tmp/agami-x.py", "local/tmp/agami-x.json.sh", "local/tmp/agami-x",
    "local/tmp/AGAMI-x.json", "local/tmp/Agami-x.json", "local/tmp/agami-x.JSON",
    "local/tmp/agami-x y.json", "local/tmp/agami-x;y.json", "local/tmp", "local/agami-x.json",
    "local/credentials", "local/.config", "agami-x.json",
])
@pytest.mark.parametrize("tool", ["Write", "Read"])
def test_other_artifact_paths_say_nothing(art, tool, rel):
    assert _decision(_file(tool, str(art) + "/" + rel), art) is None


def test_relative_and_tilde_paths_say_nothing(art):
    for path in ("local/tmp/agami-x.json", "~/agami-artifacts/local/tmp/agami-x.json", ""):
        assert _decision(_file("Write", path), art) is None


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links")
@pytest.mark.parametrize("tool", ["Write", "Read"])
def test_a_hard_linked_scratch_file_says_nothing(art, tmp_path, tool):
    outside = tmp_path / "settings.json"
    outside.write_text("{}")
    target = art / "local" / "tmp" / "agami-x.json"
    os.link(outside, target)
    assert _decision(_file(tool, str(target)), art) is None


@SYMLINKS
def test_a_link_out_of_the_scratch_folder_says_nothing(art, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    link = art / "local" / "tmp" / "agami-link.json"
    link.symlink_to(outside)
    assert _decision(_file("Write", str(link)), art) is None
    assert _decision(_file("Read", str(link)), art) is None
    dangling = art / "local" / "tmp" / "agami-dangling.json"
    dangling.symlink_to(tmp_path / "not-yet.json")
    assert _decision(_file("Write", str(dangling)), art) is None
    folder = art / "local" / "tmp" / "agami-dir.json"
    folder.mkdir()
    assert _decision(_file("Write", str(folder)), art) is None


@SYMLINKS
def test_a_scratch_folder_that_is_itself_a_link_says_nothing(tmp_path):
    art = tmp_path / "artifacts"
    (art / "local").mkdir(parents=True)
    target = tmp_path / "somewhere"
    target.mkdir()
    (art / "local" / "tmp").symlink_to(target)
    assert _decision(_file("Write", str(art / "local" / "tmp" / "agami-x.json")), art) is None


def test_no_scratch_folder_says_nothing(tmp_path):
    art = tmp_path / "artifacts"
    art.mkdir()
    assert _decision(_file("Write", str(art / "local" / "tmp" / "agami-x.json")), art) is None


@pytest.mark.parametrize("tool, extra", [("Write", {"mode": "append"}), ("Read", {"pages": "1"})])
def test_an_unknown_file_field_says_nothing(art, tool, extra):
    assert _decision(_file(tool, str(art / "local" / "tmp" / "agami-x.json"), **extra), art) is None


# --- C. Read of the plugin's own files ------------------------------------------------------------------


def test_reading_a_plugin_file_is_allowed(art):
    for rel in ("skills/agami-connect/SKILL.md", "shared/task-list.md", "scripts/sm"):
        assert _decision(_file("Read", str(PLUGIN / rel)), art) == "allow", rel


def test_writing_a_plugin_file_says_nothing(art):
    assert _decision(_file("Write", str(PLUGIN / "skills" / "agami-connect" / "SKILL.md")), art) is None


@pytest.mark.parametrize("path", [
    f"{PLUGIN}-evil/skills/x.md", f"{PLUGIN}/../agami/skills/agami-connect/SKILL.md",
    f"{PLUGIN}/skills/../../../README.md", f"{PLUGIN}", f"{PLUGIN}/skills",
    f"{PLUGIN}/no-such-file.md", "/etc/passwd",
])
def test_reading_outside_the_plugin_says_nothing(art, path):
    assert _decision(_file("Read", path), art) is None


@SYMLINKS
def test_a_link_inside_the_plugin_pointing_out_says_nothing(art, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    link = PLUGIN / "shared" / "zz-test-link.md"
    link.symlink_to(outside)
    try:
        assert _decision(_file("Read", str(link)), art) is None
    finally:
        link.unlink()


# --- malformed input --------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [b"not json", b"\xff", b"x" * (64 * 1024 * 1024 + 1)])
def test_main_in_process_is_silent_on_bad_or_huge_input(raw):
    saved_in, saved_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = _Stdin(raw), io.StringIO()
    try:
        assert HOOK_MOD.main() == 0
        printed = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = saved_in, saved_out
    assert printed == ""


def test_a_real_process_gives_the_same_answers(art):
    allowed = _run(_bash(_sm(art, "steps {R}")), art)
    assert allowed.returncode == 0
    assert json.loads(allowed.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    refused = _run(_bash(_sm(art, "steps {R} --o x")), art)
    assert refused.returncode == 0 and refused.stdout == b""


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", b"null", b'"text"', b"\xff\xfe",
                                 b'{"tool_name": "Bash"}', b'{"hook_event_name": "PreToolUse"}'])
def test_malformed_stdin_prints_nothing_and_exits_0(art, raw):
    proc = _run(None, art, raw=raw)
    assert proc.returncode == 0 and proc.stdout == b""


@pytest.mark.parametrize("event", [
    {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": f"bash {SM} batch-id"}},
    {"tool_name": "Bash", "tool_input": {"command": f"bash {SM} batch-id"}},
    {"hook_event_name": "PreToolUse", "tool_name": "Edit",
     "tool_input": {"file_path": "/x/local/tmp/agami-x.json"}},
    {"hook_event_name": "PreToolUse", "tool_name": "bash", "tool_input": {"command": f"bash {SM} batch-id"}},
    {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": f"bash {SM} batch-id"},
    {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": None}},
])
def test_other_events_and_tools_say_nothing(art, event):
    assert _decision(event, art) is None


def test_the_hook_imports_nothing_of_the_plugin_or_the_package():
    imports = re.findall(r"^\s*(?:from\s+(\S+)\s+import|import\s+(\S+))", HOOK.read_text(encoding="utf-8"),
                         re.MULTILINE)
    modules = {(a or b).split(".")[0] for a, b in imports}
    assert modules <= {"__future__", "json", "ntpath", "os", "re", "stat", "sys", "typing"}, modules
    assert "spec_from_file_location" not in HOOK.read_text(encoding="utf-8")


# --- hooks.json, and agreement with allowed-tools ---------------------------------------------------------


def test_hooks_json_runs_python3_from_path_never_the_plugin_launcher():
    doc = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    # the second entry is say_gate.py on AskUserQuestion (tests/test_say_gate.py)
    entry = doc["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash|Write|Read"
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    assert hook["command"] == (
        "in=$(cat); for p in python3 python; do "
        "out=$(printf '%s' \"$in\" | \"$p\" -I \"${CLAUDE_PLUGIN_ROOT}/scripts/approve_own_calls.py\" "
        "2>/dev/null) && { printf '%s' \"$out\"; break; }; done; true")
    assert "scripts/py" not in hook["command"] and ".config" not in hook["command"]


@pytest.mark.skipif(shutil.which("sh") is None or sys.platform == "win32", reason="needs sh")
def test_the_hooks_json_command_works_and_never_exits_2(art, tmp_path):
    """As Claude Code runs it (`sh -c`). Exit 2 would block every tool call, so a broken path must
    end in `true`, not in Python's own exit 2 for a script it can't open."""
    doc = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command = doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art), "HOME": str(art.parent)}
    event = json.dumps(_bash(_sm(art, "steps {R}"))).encode()
    proc = subprocess.run(["sh", "-c", command.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))],
                          input=event, capture_output=True, env=env, timeout=60)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    broken = subprocess.run(["sh", "-c", command.replace("${CLAUDE_PLUGIN_ROOT}", str(tmp_path / "gone"))],
                            input=event, capture_output=True, env=env, timeout=60)
    assert broken.returncode == 0 and broken.stdout == b""


@pytest.mark.skipif(shutil.which("sh") is None or sys.platform == "win32", reason="needs sh")
@pytest.mark.parametrize("stub_exit", [1, 2, 9009 % 256])
def test_a_failing_python3_stub_prints_nothing_and_python_answers(art, tmp_path, stub_exit):
    """A `python3` that fails (the Windows Store stub, a broken install) after printing must not
    reach the hook's stdout, and the event must still reach `python` intact."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "python3"
    stub.write_text(f'#!/bin/sh\ncat >/dev/null\necho \'{{"hookSpecificOutput": "garbage"}}\'\n'
                    f"exit {stub_exit}\n")
    stub.chmod(0o755)
    real = bin_dir / "python"
    real.symlink_to(sys.executable)
    doc = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command = doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"].replace(
        "${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art), "HOME": str(art.parent),
           "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    allowed = subprocess.run(["sh", "-c", command], input=json.dumps(_bash(_sm(art, "steps {R}"))).encode(),
                             capture_output=True, env=env, timeout=60)
    assert allowed.returncode == 0
    assert json.loads(allowed.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    refused = subprocess.run(["sh", "-c", command], input=json.dumps(_bash("rm -rf /")).encode(),
                             capture_output=True, env=env, timeout=60)
    assert refused.returncode == 0 and refused.stdout == b""
    (bin_dir / "python").unlink()
    (bin_dir / "python").write_text(stub.read_text())
    (bin_dir / "python").chmod(0o755)
    neither = subprocess.run(["sh", "-c", command], input=json.dumps(_bash(_sm(art, "steps {R}"))).encode(),
                             capture_output=True, env=env, timeout=60)
    assert neither.returncode == 0 and neither.stdout == b""


def _allowed_tools(skill: str) -> list[str]:
    head = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8").split("\n---\n", 1)[0]
    return yaml.safe_load(head.removeprefix("---\n"))["allowed-tools"]


def test_every_form_the_hook_approves_is_one_allowed_tools_already_covers(art):
    """The hook's forms are a subset of what agami-connect's allowed-tools cover, so it widens when
    an approval applies (every turn, every subagent), never what is approved. allowed-tools stays:
    it still helps where hooks don't run."""
    rules = [r[len("Bash("):-1] for r in _allowed_tools("agami-connect") if r.startswith("Bash(")]
    patterns = [re.compile(re.escape(r.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN)))
                           .replace(r"\*", ".*") + r"\Z") for r in rules]
    forms = [f'bash "{SM}" steps {art / "acme"}', f"bash {SM} steps {art / 'acme'}",
             f'"{SM}" steps {art / "acme"}', f"{SM} steps {art / 'acme'}"]
    for form in forms:
        assert _decision(_bash(form), art) == "allow", form
        assert any(p.match(form) for p in patterns), form
    assert "Read(/${CLAUDE_PLUGIN_ROOT}/**)" in _allowed_tools("agami-connect")


@pytest.mark.parametrize("name", ["say-pending.json", "agami-say-pending.json"])
@pytest.mark.parametrize("tool", ["Write", "Read"])
def test_the_say_gates_pending_file_is_never_approved(art, tool, name):
    """A subagent with Write could otherwise plant text the say gate then has the assistant post
    verbatim. Excluded by name, so a wider scratch pattern later still leaves it out."""
    assert _decision(_file(tool, str(art / "local" / "tmp" / name)), art) is None
    assert name in HOOK_MOD.NEVER_APPROVED
