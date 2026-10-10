"""Windows text I/O (#362): what CI's Linux runners cannot show by themselves.

Three things break only on Windows, so each is either simulated here or guarded by a source scan:

- a piped stdout there encodes in the ANSI code page and turns "\\n" into "\\r\\n", which doubled
  every CSV row terminator the query step writes and gave each result a blank row after every real
  one, on the path that hands rows to the agent;
- `open()`, `read_text()` and configparser default to that code page too, so every text read or
  write must name its encoding, which the scan enforces;
- `python3` on PATH is often the Microsoft Store stub, which `sm` used to pick even when
  `.config` named the real interpreter.
"""

from __future__ import annotations

import ast
import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
SM = SCRIPTS / "sm"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402

# The shipped Python: the package, the plugin's scripts, and the vendored copy a plugin install runs.
SCANNED = [PKG_SRC, SCRIPTS, REPO_ROOT / "plugins" / "agami" / "lib"]


def _windows_stdout(monkeypatch) -> io.BytesIO:
    """A stdout shaped like a piped one on Windows: cp1252, and "\\n" written as "\\r\\n"."""
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252", newline="\r\n"))
    return raw


def test_unfixed_windows_stdout_doubles_every_row_terminator(monkeypatch):
    # The failure itself, so the fix below is measured against something real.
    raw = _windows_stdout(monkeypatch)
    execute_sql._emit_result_csv(execute_sql.ExecResult(columns=["n"], rows=[(1,), (2,)], truncated=False))
    sys.stdout.flush()
    rows = list(csv.reader(io.StringIO(raw.getvalue().decode("cp1252"), newline=None)))
    assert rows == [["n"], [], ["1"], [], ["2"], []]


def test_query_stdout_carries_csv_bytes_unchanged_and_utf8(monkeypatch):
    raw = _windows_stdout(monkeypatch)
    execute_sql._utf8_stdout()
    execute_sql._emit_result_csv(execute_sql.ExecResult(columns=["n", "s"], rows=[(1, "₹ 5"), (2, None)], truncated=False))
    sys.stdout.flush()
    # The same bytes `test_ah012_executor_seam` pins, now also on Windows, and a symbol cp1252 lacks.
    assert raw.getvalue() == "n,s\r\n1,₹ 5\r\n2,\r\n".encode("utf-8")
    # What the parent reads with `text=True, encoding="utf-8"`: no blank rows.
    text = raw.getvalue().decode("utf-8").replace("\r\n", "\n")
    assert list(csv.reader(io.StringIO(text))) == [["n", "s"], ["1", "₹ 5"], ["2", ""]]


def test_query_error_text_reaches_the_caller_as_utf8(monkeypatch):
    # A database error is written to stderr raw; the caller decodes it as UTF-8, strictly.
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw, encoding="cp1252"))
    execute_sql._utf8_stdout()
    sys.stderr.write('relation "café_€" does not exist\n')
    sys.stderr.flush()
    assert raw.getvalue().decode("utf-8") == 'relation "café_€" does not exist\n'


