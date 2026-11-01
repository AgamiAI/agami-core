"""`uv run dev.py check` also runs the pane's mod checks, the same three CI's `mod` job runs."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _dev():
    spec = importlib.util.spec_from_file_location("dev_under_test", REPO_ROOT / "dev.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_without_claude_the_mod_checks_print_a_skip_and_pass(monkeypatch, capsys):
    dev = _dev()
    monkeypatch.setattr(dev.shutil, "which", lambda name: None)
    ran = []
    monkeypatch.setattr(dev, "run", lambda cmd, **kw: ran.append(cmd) or 0)

    assert dev.mod() == 0
    assert ran == []
    assert "skipped" in capsys.readouterr().out


def test_without_npx_the_mod_checks_print_a_skip_and_pass(monkeypatch, capsys):
    dev = _dev()
    monkeypatch.setattr(
        dev.shutil, "which", lambda name: None if name == "npx" else f"/usr/bin/{name}"
    )
    ran = []
    monkeypatch.setattr(dev, "run", lambda cmd, **kw: ran.append(cmd) or 0)

    assert dev.mod() == 0
    assert ran == []
    assert "npx" in capsys.readouterr().out


def test_with_claude_the_mod_checks_validate_typecheck_and_test(monkeypatch):
    dev = _dev()
    monkeypatch.setattr(dev.shutil, "which", lambda name: f"/usr/bin/{name}")
    ran = []
    monkeypatch.setattr(dev, "run", lambda cmd, **kw: ran.append((cmd, kw.get("env"))) or 0)

    assert dev.mod() == 0
    cmds = [" ".join(cmd) for cmd, _ in ran]
    assert cmds[0] == "claude plugin validate plugins/agami --strict"
    # tsc reads the types the engine writes when it loads the mod, so a load comes first.
    load_cmd, load_env = ran[1]
    assert load_cmd[:3] == ["claude", "--plugin-dir", "plugins/agami"]
    assert load_env["CLAUDE_CODE_PLUGIN_DIR_WATCH"] == "1"
    assert cmds[2].endswith("tsc -p plugins/agami")
    assert cmds[3] == "claude plugin test plugins/agami"


def test_a_failing_mod_check_fails_the_task(monkeypatch):
    dev = _dev()
    monkeypatch.setattr(dev.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        dev, "run", lambda cmd, **kw: 1 if cmd[:3] == ["claude", "plugin", "test"] else 0
    )

    assert dev.mod() != 0


def test_check_runs_the_mod_checks(monkeypatch):
    dev = _dev()
    called = []
    for name in ("lint", "test", "secrets", "_lib_drift"):
        monkeypatch.setattr(dev, name, lambda: 0)
    monkeypatch.setattr(dev, "mod", lambda: called.append(True) or 0)

    assert dev.check() == 0
    assert called == [True]
