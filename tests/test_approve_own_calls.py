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

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "agami"
HOOK = PLUGIN / "scripts" / "approve_own_calls.py"
SM = str(PLUGIN / "scripts" / "sm")
PY = str(PLUGIN / "scripts" / "py")
_spec = importlib.util.spec_from_file_location("approve_own_calls_under_test", HOOK)
HOOK_MOD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(HOOK_MOD)
# The package from this checkout, not an older one installed elsewhere: put at collection time,
# before the suite's fixtures import it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))
SYMLINKS = pytest.mark.skipif(
    not hasattr(os, "symlink") or sys.platform == "win32", reason="symlinks"
)


@pytest.fixture
def art(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    (root / "local" / "tmp").mkdir(parents=True)
    (root / "acme").mkdir()
    return root


def _run(
    event: object, art: Path, raw: bytes | None = None, env: dict | None = None
) -> subprocess.CompletedProcess:
    env = env or {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art), "HOME": str(art.parent)}
    data = raw if raw is not None else json.dumps(event).encode("utf-8")
    return subprocess.run(
        [sys.executable, "-I", str(HOOK)], input=data, capture_output=True, env=env, timeout=30
    )


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
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command, **extra},
        "cwd": "/",
    }


def _file(tool: str, path: str, **extra: object) -> dict:
    tool_input: dict = {"file_path": path, **extra}
    if tool == "Write":
        tool_input.setdefault("content", "{}")
    if tool == "Edit":
        tool_input.setdefault("old_string", "{}")
        tool_input.setdefault("new_string", '{"a": 1}')
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}


def _sm(art: Path, args: str, prefix: str = "bash ") -> str:
    """`bash "<sm>" <args>` with {R} for the model root and {T} for the scratch folder."""
    filled = args.format(R=str(art / "acme"), T=str(art / "local" / "tmp"), A=str(art))
    return f'{prefix}"{SM}" {filled}'


# --- A. approved sm forms ----------------------------------------------------------------------------

APPROVED = [
    "areas {R}",
    "coverage {R}",
    "review-queue {R}",
    "review-items {R}",
    "review-items {R} --scope all",
    "review-items {R} --scope rule1",
    "review-items {R} --scope rule2",
    "review-items {R} --scope preseed",
    "review-items --scope preseed {R}",
    "model-tree {R}",
    "bundle {R} --area sales",
    "bundle --area agami-example {R}",
]


@pytest.mark.parametrize("args", APPROVED)
def test_an_approved_form_is_allowed(art, args):
    assert _decision(_bash(_sm(art, args)), art) == "allow"


@pytest.mark.parametrize("prefix", ["bash ", ""])
def test_with_or_without_bash_and_quotes(art, prefix):
    root = art / "acme"
    for command in (
        f'{prefix}"{SM}" areas "{root}"',
        f"{prefix}{SM} areas {root}",
        f"{prefix}'{SM}' areas '{root}'",
    ):
        assert _decision(_bash(command), art) == "allow", command


def test_an_escaped_ordinary_character_is_read_as_the_shell_reads_it(art):
    # `\a` is `a` to the shell, so the root is the same model folder
    assert _decision(_bash(f'bash "{SM}" areas {art}/\\acme'), art) == "allow"


def test_with_no_setting_the_default_artifacts_folder_is_used(tmp_path):
    art = tmp_path / "agami-artifacts"
    (art / "acme").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k != "AGAMI_ARTIFACTS_DIR"}
    env["HOME"] = env["USERPROFILE"] = str(tmp_path)  # no ~/.config/agami/path here
    assert _decision(_bash(f'bash "{SM}" areas {art / "acme"}'), art, env=env) == "allow"


def test_a_model_root_that_does_not_exist_yet_is_allowed(art):
    # a skill's first `sm areas` is how it learns whether a model exists (exit 3, `no_model`)
    assert _decision(_bash(f'bash "{SM}" areas {art / "newdb"}'), art) == "allow"


