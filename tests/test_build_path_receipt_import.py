"""The model build does not import the semantic-model runtime for a receipt it throws away.

`sm introspect` sends every statement through its own `execute_sql.py --no-safety --build-profile`
child: 181 of them for the bundled sample, 500+ for a modest warehouse. No model is ever resolved on
that path, so the receipt is always "no model", and importing `semantic_model.runtime` to reach that
answer cost ~300 ms of each child's ~365 ms. These tests pin that the build path skips the import,
that a resolved model still gets the receipt it always got, and that the no-model degradations keep
naming the cause they named before (the ACE-088 / ACE-071 tests pin the rest of that contract).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
from semantic_model import introspect as INTRO  # noqa: E402

from test_build_profile import PROFILE, _run, _write_model  # noqa: E402


@pytest.fixture
def model(tmp_path, monkeypatch):
    """`test_build_profile`'s Postgres model of `orders`, with the pass enforcing."""
    _write_model(tmp_path / PROFILE)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", "postgresql://demo@localhost:5432/demo")
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", execute_sql.SERVED_CHILD_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_GOVERNANCE_ENFORCED", "true")
    return tmp_path


def _build_argv(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The exact command line the build-time runner starts per statement, captured, not restated."""
    seen: list[list[str]] = []

    class _Done:
        returncode, stdout, stderr = 0, "x\n1\n", ""

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen.append(cmd)
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    INTRO.make_execute_sql_runner(PROFILE, building_the_model=True)("SELECT id FROM orders")
    monkeypatch.undo()
    (cmd,) = seen
    return cmd


def test_a_build_statement_never_imports_the_runtime(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER)")
        conn.execute("INSERT INTO orders VALUES (7)")
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", execute_sql.SERVED_CHILD_ENV):
        monkeypatch.delenv(var, raising=False)

    cmd = _build_argv(monkeypatch)
    assert "--no-safety" in cmd and "--build-profile" in cmd

    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(tmp_path / "art"),
           f"DATASOURCE_URL__{PROFILE.upper()}": f"sqlite:///{db}", "PYTHONUTF8": "1"}
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", execute_sql.SERVED_CHILD_ENV):
        env.pop(var, None)
    proc = subprocess.run([cmd[0], "-X", "importtime", *cmd[1:]],
                          capture_output=True, text=True, env=env)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.split() == ["id", "7"], "the statement ran and answered"
    imported = {line.rsplit("|", 1)[-1].strip() for line in proc.stderr.splitlines()
                if line.startswith("import time:")}
    assert "execute_sql" in imported or "guardrail" in imported, "importtime saw the child's imports"
    assert "semantic_model.runtime" not in imported


# What the receipt said before the import moved, with a model resolved, on both assemblers. Pinned
# verbatim: the change is allowed to reach the no-model answer sooner and nothing else.
_UNMATCHED = {"aggregate": False, "area": None, "confidence": None, "definition_prose": None,
              "expression": None, "kind": "output", "name": None, "origin": None,
              "review_state": None, "scope": "main", "signed_off_at": None, "signed_off_by": None,
              "signed_off_role": None, "source_tables": None, "status": "unmatched"}
_OK_RECEIPT = {
    "aggregates": {"items": [], "undetermined": None},
    "assumptions": {"items": [], "undetermined": None},
    "columns": {"items": [{**_UNMATCHED, "column": "id"}, {**_UNMATCHED, "column": "status"},
                          {"column": "public.orders.id", "kind": "reference"},
                          {"column": "public.orders.status", "kind": "reference"}],
                "undetermined": None},
    "joins": {"items": [], "undetermined": None},
    "model_version": None,
    "tables": {"items": [{"alias": "o", "declared": True, "filters": [], "freshness": None,
                          "qname": "public.orders", "ref": "orders", "rows": None,
                          "rows_as_of": None, "scan_risk": None, "scope": "main"}],
               "undetermined": None},
}
_WITHHELD = ("Withheld because the statement was refused. Reporting these would name parts of the "
             "model the statement never referenced, which would turn a refusal into a listing of the "
             "model.")
_REFUSED_RECEIPT = {
    **{name: {"items": [], "undetermined": _WITHHELD}
       for name in ("aggregates", "assumptions", "columns", "joins")},
    "model_version": None,
    "tables": {"items": [{"declared": True, "ref": "orders"}],
               "undetermined": "A refused statement is told which of the names it wrote the model "
                               "declares, and nothing further about any of them: no resolved name, "
                               "no row estimate, no freshness."},
}


@pytest.mark.parametrize(("sql", "status", "receipt"), [
    ("SELECT o.id, o.status FROM orders o", "ok", _OK_RECEIPT),
    ("SELECT o.secret FROM orders o", "refused", _REFUSED_RECEIPT),
])
def test_a_resolved_model_gets_the_receipt_it_always_got(model, sql, status, receipt):
    env, _ = _run(sql)

    assert env.status == status
    assert json.loads(json.dumps(asdict(env.receipt))) == receipt


@pytest.fixture
def no_model():
    """No resolved model, as on the build path. `_guard_model` is a ContextVar a test above may have
    set through `execute_guarded`, so it is cleared here and put back after."""
    token = execute_sql._guard_model.set(None)
    yield
    execute_sql._guard_model.reset(token)


def _reasons(receipt: guardrail.Receipt) -> set[str | None]:
    return {getattr(receipt, n).undetermined for n in guardrail.Receipt.SECTIONS}


def test_no_model_with_the_runtime_installed_says_no_model(no_model):
    """The build path's own answer, reached without the import."""
    receipt = execute_sql._receipt_for("SELECT 1", PROFILE, bounded=False)

    assert _reasons(receipt) == {guardrail.RECEIPT_NO_MODEL}


def test_no_model_with_a_runtime_module_that_is_missing_still_says_runtime(no_model, monkeypatch):
    """The finer-grained absence: the package is there and its runtime module is not."""
    monkeypatch.setitem(sys.modules, "semantic_model.runtime", None)

    receipt = execute_sql._receipt_for("SELECT 1", PROFILE, bounded=True)

    assert _reasons(receipt) == {guardrail.RECEIPT_NO_RUNTIME}


def test_no_model_with_the_package_blocked_says_runtime_even_once_it_was_imported(
    no_model, monkeypatch
):
    """The vendored mirror's state, simulated after the suite already imported the runtime: the
    import resolves the package first and fails there, so the lookup has to as well."""
    import semantic_model.runtime  # noqa: F401  (present in sys.modules, which is the trap)

    monkeypatch.setitem(sys.modules, "semantic_model", None)

    receipt = execute_sql._receipt_for("SELECT 1", PROFILE, bounded=False)

    assert _reasons(receipt) == {guardrail.RECEIPT_NO_RUNTIME}


def test_no_model_with_a_runtime_lookup_that_raises_is_a_logged_defect(no_model, monkeypatch, caplog):
    """Looking the module up instead of importing it keeps "broken" apart from "absent", as the
    import did: anything but an ImportError is a defect, logged, and never "not installed"."""
    def boom(name, package=None):  # noqa: ANN001, ANN202
        raise RuntimeError("the runtime package blew up on the way in")

    monkeypatch.setattr(execute_sql.importlib.util, "find_spec", boom)

    receipt = execute_sql._receipt_for("SELECT 1", PROFILE, bounded=False)

    assert _reasons(receipt) == {guardrail.RECEIPT_BUILD_FAILED}
    assert any("failed to import" in r.getMessage() for r in caplog.records)


def test_no_model_with_a_package_whose_lookup_cannot_import_says_runtime(no_model, monkeypatch):
    """An ImportError on the way to the module (the package's own `__init__` missing a dependency)
    is the import's "absent" answer, so the lookup gives it too."""
    def missing(name, package=None):  # noqa: ANN001, ANN202
        raise ModuleNotFoundError("No module named 'pydantic'")

    monkeypatch.setattr(execute_sql.importlib.util, "find_spec", missing)

    receipt = execute_sql._receipt_for("SELECT 1", PROFILE, bounded=False)

    assert _reasons(receipt) == {guardrail.RECEIPT_NO_RUNTIME}