def test_utf8_stdout_leaves_a_stream_without_reconfigure_alone(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(sys, "stderr", buf)
    execute_sql._utf8_stdout()
    assert sys.stdout is buf and sys.stderr is buf


def test_sm_cli_prints_symbols_the_ansi_code_page_lacks(monkeypatch):
    pytest.importorskip("pydantic")
    sys.path.insert(0, str(SCRIPTS))
    from semantic_model import cli

    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    cli._utf8_stdio()
    print("→ ₹")
    sys.stdout.flush()
    assert raw.getvalue().decode("utf-8").strip() == "→ ₹"


# --- the source scan ----------------------------------------------------------------------------

_TEXT_IO = {"open", "fdopen", "read_text", "write_text"}
# Modules whose `open` is not a text-file open.
_NOT_TEXT_OPEN = {"os", "gzip", "tarfile", "zipfile", "webbrowser", "wave", "sqlite3", "shelve", "dbm"}


def _mode(call: ast.Call, position: int) -> object:
    for kw in call.keywords:
        if kw.arg == "mode":
            return kw.value.value if isinstance(kw.value, ast.Constant) else None
    if len(call.args) > position and isinstance(call.args[position], ast.Constant):
        return call.args[position].value
    return None


def _needs_encoding(call: ast.Call) -> bool:
    """A call that decodes or encodes text at the platform default unless it names an encoding."""
    if any(kw.arg == "encoding" for kw in call.keywords):
        return False
    func = call.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    receiver = func.value.id if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else None
    if name == "run" and receiver == "subprocess":
        return any(kw.arg in ("text", "universal_newlines") for kw in call.keywords)
    # configparser's `read(path)`; a file handle's `read()` takes no path. Matched by the names every
    # parser here goes by, since the AST has no types: a parser under another name is not caught.
    if name == "read" and receiver in ("cfg", "cp", "parser", "config") and call.args:
        return True
    if name not in _TEXT_IO:
        return False
    if name == "fdopen":
        mode = _mode(call, 1)
        return not (isinstance(mode, str) and "b" in mode)
    if name == "open":
        if receiver in _NOT_TEXT_OPEN:
            return False
        mode = _mode(call, 1 if isinstance(func, ast.Name) else 0)
        if isinstance(mode, str) and "b" in mode:
            return False
    return True


def test_every_text_read_and_write_names_its_encoding():
    offenders = []
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _needs_encoding(node):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {ast.unparse(node)[:100]}")
    assert not offenders, (
        "These read or write text at the platform's default encoding, which on Windows is the ANSI "
        "code page. Add encoding='utf-8':\n" + "\n".join(offenders)
    )


def test_scan_catches_each_shape_it_claims_to():
    sources = [
        'open(p)', 'open(p, "w")', 'p.open()', 'p.read_text()', 'p.write_text(s)',
        'cfg.read(path)', 'subprocess.run(cmd, text=True)', 'os.fdopen(fd, "w")',
    ]
    clean = [
        'open(p, "rb")', 'p.open("wb")', 'open(p, encoding="utf-8")', 'fh.read()', 'os.open(p, 0)',
        'subprocess.run(cmd, text=True, encoding="utf-8")', 'subprocess.run(cmd)', 'os.fdopen(fd, "wb")',
    ]
    for src in sources:
        assert _needs_encoding(ast.parse(src).body[0].value), src
    for src in clean:
        assert not _needs_encoding(ast.parse(src).body[0].value), src


# --- sm's interpreter ---------------------------------------------------------------------------

# Passes sm's import and version checks, answers its probe as a real Python, and records each call.
_SHIM = """#!/bin/sh
echo "$0 $*" >> "$SM_SHIM_LOG"
case "$2" in *agami-python-ok*) echo agami-python-ok ;; esac
exit 0
"""


# The App Execution Alias with no Store Python behind it: runs nothing, and (as reported) can still
# exit 0, so only the missing output gives it away.
_STUB = """#!/bin/sh
echo "$0 $*" >> "$SM_SHIM_LOG"
exit 0
"""


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_sm(tmp_path: Path, path_dirs: list[Path]) -> str:
    """Every interpreter call `sm` made, one per log entry (a `-c` program spans several lines)."""
    log = tmp_path / "shim.log"
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.pathsep.join([*map(str, path_dirs), "/usr/bin", "/bin"]),
        "SM_SHIM_LOG": str(log),
    }
    subprocess.run(["bash", str(SM), "areas", str(tmp_path)], env=env, capture_output=True, check=False)
    return log.read_text(encoding="utf-8") if log.exists() else ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shims; the case simulated is the Windows one")
def test_sm_reads_the_configured_interpreter_without_a_python(tmp_path):
    stub = _exe(tmp_path / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "python3", _SHIM)
    real = _exe(tmp_path / "Python312" / "python", _SHIM)
    config = tmp_path / "home" / "agami-artifacts" / "local" / ".config"
    config.parent.mkdir(parents=True)
    # Written the way JSON writes a Windows path: every separator an escaped backslash.
    config.write_text(json.dumps({"tool_paths": {"python3": str(real).replace("/", "\\")}}, indent=2), encoding="utf-8")

    ran = _run_sm(tmp_path, [stub.parent])
    assert str(stub) not in ran
    assert f"{real} -X utf8 -m semantic_model.cli areas {tmp_path}" in ran


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shims; the case simulated is the Windows one")
def test_sm_skips_the_store_stub_on_path(tmp_path):
    stub = _exe(tmp_path / "WindowsApps" / "python3", _STUB)
    real = _exe(tmp_path / "bin" / "python3", _SHIM)

    ran = _run_sm(tmp_path, [stub.parent, real.parent])
    # Probed once, found to run nothing, and never used for anything else.
    assert [line for line in ran.splitlines() if line.startswith(str(stub))] == [f'{stub} -c print("agami-python-ok")']
    assert f"{real} -X utf8 -m semantic_model.cli areas {tmp_path}" in ran


