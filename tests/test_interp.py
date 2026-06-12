"""Tests for _interp.py — the directly-invoked-script interpreter self-heal (re-exec under
agami's configured interpreter when PyYAML / the model deps are missing)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _interp  # noqa: E402


def test_present_dep_does_not_reexec(monkeypatch):
    calls = []
    monkeypatch.setattr(_interp.os, "execv", lambda p, a: calls.append((p, a)))
    _interp.ensure_deps("json")          # stdlib — always importable
    assert calls == []


def test_already_reexeced_does_not_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(_interp.os, "execv", lambda p, a: calls.append((p, a)))
    monkeypatch.setenv("AGAMI_REEXEC", "1")            # the guard flag is set
    monkeypatch.setenv("AGAMI_PYTHON", sys.executable)
    _interp.ensure_deps("a_missing_module_zzz_123")
    assert calls == []                                 # guarded — no second re-exec


def test_reexecs_under_configured_interpreter(tmp_path, monkeypatch):
    fake = tmp_path / "py"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    calls = []
    monkeypatch.setattr(_interp.os, "execv", lambda p, a: calls.append((p, a)))
    monkeypatch.delenv("AGAMI_REEXEC", raising=False)
    monkeypatch.setenv("AGAMI_PYTHON", str(fake))
    try:
        _interp.ensure_deps("a_missing_module_zzz_123")   # dep absent → should re-exec
        assert len(calls) == 1
        interp, argv = calls[0]
        assert interp == str(fake) and argv[0] == str(fake)   # re-exec under the configured interp
    finally:
        # ensure_deps sets AGAMI_REEXEC directly (not via monkeypatch) before execv — clean it up
        import os
        os.environ.pop("AGAMI_REEXEC", None)
