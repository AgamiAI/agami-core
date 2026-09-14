"""A table is (schema, name) wherever the model is compared or looked up (#332).

The scope gates used to compare a referenced table's bare name alone, so with only
`sales_data.orders` declared, `SELECT ... FROM staging.orders` passed table scope and the receipt
called it declared. These tests pin the rules `runtime._resolve_table` states:

* a qualified reference matches only a table declared with that schema;
* an unqualified name declared under two or more schemas is refused as ambiguous (echo-only);
* an unqualified name declared under one schema passes, as before;
* a table declared with NO schema is reachable only unqualified;

and that the receipt, the loader lookups and the validator agree with the gate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import guardrail  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402
from semantic_model import validator as V  # noqa: E402


def _table(name: str, schema: str | None, *cols: str) -> m.Table:
    return m.Table(
        name=name,
        schema=schema,
        storage_connection="c",
        grain=["id"],
        description=name,
        columns=[m.Column(name=c, type="integer") for c in ("id", *cols)],
    )


def _org(*areas: m.SubjectArea) -> m.Datasource:
    return m.Datasource(
        datasource="acme",
        storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
        subject_areas=list(areas),
    )


def _single_schema_org() -> m.Datasource:
    """Only `sales_data.orders` and `sales_data.customers` are declared."""
    return _org(
        m.SubjectArea(
            name="sales",
            tables_defined=[
                _table("orders", "sales_data", "amount"),
                _table("customers", "sales_data"),
            ],
        )
    )


def _clash_org() -> m.Datasource:
    """`orders` is declared twice — `sales_data.orders` and `staging.orders`, in different areas —
    with a column only the staging one has."""
    return _org(
        m.SubjectArea(
            name="sales",
            tables=[m.TableRef(storage_connection="c", schema="sales_data", table="orders")],
            tables_defined=[
                _table("orders", "sales_data", "amount"),
                _table("customers", "sales_data"),
            ],
        ),
        m.SubjectArea(
            name="landing",
            tables=[m.TableRef(storage_connection="c", schema="staging", table="orders")],
            tables_defined=[_table("orders", "staging", "raw_payload")],
        ),
        m.SubjectArea(
            name="reporting",
            tables=[m.TableRef(storage_connection="c", schema="staging", table="orders")],
            tables_defined=[_table("summary", "sales_data")],
        ),
    )


def _schemaless_org() -> m.Datasource:
    return _org(m.SubjectArea(name="sales", tables_defined=[_table("orders", None, "amount")]))


def _both_paths(sql: str, org: m.Datasource, gate):
    """The gate's verdict, asserted identical with and without a shared guard context."""
    ctx = rt.build_guard_context(sql, org)
    alone = gate(sql, org)
    assert gate(sql, org, ctx=ctx) == alone
    return alone


# ---------------------------------------------------------------- table scope


def test_qualified_reference_to_an_undeclared_schema_is_refused():
    refusal = _both_paths(
        "SELECT id FROM staging.orders", _single_schema_org(), rt.check_table_scope
    )
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_TABLE_SCOPE
    assert "staging.orders" in refusal.detail
    # Says a table of that name exists elsewhere, without naming the schema it is declared under.
    assert "different schema" in refusal.remediation
    assert "sales_data" not in refusal.detail + refusal.remediation


def test_qualified_reference_matching_the_declared_schema_passes():
    assert (
        _both_paths("SELECT id FROM sales_data.orders", _single_schema_org(), rt.check_table_scope)
        is None
    )
    assert (
        _both_paths("SELECT id FROM SALES_DATA.ORDERS", _single_schema_org(), rt.check_table_scope)
        is None
    )


def test_each_clashing_table_is_reachable_by_its_qualified_name():
    org = _clash_org()
    for sql in (
        "SELECT id FROM sales_data.orders",
        "SELECT id FROM staging.orders",
        "SELECT s.id FROM sales_data.orders s JOIN staging.orders g ON g.id = s.id",
    ):
        assert _both_paths(sql, org, rt.check_table_scope) is None, sql


def test_unique_unqualified_name_passes():
    assert _both_paths("SELECT id FROM orders", _single_schema_org(), rt.check_table_scope) is None
    # `customers` is unique in the clash model even though `orders` is not.
    assert _both_paths("SELECT id FROM customers", _clash_org(), rt.check_table_scope) is None


