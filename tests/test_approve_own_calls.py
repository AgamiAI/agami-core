"""The PreToolUse hook that approves agami's own launcher calls and scratch files.

It grants permission, so the refusals are tested harder than the approvals: anything but the exact
shapes it knows prints nothing (the normal permission prompt then applies), and it never denies.
Each case drives the script as Claude Code does, with the event as JSON on stdin.
"""

from __future__ import annotations

import json
import os
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


@pytest.fixture
def art(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    (root / "local" / "tmp").mkdir(parents=True)
    return root


def _run(event: object, art: Path, raw: bytes | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art), "HOME": str(art.parent)}
    data = raw if raw is not None else json.dumps(event).encode("utf-8")
    return subprocess.run([sys.executable, "-I", str(HOOK)], input=data, capture_output=True,
                          env=env, timeout=30)


def _decision(event: object, art: Path) -> str | None:
    proc = _run(event, art)
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return None
    out = json.loads(proc.stdout)
    spec = out["hookSpecificOutput"]
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


# --- a. Bash: approved shapes ---------------------------------------------------------------------

ALLOWED_COMMANDS = [
    f'bash "{SM}" steps /data/acme',
    f"bash {SM} steps /data/acme --mark build=done",
    f'"{SM}" steps /data/acme',
    f"{SM} overview /data/acme",
    f"bash '{SM}' summary '/data/my acme'",
    f'AGAMI_SQL_MAX_ROWS=500 bash "{SM}" seed-validate /data/acme --profile acme',
    f'AGAMI_PROFILE=acme_2 bash "{PY}" connect_resolve.py --db-type postgres',
    f'AGAMI_SQL_MAX_ROWS=10 AGAMI_PROFILE=acme bash "{SM}" areas /data/acme',
    f'bash "{PY}" inventory_summary.py --inventory "/data/inv one.json"',
    f'bash "{SM}" prepare /data/acme --sql "SELECT * FROM orders WHERE id = 1"',
    f"bash \"{SM}\" curate /data/acme --ops '[{{\"op\": \"approve\"}}]'",
    f'bash "{SM}" steps ~/agami-artifacts/acme',
    f'bash   "{SM}"   steps   /data/acme  ',
]


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_a_lone_launcher_call_is_allowed(art, command):
    assert _decision(_bash(command), art) == "allow"


def test_bash_with_the_documented_optional_fields(art):
    event = _bash(f'bash "{SM}" wait-files --file /x --wait 60', description="wait",
                  timeout=600000, run_in_background=True)
    assert _decision(event, art) == "allow"


def test_out_into_the_scratch_folder_is_allowed(art):
    target = art / "local" / "tmp" / "agami-tree.json"
    assert _decision(_bash(f'bash "{SM}" model-tree /data/acme --out "{target}"'), art) == "allow"
    assert _decision(_bash(f'bash "{SM}" model-tree /data/acme --out={target}'), art) == "allow"


# --- a. Bash: refused shapes ----------------------------------------------------------------------

