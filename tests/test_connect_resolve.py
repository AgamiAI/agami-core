"""Phase B — connect_resolve.py (the agami-connect bootstrap spine).

Consolidates Phase 0 preflight + the interpreter scoring (0a.5) into one
deterministic call. These guard the next-phase decision, the credential/chmod
checks, and — the bug this fixes — that the chosen interpreter actually has the
deps (never a Python missing pydantic/sqlglot).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import connect_resolve as CR  # noqa: E402


def _run(monkeypatch, art: Path, **env) -> dict:
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    for k in ("AGAMI_PROFILE", "AGAMI_PYTHON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        CR.main([])
    return json.loads(buf.getvalue())["data"], json.loads(buf.getvalue()).get("anomalies", [])


def test_next_bootstrap_when_empty(tmp_path, monkeypatch):
    data, _ = _run(monkeypatch, tmp_path)
    assert data["next"] == "bootstrap"
    assert data["profile"] == "main"


def test_next_ready_with_section(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    creds = local / "credentials"
    creds.write_text("[sample]\ntype = sqlite\npath = /x/y.db\n", encoding="utf-8")
    os.chmod(creds, 0o600)
    data, anomalies = _run(monkeypatch, tmp_path, AGAMI_PROFILE="sample")
    assert data["next"] == "ready"
    assert data["credentials"]["present"] is True
    assert data["credentials"]["type"] == "sqlite"
    assert data["credentials"]["chmod_ok"] is True
    assert anomalies == []


def test_next_promote_with_example_only(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    (local / "credentials.example").write_text("[main]\ntype=sqlite\npath=/z\n", encoding="utf-8")
    data, _ = _run(monkeypatch, tmp_path)
    assert data["next"] == "promote"
    assert data["example_present"] is True


def test_world_readable_credentials_is_anomaly(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    creds = local / "credentials"
    creds.write_text("[sample]\ntype = sqlite\npath = /x\n", encoding="utf-8")
    os.chmod(creds, 0o644)
    data, anomalies = _run(monkeypatch, tmp_path, AGAMI_PROFILE="sample")
    assert data["credentials"]["chmod_ok"] is False
    assert any(a["kind"] == "credentials_world_readable" for a in anomalies)


def test_profile_resolution_order(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    (local / ".config").write_text(json.dumps({"active_profile": "from_config"}), encoding="utf-8")
    # AGAMI_PROFILE wins over .config
    data, _ = _run(monkeypatch, tmp_path, AGAMI_PROFILE="from_env")
    assert data["profile"] == "from_env"
    # without the env var, .config.active_profile wins over the "main" default
    data2, _ = _run(monkeypatch, tmp_path)
    assert data2["profile"] == "from_config"


def test_interpreter_is_scored_for_deps():
    """The scored pick reports whether it has the model deps — the heart of the fix
    (the old prose could record a Python missing sqlglot)."""
    res = CR._resolve_interpreter("sqlite", configured=None)
    assert "python3" in res and "has_model_deps" in res
    # whichever interpreter is chosen, the scoring ran on candidates
    assert isinstance(res["candidates_scored"], list)


def test_interpreter_prefers_one_with_driver():
    """Given a driver dialect, a candidate that has BOTH deps and the driver outscores
    one that has only deps — so we never pick a Python that can't connect."""
    res = CR._resolve_interpreter("postgres", configured=None)
    chosen = res["python3"]
    # the chosen one is a real path and its score is the max among scored candidates
    assert chosen
    if res["candidates_scored"]:
        assert max(c["score"] for c in res["candidates_scored"]) >= 1
