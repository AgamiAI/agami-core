"""
Tests for plugins/agami/scripts/setup_desktop_mcp.py (the pip-install model).

The load-bearing contract is **merge safety**: wiring agami into
`claude_desktop_config.json` must never lose a user's other keys or other MCP
servers, must back up before writing, and must refuse to touch a file that isn't
valid JSON (rather than silently overwrite it). The rest pins interpreter
detection, profile/db-type resolution, and the platform config paths.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import setup_desktop_mcp as sd  # noqa: E402

# --- merge safety -----------------------------------------------------------

def test_merge_preserves_other_keys_and_servers(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({
        "coworkUserFilesPath": "/Users/me/Claude",
        "mcpServers": {"other": {"command": "/bin/echo", "args": ["hi"]}},
    }))
    entry = {"command": "/py", "args": ["-m", "mcp_harness"], "env": {"AGAMI_PROFILE": "main"}}
    new, backup = sd.merge_into_config(cfg, "agami", entry, dry_run=False)

    assert new["coworkUserFilesPath"] == "/Users/me/Claude"   # unrelated key kept
    assert "other" in new["mcpServers"]                        # other server kept
    assert new["mcpServers"]["agami"] == entry                 # agami added
    assert backup is not None and backup.exists()              # backed up
    # file on disk round-trips and matches
    assert json.loads(cfg.read_text()) == new


def test_merge_creates_file_when_absent(tmp_path):
    cfg = tmp_path / "sub" / "claude_desktop_config.json"  # parent doesn't exist
    entry = {"command": "/py", "args": ["-m", "mcp_harness"], "env": {}}
    new, backup = sd.merge_into_config(cfg, "agami", entry, dry_run=False)
    assert cfg.exists()
    assert backup is None                                      # nothing to back up
    assert new["mcpServers"]["agami"] == entry


def test_merge_is_idempotent_update(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"agami": {"command": "/old", "args": [], "env": {}}}}))
    entry = {"command": "/new", "args": ["-m", "mcp_harness"], "env": {"AGAMI_PROFILE": "x"}}
    new, _ = sd.merge_into_config(cfg, "agami", entry, dry_run=False)
    assert new["mcpServers"]["agami"]["command"] == "/new"     # replaced, not duplicated
    assert len(new["mcpServers"]) == 1


def test_merge_dry_run_writes_nothing(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"keep": True}))
    before = cfg.read_text()
    new, backup = sd.merge_into_config(cfg, "agami", {"command": "/py", "args": [], "env": {}}, dry_run=True)
    assert backup is None
    assert cfg.read_text() == before                           # untouched on disk
    assert new["mcpServers"]["agami"]["command"] == "/py"       # plan computed in memory


def test_merge_refuses_invalid_json(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text("{ this is not json")
    with pytest.raises(SystemExit):
        sd.merge_into_config(cfg, "agami", {"command": "/py", "args": [], "env": {}}, dry_run=False)


# --- interpreter detection --------------------------------------------------

def test_find_interpreter_none_module_returns_current():
    # module=None (sqlite/stdlib) — the running interpreter qualifies.
    assert sd.find_interpreter(None, None) is not None


def test_find_interpreter_forced_importable():
    assert sd.find_interpreter("json", sys.executable) == str(Path(sys.executable).resolve())


def test_find_interpreter_unimportable_module_fails():
    assert sd.find_interpreter("this_module_does_not_exist_xyz", sys.executable) is None


# --- resolution -------------------------------------------------------------

def test_resolve_profile_env(monkeypatch):
    monkeypatch.setenv("AGAMI_PROFILE", "envprof")
    assert sd.resolve_profile(None) == "envprof"
    assert sd.resolve_profile("explicit") == "explicit"


def test_build_server_entry_shape():
    # The Desktop entry runs the installed package as a module, not a file path.
    entry = sd.build_server_entry("/py", "main", "1.2.3")
    assert entry["command"] == "/py"
    assert entry["args"] == ["-m", "mcp_harness"]
    assert entry["env"] == {"AGAMI_PROFILE": "main", "AGAMI_VERSION": "1.2.3"}


# --- platform config paths --------------------------------------------------

def test_desktop_config_path_override():
    assert sd.desktop_config_path("/tmp/x.json") == Path("/tmp/x.json")


def test_desktop_config_path_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    p = sd.desktop_config_path(None)
    assert p.parts[-3:] == ("Application Support", "Claude", "claude_desktop_config.json")


def test_desktop_config_path_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    p = sd.desktop_config_path(None)
    assert p.parts[-3:] == (".config", "Claude", "claude_desktop_config.json")


# --- package install (delegates to `sm install`, OCR-033 #8) ----------------

def test_ensure_package_installed_dry_run_delegates_no_exec(monkeypatch, capsys):
    monkeypatch.setattr(sd, "_interpreter_can_import", lambda py, mod: False)  # not present yet
    calls = []
    monkeypatch.setattr(sd.subprocess, "run", lambda *a, **k: calls.append(a))
    sd.ensure_package_installed("/py", dry_run=True)
    assert calls == []                                   # nothing executed in dry-run
    out = capsys.readouterr().out
    assert "would install" in out and "sm" in out        # delegates to sm, not a pip command


def test_ensure_package_installed_delegates_to_sm(monkeypatch):
    # absent before install → runs `sm install` with AGAMI_PYTHON; present after → no raise.
    states = iter([False, True])
    monkeypatch.setattr(sd, "_interpreter_can_import", lambda py, mod: next(states))
    seen = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        seen["env"] = k.get("env")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(sd.subprocess, "run", fake_run)
    sd.ensure_package_installed("/py", dry_run=False)
    assert seen["cmd"][0] == "bash" and seen["cmd"][-1] == "install"
    assert seen["cmd"][1].endswith("/sm")                # delegates to the sm launcher
    assert seen["env"]["AGAMI_PYTHON"] == "/py"          # installs into the chosen interpreter


def test_ensure_package_installed_skips_when_already_present(monkeypatch):
    monkeypatch.setattr(sd, "_interpreter_can_import", lambda py, mod: True)  # already importable
    calls = []
    monkeypatch.setattr(sd.subprocess, "run", lambda *a, **k: calls.append(a))
    sd.ensure_package_installed("/py", dry_run=False)
    assert calls == []                                   # no install attempted


def test_ensure_package_installed_raises_when_still_missing(monkeypatch):
    monkeypatch.setattr(sd, "_interpreter_can_import", lambda py, mod: False)  # never becomes importable
    monkeypatch.setattr(sd.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    with pytest.raises(RuntimeError):
        sd.ensure_package_installed("/py", dry_run=False)


def test_read_version_returns_a_version():
    # No arg now — derived from the cache-dir name, else the dev pyproject fallback (single source).
    v = sd.read_version()
    assert v and v[0].isdigit()
