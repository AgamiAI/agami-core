"""A hosted execute_sql must show it looked at the datasource's examples (#376).

Clients skip `get_prompt_examples`: the schema is indispensable for writing SQL and the examples feel
optional, so advice in the schema response and the instructions was not enough. The call now names an
id the lookup returned and says whether it `followed` that example or it was `shown_only`. Only the
id is checked; `shown_only` is always accepted, so nothing pushes a statement toward a poor match.

Driven end to end on a served SQLite deployment through the real deploy and handlers. Execution is
replaced by a sentinel that proves a call got past every gate.
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
    """Raised in place of execution: the call got past every gate."""


class _Executor:
    def execute(self, vetted_sql, creds, *, profile):
        raise _Reached


def _write_model(root: Path, examples: list[tuple[str, str]]) -> None:
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
    body = "".join(f"  - question: {q}\n    sql: {s}\n" for q, s in examples)
    (d / "prompt_examples" / "Catalog" / "examples.yaml").write_text(
        "examples:\n" + body if examples else "examples: []\n"
    )


TWO = [
    ("how many products?", "SELECT COUNT(*) FROM products"),
    ("list every sku", "SELECT sku FROM products"),
]


@pytest.fixture()
def served(tmp_path, monkeypatch):
    """A served deployment; `deploy(examples)` (re)deploys `demo` with those examples."""
    arts = tmp_path / "artifacts"
    db_url = "sqlite://" + str(tmp_path / "agami.db")
    for var in ("APP_DATABASE_URL", "AGAMI_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(arts))
    monkeypatch.setenv("AGAMI_ORG_ID", "local")
    tools.resolved_org_id.cache_clear()
    tools._sole_served_datasource.cache_clear()
    tools.bootstrap_paths()

    def deploy(examples: list[tuple[str, str]]) -> None:
        _write_model(arts, examples)
        store = Store.connect(db_url)
        store.run_migrations()
        try:
            model_deploy.deploy_models(store, arts, org_id="local")
        finally:
            store.close()

    previous = tools._INJECTED_EXECUTOR
    tools.set_injected_executor(_Executor())
    monkeypatch.setattr(tools, "_run_in_process", lambda *a, **k: (_ for _ in ()).throw(_Reached))
    yield deploy, db_url
    tools.set_injected_executor(previous)
    tools._sole_served_datasource.cache_clear()
    tools.resolved_org_id.cache_clear()


def _version() -> str:
    return json.JSONDecoder().raw_decode(tools.tool_get_datasource_schema({"datasource": "demo"}))[
        0
    ]["model_version"]


def _ids() -> list[str]:
    body = json.loads(tools.tool_get_prompt_examples({"datasource": "demo", "query": "sku"}))
    return [e["id"] for e in body["examples"]]


def _run(**extra) -> dict:
    args = {"datasource": "demo", "sql": SQL, "model_version": _version(), **extra}
    return json.loads(tools.tool_execute_sql(args))


def _refused(body: dict) -> dict:
    assert body["status"] == "refused", body
    assert body["refusal"]["rule"] == guardrail.RULE_EXAMPLE_REQUIRED
    return body["refusal"]


def test_a_call_without_an_example_is_refused_and_told_what_to_do(served):
    deploy, _ = served
    deploy(TWO)
    refusal = _refused(_run())
    assert "get_prompt_examples" in refusal["remediation"]
    assert "shown_only" in refusal["remediation"] and "followed" in refusal["remediation"]
    # It names the lookup, never the examples themselves.
    assert "how many products" not in json.dumps(refusal)


@pytest.mark.parametrize("use", ["followed", "shown_only"])
def test_an_id_the_lookup_returned_runs_whichever_use_is_given(served, use):
    deploy, _ = served
    deploy(TWO)
    with pytest.raises(_Reached):
        _run(example={"id": _ids()[0], "use": use})


def test_an_id_the_datasource_does_not_store_is_refused(served):
    deploy, _ = served
    deploy(TWO)
    refusal = _refused(_run(example={"id": "0123456789ab", "use": "followed"}))
    assert "no longer exists" in refusal["remediation"]


@pytest.mark.parametrize(("org_id", "datasource"), [("local", "other_ds"), ("another-org", "demo")])
def test_an_id_stored_for_another_datasource_or_org_is_refused(served, org_id, datasource):
    """An id identifies an example within one organization's datasource. One stored anywhere else
    is refused with the same sentence as one stored nowhere, so the refusal says nothing about it."""
    deploy, db_url = served
    deploy(TWO)
    store = Store.connect(db_url)
    try:
        model_store.write_examples(
            store,
            datasource,
            [{"area": "Catalog", "question": "elsewhere", "sql": "SELECT 1", "id": "elsewhere01"}],
            org_id=org_id,
        )
    finally:
        store.close()
    refusal = _refused(_run(example={"id": "elsewhere01", "use": "followed"}))
    assert "no longer exists" in refusal["remediation"]
    assert "elsewhere" not in refusal["detail"]


@pytest.mark.parametrize("use", [None, "", "used", "FOLLOWED", 1, [], {"a": 1}])
def test_a_use_other_than_the_two_words_is_refused(served, use):
    deploy, _ = served
    deploy(TWO)
    refusal = _refused(_run(example={"id": _ids()[0], "use": use}))
    assert "'followed' or 'shown_only'" in refusal["remediation"]


@pytest.mark.parametrize("claim", ["abc", [], {"use": "followed"}, {"id": 7, "use": "followed"}])
def test_a_malformed_example_is_refused_not_raised(served, claim):
    deploy, _ = served
    deploy(TWO)
    _refused(_run(example=claim))


def test_an_example_removed_by_a_model_change_is_refused(served):
    deploy, _ = served
    deploy(TWO)
    old = next(i for i in _ids() if i)  # any id from before the change
    deploy([("how many products?", "SELECT COUNT(*) AS n FROM products")])  # both examples change
    assert old not in _ids()
    _refused(_run(example={"id": old, "use": "followed"}))
    with pytest.raises(_Reached):
        _run(example={"id": _ids()[0], "use": "followed"})


def test_a_datasource_with_no_examples_requires_nothing(served):
    deploy, _ = served
    deploy([])
    with pytest.raises(_Reached):
        _run()


def test_the_local_path_never_requires_an_example(served, monkeypatch):
    deploy, _ = served
    deploy(TWO)
    monkeypatch.delenv("AGAMI_DB_URL")
    assert tools._example_refusal({}, "demo") is None


def test_an_unreadable_store_stands_aside_and_says_so(served, caplog):
    # Not migrated: the store opens and the count fails. The audit gate owns that failure.
    with caplog.at_level("WARNING", logger=tools.__name__):
        assert tools._example_refusal({}, "demo") is None
    assert "examples unavailable" in caplog.text


def test_a_stale_version_is_refused_before_the_example_is_asked_about(served):
    deploy, _ = served
    deploy(TWO)
    body = json.loads(tools.tool_execute_sql({"datasource": "demo", "sql": SQL}))
    assert body["refusal"]["rule"] == guardrail.RULE_STALE_MODEL


def test_a_mutation_is_still_refused_as_a_mutation(served):
    deploy, _ = served
    deploy(TWO)
    body = json.loads(tools.tool_execute_sql({"datasource": "demo", "sql": "DELETE FROM products"}))
    assert body["refusal"]["rule"] == guardrail.RULE_READ_ONLY


def test_the_claim_is_recorded_with_the_call(served):
    deploy, db_url = served
    deploy(TWO)
    ex_id = _ids()[0]
    args = {
        "datasource": "demo",
        "sql": SQL,
        "model_version": _version(),
        "example": {"id": ex_id, "use": "shown_only"},
    }
    tools.record_tool_call(
        name="execute_sql",
        arguments=args,
        result_text="{}",
        execution_ms=1,
        actor="you@example.com",
    )
    tools.record_tool_call(
        name="list_datasources", arguments={}, result_text="{}", execution_ms=1, actor=None
    )
    store = Store.connect(db_url)
    try:
        rows = store.query("SELECT tool_name, example_id, example_use FROM tool_calls ORDER BY ts")
        # And the activity view's reader carries them, which is what lets the view show them.
        read = [c for s in model_store.list_sessions(store, org_id="local") for c in s["calls"]]
    finally:
        store.close()
    assert {(c["tool_name"], c["example_id"], c["example_use"]) for c in read} == {
        ("execute_sql", ex_id, "shown_only"),
        ("list_datasources", None, None),
    }
    assert [dict(r) for r in rows] == [
        {"tool_name": "execute_sql", "example_id": ex_id, "example_use": "shown_only"},
        {"tool_name": "list_datasources", "example_id": None, "example_use": None},
    ]


def test_the_rule_is_decided_before_any_model():
    assert guardrail.RULE_EXAMPLE_REQUIRED in guardrail.PRE_MODEL_RULES
    assert guardrail.REASON_FOR_RULE[guardrail.RULE_EXAMPLE_REQUIRED] == "undetermined"


def test_execute_sql_declares_the_example_without_requiring_it():
    schema = tools.TOOLS["execute_sql"]["inputSchema"]
    assert set(schema["properties"]["example"]["properties"]) == {"id", "use"}
    assert "enum" not in schema["properties"]["example"]["properties"]["use"]
    assert "example" not in schema.get("required", [])


def test_a_bad_use_does_not_look_the_id_up(served, monkeypatch):
    """The refusal for a bad `use` must not depend on whether the id exists, and costs no lookup."""
    deploy, _ = served
    deploy(TWO)
    looked = []
    real = model_store.example_by_id
    monkeypatch.setattr(
        model_store, "example_by_id", lambda *a, **k: looked.append(1) or real(*a, **k)
    )
    _refused(_run(example={"id": _ids()[0], "use": "used"}))
    assert looked == []


def test_the_activity_view_shows_the_example_on_the_call(served):
    import admin

    card = admin._call_card(
        {
            "sql": SQL,
            "tool_name": "execute_sql",
            "ts": "2026-09-17T00:00:00Z",
            "success": 1,
            "example_id": "22f7a070f7c5",
            "example_use": "shown_only",
        }
    )
    assert "Example 22f7a070f7c5" in card and "shown only" in card
    assert "Example" not in admin._call_card(
        {"sql": SQL, "tool_name": "execute_sql", "ts": "2026-09-17T00:00:00Z", "success": 1}
    )