REFUSED_COMMANDS = [
    # chained, piped, redirected, substituted
    f'bash "{SM}" steps /x; rm -rf x',
    f'bash "{SM}" steps /x ;rm -rf x',
    f'bash "{SM}" steps /x | grep text',
    f'bash "{SM}" steps /x && echo done',
    f'bash "{SM}" steps /x & echo bg',
    f'bash "{SM}" steps /x || true',
    f'bash "{SM}" steps /x > out.json',
    f'bash "{SM}" steps /x >> out.json',
    f'bash "{SM}" steps /x 2>/dev/null',
    f'bash "{SM}" steps < /etc/passwd',
    f'bash "{SM}" steps "$(whoami)"',
    f'bash "{SM}" steps $(whoami)',
    f'bash "{SM}" steps `whoami`',
    f'bash "{SM}" steps "`whoami`"',
    f'bash "{SM}" steps <(cat /etc/passwd)',
    f'bash "{SM}" steps /x\nrm -rf x',
    f'bash "{SM}" steps /x\rrm -rf x',
    # variables, in any quoting the shell still expands
    f'bash "{SM}" steps $HOME',
    f'bash "{SM}" steps "$HOME"',
    f'bash "{SM}" steps "${{HOME}}"',
    f'bash "{SM}" steps ${{HOME}}',
    f'bash "{SM}" steps "a\\$b"',
    # assignments and prefixes other than the two documented ones
    f'A=1; bash "{SM}" steps /x',
    f'A=1 bash "{SM}" steps /x',
    f'P=/x bash "{SM}" steps /x',
    f'AGAMI_SQL_MAX_ROWS=abc bash "{SM}" steps /x',
    f'AGAMI_SQL_MAX_ROWS="5" bash "{SM}" steps /x',
    f'AGAMI_SQL_MAX_ROWS=5 AGAMI_SQL_MAX_ROWS=6 bash "{SM}" steps /x',
    f'AGAMI_PROFILE=a-b bash "{SM}" steps /x',
    f'AGAMI_PROFILE= bash "{SM}" steps /x',
    f'PATH=/tmp bash "{SM}" steps /x',
    f'BASH_ENV=/tmp/x bash "{SM}" steps /x',
    f'env bash "{SM}" steps /x',
    f'sudo bash "{SM}" steps /x',
    f'bash -x "{SM}" steps /x',
    f'bash -c "{SM} steps /x"',
    f'sh "{SM}" steps /x',
    f'bash bash "{SM}" steps /x',
    f'exec "{SM}" steps /x',
    # grouping, globbing, comments, history
    f'( bash "{SM}" steps /x )',
    f'{{ bash "{SM}" steps /x }}',
    f'bash "{SM}" steps /x*',
    f'bash "{SM}" steps /x?',
    f'bash "{SM}" steps /x[ab]',
    f'bash "{SM}" steps /x # comment',
    f'bash "{SM}" steps !!',
    f'bash "{SM}" steps \\;',
    f'bash "{SM}" steps "unclosed',
    f"bash \"{SM}\" steps 'unclosed",
    f'bash "{SM}" steps /x\t\x00',
    # the launcher itself
    f'bash "{PLUGIN}/scripts/connect_helper.py" --spec x',
    f'bash "{PLUGIN}/scripts/open_in_browser" x',
    f'bash "{PLUGIN}/scripts/_python.sh"',
    f'bash "{PLUGIN}-evil/scripts/sm" steps /x',
    f'bash "{PLUGIN}/scripts/sm-evil" steps /x',
    f'bash "{PLUGIN}/scripts/../scripts/sm" steps /x',
    f'bash "{PLUGIN}/scripts/./sm" steps /x',
    f'bash "{PLUGIN}//scripts/sm" steps /x',
    f'bash "{PLUGIN}/../agami/scripts/sm" steps /x',
    "bash scripts/sm steps /x",
    "bash ./sm steps /x",
    "bash sm steps /x",
    "bash ~/agami/scripts/sm steps /x",
    f'bash "{SM}/" steps /x',
    f"bash {SM.upper()} steps /x" if sys.platform != "win32" else "bash x",
    # py running anything that isn't one of the plugin's own scripts by bare name
    f'bash "{PY}" -c "import os"',
    f'bash "{PY}" -m execute_sql --profile acme',
    f'bash "{PY}" -m pip install x',
    f'bash "{PY}" /tmp/evil.py',
    f'bash "{PY}" evil.py',
    f'bash "{PY}" ../scripts/connect_helper.py',
    f'bash "{PY}" scripts/connect_helper.py',
    f'bash "{PY}" _python.sh',
    f'bash "{PY}"',
    f'bash "{PY}" -I connect_helper.py',
    # --out anywhere but an agami scratch file
    f'bash "{SM}" model-tree /x --out /etc/agami-x.json',
    f'bash "{SM}" model-tree /x --out ~/.zshrc',
    f'bash "{SM}" model-tree /x --out=/tmp/agami-x.json',
    f'bash "{SM}" model-tree /x --out',
    # nothing to run
    "",
    "   ",
    "bash",
    "AGAMI_PROFILE=acme",
]


@pytest.mark.parametrize("command", REFUSED_COMMANDS)
def test_anything_else_says_nothing(art, command):
    assert _decision(_bash(command), art) is None