@pytest.mark.parametrize("args", APPROVED)
def test_an_approved_call_changes_no_file(art, args):
    """Each approved form, run against a real model, leaves every file under the artifacts folder's
    parent as it was: the verbs only read. A verb that started writing would fail here."""
    pytest.importorskip("pydantic")
    from semantic_model import cli

    assert _decision(_bash(_sm(art, args)), art) == "allow"
    shutil.rmtree(art / "acme")
    shutil.copytree(PLUGIN / "samples" / "store" / "model", art / "acme")
    filled = args.format(R=str(art / "acme")).split()
    if "--area" in filled:  # a subject area the sample model has
        filled[filled.index("--area") + 1] = (
            cli.L.load_datasource(art / "acme").subject_areas[0].name
        )

    def snapshot() -> dict[Path, tuple[bytes, int]]:
        return {
            p: (p.read_bytes(), p.stat().st_mtime_ns) for p in art.parent.rglob("*") if p.is_file()
        }

    before = snapshot()
    assert cli.main(filled) == 0
    assert snapshot() == before


def test_the_description_field_is_fine(art):
    assert _decision(_bash(_sm(art, "areas {R}"), description="Show the steps"), art) == "allow"


# --- A. refused sm forms: the reviewer's inputs and more ----------------------------------------------

REFUSED = {
    # abbreviated or `=` options; main's model-tree and bundle take no --out at all
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
    "abbrev --sc review-items": "review-items {R} --sc all",
    "--scope= review-items": "review-items {R} --scope=all",
    "--area= bundle": "bundle {R} --area=sales",
    # --out, which main's parser doesn't accept, even into scratch
    "--out scratch model-tree": "model-tree {R} --out {T}/agami-tree.json",
    "--out scratch bundle": "bundle {R} --area sales --out {T}/agami-x.json",
    "--out home": "model-tree {R} --out {A}/../.zshrc",
    "--out new dirs": "model-tree {R} --out {T}/new/dirs/agami-settings.json",
    "--out not agami-": "model-tree {R} --out {T}/settings.json",
    "--out local": "model-tree {R} --out {A}/local/agami-x.json",
    "--out model": "bundle {R} --area sales --out {R}/agami-x.json",
    "--out relative": "model-tree {R} --out agami-x.json",
    "--out missing value": "model-tree {R} --out",
    "--out twice": "model-tree {R} --out {T}/agami-a.json --out {T}/agami-b.json",
    # roots
    "root outside artifacts": "areas /tmp/elsewhere",
    "root outside, option": "review-items /tmp/elsewhere --scope all",
    "root is artifacts": "areas {A}",
    "root is local": "areas {A}/local",
    "root is Local": "areas {A}/Local",
    "root under local": "areas {A}/local/tmp",
    "root nested": "areas {R}/sub",
    "root hidden": "areas {A}/.git",
    "root 'local '": "areas '{A}/local '",
    "root local.": "areas {A}/local.",
    "root full-width local": "areas {A}/\uff4cocal",
    "root with a dot": "areas {A}/acme.yaml",
    "root leading dash": "areas {A}/-x",
    "root leading underscore": "areas {A}/_x",
    "root nbsp": "areas '{A}/ac\u00a0me'",
    "root space": "areas '{A}/ac me'",
    "root LOCAL": "areas {A}/LOCAL",
    "root dotdot": "areas {R}/../acme",
    "root relative": "areas acme",
    "root tilde": "areas ~/agami-artifacts/acme",
    "root missing": "areas",
    "two roots": "areas {R} {R}",
    # option values
    "scope bad": "review-items {R} --scope everything",
    "scope case": "review-items {R} --scope ALL",
    "scope no value": "review-items {R} --scope",
    "scope option value": "review-items {R} --scope --scope",
    "scope twice": "review-items {R} --scope all --scope rule1",
    "scope on areas": "areas {R} --scope all",
    "area on model-tree": "model-tree {R} --area sales",
    "area slash": "bundle {R} --area ../x",
    "area dots": "bundle {R} --area ..",
    "area missing": "bundle {R}",
    "area twice": "bundle {R} --area a --area b",
    "area leading dash": "bundle {R} --area -x",
    "unknown option": "areas {R} --verbose",
    "double dash": "areas -- {R}",
    "help": "areas {R} -h",
    "json on areas": "areas {R} --json",
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
    # verbs #662 approved that main does not have (they would only fail in main's parser, but the
    # table names real verbs only, so a verb added under one of these names is reviewed first)
    "steps": "steps {R}",
    "steps mark": "steps {R} --mark connect=in_progress",
    "progress": "progress {R} --wait 20",
    "timing": "timing {R}",
    "summary": "summary {R} --json",
    "overview": "overview {R}",
    "batch-id": "batch-id",
    "wait-files": "wait-files --file {T}/agami-a.json",
}


