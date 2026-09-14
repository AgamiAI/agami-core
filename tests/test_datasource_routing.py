"""#327 — a question reaches the datasource it was written for, or is told where to go.

On an organization serving several datasources, a call that named none ran against a fallback, and
SQL written for one datasource was refused by another as out of scope, with advice to add a table the
model already declared elsewhere. Now an omission is refused with the choices named, a table-scope
refusal names the datasource that declares the table, and a datasource's one-line description can be
written by `sm set-description` and is warned about at deploy when missing.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import guardrail  # noqa: E402
import model_store  # noqa: E402
import tools  # noqa: E402
import yaml  # noqa: E402
from store import Store  # noqa: E402

SERVED = ["acme_crm", "acme_erp"]


def _write_profile(root, *, description: str = "", tables: tuple[str, ...] = ("orders",)) -> None:
    """A minimal on-disk model: one area, the named tables, a PostgreSQL storage connection."""
    (root / "datasources" / "c").mkdir(parents=True, exist_ok=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"})
    )
    adir = root / "subject_areas" / "sales"
    (adir / "tables").mkdir(parents=True, exist_ok=True)
    refs = []
    for name in tables:
        refs.append({"storage_connection": "c", "schema": "public", "table": name})
        (adir / "tables" / f"{name}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": name,
                    "schema": "public",
                    "storage_connection": "c",
                    "grain": ["id"],
                    "description": f"{name} table",
                    "columns": [{"name": "id", "type": "integer", "primary_key": True}],
                }
            )
        )
    (adir / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": "sales", "description": "sales area", "tables": refs})
    )
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": root.name,
                "version": 1,
                "description": description,
                "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
                "subject_areas": ["subject_areas/sales"],
            }
        )
    )


@pytest.fixture
def local(tmp_path, monkeypatch):
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE", "AGAMI_ORG_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    tools.resolved_org_id.cache_clear()
    tools._sole_served_datasource.cache_clear()
    _write_profile(tmp_path / "acme_crm")
    tools.bootstrap_paths()
    yield tmp_path
    tools._sole_served_datasource.cache_clear()


# --- an omitted datasource on an organization serving several ---------------------------------------


def test_execute_sql_refuses_an_omission_and_names_the_choices(local, monkeypatch):
    monkeypatch.setattr(tools, "_served_datasources", lambda _org: list(SERVED))
    # Even with a fallback configured, it is not guessed at.
    monkeypatch.setenv("AGAMI_PROFILE", "acme_crm")

    body = json.loads(tools.tool_execute_sql({"sql": "SELECT id FROM orders"}))

    assert body["status"] == "refused"
    assert body["refusal"]["rule"] == guardrail.RULE_DATASOURCE_REQUIRED
    assert "acme_crm" in body["refusal"]["detail"] and "acme_erp" in body["refusal"]["detail"]
    assert "list_datasources" in body["refusal"]["remediation"]


def test_the_refusal_is_decided_before_any_model():
    assert guardrail.RULE_DATASOURCE_REQUIRED in guardrail.PRE_MODEL_RULES
    assert guardrail.REASON_FOR_RULE[guardrail.RULE_DATASOURCE_REQUIRED] == "undetermined"


def test_schema_and_examples_refuse_an_omission_too(local, monkeypatch):
    monkeypatch.setattr(tools, "_served_datasources", lambda _org: list(SERVED))
    monkeypatch.setenv("AGAMI_PROFILE", "acme_crm")

    for out in (
        tools.tool_get_datasource_schema({}),
        tools.tool_get_prompt_examples({"query": "x"}),
    ):
        error = json.loads(out)["error"]
        assert error["kind"] == "datasource_required"
        assert error["datasources"] == SERVED


@pytest.mark.parametrize("served", [["acme_crm"], None], ids=["one-served", "store-unreachable"])
def test_one_served_or_an_unanswerable_store_keeps_resolving(local, monkeypatch, served):
    monkeypatch.setattr(tools, "_served_datasources", lambda _org: served)
    monkeypatch.setenv("AGAMI_PROFILE", "acme_crm")

    assert tools._datasources_to_choose_from({}) is None
    head, _ = json.JSONDecoder().raw_decode(tools.tool_get_datasource_schema({}))
    assert head.get("datasource") == "acme_crm"


def test_a_named_datasource_is_never_refused_for_omission(local, monkeypatch):
    monkeypatch.setattr(tools, "_served_datasources", lambda _org: list(SERVED))

    assert tools._datasources_to_choose_from({"datasource": "acme_erp"}) is None


# --- a table-scope refusal names where the table is declared ---------------------------------------


def _scope_refusal_envelope():
    refusal = guardrail.refuse(
        guardrail.RULE_TABLE_SCOPE,
        detail="query references table(s) not in the semantic model: invoices",
        remediation="Add the table to the model (agami-connect / '/agami-model'), or remove it from the query.",
    )
    return tools._envelope(
        "refused", refusal=refusal, receipt=guardrail.undetermined_receipt("test")
    )


def test_tables_declared_in_one_other_datasource_are_pointed_there(local, monkeypatch):
    monkeypatch.setattr(
        tools, "_declared_elsewhere", lambda names, profile: {"invoices": ["acme_erp"]}
    )

    env = tools._point_to_declaring_datasource(
        _scope_refusal_envelope(), "SELECT id FROM invoices", "acme_crm"
    )

    remediation = env.refusal.remediation
    assert "acme_erp" in remediation and "invoices" in remediation
    assert "Add the table to the model" not in remediation
    # The verdict is unchanged; only the advice.
    assert env.refusal.rule == guardrail.RULE_TABLE_SCOPE


def test_a_target_must_declare_every_table_in_the_statement(local, monkeypatch):
    """`orders` is declared only here and `invoices` only in `acme_erp`: sending the statement to
    `acme_erp` would be refused again on `orders`, so no datasource is named as the place to run it."""
    monkeypatch.setattr(
        tools, "_declared_elsewhere", lambda names, profile: {"invoices": ["acme_erp"]}
    )

    env = tools._point_to_declaring_datasource(
        _scope_refusal_envelope(), "SELECT id FROM invoices JOIN orders USING (id)", "acme_crm"
    )

    assert "cannot join" in env.refusal.remediation
    assert "set to `acme_erp`" not in env.refusal.remediation


def test_emit_carries_the_hint_in_the_body(local, monkeypatch):
    """The hint is applied inside `_emit`, before the body and the audit row are built."""
    monkeypatch.setattr(
        tools, "_declared_elsewhere", lambda names, profile: {"invoices": ["acme_erp"]}
    )

    body = json.loads(
        tools._emit(
            _scope_refusal_envelope(),
            sql="SELECT id FROM invoices",
            execution_ms=None,
            profile="acme_crm",
            args={"datasource": "acme_crm"},
        )
    )

    assert "acme_erp" in body["refusal"]["remediation"]


def test_tables_split_across_datasources_say_one_statement_cannot_join_them(local, monkeypatch):
    monkeypatch.setattr(
        tools,
        "_declared_elsewhere",
        lambda names, profile: {"invoices": ["acme_erp"], "tickets": ["acme_help"]},
    )

    env = tools._point_to_declaring_datasource(
        _scope_refusal_envelope(), "SELECT id FROM invoices JOIN tickets USING (id)", "acme_crm"
    )

    assert "cannot join" in env.refusal.remediation
    assert "acme_erp" in env.refusal.remediation and "acme_help" in env.refusal.remediation


def test_declared_nowhere_or_another_rule_leaves_the_refusal_alone(local, monkeypatch):
    monkeypatch.setattr(tools, "_declared_elsewhere", lambda names, profile: {})
    original = _scope_refusal_envelope()

    assert (
        tools._point_to_declaring_datasource(original, "SELECT id FROM invoices", "acme_crm")
        is original
    )

    star = replace(original, refusal=replace(original.refusal, rule=guardrail.RULE_SELECT_STAR))
    assert tools._point_to_declaring_datasource(star, "SELECT * FROM invoices", "acme_crm") is star


def test_a_failing_lookup_never_breaks_the_refusal(local, monkeypatch):
    def _boom(names, profile):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(tools, "_declared_elsewhere", _boom)
    original = _scope_refusal_envelope()

    assert (
        tools._point_to_declaring_datasource(original, "SELECT id FROM invoices", "acme_crm")
        is original
    )


def test_a_name_declared_under_two_schemas_here_is_not_pointed_elsewhere(local, monkeypatch):
    """#332: `invoices` in two schemas of THIS datasource is refused as ambiguous, not undeclared.
    The bare-name index leaves such a name out, so reading it as "declared" would send the caller to
    another datasource for a table it only has to qualify."""
    from semantic_model import models as m

    def _invoices(schema):
        return m.Table(name="invoices", schema=schema, storage_connection="c", grain=["id"],
                       columns=[m.Column(name="id", type="integer")])

    org = m.Datasource(datasource="acme_crm", subject_areas=[
        m.SubjectArea(name="billing", tables_defined=[_invoices("billing")]),
        m.SubjectArea(name="crm", tables_defined=[_invoices("crm")]),
    ])
    monkeypatch.setattr(tools, "get_cached_org", lambda profile: org)
    monkeypatch.setattr(
        tools, "_declared_elsewhere", lambda names, profile: {"invoices": ["acme_erp"]}
    )
    original = _scope_refusal_envelope()

    assert (
        tools._point_to_declaring_datasource(original, "SELECT id FROM invoices", "acme_crm")
        is original
    )


def test_a_physical_table_beside_a_same_named_inner_cte_still_gets_the_hint(local, monkeypatch):
    """The inner WITH binds `invoices` for its own subquery only; the outer read is the physical
    table, which the gate refused — so the hint judges it too, rather than dropping it by name."""
    monkeypatch.setattr(
        tools, "_declared_elsewhere", lambda names, profile: {"invoices": ["acme_erp"]}
    )

    env = tools._point_to_declaring_datasource(
        _scope_refusal_envelope(),
        "SELECT id FROM invoices WHERE EXISTS (WITH invoices AS (SELECT 1 AS id) SELECT id FROM invoices)",
        "acme_crm",
    )

    assert "acme_erp" in env.refusal.remediation


def test_the_lookup_is_scoped_to_the_organization(tmp_path):
    url = "sqlite://" + str(tmp_path / "m.db")
    store = Store.connect(url)
    store.run_migrations()
    for org, ds, name in [("acme", "acme_erp", "Invoices"), ("globex", "globex_erp", "invoices")]:
        store.execute(
            "INSERT INTO model_table (org_id, datasource, area, name, est_row_count, doc) VALUES (?, ?, ?, ?, ?, ?)",
            (org, ds, "sales", name, None, "{}"),
        )
    store.commit()

    found = model_store.datasources_declaring(store, ["invoices", "missing"], org_id="acme")
    store.close()

    assert found == {"invoices": ["acme_erp"]}


# --- the one-line description ----------------------------------------------------------------------


def test_set_description_writes_one_line_and_validates(tmp_path):
    from semantic_model import curate, loader

    root = tmp_path / "acme_crm"
    _write_profile(root)

    res = curate.set_datasource_description(
        root, "  Orders and customers for sales questions.\nsecond line"
    )

    assert res.validated and not res.errors
    assert loader.load_datasource(root).description == "Orders and customers for sales questions."
    assert curate.set_datasource_description(root, "   ").errors == ["description is empty"]


def test_deploy_warns_about_a_missing_description_and_not_a_present_one(tmp_path, capsys):
    import model_deploy

    store = Store.connect("sqlite://" + str(tmp_path / "m.db"))
    store.run_migrations()
    bare, described = tmp_path / "acme_crm", tmp_path / "acme_erp"
    _write_profile(bare)
    _write_profile(described, description="Invoices and payments for revenue questions.")

    model_deploy.deploy_one(store, "acme_crm", bare, org_id="acme")
    warned = capsys.readouterr().err
    model_deploy.deploy_one(store, "acme_erp", described, org_id="acme")
    quiet = capsys.readouterr().err
    store.close()

    assert "acme_crm" in warned and "no description" in warned
    assert "no description" not in quiet


def test_reintrospect_keeps_an_existing_description_and_a_new_one_wins(tmp_path):
    """The introspector builds a model with no description; writing it must not wipe the line a
    human set, while a model that carries its own description still replaces the old one."""
    from semantic_model import build, loader

    root = tmp_path / "acme_crm"
    _write_profile(root, description="Orders and customers for sales questions.")
    org = loader.load_datasource(root)

    build.write_tree(org.model_copy(update={"description": ""}), root)
    assert loader.load_datasource(root).description == "Orders and customers for sales questions."

    build.write_tree(org.model_copy(update={"description": "Orders only."}), root)
    assert loader.load_datasource(root).description == "Orders only."