def test_unqualified_name_declared_under_two_schemas_is_refused_as_ambiguous():
    refusal = _both_paths("SELECT id FROM orders", _clash_org(), rt.check_table_scope)
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_TABLE_SCOPE
    assert (
        "more than one schema, so which one is meant cannot be decided: orders." in refusal.detail
    )
    assert "not in the semantic model" not in refusal.detail
    assert refusal.remediation == "Qualify each of those tables with its schema (schema.table)."
    # Echo-only: the schemas are model facts the caller did not send.
    for schema in ("sales_data", "staging"):
        assert schema not in refusal.detail + refusal.remediation


def test_ambiguous_and_undeclared_in_one_statement_report_both():
    refusal = rt.check_table_scope(
        "SELECT o.id FROM orders o JOIN ghost g ON g.id = o.id", _clash_org()
    )
    assert refusal is not None
    assert "not in the semantic model: ghost" in refusal.detail
    assert "cannot be decided: orders." in refusal.detail
    assert "Add the table to the model" in refusal.remediation
    assert "Qualify" in refusal.remediation


def test_a_model_of_only_clashing_names_is_not_mistaken_for_an_empty_one():
    # No bare key survives, so a check reading the bare map as "declares nothing" would go inert.
    org = _org(
        m.SubjectArea(name="a", tables_defined=[_table("orders", "sales_data")]),
        m.SubjectArea(name="b", tables_defined=[_table("orders", "staging")]),
    )
    assert rt.check_table_scope("SELECT id FROM ghost", org) is not None
    assert rt.check_table_scope("SELECT id FROM orders", org) is not None
    assert rt.check_scopable("SELECT id FROM generate_series(1, 3)", org) is not None


def test_qualified_reference_is_never_taken_for_a_cte_of_the_same_name():
    # A WITH binds an unqualified name only; skipping `staging.orders` by name let it through.
    sql = "WITH orders AS (SELECT 1 AS id) SELECT id FROM staging.orders"
    refusal = _both_paths(sql, _single_schema_org(), rt.check_table_scope)
    assert refusal is not None and "staging.orders" in refusal.detail
    # The unqualified CTE reference itself is still not a table.
    assert (
        rt.check_table_scope(
            "WITH orders AS (SELECT 1 AS id) SELECT id FROM orders", _single_schema_org()
        )
        is None
    )


def test_a_table_declared_without_a_schema_is_reachable_only_unqualified():
    org = _schemaless_org()
    assert _both_paths("SELECT id FROM orders", org, rt.check_table_scope) is None
    refusal = _both_paths("SELECT id FROM public.orders", org, rt.check_table_scope)
    assert refusal is not None and "public.orders" in refusal.detail


def test_schemaless_and_schema_declarations_of_one_name_are_ambiguous_unqualified():
    org = _org(
        m.SubjectArea(name="a", tables_defined=[_table("orders", None)]),
        m.SubjectArea(name="b", tables_defined=[_table("orders", "staging")]),
    )
    refusal = rt.check_table_scope("SELECT id FROM orders", org)
    assert refusal is not None and "cannot be decided: orders." in refusal.detail
    assert "staging" not in refusal.detail
    assert rt.check_table_scope("SELECT id FROM staging.orders", org) is None


def test_catalog_does_not_rescue_a_mismatched_schema():
    org = _single_schema_org()
    assert rt.check_table_scope("SELECT id FROM warehouse.sales_data.orders", org) is None
    assert rt.check_table_scope("SELECT id FROM warehouse.staging.orders", org) is not None


# ---------------------------------------------------------------- column scope


def test_column_scope_binds_to_the_qualified_table_not_the_union_of_same_named_ones():
    org = _clash_org()
    # `raw_payload` is declared on staging.orders only.
    refusal = _both_paths(
        "SELECT o.raw_payload FROM sales_data.orders o", org, rt.check_column_scope
    )
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_COLUMN_SCOPE
    assert "orders.raw_payload" in refusal.detail
    assert (
        _both_paths("SELECT o.raw_payload FROM staging.orders o", org, rt.check_column_scope)
        is None
    )
    assert _both_paths("SELECT amount FROM sales_data.orders", org, rt.check_column_scope) is None
    assert _both_paths("SELECT amount FROM staging.orders", org, rt.check_column_scope) is not None


def test_column_scope_binds_an_outer_table_an_inner_with_shadows_by_name():
    # The inner WITH binds `orders` for its own subquery only. Skipped by name, the outer `orders o`
    # left `o` unbound and every `o.<column>` failed open.
    sql = (
        "SELECT o.bogus FROM orders o WHERE EXISTS "
        "(WITH orders AS (SELECT 1 AS id) SELECT id FROM orders)"
    )
    refusal = _both_paths(sql, _single_schema_org(), rt.check_column_scope)
    assert refusal is not None and "orders.bogus" in refusal.detail