@pytest.mark.parametrize("args", REFUSED.values(), ids=REFUSED.keys())
def test_a_refused_form_says_nothing(art, args):
    assert _decision(_bash(_sm(art, args)), art) is None


SHELL_REFUSED = [
    # chained, piped, redirected, substituted; with and without spaces around the operator
    "{L} areas {R};id",
    "{L} areas {R}|id",
    "{L} areas {R}&id",
    "{L} areas {R}>y",
    "{L} areas {R}<y",
    "{L} areas {R}; rm -rf x",
    "{L} areas {R} | grep text",
    "{L} areas {R} && echo done",
    "{L} areas {R} & echo bg",
    "{L} areas {R} || true",
    "{L} areas {R} > out.json",
    "{L} areas {R} 2>/dev/null",
    "{L} areas < /etc/passwd",
    '{L} areas "$(whoami)"',
    "{L} areas `whoami`",
    "{L} areas {R}\nrm -rf x",
    "{L} areas {R}\rrm -rf x",
    # variables in any quoting the shell still expands
    "{L} areas $HOME",
    '{L} areas "$HOME"',
    '{L} areas "${{HOME}}"',
    '{L} areas "a\\$b"',
    # env prefixes are not approved any more
    "AGAMI_SQL_MAX_ROWS=50 {L} areas {R}",
    "AGAMI_PROFILE=acme {L} areas {R}",
    "A=1; {L} areas {R}",
    "A=1 {L} areas {R}",
    "PATH=/tmp {L} areas {R}",
    # other runners
    "env {L} areas {R}",
    "sudo {L} areas {R}",
    'bash -x "{S}" areas {R}',
    'sh "{S}" areas {R}',
    'bash bash "{S}" areas {R}',
    'exec "{S}" areas {R}',
    'bash -c "{S} areas {R}"',
    # grouping, globbing, comments
    "( {L} areas {R} )",
    "{L} areas {R}*",
    "{L} areas {R} # c",
    '{L} areas "unclosed',
    "{L} areas 'unclosed",
    "{L} areas {R}\\ x",
    "{L} areas {R}\\",
    "{L} areas {R}\\$x",
]


@pytest.mark.parametrize("template", SHELL_REFUSED)
def test_shell_syntax_says_nothing(art, template):
    command = template.format(L=f'bash "{SM}"', S=SM, R=str(art / "acme"))
    assert _decision(_bash(command), art) is None


@pytest.mark.parametrize(
    "launcher",
    [
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
    ],
)
def test_anything_but_this_plugins_sm_says_nothing(art, launcher):
    assert _decision(_bash(f'bash "{launcher}" areas {art / "acme"}'), art) is None


@pytest.mark.parametrize(
    "args",
    [
        "connect_resolve.py --db-type postgres",
        "-c 'import os'",
        "-m execute_sql --profile acme",
        "setup_desktop_mcp.py --dry-run",
        "prepare_deploy.py --target /tmp/x",
        "serve_page.py x --open",
    ],
)
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
    monkeypatch.setattr(
        HOOK_MOD, "FORBIDDEN_ANYWHERE", tuple(c for c in HOOK_MOD.FORBIDDEN_ANYWHERE if c != seq)
    )
    assert HOOK_MOD.bash_allowed(command) is True


