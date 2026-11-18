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
    "steps {R}",
    "steps {R} --mark connect=in_progress",
    "steps {R} --mark connect=done --mark reports=skipped --mark build=in_progress",
    "steps {R} --full",
    "steps --full {R} --mark describe=done",
    "progress {R}",
    "progress {R} --wait 20",
    "progress {R} --wait 0",
    "progress {R} --wait 600",
    "summary {R}",
    "summary {R} --json",
    "overview {R}",
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
    "model-tree {R} --out {T}/agami-tree.json",
    "bundle {R} --area sales",
    "bundle --area agami-example {R}",
    "bundle {R} --area agami-example --out {T}/agami-examples-1-sales.input.json",
    "batch-id",
]

# The approved forms that write: into the model's own .introspect/ (pinned by
# test_an_approved_steps_or_progress_call_writes_only_inside_introspect), or to the scratch file
# --out names. Every other approved form changes no file.
WRITES = ("steps ", "progress ", "--out ")


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


def test_a_step_mark_on_a_model_root_that_does_not_exist_yet_is_not_approved(art):
    # A mark creates <root>/.introspect/: approved, it would let any call make a new profile folder
    # in <artifacts_dir>. Onboarding's first mark comes before the build makes the folder; it runs
    # inside agami-connect and asks the person once. Reading the list there is still fine.
    assert _decision(_bash(f'bash "{SM}" steps {art / "newdb"} --mark connect=in_progress'), art) is None
    assert _decision(_bash(f'bash "{SM}" steps {art / "newdb"}'), art) == "allow"
    assert not (art / "newdb").exists()


@pytest.mark.parametrize("args", [a for a in APPROVED if not any(w in a for w in WRITES)])
def test_an_approved_call_changes_no_file(art, args):
    """Each approved form, run against a real model, leaves every file under the artifacts folder's
    parent as it was: the verbs only read. A verb that started writing would fail here."""
    pytest.importorskip("pydantic")
    from semantic_model import cli

    assert _decision(_bash(_sm(art, args)), art) == "allow"
    shutil.rmtree(art / "acme")
    shutil.copytree(PLUGIN / "samples" / "store" / "model", art / "acme")
    filled = args.format(R=str(art / "acme"), T=str(art / "local" / "tmp")).split()
    if "--wait" in filled:  # the files never appear here: don't sit out the whole wait
        filled[filled.index("--wait") + 1] = "0"
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


@pytest.mark.parametrize(
    "args",
    [
        "steps {R}",
        "steps {R} --mark describe=in_progress",
        "steps {R} --full",
        "progress {R}",
        "progress {R} --wait 0",
    ],
)
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


@pytest.mark.parametrize(
    "args",
    [
        "model-tree {R} --out {T}/agami-tree.json",
        "bundle {R} --area agami-example --out {T}/agami-b.json",
    ],
)
def test_an_approved_out_call_writes_only_its_scratch_file(art, args, monkeypatch):
    pytest.importorskip("pydantic")
    from semantic_model import cli

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))  # where `--out` may write

    assert _decision(_bash(_sm(art, args)), art) == "allow"
    shutil.rmtree(art / "acme")
    shutil.copytree(PLUGIN / "samples" / "store" / "model", art / "acme")
    before = {p: p.read_bytes() for p in art.parent.rglob("*") if p.is_file()}
    filled = args.format(R=str(art / "acme"), T=str(art / "local" / "tmp")).split()
    assert cli.main(filled) == 0
    after = {p: p.read_bytes() for p in art.parent.rglob("*") if p.is_file()}
    touched = {p for p in after if before.get(p) != after[p]} | (set(before) - set(after))
    assert touched == {Path(filled[-1])}


def _tree(top: Path) -> dict[str, tuple]:
    """Every entry under `top`, links not followed: kind, and a file's bytes and mtime."""
    out: dict[str, tuple] = {}
    for dirpath, dirs, files in os.walk(top):
        for name in dirs + files:
            p = Path(dirpath) / name
            if p.is_symlink():
                out[str(p)] = ("link", os.readlink(p))
            elif p.is_dir():
                out[str(p)] = ("dir",)
            else:
                out[str(p)] = ("file", p.read_bytes(), p.stat().st_mtime_ns)
    return out