# ---------------------------------------------------------------- receipt agrees with the gate


def _table_items(org, sql):
    return rt.assemble_receipt(org, sql)["tables"]["items"]


@pytest.mark.parametrize(
    "org_fn,sql",
    [
        (_clash_org, "SELECT id FROM sales_data.orders"),
        (_clash_org, "SELECT id FROM staging.orders"),
        (_clash_org, "SELECT id FROM orders"),
        (_clash_org, "SELECT id FROM customers"),
        (_single_schema_org, "SELECT id FROM staging.orders"),
        (_single_schema_org, "SELECT id FROM sales_data.orders"),
        (_single_schema_org, "SELECT id FROM orders"),
        (_schemaless_org, "SELECT id FROM public.orders"),
    ],
)
def test_receipt_and_gate_agree_about_every_reference(org_fn, sql):
    org = org_fn()
    passed = rt.check_table_scope(sql, org) is None
    assert all(item["declared"] for item in _table_items(org, sql)) is passed
    assert (
        all(item["declared"] for item in rt.assemble_refusal_receipt(org, sql)["tables"]["items"])
        is passed
    )


def test_receipt_and_gate_agree_when_an_inner_with_shadows_a_declared_name():
    org = _single_schema_org()
    sql = (
        "SELECT id FROM orders WHERE EXISTS (WITH orders AS (SELECT 1 AS id) SELECT id FROM orders)"
    )
    assert rt.check_table_scope(sql, org) is None
    # The outer reference is the declared table; the inner one names the CTE.
    assert sorted(i["declared"] for i in _table_items(org, sql)) == [False, True]
    refusal_items = rt.assemble_refusal_receipt(org, sql)["tables"]["items"]
    assert sorted(i["declared"] for i in refusal_items) == [False, True]


def test_receipt_resolves_a_clashing_name_to_the_table_the_statement_read():
    org = _clash_org()
    (staging,) = _table_items(org, "SELECT id FROM staging.orders")
    assert staging["declared"] is True and staging["qname"] == "staging.orders"
    (sales,) = _table_items(org, "SELECT id FROM sales_data.orders")
    assert sales["declared"] is True and sales["qname"] == "sales_data.orders"
    (bare,) = _table_items(org, "SELECT id FROM orders")
    assert bare["declared"] is False and bare["qname"] is None


def test_receipt_never_labels_an_undeclared_schema_with_the_declared_tables_facts():
    sections = rt.assemble_receipt(_single_schema_org(), "SELECT o.amount FROM staging.orders o")
    (item,) = sections["tables"]["items"]
    assert item["declared"] is False and item["qname"] is None
    labels = [c["column"] for c in sections["columns"]["items"] if c.get("kind") == "reference"]
    assert labels and not any("sales_data" in label for label in labels)


def test_declared_filters_are_not_accounted_against_another_schemas_table():
    org = _single_schema_org()
    org.subject_areas[0].tables_defined[0].default_filters = ["{alias}.amount > 0"]
    tree = rt._parse_sql("SELECT id FROM staging.orders", "postgres")
    ((_ref, filters),) = rt.check_declared_filters(tree, org)
    assert filters == []
    tree = rt._parse_sql("SELECT id FROM sales_data.orders", "postgres")
    ((_ref, filters),) = rt.check_declared_filters(tree, org)
    assert len(filters) == 1


# ---------------------------------------------------------------- loader lookups


@pytest.mark.parametrize(
    "name,area,expected",
    [
        ("orders", None, None),  # clash, unsettled → not first-defined
        ("staging.orders", None, ("staging", "raw_payload")),
        ("sales_data.orders", None, ("sales_data", "amount")),
        ("orders", "sales", ("sales_data", "amount")),  # unique within its own area
        ("orders", "landing", ("staging", "raw_payload")),
        (
            "orders",
            "reporting",
            ("staging", "raw_payload"),
        ),  # TableRef fallback, settled by its schema
        ("customers", None, ("sales_data", None)),  # unique name, unchanged
    ],
)
def test_loader_lookup_never_resolves_a_clash_by_definition_order(name, area, expected):
    org = _clash_org()
    idx = L.build_table_index(org)
    linear = L._find_table(org, name, area)
    assert L._find_table(org, name, area, index=idx) is linear
    if expected is None:
        assert linear is None
        return
    schema, column = expected
    assert linear is not None and linear.schema_name == schema
    if column:
        assert column in {c.name for c in linear.columns}