def test_a_copy_or_link_of_the_launcher_says_nothing(art, tmp_path):
    copy = tmp_path / "elsewhere" / "scripts"
    copy.mkdir(parents=True)
    shutil.copy(SM, copy / "sm")
    assert _decision(_bash(f'bash "{copy / "sm"}" areas {art / "acme"}'), art) is None
    if hasattr(os, "symlink") and sys.platform != "win32":
        (tmp_path / "sm").symlink_to(SM)
        assert _decision(_bash(f'bash "{tmp_path / "sm"}" areas {art / "acme"}'), art) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"run_in_background": True},
        {"timeout": 600000},
        {"run_in_background": False},
        {"dangerouslyDisableSandbox": True},
    ],
)
def test_a_bash_field_beyond_command_and_description_says_nothing(art, extra):
    assert _decision(_bash(_sm(art, "areas {R}"), **extra), art) is None


@SYMLINKS
def test_a_root_that_is_a_link_out_says_nothing(art, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (art / "linked").symlink_to(outside)
    assert _decision(_bash(f'bash "{SM}" areas {art / "linked"}'), art) is None
    (art / "tolocal").symlink_to(art / "local")
    assert _decision(_bash(f'bash "{SM}" areas {art / "tolocal"}'), art) is None


def test_no_artifacts_folder_says_nothing(tmp_path):
    art = tmp_path / "nothing-here"
    assert _decision(_bash(f'bash "{SM}" areas {art / "acme"}'), art) is None


def test_a_relative_artifacts_setting_says_nothing(art):
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": "artifacts", "HOME": str(art.parent)}
    assert _decision(_bash(f'bash "{SM}" areas {art / "acme"}'), art, env=env) is None


def test_every_other_cli_verb_is_refused():
    """A verb added to cli.py later is not approved by accident: a plain call of every verb the
    hook's table doesn't list says nothing, and the table names only real verbs."""
    pytest.importorskip("pydantic")
    # In a child, so this checkout's cli.py is the one read, whatever another test imported first.
    src = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
    probe = (
        "import argparse, json, sys; sys.path.insert(0, sys.argv[1]); "
        "from semantic_model import cli; "
        "sub = next(a for a in cli.build_parser()._actions "
        "if isinstance(a, argparse._SubParsersAction)); "
        "print(json.dumps(sorted(sub.choices)))"
    )
    verbs = set(
        json.loads(
            subprocess.run(
                [sys.executable, "-c", probe, str(src)], check=True, capture_output=True, text=True
            ).stdout
        )
    )
    table = set(HOOK_MOD.VERBS)
    assert table <= verbs
    others = sorted(verbs - table)
    assert {"describe-file", "curate", "introspect", "seed-examples", "claims", "org-draft"} <= set(
        others
    )
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        art = Path(d) / "artifacts"
        (art / "local" / "tmp").mkdir(parents=True)
        (art / "acme").mkdir()
        for verb in [*others, "install"]:
            assert _decision(_bash(f'bash "{SM}" {verb} {art / "acme"}'), art) is None, verb
            assert _decision(_bash(f'bash "{SM}" {verb}'), art) is None, verb


# --- B. Write / Edit / Read of agami's scratch files ----------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "agami-x.json",
        "agami-enrich-20261112-ab12-sales-1.result.json",
        "agami-notes.md",
        "agami-cols.tsv",
        "agami-a_b.txt",
    ],
)
@pytest.mark.parametrize("tool", ["Write", "Read", "Edit"])
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