def test_a_copy_of_the_launcher_outside_the_plugin_says_nothing(art, tmp_path):
    copy = tmp_path / "elsewhere" / "scripts"
    copy.mkdir(parents=True)
    shutil.copy(SM, copy / "sm")
    assert _decision(_bash(f'bash "{copy / "sm"}" steps /x'), art) is None


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32", reason="symlinks")
def test_a_link_to_the_launcher_from_elsewhere_says_nothing(art, tmp_path):
    # `bash <link>` would source the _python.sh beside the link, not the plugin's
    link = tmp_path / "sm"
    link.symlink_to(SM)
    assert _decision(_bash(f'bash "{link}" steps /x'), art) is None
    plugin_link = tmp_path / "plugin"
    plugin_link.symlink_to(PLUGIN)
    assert _decision(_bash(f'bash "{plugin_link}/scripts/sm" steps /x'), art) is None


def test_an_unknown_bash_field_says_nothing(art):
    assert _decision(_bash(f'bash "{SM}" steps /x', dangerouslyDisableSandbox=True), art) is None


def test_a_command_that_is_not_text_says_nothing(art):
    for command in (None, 1, [f"bash {SM}"], {"c": 1}):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                 "tool_input": {"command": command}}
        assert _decision(event, art) is None


# --- b. Write / Read of agami's scratch files ------------------------------------------------------


@pytest.mark.parametrize("name", ["agami-x.json", "agami-enrich-20261112-ab12-sales-1.result.json",
                                  "agami-notes.md", "agami-cols.tsv", "agami-a_b.txt"])
@pytest.mark.parametrize("tool", ["Write", "Read"])
def test_a_scratch_file_is_allowed(art, tool, name):
    assert _decision(_file(tool, str(art / "local" / "tmp" / name)), art) == "allow"


def test_the_scratch_folder_comes_from_the_pointer_file(tmp_path):
    other = tmp_path / "chosen"
    (other / "local" / "tmp").mkdir(parents=True)
    (tmp_path / ".config" / "agami").mkdir(parents=True)
    (tmp_path / ".config" / "agami" / "path").write_text(f"{other}\n")
    env = {k: v for k, v in os.environ.items() if k != "AGAMI_ARTIFACTS_DIR"}
    env["HOME"] = str(tmp_path)
    if sys.platform == "win32":
        env["USERPROFILE"] = str(tmp_path)
    event = _file("Write", str(other / "local" / "tmp" / "agami-x.json"))
    proc = subprocess.run([sys.executable, "-I", str(HOOK)], input=json.dumps(event).encode(),
                          capture_output=True, env=env, timeout=30)
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


@pytest.mark.parametrize("rel", [
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
    "local/tmp/agami-x y.json",
    "local/tmp/agami-x;y.json",
    "local/tmp",
    "local/agami-x.json",
    "local/credentials",
    "local/.config",
    "agami-x.json",
])
@pytest.mark.parametrize("tool", ["Write", "Read"])
def test_other_artifact_paths_say_nothing(art, tool, rel):
    assert _decision(_file(tool, str(art) + "/" + rel), art) is None


def test_relative_and_tilde_paths_say_nothing(art):
    for path in ("local/tmp/agami-x.json", "~/agami-artifacts/local/tmp/agami-x.json", ""):
        assert _decision(_file("Write", path), art) is None


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32", reason="symlinks")
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


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32", reason="symlinks")
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


def test_an_unknown_write_field_says_nothing(art):
    event = _file("Write", str(art / "local" / "tmp" / "agami-x.json"), mode="append")
    assert _decision(event, art) is None


# --- c. Read of the plugin's own files -------------------------------------------------------------


def test_reading_a_plugin_file_is_allowed(art):
    for rel in ("skills/agami-connect/SKILL.md", "shared/task-list.md", "scripts/sm"):
        assert _decision(_file("Read", str(PLUGIN / rel)), art) == "allow", rel


def test_writing_a_plugin_file_says_nothing(art):
    assert _decision(_file("Write", str(PLUGIN / "skills" / "agami-connect" / "SKILL.md")), art) is None


