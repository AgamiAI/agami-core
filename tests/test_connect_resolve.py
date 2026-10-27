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


def _fake_candidates(monkeypatch, paths, *, deps_for, driver_for, no_pip=(), lib=None, plugin="0.9.18"):
    """Wire _resolve_interpreter's probe seams to fake interpreters, so the
    scoring/tie-break runs on controlled inputs (the real host Python set can't
    exercise the fix — every interpreter there happens to be fully equipped).
    `no_pip` lists interpreters without pip; `lib` maps one to its agami-core version
    (the rest carry the plugin's own version)."""
    monkeypatch.setattr(CR, "_candidate_interpreters", lambda: list(paths))
    monkeypatch.setattr(CR, "plugin_version", lambda: plugin)
    lib = lib or {}
    monkeypatch.setattr(CR, "_probe_install", lambda py: {
        "has_pip": py not in no_pip, "agami_core": lib.get(py, plugin), "this_checkout": False})

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


def test_duckdb_driver_requires_pytz(monkeypatch):
    """ACE-062 A3: the duckdb driver probe must require pytz as well — duckdb needs it to
    materialize TIMESTAMP WITH TIME ZONE values. An interpreter with duckdb but not pytz
    must score has_driver=False so the skill installs pytz before the first timestamptz
    query (rather than the query failing at runtime with 'pytz failed to import')."""
    no_pytz, full = "/fake/duckdb-no-pytz/python3", "/fake/duckdb-pytz/python3"
    have = {no_pytz: {"pydantic", "sqlglot", "yaml", "duckdb"},
            full: {"pydantic", "sqlglot", "yaml", "duckdb", "pytz"}}
    monkeypatch.setattr(CR, "_candidate_interpreters", lambda: [no_pytz, full])
    # content-aware probe: an interpreter passes only if it has EVERY requested module
    monkeypatch.setattr(CR, "_probe",
                        lambda py, mods: all(m in have.get(py, set()) for m in mods if m))

    res = CR._resolve_interpreter("duckdb", configured=None)
    scored = {c["python3"]: c for c in res["candidates_scored"]}
    assert scored[no_pytz]["has_driver"] is False   # duckdb present, pytz missing → not ready
    assert scored[full]["has_driver"] is True
    assert res["python3"] == full                    # the pytz-equipped interpreter is chosen


def test_secret_values_never_reach_the_output(tmp_path, monkeypatch, capsys):
    # The output lands in the agent's transcript: a field's presence is reported, never a secret's value,
    # and a connection URL keeps its host/user but not the password written inline.
    local = tmp_path / "local"
    local.mkdir()
    creds = local / "credentials"
    creds.write_text(
        "[p]\ntype = redshift\nhost = your-cluster.example.com\nuser = demo\n"
        "password = hunter2-pw\ntoken = tok-xyz\nprivate_key_passphrase = pp-abc\n"
        "url = postgresql://demo:url-pw-123@your-cluster.example.com:5439/db?sslmode=require\n",
        encoding="utf-8",
    )
    os.chmod(creds, 0o600)
    data, _ = _run(monkeypatch, tmp_path, AGAMI_PROFILE="p")
    fields = data["credentials"]["fields"]
    raw = json.dumps(data)
    for secret in ("hunter2-pw", "tok-xyz", "pp-abc", "url-pw-123"):
        assert secret not in raw
    assert fields["password"] == fields["token"] == CR._REDACTED
    assert fields["url"].startswith("postgresql://demo:") and "your-cluster.example.com:5439" in fields["url"]
    assert fields["host"] == "your-cluster.example.com" and data["credentials"]["type"] == "redshift"


def test_pipless_stale_venv_is_skipped_with_a_reason(monkeypatch):
    # #529: a project .venv first on PATH, with an old agami-core and no pip, was recorded as the
    # interpreter, and `sm install` could never upgrade it.
    venv, good = "/fake/repo/.venv/bin/python3", "/fake/usr/python3"
    _fake_candidates(monkeypatch, [venv, good], deps_for={venv, good}, driver_for={venv, good},
                     no_pip={venv}, lib={venv: "0.9.14"})
    res = CR._resolve_interpreter("postgres", configured=None)
    assert res["python3"] == good
    assert "postgres driver" in res["chosen_because"]
    scored = {c["python3"]: c for c in res["candidates_scored"]}
    assert scored[venv]["skipped"] is True and scored[venv]["has_pip"] is False
    assert "no pip" in scored[venv]["reason"] and "0.9.14" in scored[venv]["reason"]
    assert scored[good]["skipped"] is False and "reason" not in scored[good]
    assert res["warnings"] == []