@pytest.mark.parametrize(
    "rel",
    [
        "local/tmp/../../x.json",
        "local/tmp/../agami-x.json",
        "local/tmp/./agami-x.json",
        "local/tmp/sub/agami-x.json",
        "local/tmp/x.json",
        "local/tmp/agami-.json",
        "local/tmp/agami-x.py",
        "local/tmp/agami-x.json.sh",
        "local/tmp/agami-x",
        "local/tmp/AGAMI-x.json",
        "local/tmp/Agami-x.json",
        "local/tmp/agami-x.JSON",
        "local/tmp/agami-x y.json",
        "local/tmp/agami-x;y.json",
        "local/tmp",
        "local/agami-x.json",
        "local/credentials",
        "local/.config",
        "agami-x.json",
    ],
)
@pytest.mark.parametrize("tool", ["Write", "Read", "Edit"])
def test_other_artifact_paths_say_nothing(art, tool, rel):
    assert _decision(_file(tool, str(art) + "/" + rel), art) is None


def test_relative_and_tilde_paths_say_nothing(art):
    for path in ("local/tmp/agami-x.json", "~/agami-artifacts/local/tmp/agami-x.json", ""):
        assert _decision(_file("Write", path), art) is None


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links")
@pytest.mark.parametrize("tool", ["Write", "Read", "Edit"])
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


@pytest.mark.parametrize(
    "tool, extra",
    [
        ("Write", {"mode": "append"}),
        ("Read", {"pages": "1"}),
        ("Edit", {"edits": []}),
        ("Edit", {"content": "{}"}),
    ],
)
def test_an_unknown_file_field_says_nothing(art, tool, extra):
    assert _decision(_file(tool, str(art / "local" / "tmp" / "agami-x.json"), **extra), art) is None


# --- C. Read of the plugin's own files ------------------------------------------------------------------


def test_reading_a_plugin_file_is_allowed(art):
    for rel in ("skills/agami-connect/SKILL.md", "shared/question-cards.md", "scripts/sm"):
        assert _decision(_file("Read", str(PLUGIN / rel)), art) == "allow", rel


def test_writing_a_plugin_file_says_nothing(art):
    assert (
        _decision(_file("Write", str(PLUGIN / "skills" / "agami-connect" / "SKILL.md")), art)
        is None
    )


@pytest.mark.parametrize(
    "path",
    [
        f"{PLUGIN}-evil/skills/x.md",
        f"{PLUGIN}/../agami/skills/agami-connect/SKILL.md",
        f"{PLUGIN}/skills/../../../README.md",
        f"{PLUGIN}",
        f"{PLUGIN}/skills",
        f"{PLUGIN}/no-such-file.md",
        "/etc/passwd",
    ],
)
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
    allowed = _run(_bash(_sm(art, "areas {R}")), art)
    assert allowed.returncode == 0
    assert json.loads(allowed.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    refused = _run(_bash(_sm(art, "areas {R} --o x")), art)
    assert refused.returncode == 0 and refused.stdout == b""


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[1, 2]",
        b"null",
        b'"text"',
        b"\xff\xfe",
        b'{"tool_name": "Bash"}',
        b'{"hook_event_name": "PreToolUse"}',
    ],
)
def test_malformed_stdin_prints_nothing_and_exits_0(art, raw):
    proc = _run(None, art, raw=raw)
    assert proc.returncode == 0 and proc.stdout == b""


@pytest.mark.parametrize(
    "event",
    [
        {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "{C}"}},
        {"tool_name": "Bash", "tool_input": {"command": "{C}"}},
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x/local/tmp/agami-x.json"},
        },
        {"hook_event_name": "PreToolUse", "tool_name": "bash", "tool_input": {"command": "{C}"}},
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "{C}"},
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": None}},
    ],
)
def test_other_events_and_tools_say_nothing(art, event):
    # `{C}` is a call the hook approves in a well-formed PreToolUse Bash event
    command = _sm(art, "areas {R}")
    assert _decision(_bash(command), art) == "allow"
    filled = json.loads(json.dumps(event).replace("{C}", command.replace('"', '\\"')))
    assert _decision(filled, art) is None