@SYMLINKS
@pytest.mark.parametrize("args", APPROVED)
def test_an_approved_call_through_the_launcher_writes_only_what_its_row_allows(art, tmp_path, args):
    """The same promise through the real `sm`, the way the hook lets it run, for every form the table
    approves, so a verb added later is covered without a new case. With <artifacts_dir>/local a link
    to a folder elsewhere, the launcher's own setup (the scratch folder, Python's bytecode cache)
    writes nothing, there or anywhere under the test's folder; the verb itself writes only what its
    row allows: `steps` and `progress` inside the model's .introspect/, `--out` its scratch file.
    An `--out` form is approved only with a real scratch folder, so it runs with one."""
    pytest.importorskip("pydantic")
    from semantic_model import cli

    shutil.rmtree(art / "acme")
    shutil.copytree(PLUGIN / "samples" / "store" / "model", art / "acme")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if "--out" not in args:
        shutil.rmtree(art / "local")
        (art / "local").symlink_to(elsewhere)
    filled = (
        re.sub(r"--wait \d+", "--wait 0", args)  # the log never moves here: don't sit out the wait
        .format(R=str(art / "acme"), T=str(art / "local" / "tmp"))
        .split()
    )
    if "--area" in filled:
        filled[filled.index("--area") + 1] = (
            cli.L.load_datasource(art / "acme").subject_areas[0].name
        )
    env = {
        **os.environ,
        "AGAMI_ARTIFACTS_DIR": str(art),
        "HOME": str(tmp_path),
        "AGAMI_PYTHON": sys.executable,
    }
    before = _tree(tmp_path)
    proc = subprocess.run(
        ["bash", SM, *filled], capture_output=True, env=env, cwd=tmp_path, timeout=120
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    after = _tree(tmp_path)
    touched = {p for p in after if before.get(p) != after[p]} | (set(before) - set(after))
    if "--out" in filled:
        allowed = {filled[filled.index("--out") + 1]}
    elif filled[0] in ("steps", "progress"):
        introspect = str(art / "acme" / ".introspect")
        allowed = {p for p in touched if p == introspect or p.startswith(introspect + os.sep)}
    else:
        allowed = set()
    assert touched == allowed, sorted(touched - allowed)
    assert list(elsewhere.iterdir()) == []


# --- what an approved verb reads: only files inside the model folder --------------------------------
# The hook checks the folder a call names, but the model inside it names more files: a storage `ref`,
# a subject area's path, a link. Before this, a ref to a credentials file outside the model reached
# Claude through the load's own error (the schema error quotes each unexpected value), and a subject
# area pointed outside printed that folder's text. Every approved verb now refuses any read outside
# the folder, before the read.

_AREA = "name: sales\ndescription: d\n"
_TABLE = "name: orders\ndescription: o\ncolumns:\n  - name: id\n    type: integer\n"


def _small_model(root: Path, datasource: str) -> None:
    tables = root / "subject_areas" / "sales" / "tables"
    tables.mkdir(parents=True)
    (tables.parent / "subject_area.yaml").write_text(_AREA)
    (tables / "orders.yaml").write_text(_TABLE)
    (root / "datasource.yaml").write_text(datasource)


def _outside_area(tmp_path: Path) -> Path:
    area = tmp_path / "outside" / "area"
    (area / "tables").mkdir(parents=True)
    (area / "subject_area.yaml").write_text("name: sales\ndescription: SECRET-AREA\n")
    (area / "tables" / "t.yaml").write_text(
        _TABLE.replace("description: o", "description: SECRET-T")
    )
    return area


def _secret_ref(root: Path, tmp_path: Path) -> None:
    secret = tmp_path / "outside" / "creds.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('{"name": "x", "storage_type": "PostgreSQL", "password": "SECRET-PW"}')
    _small_model(
        root,
        "datasource: acme\nstorage_connections:\n  - ref: ../../outside/creds.json\n"
        "subject_areas:\n  - subject_areas/sales\n",
    )


def _absolute_area(root: Path, tmp_path: Path) -> None:
    _small_model(root, f"datasource: acme\nsubject_areas:\n  - {_outside_area(tmp_path)}\n")


def _relative_area(root: Path, tmp_path: Path) -> None:
    _outside_area(tmp_path)
    _small_model(root, "datasource: acme\nsubject_areas:\n  - ../../outside/area\n")


def _linked_area(root: Path, tmp_path: Path) -> None:
    _small_model(root, "datasource: acme\nsubject_areas:\n  - subject_areas/linked\n")
    (root / "subject_areas" / "linked").symlink_to(_outside_area(tmp_path))


def _linked_table(root: Path, tmp_path: Path) -> None:
    _small_model(root, "datasource: acme\nsubject_areas:\n  - subject_areas/sales\n")
    outside = tmp_path / "outside" / "t.yaml"
    outside.parent.mkdir(parents=True)
    outside.write_text("name: other\ndescription: SECRET-LINK\ncolumns: []\n")
    (root / "subject_areas" / "sales" / "tables" / "z.yaml").symlink_to(outside)


@SYMLINKS
@pytest.mark.parametrize(
    "build", [_secret_ref, _absolute_area, _relative_area, _linked_area, _linked_table]
)
@pytest.mark.parametrize("args", [a for a in APPROVED if a != "batch-id"])
def test_an_approved_verb_reads_nothing_outside_the_model(
    art, tmp_path, capsys, monkeypatch, build, args
):
    pytest.importorskip("pydantic")
    import builtins
    import io

    from semantic_model import cli

    shutil.rmtree(art / "acme")
    build(art / "acme", tmp_path)
    # `progress --wait N` would sit out N seconds on a log that never moves; the reads are the same.
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))  # where `--out` may write
    filled = (
        re.sub(r"--wait \d+", "--wait 0", args)
        .format(R=str(art / "acme"), T=str(art / "local" / "tmp"))
        .split()
    )
    # Every file opened while the verb runs, so a verb that turns a refused load into a view (the
    # step list reads an unreadable model as not built yet) is held to the same rule as one that
    # stops with `outside_model`: nothing outside the folder is opened at all.
    opened: list[str] = []
    real_open = builtins.open

    def spy(file, *a, **k):
        if isinstance(file, (str, bytes, os.PathLike)):
            opened.append(os.path.realpath(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", spy)
    try:
        rc = cli.main(filled)
    finally:
        monkeypatch.setattr(builtins, "open", real_open)
        monkeypatch.setattr(io, "open", real_open)
    out, err = capsys.readouterr()
    assert "SECRET" not in out + err
    outside = str(tmp_path / "outside")
    assert not [f for f in opened if f.startswith(os.path.realpath(outside))], opened
    if rc != 0:
        assert rc == 1 and json.loads(out)["error"] == "outside_model"
    else:
        assert args.split()[0] in ("steps", "progress"), (args, out)


def test_every_approved_verb_runs_contained():
    """Structural: the CLI handler of each verb the hook approves is the contained one, so a verb
    added to the table without it fails here rather than in someone's session."""
    from semantic_model import cli

    for verb in HOOK_MOD.VERBS:
        if verb == "batch-id":
            # Takes no model folder and reads no file: it prints a stamp from the clock and urandom.
            # Marked all the same, since `sm` runs an approved verb only on a marked handler.
            assert HOOK_MOD.VERBS[verb].positionals == (), verb
            assert cli._parse_args([verb]).func.within_model is True, verb
            continue
        extra = ["--area", "x"] if verb == "bundle" else []
        handler = cli._parse_args([verb, "/r", *extra]).func
        assert getattr(handler, "within_model", False) is True, verb


def test_a_verb_the_hook_does_not_approve_still_reads_a_ref_outside(tmp_path):
    """The containment is only for the approved verbs: a model with a shared storage file outside
    its folder still loads everywhere else."""
    pytest.importorskip("pydantic")
    from semantic_model import loader

    shared = tmp_path / "shared" / "storage.yaml"
    shared.parent.mkdir()
    shared.write_text("name: wh\nstorage_type: PostgreSQL\n")
    root = tmp_path / "acme"
    _small_model(
        root,
        "datasource: acme\nstorage_connections:\n  - ref: ../shared/storage.yaml\n"
        "subject_areas:\n  - subject_areas/sales\n",
    )
    assert loader.load_datasource(root).storage_connections[0].name == "wh"
    with loader.contained(root), pytest.raises(loader.OutsideModel):
        loader.load_datasource(root)


@SYMLINKS
def test_a_curation_log_linked_out_is_refused_when_contained(tmp_path):
    from semantic_model import curate, loader

    root = tmp_path / "acme"
    root.mkdir()
    outside = tmp_path / "log.jsonl"
    outside.write_text('{"op": "SECRET"}\n')
    (root / curate.CURATION_LOG).symlink_to(outside)
    assert curate._read_log_lines(root)  # uncontained: read as before
    with loader.contained(root), pytest.raises(loader.OutsideModel):
        curate._read_log_lines(root)


def test_the_description_field_is_fine(art):
    assert _decision(_bash(_sm(art, "areas {R}"), description="Show the steps"), art) == "allow"


# --- A. refused sm forms: the reviewer's inputs and more ----------------------------------------------

REFUSED = {
    # abbreviated or `=` options
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
    "root outside, mark": "steps /tmp/elsewhere --mark build=done",
    # option values
    "mark bad status": "steps {R} --mark build=finished",
    # a mark writes under <root>: approved only on a folder that exists, never one it would create
    "mark a new folder": "steps {A}/newprofile --mark connect=in_progress",
    "mark bad id": "steps {R} --mark Build=done",
    "mark no value": "steps {R} --mark",
    "mark option value": "steps {R} --mark --full",
    "full twice": "steps {R} --full --full",
    "wait negative": "progress {R} --wait -1",
    "wait float": "progress {R} --wait 2.5",
    "wait too long": "progress {R} --wait 601",
    "wait twice": "progress {R} --wait 1 --wait 2",
    "batch-id arg": "batch-id {R}",
    "json on steps": "steps {R} --json",
    "help on steps": "steps {R} -h",
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


# --- UNC and device paths --------------------------------------------------------------------------------
# On Windows, resolving `\\host\share\x` (either slash style) can open an SMB session and send the
# person's Windows credentials to `host` before anything has been approved. So such a path is refused
# by its text alone, before any filesystem call, in every check that takes a path from the call.

UNC_FORMS = [
    "\\\\uncprobe\\share\\agami-x.json",
    "//uncprobe/share/agami-x.json",
    "/\\uncprobe/share/agami-x.json",
    "\\/uncprobe\\share\\agami-x.json",
    "\\\\?\\C:\\uncprobe\\agami-x.json",
    "\\\\?\\UNC\\uncprobe\\share\\agami-x.json",
    "\\\\.\\pipe\\uncprobe",
    "//?/C:/uncprobe/agami-x.json",
]


@pytest.mark.parametrize("value", UNC_FORMS)
def test_a_unc_or_device_path_is_not_a_plain_path(value):
    assert HOOK_MOD._plain_path(value) is False


def _no_filesystem_call_on_unc(monkeypatch) -> list:
    """Every filesystem call the hook's checks make, made to fail the test when its argument is the
    UNC probe: the refusal must come from the text alone."""
    touched: list = []
    for name in ("realpath", "exists", "isfile", "isdir", "islink", "lexists"):
        real = getattr(os.path, name)

        def spy(p, *a, _real=real, **k):
            if "uncprobe" in str(p):
                touched.append(p)
            return _real(p, *a, **k)

        monkeypatch.setattr(os.path, name, spy)
    for name in ("stat", "lstat"):
        real = getattr(os, name)

        def spy(p, *a, _real=real, **k):
            if "uncprobe" in str(p):
                touched.append(p)
            return _real(p, *a, **k)

        monkeypatch.setattr(os, name, spy)
    return touched


@pytest.mark.parametrize("value", UNC_FORMS)
def test_every_path_check_refuses_a_unc_path_before_touching_it(art, monkeypatch, value):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    touched = _no_filesystem_call_on_unc(monkeypatch)
    assert HOOK_MOD.scratch_file(value) is False
    assert HOOK_MOD.plugin_file(value) is False
    assert HOOK_MOD.model_root(value) is False
    assert HOOK_MOD._is_launcher(value) is False
    assert touched == []


@pytest.mark.parametrize("tool", ["Write", "Read", "Edit"])
@pytest.mark.parametrize("value", UNC_FORMS)
def test_a_unc_file_path_says_nothing(art, tool, value):
    assert _decision(_file(tool, value), art) is None


@pytest.mark.parametrize("value", UNC_FORMS)
def test_a_unc_model_root_or_launcher_says_nothing(art, value):
    assert _decision(_bash(f"bash '{SM}' areas '{value}'"), art) is None
    assert _decision(_bash(f"bash '{value}' areas '{art / 'acme'}'"), art) is None


def test_a_doubled_leading_slash_on_an_allowed_path_says_nothing(art):
    """POSIX leaves `//x` to the system and Windows reads it as a server, so an otherwise approved
    path written with two leading slashes is refused too."""
    assert _decision(_file("Read", "/" + str(PLUGIN / "scripts" / "sm")), art) is None
    assert _decision(_file("Write", "/" + str(art / "local" / "tmp" / "agami-x.json")), art) is None
    assert _decision(_bash(f"bash '{SM}' areas '/{art / 'acme'}'"), art) is None
    assert _decision(_bash(f"bash '/{SM}' areas '{art / 'acme'}'"), art) is None


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


# --- UNC and device references inside the model -----------------------------------------------------------
# The hook refuses a network path the call names, but the model names more: a storage `ref`, a subject
# area's path, a link's target. A contained load refuses each from its text, before `realpath`,
# `exists` or a read could reach the host.


def _no_model_filesystem_call_on_unc(monkeypatch) -> list:
    """The hook's spy, widened to what a model load can call: opens, directory listings and link reads."""
    import io as _io

    touched = _no_filesystem_call_on_unc(monkeypatch)
    for mod, name in (
        (os, "open"),
        (os, "listdir"),
        (os, "scandir"),
        (os, "readlink"),
        (_io, "open"),
    ):
        real = getattr(mod, name)

        def spy(p, *a, _real=real, **k):
            if "uncprobe" in str(p):
                touched.append(p)
            return _real(p, *a, **k)

        monkeypatch.setattr(mod, name, spy)
    return touched


def _model_naming(root: Path, **datasource: object) -> None:
    import yaml

    _small_model(root, yaml.safe_dump({"datasource": "acme", **datasource}))


@pytest.mark.parametrize("value", UNC_FORMS)
def test_a_unc_reference_in_the_model_is_network(value):
    from semantic_model import loader

    assert loader._network_path(value) is True


def test_a_local_reference_is_not_network():
    from semantic_model import loader

    for value in ("subject_areas/sales", "/srv/models/acme", "C:\\models\\acme", "sales"):
        assert loader._network_path(value) is False, value


@pytest.mark.parametrize(
    "where",
    ["storage_ref", "storage_name", "area_path", "area_dict_path"],
)
@pytest.mark.parametrize("value", UNC_FORMS)
def test_a_unc_reference_in_the_model_is_refused_before_any_filesystem_call(
    art, monkeypatch, capsys, where, value
):
    pytest.importorskip("pydantic")
    from semantic_model import cli

    root = art / "acme"
    shutil.rmtree(root)
    areas = ["subject_areas/sales"]
    fields: dict = {
        "storage_ref": {"storage_connections": [{"ref": value}], "subject_areas": areas},
        "storage_name": {"storage_connections": [{"name": value}], "subject_areas": areas},
        "area_path": {"subject_areas": [value]},
        "area_dict_path": {"subject_areas": [{"path": value}]},
    }[where]
    _model_naming(root, **fields)
    touched = _no_model_filesystem_call_on_unc(monkeypatch)
    assert cli.main(["areas", str(root)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "outside_model"
    assert touched == []


@SYMLINKS
@pytest.mark.parametrize("value", ["//uncprobe/share/area", "\\\\uncprobe\\share\\area"])
@pytest.mark.parametrize("linked", ["area", "tables", "table_file", "chain"])
def test_a_link_in_the_model_to_a_unc_path_is_refused_before_following_it(
    art, monkeypatch, capsys, value, linked
):
    """A link's target is read as text and refused; nothing follows it, so nothing reaches the host."""
    pytest.importorskip("pydantic")
    from semantic_model import cli

    root = art / "acme"
    shutil.rmtree(root)
    _model_naming(root, subject_areas=["subject_areas/sales"])
    sales = root / "subject_areas" / "sales"
    if linked == "area":
        shutil.rmtree(sales)
        sales.symlink_to(value)
    elif linked == "tables":
        shutil.rmtree(sales / "tables")
        (sales / "tables").symlink_to(value)
    elif linked == "table_file":
        (sales / "tables" / "z.yaml").symlink_to(value)
    else:  # a local link to a link to the host
        (root / "hop").symlink_to(value)
        shutil.rmtree(sales)
        sales.symlink_to(root / "hop")
    touched = _no_model_filesystem_call_on_unc(monkeypatch)
    assert cli.main(["areas", str(root)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "outside_model"
    assert touched == []


@SYMLINKS
def test_a_link_loop_in_the_model_is_refused(tmp_path):
    from semantic_model import loader

    root = tmp_path / "acme"
    root.mkdir()
    (root / "a").symlink_to(root / "b")
    (root / "b").symlink_to(root / "a")
    with loader.contained(root), pytest.raises(loader.OutsideModel, match="too many links"):
        loader.check_contained(root / "a" / "x.yaml")


@SYMLINKS
def test_a_local_link_inside_the_model_still_loads_contained(tmp_path):
    """The link walk follows a link that stays inside the folder, so a model arranged with one loads."""
    pytest.importorskip("pydantic")
    from semantic_model import loader

    root = tmp_path / "acme"
    _small_model(root, "datasource: acme\nsubject_areas:\n  - subject_areas/alias\n")
    (root / "subject_areas" / "alias").symlink_to("sales")
    with loader.contained(root):
        assert [a.name for a in loader.load_datasource(root).subject_areas] == ["sales"]


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


# --- hooks.json ---------------------------------------------------------


def test_hooks_json_runs_python3_from_path_never_the_plugin_launcher():
    doc = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    (entry,) = doc["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash|Write|Read|Edit"
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    # Each interpreter is accepted only once it has printed the probe's word: the Windows Store
    # `python3` alias can exit 0 without running anything, and the loop must then try `python`.
    assert hook["command"] == (
        "in=$(cat); for p in python3 python; do "
        '[ "$("$p" -I -c \'print(end="agami-ok")\' </dev/null 2>/dev/null)" = agami-ok ] && '
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


@pytest.mark.skipif(shutil.which("sh") is None or sys.platform == "win32", reason="needs sh")
@pytest.mark.parametrize("stub_out", ["", "agami-ok-not", '{"hookSpecificOutput": "garbage"}'])
def test_a_python3_that_exits_0_without_running_python_is_skipped(art, tmp_path, stub_out):
    """The Windows Store `python3` alias can exit 0 having run nothing. The loop must not stop on
    it: `python` still answers, and nothing the stub printed reaches the hook's output."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "python3"
    stub.write_text(f"#!/bin/sh\ncat >/dev/null\nprintf '%s' '{stub_out}'\nexit 0\n")
    stub.chmod(0o755)
    (bin_dir / "python").symlink_to(sys.executable)
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
    # with no working interpreter at all, no output: no decision
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


def test_the_table_is_exactly_these_verbs():
    """A verb joins the table only after its handler has been read: it reads the model at its root
    and writes nothing but the model's own .introspect/ and a scratch --out file; no database, no
    credentials, no file the call names outside the scratch folder."""
    assert set(HOOK_MOD.VERBS) == {
        "steps",
        "progress",
        "summary",
        "overview",
        "areas",
        "coverage",
        "review-queue",
        "review-items",
        "model-tree",
        "bundle",
        "batch-id",
    }


def _no_install_verbs() -> set[str]:
    text = Path(SM).read_text(encoding="utf-8")
    (line,) = re.findall(r'^NO_INSTALL_VERBS="([^"]*)"$', text, re.MULTILINE)
    return set(line.split())


def _stale_library_exit() -> int:
    text = Path(SM).read_text(encoding="utf-8")
    (code,) = re.findall(r"^STALE_LIBRARY_EXIT=(\d+)$", text, re.MULTILINE)
    return int(code)


_OLD_CLI = """
import argparse


def _parse_args(argv):
    return argparse.Namespace(func=lambda args: 0)


def main(argv=None):
    print("RAN")
    return 0
"""
_CONTAINED_CLI = """
import argparse
import contextlib


class L:
    contained = staticmethod(contextlib.nullcontext)


def handler(args):
    return 0


handler.within_model = True


def _parse_args(argv):
    return argparse.Namespace(func=handler)


def main(argv=None):
    print("RAN")
    return 0
"""


@pytest.mark.parametrize(
    "cli_source, runs",
    [
        (_OLD_CLI, False),  # a handler with no containment
        ("def main(argv=None):\n    print('RAN')\n    return 0\n", False),  # older still
        (_CONTAINED_CLI, True),  # the stub itself is what runs, once it holds the verb
    ],
)
@pytest.mark.parametrize("verb", sorted(_no_install_verbs()))
def test_an_approved_verb_runs_only_on_a_library_that_contains_it(
    art, tmp_path, cli_source, runs, verb
):
    """A `semantic_model` with no install metadata on PYTHONPATH passes the import and version checks
    (an unknown version is not a stale one), so the launcher must ask the code itself whether it
    holds the verb inside the model folder, and exit 12 having run nothing when it does not."""
    pytest.importorskip("pydantic")
    cache = tmp_path / "cache" / "agami-core" / "0.0.1" / "scripts"  # a marketplace layout
    cache.mkdir(parents=True)
    for name in ("sm", "_python.sh", "_plugin_version.py"):
        shutil.copy2(PLUGIN / "scripts" / name, cache / name)
    stub = tmp_path / "stub" / "semantic_model"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text("")
    main_block = '\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
    (stub / "cli.py").write_text(cli_source + main_block)
    env = {
        **os.environ,
        "AGAMI_ARTIFACTS_DIR": str(art),
        "HOME": str(tmp_path),
        "AGAMI_PYTHON": sys.executable,
        "PYTHONPATH": str(stub.parent),
    }
    extra = ["--area", "sales"] if verb == "bundle" else []
    proc = subprocess.run(
        ["bash", str(cache / "sm"), verb, str(art / "acme"), *extra],
        capture_output=True,
        env=env,
        timeout=120,
    )
    out, err = proc.stdout.decode(), proc.stderr.decode(errors="replace")
    if runs:
        assert (proc.returncode, out.strip()) == (0, "RAN"), err
    else:
        assert proc.returncode == _stale_library_exit(), err
        assert "RAN" not in out
        assert 'sm" install' in err


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


# A Python whose library is ready the first time each readiness probe asks and broken after: a
# concurrent update landing between `sm`'s guard and anything that probes again. The CLI run itself
# leaves a mark, and so does every pip call.
_SHIM_READY_THEN_BROKEN = """#!/bin/sh
case "$1" in
  -m) [ "$2 $3" = "pip --version" ] && exit 0; echo "$*" >> "$PIP_MARK"; exit 0 ;;
  -X) echo "$*" >> "$RAN_MARK"; exit 0 ;;
  -c|-)
    [ "$1" = "-" ] && cat >/dev/null
    key=$(printf '%s' "$1 $2" | cksum | cut -d' ' -f1)
    if grep -qx "$key" "$SEEN" 2>/dev/null; then exit 1; fi
    echo "$key" >> "$SEEN"; exit 0 ;;
esac
exit 0
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell shim")
@pytest.mark.parametrize("verb", sorted(HOOK_MOD.VERBS))
def test_an_approved_verb_never_reaches_the_installer_after_its_guard(art, tmp_path, verb):
    """Once the guard has passed, nothing probes again: a library that breaks in between makes the
    CLI fail on its own, and never turns an approved call into a pip install."""
    cache = tmp_path / "agami-core" / "0.0.1"
    shutil.copytree(PLUGIN / "scripts", cache / "scripts")
    shim = tmp_path / "pyshim"
    shim.write_text(_SHIM_READY_THEN_BROKEN)
    shim.chmod(0o755)
    pip, ran = tmp_path / "pip-calls", tmp_path / "cli-runs"
    env = {
        **os.environ,
        "AGAMI_PYTHON": str(shim),
        "AGAMI_ARTIFACTS_DIR": str(art),
        "PIP_MARK": str(pip),
        "RAN_MARK": str(ran),
        "SEEN": str(tmp_path / "seen"),
    }
    args = [verb, str(art / "acme"), *(["--area", "sales"] if verb == "bundle" else [])]
    r = subprocess.run(
        ["bash", str(cache / "scripts" / "sm"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert not pip.exists(), pip.read_text()
    assert r.returncode == 0, r.stderr
    # the run is the one that checks the verb is contained, then calls the CLI (no second probe)
    runs = ran.read_text()
    assert "from semantic_model import cli" in runs
    assert f" {verb} {art / 'acme'}" in runs
