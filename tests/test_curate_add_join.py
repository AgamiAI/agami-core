"""`sm curate` and the model explorer add a join (#512).

An `add` op writes the join unreviewed where its two tables' areas say, validated and logged; the same
join already in the model is skipped; a table or column the model lacks fails the batch. Synthetic,
generic names only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import parse_model_feedback as F  # noqa: E402
from render_model_explorer import build_manifest, render  # noqa: E402
from semantic_model import cli, curate, loader  # noqa: E402

from test_model_explorer_bulk_reject import DOM, NODE, needs_node  # noqa: E402

_COMPOSITE = "orders.region = customers.region AND orders.cust_no = customers.cust_no"


def _table(root: Path, area: str, name: str, cols: list[str]) -> None:
    d = root / "subject_areas" / area / "tables"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(yaml.safe_dump({
        "name": name, "schema": "public", "storage_connection": "c", "grain": ["id"], "description": name,
        "columns": [{"name": "id", "type": "integer", "primary_key": True}]
                   + [{"name": c, "type": "integer"} for c in cols]}))


@pytest.fixture
def model(tmp_path: Path) -> Path:
    """sales: orders, customers (one join, orders.customer_id); catalog: products."""
    root = tmp_path / "demo"
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "demo", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/sales", "subject_areas/catalog"]}))
    for area, tables in (("sales", ["orders", "customers"]), ("catalog", ["products"])):
        (root / "subject_areas" / area).mkdir(parents=True)
        (root / "subject_areas" / area / "subject_area.yaml").write_text(yaml.safe_dump({
            "name": area, "description": area,
            "tables": [{"storage_connection": "c", "schema": "public", "table": t} for t in tables]}))
    _table(root, "sales", "orders", ["customer_id", "cust_ref", "region", "cust_no", "product_id"])
    _table(root, "sales", "customers", ["region", "cust_no"])
    _table(root, "catalog", "products", [])
    (root / "subject_areas" / "sales" / "relationships.yaml").write_text(yaml.safe_dump({"relationships": [{
        "from_table": "orders", "from_column": "customer_id", "to_table": "customers", "to_column": "id",
        "relationship": "many_to_one", "confidence": "inferred", "review_state": "unreviewed"}]}))
    return root


def _add(**fields) -> dict:
    return {"op": "add", "kind": "relationship", "relationship": "many_to_one", **fields}


_CUST_REF = _add(from_table="orders", from_column="cust_ref", to_table="customers", to_column="id")


def _rels(root: Path, area: str = "sales") -> list[dict]:
    return yaml.safe_load((root / "subject_areas" / area / "relationships.yaml").read_text())["relationships"]


def _snapshot(root: Path) -> dict:
    return {p: p.read_text() for p in sorted(root.rglob("*.yaml"))}


def test_add_writes_a_single_column_join_unreviewed_and_logged(model):
    res = curate.apply(model, [_CUST_REF])
    assert res.validated and res.applied == ["add relationship sales/orders->customers"]
    [r] = [r for r in _rels(model) if r.get("from_column") == "cust_ref"]
    assert r == {"from_table": "orders", "from_column": "cust_ref", "to_table": "customers", "to_column": "id",
                 "from_schema": "public", "to_schema": "public", "relationship": "many_to_one",
                 "confidence": "proposed", "review_state": "unreviewed"}
    queued = curate.review_queue(loader.load_datasource(model))["rule_2"]
    assert any(it["kind"] == "relationship" and it.get("from_column") == "cust_ref" for it in queued)
    entry = curate.history(model)[0]
    assert (entry["op"], entry["kind"], entry["area"], entry["from_column"]) == ("add", "relationship", "sales",
                                                                                 "cust_ref")


def test_add_writes_a_composite_on_join(model):
    res = curate.apply(model, [_add(from_table="orders", to_table="customers", on=_COMPOSITE)])
    assert res.validated and res.applied
    [r] = [r for r in _rels(model) if r.get("on")]
    assert r["on"] == _COMPOSITE and "from_column" not in r and r["review_state"] == "unreviewed"


def test_an_add_never_approves(model):
    curate.apply(model, [{**_CUST_REF, "review_state": "approved", "signed_off_by": "you@example.com"}],
                 signer="you@example.com", role="data_lead")
    [r] = [r for r in _rels(model) if r.get("from_column") == "cust_ref"]
    assert r["review_state"] == "unreviewed" and "signed_off_by" not in r


def test_the_same_join_again_is_skipped_with_a_note(model):
    before = _snapshot(model)
    res = curate.apply(model, [_add(from_table="orders", from_column="customer_id", to_table="customers",
                                    to_column="id")])
    assert res.validated and not res.applied and not res.errors
    assert "already in the model" in res.skipped[0]["reason"]
    assert _snapshot(model) == before

    res = curate.apply(model, [_add(from_table="orders", to_table="customers", on=_COMPOSITE)] * 2)
    assert len(res.applied) == 1 and len(res.skipped) == 1
    assert len([r for r in _rels(model) if r.get("on")]) == 1


@pytest.mark.parametrize("bad", [
    {"from_column": "cust_reff"},
    {"to_table": "customerz"},
    {"from_column": None, "to_column": None, "on": "orders.regionn = customers.region"},
])
def test_a_misspelled_table_or_column_undoes_the_batch(model, bad):
    before = _snapshot(model)
    approve = {"op": "approve", "kind": "relationship", "name": "orders->customers", "area": "sales",
               "from_column": "customer_id", "at": "2026-01-01T00:00:00Z"}
    res = curate.apply(model, [approve, {**_CUST_REF, **bad}], signer="you@example.com", role="data_lead")
    assert res.errors and not res.applied and not res.validated
    assert _snapshot(model) == before
    assert not (model / "curation_log.jsonl").exists()


@pytest.mark.parametrize("on", [
    "orders.cust_no = customers.cust_no AND customers.missing_flag = 'Y'",
    "CAST(orders.cust_noo AS INTEGER) = customers.cust_no",
    "orders.cust_no > customers.nope",
    "orders.cust_no = customers.cust_no AND products.id = 1",
    "orders.cust_no = customers.cust_no AND region = 1",
    "orders.cust_no = = customers.cust_no",
])
def test_every_column_of_a_join_condition_is_checked(model, on):
    before = _snapshot(model)
    res = curate.apply(model, [_add(from_table="orders", to_table="customers", on=on)])
    assert res.errors and not res.applied and not res.validated
    assert _snapshot(model) == before


def test_a_join_condition_with_a_filter_and_a_cast_is_added_when_its_columns_exist(model):
    on = "CAST(orders.cust_no AS INTEGER) = customers.cust_no AND Customers.Region = 1"
    res = curate.apply(model, [_add(from_table="orders", to_table="customers", on=on)])
    assert res.validated and res.applied, res.as_dict()


def test_filtered_joins_differing_only_in_a_literal_case_are_two_joins(model):
    on = "orders.cust_no = customers.cust_no AND customers.region = '{}'"
    res = curate.apply(model, [_add(from_table="orders", to_table="customers", on=on.format("Y")),
                               _add(from_table="orders", to_table="customers", on=on.format("y")),
                               _add(from_table="orders", to_table="customers", on=on.format("Y").upper())])
    assert len(res.applied) == 2 and len(res.skipped) == 1, res.as_dict()
    assert sorted(r["on"][-3:] for r in _rels(model) if r.get("on")) == ["'Y'", "'y'"]


def test_each_of_two_joins_differing_only_in_a_literal_case_is_approved_on_its_own(model):
    on = "orders.cust_no = customers.cust_no AND customers.region = '{}'"
    curate.apply(model, [_add(from_table="orders", to_table="customers", on=on.format(v)) for v in "Yy"])
    approve = {"op": "approve", "kind": "relationship", "name": "orders->customers", "area": "sales",
               "on": on.format("y"), "at": "2026-01-01T00:00:00Z"}
    res = curate.apply(model, [approve], signer="you@example.com", role="data_lead")
    assert res.validated and res.applied and not res.errors, res.as_dict()
    states = {r["on"][-3:]: r["review_state"] for r in _rels(model) if r.get("on")}
    assert states == {"'Y'": "unreviewed", "'y'": "approved"}
    res = curate.apply(model, [{**approve, "op": "reject", "on": on.format("Y")}], signer="you@example.com",
                       role="data_lead")
    assert res.applied and not res.errors, res.as_dict()
    assert {r["on"][-3:]: r["review_state"] for r in _rels(model) if r.get("on")} == {"'Y'": "rejected",
                                                                                      "'y'": "approved"}


@pytest.mark.parametrize("fields", [
    {"from_column": "cust_ref", "on": "orders.cust_reff = customers.id"},
    {"to_column": "id", "on": "orders.cust_ref = customers.idd"},
])
def test_the_join_condition_is_checked_beside_an_explicit_column(model, fields):
    before = _snapshot(model)
    res = curate.apply(model, [_add(from_table="orders", to_table="customers", **fields)])
    assert res.errors and not res.applied and not res.validated, res.as_dict()
    assert _snapshot(model) == before


def _set_review_state(root: Path, area: str, table: str, state: str, column: str | None = None) -> None:
    tf = root / "subject_areas" / area / "tables" / f"{table}.yaml"
    doc = yaml.safe_load(tf.read_text())
    target = next(c for c in doc["columns"] if c["name"] == column) if column else doc
    target["review_state"] = state
    tf.write_text(yaml.safe_dump(doc))


@pytest.mark.parametrize("state", ["rejected", "stale"])
def test_an_added_join_is_placed_by_the_served_copy_of_a_table(model, state):
    # sales holds an excluded copy of customers; catalog serves it.
    _table(model, "catalog", "customers", ["region", "cust_no"])
    _set_review_state(model, "sales", "customers", state)
    res = curate.apply(model, [_CUST_REF])
    assert res.validated and res.applied == ["add relationship /orders->customers"], res.as_dict()
    [edge] = yaml.safe_load((model / "cross_subject_area_relationships.yaml").read_text())["edges"]
    assert (edge["from_subject_area"], edge["to_subject_area"]) == ("sales", "catalog")
    org = loader.load_datasource(model)
    assert ("orders", "cust_ref") in {(r.from_table, r.from_column) for r in org.cross_subject_area_relationships}
    assert any(it.get("from_column") == "cust_ref" for it in curate.review_queue(org)["rule_2"])


def test_a_moved_tables_joins_are_placed_by_the_served_copy_of_the_other_end(model):
    _table(model, "catalog", "customers", ["region", "cust_no"])
    _set_review_state(model, "sales", "customers", "rejected")
    res = curate.apply(model, [{"op": "move", "kind": "table", "name": "orders", "area": "sales",
                                "to_area": "catalog"}])
    assert res.validated and not res.errors, res.as_dict()
    assert [(r["from_table"], r["to_table"]) for r in _rels(model, "catalog")] == [("orders", "customers")]
    assert not (model / "cross_subject_area_relationships.yaml").exists()


@pytest.mark.parametrize("excluded", [("customers", None), ("orders", "cust_ref"), ("orders", "region")])
def test_an_add_on_a_table_or_column_no_copy_serves_undoes_the_batch(model, excluded):
    table, column = excluded
    _set_review_state(model, "sales", table, "rejected", column)
    before = _snapshot(model)
    on = "orders.region = customers.region AND orders.cust_ref = customers.id"
    res = curate.apply(model, [_CUST_REF if column != "region" else _add(from_table="orders",
                                                                         to_table="customers", on=on)])
    assert res.errors and not res.applied and not res.validated, res.as_dict()
    assert _snapshot(model) == before


def _schema_table(root: Path, area: str, schema: str, cols: list[str]) -> None:
    d = root / "subject_areas" / area / "tables"
    d.mkdir(parents=True, exist_ok=True)
    (d / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": schema, "storage_connection": "c", "grain": ["id"], "description": schema,
        "columns": [{"name": "id", "type": "integer", "primary_key": True}]
                   + [{"name": c, "type": "integer"} for c in cols]}))


@pytest.fixture
def two_schemas(model: Path) -> Path:
    """sales serves crm.orders (source_key), catalog serves billing.orders (target_key)."""
    (model / "subject_areas" / "sales" / "tables" / "orders.yaml").unlink()
    (model / "subject_areas" / "sales" / "relationships.yaml").unlink()
    _schema_table(model, "sales", "crm", ["source_key"])
    _schema_table(model, "catalog", "billing", ["target_key"])
    for area, schema in (("sales", "crm"), ("catalog", "billing")):
        sap = model / "subject_areas" / area / "subject_area.yaml"
        sa = yaml.safe_load(sap.read_text())
        sa["tables"] = [t for t in sa["tables"] if t["table"] != "orders"] + [
            {"storage_connection": "c", "schema": schema, "table": "orders"}]
        sap.write_text(yaml.safe_dump(sa))
    return model


def _orders_to_orders(on: str) -> dict:
    return _add(from_table="orders", from_schema="crm", to_table="orders", to_schema="billing", on=on)


def test_a_join_condition_between_same_named_tables_matches_each_column_to_its_schema(two_schemas):
    res = curate.apply(two_schemas, [_orders_to_orders("crm.orders.source_key = billing.orders.target_key")])
    assert res.validated and res.applied, res.as_dict()
    [edge] = yaml.safe_load((two_schemas / "cross_subject_area_relationships.yaml").read_text())["edges"]
    assert (edge["from_schema"], edge["to_schema"]) == ("crm", "billing")


@pytest.mark.parametrize("on", [
    "crm.orders.source_key = billing.orders.source_key",   # billing.orders has no source_key
    "orders.source_key = orders.target_key",               # either end: needs its schema
])
def test_a_join_condition_between_same_named_tables_is_checked_per_end(two_schemas, on):
    before = _snapshot(two_schemas)
    res = curate.apply(two_schemas, [_orders_to_orders(on)])
    assert res.errors and not res.applied and not res.validated, res.as_dict()
    assert _snapshot(two_schemas) == before


def test_dollar_quoted_literals_differing_only_in_case_are_two_joins(model):
    # The fixture's engine is PostgreSQL, whose $$..$$ is a string literal.
    on = "orders.cust_no = customers.cust_no AND customers.region = $${}$$"
    res = curate.apply(model, [_add(from_table="orders", to_table="customers", on=on.format(v)) for v in "Yy"])
    assert len(res.applied) == 2 and not res.skipped, res.as_dict()


@pytest.mark.parametrize("dialect, a, b", [
    ("postgres", "b.f = $$Y$$", "b.f = $$y$$"),
    ("postgres", "b.f = E'it\\'s Y'", "b.f = E'it\\'s y'"),
    ("mysql", "b.f = 'it\\'s Y'", "b.f = 'it\\'s y'"),
    ("tsql", "b.f = N'Y'", "b.f = N'y'"),
    ("postgres", 'b."Flag" = 1', 'b."flag" = 1'),
])
def test_on_text_key_keeps_what_the_dialect_reads_case_sensitively(dialect, a, b):
    from semantic_model.models import on_text_key

    assert on_text_key(a, dialect) != on_text_key(b, dialect)


@pytest.mark.parametrize("dialect, a, b", [
    ("postgres", "B.F  =\n $$Y$$", "b.f = $$Y$$"),
    ("duckdb", 'b."Flag" = 1', 'b."flag" = 1'),
    (None, "A.X = B.Y AND b.f = 'Y'", "a.x = b.y and B.F = 'Y'"),
])
def test_on_text_key_still_folds_case_and_spacing_elsewhere(dialect, a, b):
    from semantic_model.models import on_text_key

    assert on_text_key(a, dialect) == on_text_key(b, dialect)


def test_an_added_join_keeps_the_models_spelling_of_its_tables(model):
    res = curate.apply(model, [_add(from_table="ORDERS", from_column="cust_ref", to_table="Customers",
                                    to_column="ID", from_schema="PUBLIC")])
    assert res.validated and res.applied
    [r] = [r for r in _rels(model) if r.get("from_column") == "cust_ref"]
    assert (r["from_table"], r["from_schema"], r["to_table"], r["to_column"]) == ("orders", "public",
                                                                                   "customers", "id")
    org = loader.load_datasource(model)
    assert ("orders", "cust_ref") in {(r.from_table, r.from_column) for sa in org.subject_areas
                                      for r in sa.relationships}
    assert any(it.get("from_column") == "cust_ref" for it in curate.review_queue(org)["rule_2"])


def test_a_join_between_two_areas_goes_to_the_cross_area_joins(model):
    res = curate.apply(model, [_add(from_table="orders", from_column="product_id", to_table="products",
                                    to_column="id")])
    assert res.validated and res.applied == ["add relationship /orders->products"]
    [edge] = yaml.safe_load((model / "cross_subject_area_relationships.yaml").read_text())["edges"]
    assert (edge["from_subject_area"], edge["to_subject_area"], edge["review_state"]) == ("sales", "catalog",
                                                                                         "unreviewed")
    org = loader.load_datasource(model)
    assert [(r.from_table, r.to_table) for r in org.cross_subject_area_relationships] == [("orders", "products")]
    assert any(it.get("from_column") == "product_id" for it in curate.review_queue(org)["rule_2"])
    # and the cross-area copy counts as already there
    again = curate.apply(model, [_add(from_table="orders", from_column="product_id", to_table="products",
                                      to_column="id")])
    assert again.skipped and not again.applied


def test_an_added_join_follows_a_move_queued_after_it(model):
    res = curate.apply(model, [_CUST_REF, {"op": "move", "kind": "table", "name": "customers", "area": "sales",
                                           "to_area": "catalog"}])
    assert res.validated and not res.errors
    org = loader.load_datasource(model)
    assert ("orders", "cust_ref") in {(r.from_table, r.from_column) for r in org.cross_subject_area_relationships}


def test_sm_curate_applies_an_add_op_file(model, tmp_path, capsys):
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([_CUST_REF]))
    assert cli.main(["curate", str(model), "--ops-file", str(ops)]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] == ["add relationship sales/orders->customers"]
    assert cli.main(["curate", str(model), "--ops-file", str(ops), "--candidate", "x"]) == 2


def test_the_parser_keeps_an_add_before_a_move_and_flags_an_incomplete_one():
    move = {"op": "move", "kind": "table", "area": "sales", "name": "customers", "to_area": "catalog"}
    data, anomalies, _ = F.parse(f"profile: demo\ncurate-ops: {json.dumps([move, _CUST_REF])}\ndone\n")
    assert [o["op"] for o in data["ops"]] == ["add", "move"] and not anomalies
    _, anomalies, _ = F.parse('curate-ops: [{"op":"add","kind":"relationship","from_table":"orders"}]\ndone\n')
    assert anomalies[0]["kind"] == "add_join_missing_field"


def _run_page(model: Path, js: str) -> dict:
    import subprocess
    html = render(title="t", profile="demo", manifest=build_manifest(model, "demo"))
    script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    proc = subprocess.run([NODE, "-"], input=DOM + script + "\n" + js, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# Pick a value in the Add join form's control and fire its change/input listener, as a person would.
_FORM = """
function field(key) { return document.getElementById('tab-content').walk([]).find(n => n.getAttribute('data-join') === key); }
function choose(key, value) {
  const n = field(key); n.value = value;
  (n.listeners.change || n.listeners.input || []).forEach(f => f({ target: n, stopPropagation() {} }));
}
function tableIndex(name) { return String(JOIN_TABLES.findIndex(t => t.name === name)); }
"""


@needs_node
def test_the_explorer_add_join_form_queues_an_add_op_before_moves(model):
    out = _run_page(model, _FORM + """
      activeTab = 'joins'; renderAll();
      btn('+ Add a join').click();
      choose('from_table', tableIndex('orders')); choose('from_column', 'cust_ref');
      choose('to_table', tableIndex('customers')); choose('to_column', 'id');
      choose('relationship', 'many_to_one');
      btn('✓ Add join').click();
      moves['sales.customers'] = { op: 'move', kind: 'table', area: 'sales', name: 'customers', to_area: 'catalog' };
      console.log(JSON.stringify({ block: feedback(), queued: totalQueued() }));""")
    data, anomalies, needs = F.parse(out["block"])
    assert not anomalies and needs is None and out["queued"] == 2
    add, move = data["ops"]
    assert add == {"op": "add", "kind": "relationship", "from_table": "orders", "to_table": "customers",
                   "relationship": "many_to_one", "from_schema": "public", "to_schema": "public",
                   "from_column": "cust_ref", "to_column": "id"}
    assert move["op"] == "move"
    res = curate.apply(model, data["ops"])
    assert res.validated and not res.errors and len(res.applied) == 2


@needs_node
def test_the_explorer_add_join_form_takes_a_join_condition_and_refuses_an_incomplete_one(model):
    out = _run_page(model, _FORM + f"""
      activeTab = 'joins'; renderAll();
      btn('+ Add a join').click();
      choose('from_table', tableIndex('orders'));
      btn('✓ Add join').click();
      const refused = newJoins.length;
      choose('to_table', tableIndex('customers')); choose('relationship', 'many_to_one');
      choose('on', {json.dumps(_COMPOSITE)});
      btn('✓ Add join').click();
      console.log(JSON.stringify({{ refused, block: feedback() }}));""")
    assert out["refused"] == 0
    [op] = F.parse(out["block"])[0]["ops"]
    assert op["on"] == _COMPOSITE and "from_column" not in op
    assert curate.apply(model, [op]).validated


def test_a_refresh_keeps_two_joins_whose_dollar_quoted_literals_differ_in_case():
    from semantic_model.introspect import _same_join
    from semantic_model.models import Relationship

    def rel(v: str) -> Relationship:
        return Relationship(from_table="orders", to_table="customers", relationship="many_to_one",
                            on=f"orders.cust_no = customers.cust_no AND customers.region = $${v}$$")

    assert not _same_join(rel("Y"), rel("y"), "postgres")
    assert _same_join(rel("Y"), rel("Y"), "postgres")