def test_the_hook_imports_nothing_of_the_plugin_or_the_package():
    imports = re.findall(
        r"^\s*(?:from\s+(\S+)\s+import|import\s+(\S+))",
        HOOK.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    modules = {(a or b).split(".")[0] for a, b in imports}
    assert modules <= {"__future__", "json", "ntpath", "os", "re", "stat", "sys", "typing"}, modules
    assert "spec_from_file_location" not in HOOK.read_text(encoding="utf-8")


# --- hooks.json, and agreement with allowed-tools ---------------------------------------------------------


def test_hooks_json_runs_python3_from_path_never_the_plugin_launcher():
    doc = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    (entry,) = doc["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash|Write|Read|Edit"
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    assert hook["command"] == (
        "in=$(cat); for p in python3 python; do "
        'out=$(printf \'%s\' "$in" | "$p" -I "${CLAUDE_PLUGIN_ROOT}/scripts/approve_own_calls.py" '
        "2>/dev/null) && { printf '%s' \"$out\"; break; }; done; true"
    )
    assert "scripts/py" not in hook["command"] and ".config" not in hook["command"]


@pytest.mark.skipif(shutil.which("sh") is None or sys.platform == "win32", reason="needs sh")
def test_the_hooks_json_command_works_and_never_exits_2(art, tmp_path):
    """As Claude Code runs it (`sh -c`). Exit 2 would block every tool call, so a broken path must
    end in `true`, not in Python's own exit 2 for a script it can't open."""
    doc = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command = doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art), "HOME": str(art.parent)}
    event = json.dumps(_bash(_sm(art, "areas {R}"))).encode()
    proc = subprocess.run(
        ["sh", "-c", command.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))],
        input=event,
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    broken = subprocess.run(
        ["sh", "-c", command.replace("${CLAUDE_PLUGIN_ROOT}", str(tmp_path / "gone"))],
        input=event,
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert broken.returncode == 0 and broken.stdout == b""


@pytest.mark.skipif(shutil.which("sh") is None or sys.platform == "win32", reason="needs sh")
@pytest.mark.parametrize("stub_exit", [1, 2, 9009 % 256])
def test_a_failing_python3_stub_prints_nothing_and_python_answers(art, tmp_path, stub_exit):
    """A `python3` that fails (the Windows Store stub, a broken install) after printing must not
    reach the hook's stdout, and the event must still reach `python` intact."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "python3"
    stub.write_text(
        f'#!/bin/sh\ncat >/dev/null\necho \'{{"hookSpecificOutput": "garbage"}}\'\n'
        f"exit {stub_exit}\n"
    )
    stub.chmod(0o755)
    real = bin_dir / "python"
    real.symlink_to(sys.executable)
    doc = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command = doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"].replace(
        "${CLAUDE_PLUGIN_ROOT}", str(PLUGIN)
    )
    env = {
        **os.environ,
        "AGAMI_ARTIFACTS_DIR": str(art),
        "HOME": str(art.parent),
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
    }
    allowed = subprocess.run(
        ["sh", "-c", command],
        input=json.dumps(_bash(_sm(art, "areas {R}"))).encode(),
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert allowed.returncode == 0
    assert json.loads(allowed.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    refused = subprocess.run(
        ["sh", "-c", command],
        input=json.dumps(_bash("rm -rf /")).encode(),
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert refused.returncode == 0 and refused.stdout == b""
    (bin_dir / "python").unlink()
    (bin_dir / "python").write_text(stub.read_text())
    (bin_dir / "python").chmod(0o755)
    neither = subprocess.run(
        ["sh", "-c", command],
        input=json.dumps(_bash(_sm(art, "areas {R}"))).encode(),
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert neither.returncode == 0 and neither.stdout == b""


# --- Edit of a scratch file: the same files, the same checks as Write ------------------------------------
# A live run prompted three times on Edits of `agami-metrics.json` it had just written. The hooks
# reference (https://code.claude.com/docs/en/hooks, "PreToolUse input") documents Edit's input as
# `file_path`, `old_string`, `new_string`, `replace_all`, and lists no MultiEdit tool.


def test_an_edit_of_an_existing_scratch_file_is_allowed(art):
    target = art / "local" / "tmp" / "agami-metrics.json"
    target.write_text("{}")
    assert _decision(_file("Edit", str(target)), art) == "allow"
    assert _decision(_file("Edit", str(target), replace_all=True), art) == "allow"


def test_an_edit_outside_the_scratch_folder_says_nothing(art, tmp_path):
    outside = tmp_path / "agami-x.json"
    outside.write_text("{}")
    for path in (
        outside,
        art / "local" / "agami-x.json",
        art / "acme" / "agami-x.json",
        PLUGIN / "shared" / "question-cards.md",
    ):
        assert _decision(_file("Edit", str(path)), art) is None, path


@SYMLINKS
def test_an_edit_through_a_link_says_nothing(art, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    link = art / "local" / "tmp" / "agami-link.json"
    link.symlink_to(outside)
    assert _decision(_file("Edit", str(link)), art) is None
    inside = art / "local" / "tmp" / "agami-real.json"
    inside.write_text("{}")
    renamed = art / "local" / "tmp" / "agami-alias.json"
    renamed.symlink_to(
        inside
    )  # even a link to a scratch file of its own: Write allows this one too
    assert _decision(_file("Edit", str(renamed)), art) == _decision(
        _file("Write", str(renamed)), art
    )


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links")
def test_an_edit_of_a_hard_linked_file_says_nothing(art, tmp_path):
    outside = tmp_path / "settings.json"
    outside.write_text("{}")
    target = art / "local" / "tmp" / "agami-x.json"
    os.link(outside, target)
    assert _decision(_file("Edit", str(target)), art) is None


@pytest.mark.parametrize("tool", ["MultiEdit", "NotebookEdit", "edit", "Edits"])
def test_other_edit_tools_say_nothing(art, tool):
    target = art / "local" / "tmp" / "agami-x.json"
    target.write_text("{}")
    assert _decision(_file(tool, str(target)), art) is None


def test_edit_and_write_agree_on_every_scratch_path(art, tmp_path):
    tmp = art / "local" / "tmp"
    (tmp / "agami-there.json").write_text("{}")
    for path in (
        tmp / "agami-there.json",
        tmp / "agami-new.md",
        tmp / "x.json",
        tmp / "notes.json",
        tmp / "AGAMI-x.json",
        tmp_path / "agami-x.json",
    ):
        assert _decision(_file("Edit", str(path)), art) == _decision(
            _file("Write", str(path)), art
        ), path


# --- the table itself, and the launcher's promise not to install for it ---------------------------------


def test_the_table_is_exactly_these_read_only_verbs():
    """A verb joins the table only after its handler has been read and found to read nothing but
    the model at its root: no write, no database, no credentials, no file the call names."""
    assert set(HOOK_MOD.VERBS) == {
        "areas",
        "coverage",
        "review-queue",
        "review-items",
        "model-tree",
        "bundle",
    }


@pytest.mark.parametrize(
    "verb", ["steps", "progress", "timing", "summary", "overview", "batch-id", "wait-files"]
)
def test_a_verb_main_does_not_have_is_not_approved(art, verb):
    assert verb not in HOOK_MOD.VERBS
    assert _decision(_bash(_sm(art, f"{verb} {{R}}")), art) is None
    assert _decision(_bash(_sm(art, verb)), art) is None


def _no_install_verbs() -> set[str]:
    text = Path(SM).read_text(encoding="utf-8")
    (line,) = re.findall(r'^NO_INSTALL_VERBS="([^"]*)"$', text, re.MULTILINE)
    return set(line.split())


def _stale_library_exit() -> int:
    text = Path(SM).read_text(encoding="utf-8")
    (code,) = re.findall(r"^STALE_LIBRARY_EXIT=(\d+)$", text, re.MULTILINE)
    return int(code)


def test_the_stale_library_refusal_is_not_the_no_model_code(tmp_path):
    """A skill reads exit 3 from its first `sm areas` as "no model here, set the database up". A
    library left stale by a plugin update must not read the same, or the person is sent to connect
    a database that is already connected. The no-model code is taken from the CLI itself."""
    from semantic_model.cli import main

    empty = tmp_path / "no-model"
    empty.mkdir()
    no_model = main(["areas", str(empty)])
    assert no_model == 3
    assert _stale_library_exit() != no_model
    # …nor any code sm passes through from the CLI or uses itself (0-3)
    assert _stale_library_exit() not in {0, 1, 2, 3}


def _skills_calling_an_approved_verb() -> dict[str, str]:
    verbs = "|".join(re.escape(v) for v in HOOK_MOD.VERBS)
    call = re.compile(rf'scripts/sm"?\s+({verbs})\b|\bsm\s+({verbs})\b')
    found = {}
    for skill in sorted((PLUGIN / "skills").glob("*/SKILL.md")):
        text = skill.read_text(encoding="utf-8")
        if call.search(text):
            found[skill.parent.name] = text
    return found


def test_every_skill_that_calls_an_approved_verb_says_what_its_refusal_means():
    """Claude only knows what the code means if the skill it is running says so, and the fix is a
    step the person approves, so the skill turns the code into that step and then the call again."""
    found = _skills_calling_an_approved_verb()
    assert {"agami-connect", "agami-query", "agami-model", "agami-save-correction"} <= set(found)
    code = _stale_library_exit()
    for name, text in found.items():
        lines = [ln for ln in text.splitlines() if f"exits {code}" in ln]
        assert lines, name
        (line,) = lines
        assert "older than the plugin" in line, name
        assert 'bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" install' in line, name
        assert "the person approves it" in line, name
        assert "repeat the call" in line, name


def test_the_launchers_no_install_list_is_the_hooks_table():
    # An approved call runs with no prompt, so it must never reach `sm`'s pip install.
    assert _no_install_verbs() == set(HOOK_MOD.VERBS)


# A Python whose agami-core is missing and whose pip would install: every pip call leaves a mark.
_SHIM_MISSING_WITH_PIP = """#!/bin/sh
case "$1 $2" in
  "-m pip") [ "$3" = "--version" ] && exit 0; echo "$*" >> "$PIP_MARK"; exit 0 ;;
  "-c "*) exit 1 ;;