# --- the other Windows-only failures ------------------------------------------------------------


def test_no_glibc_only_strftime_directive():
    # `%-d` and friends raise "Invalid format string" on Windows; two dashboards never rendered.
    offenders = []
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "strftime" and node.args
                        and isinstance(node.args[0], ast.Constant) and "%-" in str(node.args[0].value)):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, offenders


def test_credentials_mode_check_is_posix_only():
    sys.path.insert(0, str(SCRIPTS))
    import connect_resolve

    assert connect_resolve._mode_is_private(0o100600, posix=True)
    assert not connect_resolve._mode_is_private(0o100666, posix=True)
    # What NTFS reports for every file; no chmod can change it.
    assert connect_resolve._mode_is_private(0o100666, posix=False)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8-sig"])
def test_promote_accepts_a_template_saved_with_a_byte_order_mark(tmp_path, encoding):
    sys.path.insert(0, str(SCRIPTS))
    from promote_credentials import promote

    (tmp_path / "credentials.example").write_text("[main]\ntype = postgres\npassword = pässwörd\n", encoding=encoding)
    msg, code = promote(tmp_path)
    assert (msg, code) == ("SECURED main", 0)
    # Moved into place as plain UTF-8, which every reader of the credentials file expects.
    assert (tmp_path / "credentials").read_bytes() == "[main]\ntype = postgres\npassword = pässwörd\n".encode("utf-8")


def test_promote_refuses_a_template_that_is_not_text(tmp_path):
    sys.path.insert(0, str(SCRIPTS))
    from promote_credentials import promote

    (tmp_path / "credentials.example").write_bytes("[main]\npassword = café\n".encode("cp1252"))
    msg, code = promote(tmp_path)
    assert code == 4 and "save it as UTF-8" in msg
    assert not (tmp_path / "credentials").exists()


def test_promote_appends_to_credentials_that_carry_a_utf8_bom(tmp_path):
    sys.path.insert(0, str(SCRIPTS))
    from promote_credentials import promote

    (tmp_path / "credentials").write_text("[main]\ntype = postgres\n", encoding="utf-8-sig")
    (tmp_path / "credentials.example").write_text("[other]\ntype = sqlite\n", encoding="utf-8")
    assert promote(tmp_path) == ("APPENDED other", 0)


def test_a_second_legacy_backup_does_not_collide_with_the_first(tmp_path):
    pytest.importorskip("pydantic")
    sys.path.insert(0, str(SCRIPTS))
    from semantic_model import introspect

    for generation in ("first", "second"):
        (tmp_path / "index.yaml").write_text(generation, encoding="utf-8")
        (tmp_path / "sales").mkdir()
        (tmp_path / "sales" / "_schema.yaml").write_text(generation, encoding="utf-8")
        introspect._backup_legacy_model(tmp_path)

    backup = tmp_path / ".legacy_backup"
    assert (backup / "index.yaml").read_text(encoding="utf-8") == "first"
    assert (backup / "index.yaml.1").read_text(encoding="utf-8") == "second"
    assert (backup / "sales" / "_schema.yaml").read_text(encoding="utf-8") == "first"
    assert (backup / "sales.1" / "_schema.yaml").read_text(encoding="utf-8") == "second"


@pytest.mark.parametrize("schema", ["CON", "aux", "Com1", "lpt9", "nul"])
def test_a_schema_named_for_a_windows_device_gets_a_usable_area(schema):
    pytest.importorskip("pydantic")
    sys.path.insert(0, str(SCRIPTS))
    from semantic_model import build

    assert build._area_key(schema) == schema.lower() + "_"
    assert build._area_key("console") == "console"