@pytest.mark.parametrize("path", [
    f"{PLUGIN}-evil/skills/x.md",
    f"{PLUGIN}/../agami/skills/agami-connect/SKILL.md",
    f"{PLUGIN}/skills/../../../README.md",
    f"{PLUGIN}",
    f"{PLUGIN}/skills",
    f"{PLUGIN}/no-such-file.md",
    "/etc/passwd",
])
def test_reading_outside_the_plugin_says_nothing(art, path):
    assert _decision(_file("Read", path), art) is None


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32", reason="symlinks")
def test_a_link_inside_the_plugin_pointing_out_says_nothing(art, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    link = PLUGIN / "shared" / "zz-test-link.md"
    link.symlink_to(outside)
    try:
        assert _decision(_file("Read", str(link)), art) is None
    finally:
        link.unlink()


# --- malformed input -------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", b"null", b'"text"', b"\xff\xfe",
                                 b'{"tool_name": "Bash"}', b'{"hook_event_name": "PreToolUse"}'])
def test_malformed_stdin_prints_nothing_and_exits_0(art, raw):
    proc = _run(None, art, raw=raw)
    assert proc.returncode == 0
    assert proc.stdout == b""


@pytest.mark.parametrize("event", [
    {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": f"bash {SM} x"}},
    {"tool_name": "Bash", "tool_input": {"command": f"bash {SM} x"}},
    {"hook_event_name": "PreToolUse", "tool_name": "Edit",
     "tool_input": {"file_path": "/x/local/tmp/agami-x.json"}},
    {"hook_event_name": "PreToolUse", "tool_name": "bash", "tool_input": {"command": f"bash {SM} x"}},
    {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": f"bash {SM} x"},
])
def test_other_events_and_tools_say_nothing(art, event):
    assert _decision(event, art) is None


def test_the_hook_imports_nothing_of_the_engine():
    text = HOOK.read_text(encoding="utf-8")
    assert "semantic_model" not in text and "import yaml" not in text


# --- hooks.json and allowed-tools agree ------------------------------------------------------------


def test_hooks_json_runs_the_hook_through_the_plugin_launcher():
    doc = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    (entry,) = doc["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash|Write|Read"
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    assert hook["command"] == ('bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" -I '
                               '"${CLAUDE_PLUGIN_ROOT}/scripts/approve_own_calls.py"')


def _allowed_tools(skill: str) -> list[str]:
    head = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8").split("\n---\n", 1)[0]
    return yaml.safe_load(head.removeprefix("---\n"))["allowed-tools"]


def test_the_hook_and_agami_connects_allowed_tools_cover_the_same_launchers(art):
    """Each Bash rule in agami-connect's allowed-tools, filled in with this plugin's root and a
    typical argument, is a call the hook approves too, and the launchers named agree. allowed-tools
    stays: it still helps where hooks don't run."""
    import re
    rules = [r for r in _allowed_tools("agami-connect") if r.startswith("Bash(")]
    named = {re.search(r"scripts/(\w+)", r).group(1) for r in rules}
    assert named == {"sm", "py"}
    for rule in rules:
        pattern = rule[len("Bash("):-1]
        script_arg = "connect_resolve.py" if "scripts/py" in pattern else "steps /data/acme"
        command = (pattern.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
                   .replace("AGAMI_SQL_MAX_ROWS=*", "AGAMI_SQL_MAX_ROWS=50")
                   .replace("AGAMI_PROFILE=*", "AGAMI_PROFILE=acme")
                   .replace(" *", " " + script_arg))
        assert _decision(_bash(command), art) == "allow", rule
    reads = [r for r in _allowed_tools("agami-connect") if r.startswith("Read(")]
    assert reads == ["Read(/${CLAUDE_PLUGIN_ROOT}/**)"]


@pytest.mark.skipif(shutil.which("bash") is None or sys.platform == "win32", reason="needs bash")
def test_the_hooks_json_command_works_through_the_launcher(art):
    """As hooks.json runs it: through `scripts/py`, which picks the interpreter and never installs."""
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art), "AGAMI_PYTHON": sys.executable}
    event = json.dumps(_bash(f'bash "{SM}" steps /data/acme')).encode()
    proc = subprocess.run(["bash", PY, "-I", str(HOOK)], input=event, capture_output=True,
                          env=env, timeout=60)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    refused = json.dumps(_bash(f'bash "{SM}" steps /x | sh')).encode()
    proc = subprocess.run(["bash", PY, "-I", str(HOOK)], input=refused, capture_output=True,
                          env=env, timeout=60)
    assert proc.returncode == 0 and proc.stdout == b""
