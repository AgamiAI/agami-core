"""Regression (#539): the plugin's bundled `lib/` must not shadow the installed `semantic_model`.

`lib/` used to hold a partial `semantic_model/` package (`__init__.py` + `units.py`). Started from
inside `lib/` (`python -m` puts the cwd first on sys.path) or with `lib/` first on the path, Python
imported that partial copy, so `semantic_model.cli` was "not found" and the query runner refused
with `model_unavailable`. Each test runs the real entry point in a subprocess on the suite's
interpreter, which has agami-core installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "plugins" / "agami" / "lib"
SAMPLE = REPO / "plugins" / "agami" / "samples" / "store"
sys.path.insert(0, str(SAMPLE))

import build_sample  # noqa: E402

PROFILE = "demo"
SQL = "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY status"


@pytest.fixture(scope="module")
def store(tmp_path_factory) -> dict[str, Path]:
    """The sample store as profile `demo`: its model under an artifacts dir, and its SQLite file."""
    base = tmp_path_factory.mktemp("no-shadow")
    db = base / "store.db"
    build_sample.build(db, prefer_cli=False)
    shutil.copytree(SAMPLE / "model", base / PROFILE)
    return {"base": base, "db": db, "model": base / PROFILE}


def _env(store: dict[str, Path], *, lib_first: bool) -> dict[str, str]:
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(store["base"]),
           f"DATASOURCE_URL__{PROFILE.upper()}": f"sqlite:///{store['db']}",
           "PYTHONPATH": str(LIB) if lib_first else ""}
    for key in ("AGAMI_DB_URL", "APP_DATABASE_URL", "PYTHONSAFEPATH"):
        env.pop(key, None)
    return env


def _run(args: list[str], store: dict[str, Path], *, cwd: Path, lib_first: bool) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=cwd, env=_env(store, lib_first=lib_first),
                          capture_output=True, text=True, timeout=120)


# cwd = lib/ is `python -m` from inside the folder; PYTHONPATH = lib/ is lib first on sys.path.
_LAYOUTS = [pytest.param(True, False, id="cwd-is-lib"), pytest.param(False, True, id="lib-first-on-path")]


@pytest.mark.parametrize(("in_lib", "lib_first"), _LAYOUTS)
def test_sm_validate_runs_with_lib_first(store, tmp_path, in_lib, lib_first):
    r = _run(["-m", "semantic_model.cli", "validate", str(store["model"])], store,
             cwd=LIB if in_lib else tmp_path, lib_first=lib_first)
    assert r.returncode == 0, r.stderr + r.stdout


@pytest.mark.parametrize(("in_lib", "lib_first"), _LAYOUTS)
def test_query_runner_runs_with_lib_first(store, tmp_path, in_lib, lib_first):
    r = _run(["-m", "execute_sql", "--profile", PROFILE, "--sql", SQL], store,
             cwd=LIB if in_lib else tmp_path, lib_first=lib_first)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "model_unavailable" not in r.stdout + r.stderr
    assert r.stdout.splitlines()[0].replace('"', "") == "status,n"


def test_lib_ships_no_semantic_model_package():
    """Any `semantic_model` package under lib/ is a partial one that shadows the installed one."""
    assert not (LIB / "semantic_model" / "__init__.py").exists()