def test_preview_cells_follow_the_label_rule_answers_use():
    sys.path.insert(0, str(SCRIPTS))
    from semantic_model import units

    rows = units.format_rows(["fiscal_year", "project_number", "total"], [[2022, 1234567, 1234567]], {"total": "USD"})
    assert rows == [["2022", "1234567", "$1,234,567.00"]]
    assert "| 2022 | 1234567 | $1,234,567.00 |" in units.format_table(
        ["fiscal_year", "project_number", "total"], [[2022, 1234567, 1234567]], {"total": "USD"})


# --- Claude Desktop on Windows ------------------------------------------------------------------


def _desktop():
    sys.path.insert(0, str(SCRIPTS))
    import setup_desktop_mcp

    return setup_desktop_mcp


def test_desktop_config_goes_where_the_store_build_reads_it(monkeypatch, tmp_path):
    sd = _desktop()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert sd.desktop_config_path(None) == tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"

    store = tmp_path / "Local" / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
    store.mkdir(parents=True)
    assert sd.desktop_config_path(None) == store / "claude_desktop_config.json"


@pytest.mark.parametrize(("platform", "quit_how", "logs"), [
    ("darwin", "Cmd+Q", "Library/Logs/Claude"),
    ("win32", "system tray", "logs"),
    ("linux", "fully quit", "logs"),
])
def test_restart_hint_names_this_platforms_steps(monkeypatch, platform, quit_how, logs):
    sd = _desktop()
    monkeypatch.setattr(sys, "platform", platform)
    hint = sd.restart_hint(Path("cfgdir") / "claude_desktop_config.json", "agami")
    assert quit_how in hint and logs in hint and "mcp-server-agami.log" in hint
    if platform != "darwin":
        assert "Cmd+Q" not in hint and "Library/Logs" not in hint


def test_desktop_merge_warns_before_repointing_a_profile(tmp_path, capsys):
    sd = _desktop()
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"agami": {"env": {"AGAMI_PROFILE": "first"}}}}), encoding="utf-8")
    sd.merge_into_config(cfg, "agami", {"command": "/py", "args": [], "env": {"AGAMI_PROFILE": "second"}}, dry_run=False)
    err = capsys.readouterr().err
    assert "served profile 'first'" in err and "--server-name agami-second" in err


def test_desktop_merge_reads_a_config_with_a_bom_and_leaves_a_non_utf8_one_alone(tmp_path):
    sd = _desktop()
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {}}}), encoding="utf-8-sig")
    new, _ = sd.merge_into_config(cfg, "agami", {"command": "/py", "args": [], "env": {}}, dry_run=False)
    assert set(new["mcpServers"]) == {"other", "agami"}

    cfg.write_bytes(json.dumps({"mcpServers": {"other": {"note": "café"}}}, ensure_ascii=False).encode("cp1252"))
    before = cfg.read_bytes()
    with pytest.raises(SystemExit, match="not UTF-8 text"):
        sd.merge_into_config(cfg, "agami", {"command": "/py", "args": [], "env": {}}, dry_run=False)
    assert cfg.read_bytes() == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shims; the case simulated is the Windows one")
def test_sm_uses_a_python_installed_from_the_store(tmp_path):
    # Its aliases share the stub's folder, so the path alone cannot rule it out.
    store = _exe(tmp_path / "WindowsApps" / "python3", _SHIM)

    ran = _run_sm(tmp_path, [store.parent])
    assert f"{store} -X utf8 -m semantic_model.cli areas {tmp_path}" in ran


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shims; the case simulated is the Windows one")
def test_sm_does_not_trust_a_configured_store_stub(tmp_path):
    stub = _exe(tmp_path / "WindowsApps" / "python3", _STUB)
    real = _exe(tmp_path / "bin" / "python3", _SHIM)
    config = tmp_path / "home" / "agami-artifacts" / "local" / ".config"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"tool_paths": {"python3": str(stub)}}), encoding="utf-8")

    ran = _run_sm(tmp_path, [real.parent])
    assert f"{real} -X utf8 -m semantic_model.cli areas {tmp_path}" in ran
