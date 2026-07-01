"""Phase B — connect_resolve.py (the agami-connect bootstrap spine).

Consolidates Phase 0 preflight + the interpreter scoring (0a.5) into one
deterministic call. These guard the next-phase decision, the credential/chmod
checks, and — the bug this fixes — that the chosen interpreter actually has the
deps (never a Python missing pydantic/sqlglot).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import connect_resolve as CR  # noqa: E402


def _run(monkeypatch, art: Path, **env) -> dict:
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    for k in ("AGAMI_PROFILE", "AGAMI_PYTHON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        CR.main([])
    return json.loads(buf.getvalue())["data"], json.loads(buf.getvalue()).get("anomalies", [])


def test_next_bootstrap_when_empty(tmp_path, monkeypatch):
    data, _ = _run(monkeypatch, tmp_path)
    assert data["next"] == "bootstrap"
    # First-time bootstrap: the profile is unresolved — emit null (+ source), never the invented "main"
    # (so the skill narrates "first-time setup" without a fake name).
    assert data["profile"] is None
    assert data["profile_source"] == "default"


def test_named_profile_without_creds_keeps_name(tmp_path, monkeypatch):
    # The OTHER half of the gate: a named-but-not-onboarded profile → bootstrap, but the name must NOT
    # be nulled (only the bare "default" fallback is). This is what the skill's "name it only when
    # non-null" narration depends on.
    data, _ = _run(monkeypatch, tmp_path, AGAMI_PROFILE="staging")
    assert data["next"] == "bootstrap"
    assert data["profile"] == "staging"
    assert data["profile_source"] == "env"


def test_ready_with_default_main_section_is_not_nulled(tmp_path, monkeypatch):
    # A real [main] section with no env/active_profile → next=ready → the name must NOT be hidden
    # (the null-out is scoped to default-AND-bootstrap).
    local = tmp_path / "local"
    local.mkdir()
    creds = local / "credentials"
    creds.write_text("[main]\ntype = sqlite\npath = /x/y.db\n", encoding="utf-8")
    os.chmod(creds, 0o600)
    data, _ = _run(monkeypatch, tmp_path)
    assert data["next"] == "ready"
    assert data["profile"] == "main"
    assert data["profile_source"] == "default"


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
    assert data["profile_source"] == "env"
    # without the env var, .config.active_profile wins over the "main" default
    data2, _ = _run(monkeypatch, tmp_path)
    assert data2["profile"] == "from_config"
    assert data2["profile_source"] == "active_profile"


def _fake_candidates(monkeypatch, paths, *, deps_for, driver_for):
    """Wire _resolve_interpreter's two probe seams to fake interpreters, so the
    scoring/tie-break runs on controlled inputs (the real host Python set can't
    exercise the fix — every interpreter there happens to be fully equipped)."""
    monkeypatch.setattr(CR, "_candidate_interpreters", lambda: list(paths))

    def fake_probe(py: str, mods: list[str]) -> bool:
        mods = [m for m in mods if m]
        if not mods:
            return True
        # the model-deps probe asks for _MODEL_DEPS; anything else is the driver probe
        is_deps_probe = set(mods) == set(CR._MODEL_DEPS)
        return (py in deps_for) if is_deps_probe else (py in driver_for)

    monkeypatch.setattr(CR, "_probe", fake_probe)


def test_interpreter_prefers_candidate_with_driver(monkeypatch):
    """A candidate with BOTH model deps and the driver must outscore one with the deps
    only — the fix that stops us recording a Python that loads the model but can't
    connect. The deps-only Python is first in priority order, so a naive 'first that
    imports pydantic' would wrongly pick it."""
    deps_only, full = "/fake/deps-only/python3", "/fake/full/python3"
    _fake_candidates(monkeypatch, [deps_only, full],
                     deps_for={deps_only, full}, driver_for={full})
    res = CR._resolve_interpreter("postgres", configured=None)
    assert res["python3"] == full
    assert res["has_model_deps"] is True and res["has_driver"] is True
    scored = {c["python3"]: c for c in res["candidates_scored"]}
    assert scored[deps_only]["score"] == 1  # deps but no driver
    assert scored[full]["score"] == 2       # deps + driver → chosen


def test_interpreter_falls_back_to_base_when_none_equipped(monkeypatch):
    """When no candidate has the model deps, we still return a working base interpreter
    (has_model_deps=False) for the caller to pip-install into — never None/crash."""
    base = "/fake/bare/python3"
    _fake_candidates(monkeypatch, [base], deps_for=set(), driver_for=set())
    res = CR._resolve_interpreter("postgres", configured=None)
    assert res["python3"] == base
    assert res["has_model_deps"] is False