def test_pipless_but_current_venv_is_still_chosen(monkeypatch):
    # No pip is only disqualifying when something needs installing.
    venv, other = "/fake/repo/.venv/bin/python3", "/fake/usr/python3"
    _fake_candidates(monkeypatch, [venv, other], deps_for={venv, other}, driver_for={venv, other},
                     no_pip={venv})
    res = CR._resolve_interpreter("postgres", configured=None)
    assert res["python3"] == venv
    assert not res["candidates_scored"][0]["skipped"]


def test_pipless_configured_interpreter_without_the_library_is_skipped(monkeypatch):
    configured, good = "/fake/cfg/python3", "/fake/usr/python3"
    _fake_candidates(monkeypatch, [good], deps_for={configured, good}, driver_for=set(),
                     no_pip={configured}, lib={configured: None})
    monkeypatch.setattr(CR.os, "access", lambda p, m: True)
    res = CR._resolve_interpreter("sqlite", configured=configured)
    assert res["python3"] == good
    assert "no agami-core installed" in res["candidates_scored"][0]["reason"]


def test_agami_python_is_kept_but_warned_when_it_cannot_be_brought_current(monkeypatch):
    pinned = "/fake/pinned/python3"
    monkeypatch.setenv("AGAMI_PYTHON", pinned)
    _fake_candidates(monkeypatch, [pinned, "/fake/usr/python3"], deps_for={pinned, "/fake/usr/python3"},
                     driver_for=set(), no_pip={pinned}, lib={pinned: "0.9.11"})
    res = CR._resolve_interpreter("sqlite", configured=None)
    assert res["python3"] == pinned and "$AGAMI_PYTHON" in res["chosen_because"]
    kinds = {w["kind"]: w["detail"] for w in res["warnings"]}
    assert "no pip" in kinds["agami_python_cannot_be_brought_current"]
    stale = kinds["library_older_than_plugin"]
    assert "0.9.11" in stale and "0.9.18" in stale and pinned in stale and "sm\" install" in stale


def test_stale_library_with_pip_is_chosen_with_a_warning_in_anomalies(tmp_path, monkeypatch, capsys):
    py = "/fake/usr/python3"
    _fake_candidates(monkeypatch, [py], deps_for={py}, driver_for=set(), lib={py: "0.9.11"})
    data, anomalies = _run(monkeypatch, tmp_path)
    assert data["interpreter"]["python3"] == py and data["interpreter"]["agami_core"] == "0.9.11"
    assert data["interpreter"]["plugin_version"] == "0.9.18"
    (stale,) = [a for a in anomalies if a["kind"] == "library_older_than_plugin"]
    assert "0.9.11" in stale["detail"] and "0.9.18" in stale["detail"]
    assert "older than this plugin (0.9.18)" in capsys.readouterr().err


def test_unknown_versions_skip_nothing_and_warn_nothing(monkeypatch):
    py = "/fake/usr/python3"
    _fake_candidates(monkeypatch, [py], deps_for={py}, driver_for=set(), no_pip={py}, plugin=None,
                     lib={py: None})
    res = CR._resolve_interpreter("sqlite", configured=None)
    assert res["python3"] == py and res["warnings"] == []
    assert res["candidates_scored"][0]["skipped"] is False


def test_windows_virtualenv_layout():
    assert CR._venv_python("venv", windows=True).replace("\\", "/") == "venv/Scripts/python.exe"
    assert CR._venv_python("venv", windows=False).replace("\\", "/") == "venv/bin/python"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executables")
def test_later_pythons_on_path_are_candidates_too(monkeypatch, tmp_path):
    # The first python3 on PATH may be skipped; the next one on PATH must still be considered.
    first, second = tmp_path / "venv" / "bin", tmp_path / "usr" / "bin"
    for d in (first, second):
        d.mkdir(parents=True)
        (d / "python3").write_text("#!/bin/sh\n")
        (d / "python3").chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(first), str(second)]))
    monkeypatch.delenv("AGAMI_PYTHON", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    got = CR._candidate_interpreters()
    assert got.index(str(first / "python3")) < got.index(str(second / "python3"))