esac
exit 0
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell shim")
@pytest.mark.parametrize("verb", sorted(HOOK_MOD.VERBS))
def test_an_approved_verb_never_installs(art, tmp_path, verb):
    cache = tmp_path / "agami-core" / "0.0.1"  # a versioned cache folder: no dev checkout beside it
    shutil.copytree(PLUGIN / "scripts", cache / "scripts")
    shim = tmp_path / "pyshim"
    shim.write_text(_SHIM_MISSING_WITH_PIP)
    shim.chmod(0o755)
    mark = tmp_path / "pip-calls"
    env = {
        **os.environ,
        "AGAMI_PYTHON": str(shim),
        "AGAMI_ARTIFACTS_DIR": str(art),
        "PIP_MARK": str(mark),
    }
    args = [verb, str(art / "acme"), *(["--area", "sales"] if verb == "bundle" else [])]
    r = subprocess.run(
        ["bash", str(cache / "scripts" / "sm"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == _stale_library_exit(), r.stderr
    assert not mark.exists(), mark.read_text()
    assert 'sm" install' in r.stderr
    # the same Python on a verb the hook doesn't approve does reach pip, so the shim is honest
    r = subprocess.run(
        ["bash", str(cache / "scripts" / "sm"), "validate", str(art / "acme")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert mark.exists()
