"""A function that runs a nested query or dumps a table is refused at the execution chokepoint.

`tests/test_sql_guard.py` proves the read-only gate refuses each such call. This proves the property
that matters end to end: through `execute_guarded`, against a real model on disk that leaves a column
out, the statement never reaches the executor. Before the fix, `table_to_xml` and `ts_stat` passed
the read-only gate AND every scope gate in the model pass, because the relation they read is named
inside a string literal, and the literal is blanked before any gate looks.

The controls pin the other half: the same column, read directly, is refused by the column-scope gate.
So the refusal here is the read-only gate closing a road around the model, not the model pass
having learnt to see inside strings.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
import tools  # noqa: E402

PROFILE = "acme"


class _SpyExecutor:
    """Records what reached the connect-and-run step, so a refusal can be shown to have stopped it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, vetted_sql: str, creds: dict, *, profile: str) -> execute_sql.ExecResult:
        self.calls.append(vetted_sql)
        return execute_sql.ExecResult(columns=["c"], rows=[(1,)], truncated=False)


def _write_model(root: Path) -> None:
    """`demo.customers` declaring `id` and `name` only; the table also holds `tax_id`, left out."""
    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump({"datasource": "Shop", "version": 1,
                        "storage_connections": [{"name": "c", "storage_type": "PostgreSQL"}],
                        "subject_areas": ["subject_areas/sales"]})
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": "sales", "tables": [
            {"storage_connection": "c", "schema": "demo", "table": "customers"}]})
    )
    (root / "subject_areas" / "sales" / "tables" / "customers.yaml").write_text(
        yaml.safe_dump({
            "name": "customers", "schema": "demo", "storage_connection": "c", "grain": ["id"],
            "description": "customers",
            "columns": [
                {"name": "id", "type": "integer", "primary_key": True},
                {"name": "name", "type": "string"},
            ],
        })
    )


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    """`execute_guarded` over the model above. Returns (envelope, spy)."""
    tools.set_injected_executor(None)
    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setattr(execute_sql, "_load_credentials",
                        lambda p, org_id="local": {"type": "postgres", "host": "your-cluster.example.com"})

    def _run(sql: str):
        spy = _SpyExecutor()
        return execute_sql.execute_guarded(sql, PROFILE, "sales", executor=spy), spy

    yield _run
    tools.set_injected_executor(None)


@pytest.mark.parametrize("sql", [
    "SELECT table_to_xml('demo.customers', true, false, '') AS dump",
    "SELECT ts_stat('SELECT to_tsvector(''simple'', tax_id) FROM demo.customers') AS s",
    "SELECT query_to_xml_and_xmlschema('SELECT tax_id FROM demo.customers', true, false, '') AS d",
    "SELECT id FROM demo.customers WHERE length(table_to_xml('demo.customers', true, false, '')::text) > 0",
])
def test_a_string_named_read_never_reaches_the_executor(guarded, sql):
    env, spy = guarded(sql)
    assert env.status == "refused", env
    assert env.refusal.rule == guardrail.RULE_READ_ONLY
    assert spy.calls == []


def test_the_model_pass_still_owns_a_direct_read_of_the_left_out_column(guarded):
    """The control: the column the dump would have carried is out of scope when named directly."""
    env, spy = guarded("SELECT tax_id FROM demo.customers")
    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_COLUMN_SCOPE
    assert spy.calls == []


def test_a_declared_read_still_runs(guarded):
    """And the fixture is not refusing everything: a declared column reaches the executor."""
    env, spy = guarded("SELECT id, name FROM demo.customers")
    assert env.status == "ok", env
    assert spy.calls == ["SELECT id, name FROM demo.customers"]
