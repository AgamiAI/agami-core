"""A conversation holding an out-of-date schema must not run SQL against the live model (#364).

The client keeps `get_datasource_schema` output as text in its context, so a conversation resumed
after the model changed — or after an older version was put back — writes SQL from a model the
server no longer serves. The schema response carries `model_version`, execute_sql takes it back, and
a missing or different one is refused with the live version named.

Driven end to end on a real served deployment (a SQLite store, the real deploy, the real handlers).
The only stand-in is the execution step, replaced by a sentinel that proves the gate let a call
through without needing a warehouse.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import guardrail  # noqa: E402
import model_deploy  # noqa: E402
import model_store  # noqa: E402
import tools  # noqa: E402
from store import Store  # noqa: E402

SQL = "SELECT sku FROM products"


class _Reached(Exception):
    """Raised in place of execution: the call got past every gate before running."""


class _Executor:
    def execute(self, vetted_sql, creds, *, profile):
        raise _Reached


def _write_model(root: Path, example_question: str = "how many products?") -> None:
    d = root / "demo"
    (d / "subject_areas" / "Catalog" / "tables").mkdir(parents=True, exist_ok=True)
    (d / "prompt_examples" / "Catalog").mkdir(parents=True, exist_ok=True)
    (d / "datasource.yaml").write_text(
        "datasource: acme\nversion: 1\ndescription: A neutral demo model.\n"
        "storage_connections:\n  - name: warehouse\n    storage_type: PostgreSQL\n"
        "subject_areas:\n  - Catalog\n"
    )
    (d / "subject_areas" / "Catalog" / "subject_area.yaml").write_text(
        "name: Catalog\ndescription: Products and pricing.\n"
    )
    (d / "subject_areas" / "Catalog" / "tables" / "products.yaml").write_text(
        "name: products\ndescription: Master product catalog.\n"
        "columns:\n  - name: id\n    type: uuid\n    primary_key: true\n  - name: sku\n    type: string\n"
    )
    (d / "prompt_examples" / "Catalog" / "examples.yaml").write_text(
        f"examples:\n  - question: {example_question}\n    sql: SELECT COUNT(*) FROM products\n"
    )


@pytest.fixture()
def served(tmp_path, monkeypatch):
    """A served deployment with `demo` deployed, and a `deploy()` to change it."""
    arts = tmp_path / "artifacts"
    _write_model(arts)
    db_url = "sqlite://" + str(tmp_path / "agami.db")
    for var in ("APP_DATABASE_URL", "AGAMI_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(arts))
    monkeypatch.setenv("AGAMI_ORG_ID", "local")
    tools.resolved_org_id.cache_clear()
    tools._sole_served_datasource.cache_clear()
    tools.bootstrap_paths()

    def deploy() -> str:
        store = Store.connect(db_url)
        store.run_migrations()
        try:
            model_deploy.deploy_models(store, arts, org_id="local")
            return model_store.newest_model_version(store, "demo", org_id="local")
        finally:
            store.close()

    def reached(*_a, **_k):
        raise _Reached

    previous = tools._INJECTED_EXECUTOR
    tools.set_injected_executor(_Executor())
    monkeypatch.setattr(tools, "_run_in_process", reached)
    yield arts, deploy
    tools.set_injected_executor(previous)
    tools._sole_served_datasource.cache_clear()
    tools.resolved_org_id.cache_clear()


def _schema(**extra) -> dict:
    return json.JSONDecoder().raw_decode(
        tools.tool_get_datasource_schema({"datasource": "demo", **extra})
    )[0]


def _run(**extra) -> dict:
    return json.loads(tools.tool_execute_sql({"datasource": "demo", "sql": SQL, **extra}))


def test_schema_and_examples_report_the_live_version(served):
    _arts, deploy = served
    live = deploy()
    assert _schema()["model_version"] == live
    examples = json.loads(tools.tool_get_prompt_examples({"datasource": "demo"}))
    assert examples["model_version"] == live


def test_the_version_the_schema_returned_is_let_through(served):
    _arts, deploy = served
    deploy()
    with pytest.raises(_Reached):
        _run(model_version=_schema()["model_version"])


def test_a_call_with_no_version_is_refused_and_told_what_to_fetch(served):
    _arts, deploy = served
    live = deploy()
    body = _run()
    assert body["status"] == "refused"
    assert body["refusal"]["rule"] == guardrail.RULE_STALE_MODEL
    assert live in body["refusal"]["remediation"]
    assert "get_datasource_schema" in body["refusal"]["remediation"]
    assert "get_prompt_examples" in body["refusal"]["remediation"]


def test_a_version_from_before_a_changed_example_is_refused(served):
    arts, deploy = served
    held = deploy()
    _write_model(arts, example_question="how many skus?")
    live = deploy()
    assert live != held
    body = _run(model_version=held)
    assert body["refusal"]["rule"] == guardrail.RULE_STALE_MODEL
    assert live in body["refusal"]["remediation"]
    with pytest.raises(_Reached):
        _run(model_version=_schema()["model_version"])


def test_putting_an_older_version_back_is_caught_by_equality_not_age(served):
    arts, deploy = served
    first = deploy()
    _write_model(arts, example_question="how many skus?")
    second = deploy()
    _write_model(arts)  # the first model's content again
    assert deploy() == first
    assert _run(model_version=second)["refusal"]["rule"] == guardrail.RULE_STALE_MODEL
    with pytest.raises(_Reached):
        _run(model_version=first)


def test_a_mutation_is_still_refused_as_a_mutation(served):
    _arts, deploy = served
    deploy()
    body = json.loads(tools.tool_execute_sql({"datasource": "demo", "sql": "DELETE FROM products"}))
    assert body["refusal"]["rule"] == guardrail.RULE_READ_ONLY


def test_nothing_recorded_means_nothing_to_be_stale_against(served):
    # A served deployment that has never been through a versioned deploy must stay usable.
    with pytest.raises(_Reached):
        _run()


def test_the_local_path_never_refuses_on_version(served, monkeypatch):
    # Locally the version names the newest snapshot, not the files being read, so it proves nothing.
    _arts, deploy = served
    deploy()
    monkeypatch.delenv("AGAMI_DB_URL")
    assert tools._stale_model_refusal({}, "demo") is None


def test_the_rule_is_decided_before_any_model():
    assert guardrail.RULE_STALE_MODEL in guardrail.PRE_MODEL_RULES
    assert guardrail.REASON_FOR_RULE[guardrail.RULE_STALE_MODEL] == "undetermined"


def test_execute_sql_declares_the_version_without_requiring_it():
    # Required-in-schema would fail validation before the handler, with no fix named.
    schema = tools.TOOLS["execute_sql"]["inputSchema"]
    assert "model_version" in schema["properties"]
    assert "model_version" not in schema.get("required", [])