def test_model_table_index_has_no_bare_key_for_a_clashing_name():
    tidx = rt._model_table_index(_clash_org())
    assert "orders" not in tidx and "customers" in tidx
    assert tidx.schemas["orders"] == ["sales_data", "staging"]
    assert ("staging", "orders") in tidx.qualified and ("sales_data", "orders") in tidx.qualified


# ---------------------------------------------------------------- validator


def _duplicate_findings(org):
    return [f for f in V.validate(org).findings if f.code == "duplicate_table_name"]


def test_validator_errors_on_two_same_named_tables_in_one_subject_area():
    org = _org(
        m.SubjectArea(
            name="Sales",
            tables_defined=[_table("orders", "sales_data"), _table("ORDERS", "staging")],
        )
    )
    (finding,) = _duplicate_findings(org)
    assert finding.severity == "error"
    assert finding.message == (
        "two tables named orders in subject area Sales (sales_data, staging); "
        "put them in different subject areas"
    )


def test_validator_allows_the_same_name_in_different_subject_areas():
    assert _duplicate_findings(_clash_org()) == []


def test_validator_keys_on_the_full_name_like_the_runtime():
    # `sales.orders` and `orders` are different keys to `_model_table_index` and the store.
    org = _org(
        m.SubjectArea(
            name="Sales",
            tables_defined=[_table("orders", "sales_data"), _table("sales.orders", "sales_data")],
        )
    )
    assert _duplicate_findings(org) == []


# ---------------------------------------------------------------- get_datasource_schema


def _write_multi_schema_model(root: Path) -> None:
    """What connect produces for a two-schema warehouse: `products` in both, one area each."""
    import yaml

    (root / "datasources" / "c").mkdir(parents=True, exist_ok=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"})
    )
    areas = {
        "billing": [("products", "price"), ("invoices", "total")],
        "crm": [("products", "owner")],
    }
    for area, tables in areas.items():
        (root / "subject_areas" / area / "tables").mkdir(parents=True)
        refs = []
        for name, column in tables:
            refs.append({"storage_connection": "c", "schema": area, "table": name})
            doc = {
                "name": name,
                "schema": area,
                "storage_connection": "c",
                "grain": ["id"],
                "description": f"{area} {name}",
                "columns": [
                    {"name": "id", "type": "integer", "primary_key": True},
                    {"name": column, "type": "integer"},
                ],
            }
            (root / "subject_areas" / area / "tables" / f"{name}.yaml").write_text(
                yaml.safe_dump(doc)
            )
        (root / "subject_areas" / area / "subject_area.yaml").write_text(
            yaml.safe_dump({"name": area, "description": area, "tables": refs})
        )
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "acme",
                "version": 1,
                "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
                "subject_areas": [f"subject_areas/{a}" for a in areas],
            }
        )
    )


@pytest.fixture
def schema_head(tmp_path, monkeypatch):
    import tools

    art = tmp_path / "art"
    _write_multi_schema_model(art / "acme")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))

    def head(**args):
        out = tools.tool_get_datasource_schema({"datasource": "acme", **args})
        return json.JSONDecoder().raw_decode(out)[0]

    return head


def _columns(ctx: dict) -> set[str]:
    return {c["name"] for c in ctx["columns"]}


def test_full_tier_serves_every_same_named_table_keyed_by_schema(schema_head):
    tables = schema_head(mode="full")["tables"]
    assert set(tables) == {"billing.products", "crm.products", "invoices"}
    assert _columns(tables["billing.products"]) == {"id", "price"}
    assert _columns(tables["crm.products"]) == {"id", "owner"}
    assert _columns(tables["invoices"]) == {"id", "total"}


def test_dataset_names_settles_a_clash_with_schema_table(schema_head):
    crm = schema_head(dataset_names=["crm.products"])
    assert list(crm["tables"]) == ["crm.products"]
    assert _columns(crm["tables"]["crm.products"]) == {"id", "owner"}
    both = schema_head(dataset_names=["billing.products", "crm.products"])["tables"]
    assert _columns(both["billing.products"]) == {"id", "price"}
    assert _columns(both["crm.products"]) == {"id", "owner"}


def test_dataset_names_bare_clash_asks_for_the_qualified_name(schema_head):
    tables = schema_head(dataset_names=["products"])["tables"]
    assert tables == {
        "products": {"error": "declared in more than one schema; name it as schema.table"}
    }


def test_dataset_names_bare_and_qualified_unique_table_still_resolve(schema_head):
    for name in ("invoices", "billing.invoices"):
        tables = schema_head(dataset_names=[name])["tables"]
        assert _columns(tables["invoices"]) == {"id", "total"}, name
    assert schema_head(dataset_names=["missing"])["tables"] == {
        "missing": {"error": "not found in scope"}
    }
