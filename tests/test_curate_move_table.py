"""Moving a table between subject areas keeps its joins, metrics and entities (#513).

Synthetic, generic names only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate, loader  # noqa: E402
from semantic_model import validator as V  # noqa: E402


def _table(root: Path, area: str, name: str, cols: list[str], schema: str = "public") -> None:
    d = root / "subject_areas" / area / "tables"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(yaml.safe_dump({
        "name": name, "schema": schema, "storage_connection": "c", "grain": ["id"],
        "description": name,
        "columns": [{"name": "id", "type": "integer", "primary_key": True}]
                   + [{"name": c, "type": "integer"} for c in cols]}))


def _area(root: Path, area: str, tables: list[str], schema: str = "public") -> None:
    d = root / "subject_areas" / area
    d.mkdir(parents=True, exist_ok=True)
    (d / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": area, "description": area,
        "tables": [{"storage_connection": "c", "schema": schema, "table": t} for t in tables]}))


def _join(frm: str, fc: str, to: str, **extra) -> dict:
    return {"from_table": frm, "from_column": fc, "to_table": to, "to_column": "id",
            "relationship": "many_to_one", "confidence": "inferred", "review_state": "unreviewed", **extra}


def _item(root: Path, area: str, sub: str, doc: dict) -> None:
    d = root / "subject_areas" / area / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{doc['name']}.yaml").write_text(yaml.safe_dump(doc))


def _model(root: Path, *, git: bool = False) -> Path:
    """sales owns orders, customers, order_items; catalog owns products; one cross-area join."""
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "movedemo", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/sales", "subject_areas/catalog"],
        "cross_subject_area_relationships": [
            _join("order_items", "product_id", "products",
                  from_subject_area="sales", to_subject_area="catalog")]}))
    _area(root, "sales", ["orders", "customers", "order_items"])
    _area(root, "catalog", ["products"])
    _table(root, "sales", "orders", ["customer_id"])
    _table(root, "sales", "customers", [])
    _table(root, "sales", "order_items", ["order_id", "product_id", "qty"])
    _table(root, "catalog", "products", [])
    (root / "subject_areas" / "sales" / "relationships.yaml").write_text(yaml.safe_dump({
        "relationships": [_join("orders", "customer_id", "customers"),
                          _join("order_items", "order_id", "orders")]}))
    metric = {"confidence": "proposed", "review_state": "unreviewed"}
    _item(root, "sales", "metrics", {"name": "units_sold", "calculation": "units sold",
                                     "bindings": {"PostgreSQL": "SUM(order_items.qty)"},
                                     "source_tables": ["order_items"], "subject_areas": ["sales"], **metric})
    _item(root, "sales", "metrics", {"name": "units_per_order", "calculation": "units per order",
                                     "bindings": {"PostgreSQL": "SUM(order_items.qty) / COUNT(orders.id)"},
                                     "source_tables": ["order_items", "orders"], **metric})
    _item(root, "sales", "metrics", {"name": "customer_count", "calculation": "customers",
                                     "bindings": {"PostgreSQL": "COUNT(customers.id)"},
                                     "primary_table": "customers", **metric})
    _item(root, "sales", "entities", {"name": "line", "maps_to": [{"table": "order_items", "column": "id"}]})
    _item(root, "sales", "entities", {"name": "order", "maps_to": [
        {"table": "orders", "column": "id", "primary": True}, {"table": "order_items", "column": "order_id"}]})
    if git:
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(root), "config", k, v], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)
    return root


def _move(root: Path, name: str, to: str, **extra):
    return curate.apply(root, [{"op": "move", "kind": "table", "name": name, "to_area": to, **extra}])


def _where(org, table: str) -> list[str]:
    return [sa.name for sa in org.subject_areas if any(t.name == table for t in sa.tables_defined)]


def _joins(org):
    intra = {(sa.name, r.from_table, r.to_table) for sa in org.subject_areas for r in sa.relationships}
    cross = {(r.from_subject_area, r.to_subject_area, r.from_table, r.to_table)
             for r in org.cross_subject_area_relationships}
    return intra, cross


def test_moving_a_table_carries_its_joins_metrics_and_entities(tmp_path):
    root = _model(tmp_path)
    res = _move(root, "order_items", "catalog", area="sales")
    assert res.validated and res.applied == ["move table sales/order_items -> catalog"], res.as_dict()

    org = loader.load_datasource(root)
    assert V.validate(org).ok
    assert _where(org, "order_items") == ["catalog"]
    sales, catalog = org.subject_area("sales"), org.subject_area("catalog")
    assert "order_items" not in {r.table for r in sales.tables}
    assert "order_items" in {r.table for r in catalog.tables}
    intra, cross = _joins(org)
    # the join inside sales now spans two areas; the cross-area join now sits inside one
    assert ("sales", "orders", "customers") in intra
    assert ("catalog", "order_items", "products") in intra
    assert cross == {("catalog", "sales", "order_items", "orders")}
    assert {m.name for m in catalog.metrics} == {"units_sold"}
    assert next(m for m in catalog.metrics if m.name == "units_sold").subject_areas == ["catalog"]
    assert {m.name for m in sales.metrics} == {"units_per_order", "customer_count"}
    assert {e.name for e in catalog.entities} == {"line"}
    assert {e.name for e in sales.entities} == {"order"}
    assert "left in sales: metric units_per_order also reads orders" in res.notes
    assert "left in sales: entity order also reads orders" in res.notes
    assert "join order_items->orders is now a cross-area join (catalog -> sales)" in res.notes
    assert "cross-area join order_items->products is now inside area 'catalog'" in res.notes


def test_a_metric_filed_under_the_table_moves_with_it(tmp_path):
    root = _model(tmp_path)
    res = _move(root, "customers", "catalog")   # area resolved from the model
    assert res.validated, res.as_dict()
    org = loader.load_datasource(root)
    assert "customer_count" in {m.name for m in org.subject_area("catalog").metrics}
    _, cross = _joins(org)
    assert ("sales", "catalog", "orders", "customers") in cross


def test_moving_back_restores_the_join_inside_the_area(tmp_path):
    root = _model(tmp_path)
    assert _move(root, "order_items", "catalog").applied
    assert _move(root, "order_items", "sales").applied
    intra, cross = _joins(loader.load_datasource(root))
    assert ("sales", "order_items", "orders") in intra
    assert cross == {("sales", "catalog", "order_items", "products")}


def test_a_move_that_fails_validation_is_undone(tmp_path):
    root = _model(tmp_path)
    for i in range(29):   # catalog at the validator's ceiling: one more table is refused
        _table(root, "catalog", f"t{i}", [])
    _area(root, "catalog", ["products", *(f"t{i}" for i in range(29))])
    before = {p: p.read_bytes() for p in root.rglob("*.yaml")}
    res = _move(root, "order_items", "catalog")
    assert not res.validated and not res.applied and not res.notes
    assert any("has 31 tables" in e for e in res.errors), res.errors
    assert {p: p.read_bytes() for p in root.rglob("*.yaml")} == before
    assert not (root / "curation_log.jsonl").exists()


def test_a_move_is_logged_with_both_areas_and_committed(tmp_path):
    root = _model(tmp_path, git=True)
    res = _move(root, "order_items", "catalog")
    assert res.validated and res.committed, res.as_dict()
    entry = json.loads((root / "curation_log.jsonl").read_text().splitlines()[-1])
    assert entry["op"] == "move" and entry["area"] == "sales" and entry["to_area"] == "catalog"
    log = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert log.startswith("curation:")


def test_a_move_to_a_new_area_creates_and_lists_it(tmp_path):
    root = _model(tmp_path)
    res = _move(root, "customers", "people")
    assert res.validated and "created subject area 'people'" in res.notes, res.as_dict()
    assert "subject_areas/people" in yaml.safe_load((root / "datasource.yaml").read_text())["subject_areas"]
    assert _where(loader.load_datasource(root), "customers") == ["people"]


def test_a_move_to_a_new_area_keeps_it_listed_when_an_inline_cross_join_is_rewritten(tmp_path):
    # The inline cross-area join is rewritten after the new area is listed in the same file.
    root = _model(tmp_path)
    res = _move(root, "order_items", "lines")
    assert res.validated and res.applied, res.as_dict()
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    assert "subject_areas/lines" in ds["subject_areas"]
    assert [(r["from_subject_area"], r["to_subject_area"]) for r in ds["cross_subject_area_relationships"]] \
        == [("lines", "catalog")]
    org = loader.load_datasource(root)
    assert V.validate(org).ok
    assert _where(org, "order_items") == ["lines"]


def test_a_new_area_name_must_be_a_plain_key(tmp_path):
    root = _model(tmp_path)
    res = _move(root, "customers", "Bad Name")
    assert not res.applied and "can't name a new subject area" in res.skipped[0]["reason"]


def test_a_move_names_the_table_by_schema_when_two_schemas_share_it(tmp_path):
    root = _model(tmp_path)
    _area(root, "archive", ["orders"], schema="archive")
    _table(root, "archive", "orders", ["customer_id"], schema="archive")
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["subject_areas"].append("subject_areas/archive")
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds))

    ambiguous = _move(root, "orders", "catalog")
    assert not ambiguous.applied and "multiple areas" in ambiguous.skipped[0]["reason"]
    res = _move(root, "orders", "catalog", schema="archive")
    assert res.validated and res.applied == ["move table archive/orders -> catalog"], res.as_dict()
    org = loader.load_datasource(root, include_rejected=True)
    assert [t.schema_name for t in org.subject_area("catalog").tables_defined if t.name == "orders"] == ["archive"]
    assert _where(org, "orders") == ["sales", "catalog"]
    wrong = _move(root, "orders", "archive", area="sales", schema="archive")
    assert not wrong.applied and "not 'archive'" in wrong.skipped[0]["reason"]


def test_moving_to_the_same_area_or_an_unowned_table_is_skipped(tmp_path):
    root = _model(tmp_path)
    assert "already in area" in _move(root, "orders", "sales", area="sales").skipped[0]["reason"]
    assert "doesn't own" in _move(root, "products", "catalog", area="sales").skipped[0]["reason"]
    assert "to_area" in curate.apply(root, [{"op": "move", "kind": "table", "name": "orders"}]).skipped[0]["reason"]
    assert "tables only" in curate.apply(
        root, [{"op": "move", "kind": "metric", "name": "units_sold", "area": "sales",
                "to_area": "catalog"}]).skipped[0]["reason"]


def test_a_metric_follows_once_every_table_it_reads_has_moved(tmp_path):
    root = _model(tmp_path)
    res = curate.apply(root, [{"op": "move", "kind": "table", "name": t, "to_area": "catalog"}
                              for t in ("order_items", "orders")])
    assert res.validated, res.as_dict()
    catalog = loader.load_datasource(root).subject_area("catalog")
    assert {"units_sold", "units_per_order"} <= {m.name for m in catalog.metrics}
    assert {"line", "order"} <= {e.name for e in catalog.entities}
    assert "left in sales: metric units_per_order also reads orders" in res.notes   # after the first move


def _with_archive(root: Path, *, cross_from: str = "archive") -> Path:
    """An `archive` area with its own `orders` (schema archive), joined to `buyers` by bare name."""
    _area(root, "archive", ["orders", "buyers"], schema="archive")
    _table(root, "archive", "orders", ["buyer_id"], schema="archive")
    _table(root, "archive", "buyers", [], schema="archive")
    (root / "subject_areas" / "archive" / "relationships.yaml").write_text(yaml.safe_dump({
        "relationships": [_join("orders", "buyer_id", "buyers")]}))
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["subject_areas"].append("subject_areas/archive")
    ds["cross_subject_area_relationships"].append(
        _join("orders", "product_id", "products", from_subject_area=cross_from, to_subject_area="catalog"))
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds))
    return root


def test_a_bare_join_follows_the_table_when_two_schemas_share_its_name(tmp_path):
    root = _with_archive(_model(tmp_path))
    res = _move(root, "orders", "catalog", area="archive", schema="archive")
    assert res.validated and res.applied, res.as_dict()
    org = loader.load_datasource(root)
    intra, cross = _joins(org)
    assert ("catalog", "archive", "orders", "buyers") in cross
    edge = next(r for r in org.cross_subject_area_relationships if r.to_table == "buyers")
    assert edge.from_schema == "archive"   # written, so the join keeps meaning this orders
    # the cross-area edge declared from archive is now inside catalog
    assert ("catalog", "orders", "products") in intra
    assert next(r for r in org.subject_area("catalog").relationships if r.to_table == "products").from_schema == "archive"
    assert ("sales", "orders", "customers") in intra   # the other schema's orders is untouched
    assert not org.subject_area("archive").relationships


def test_a_move_is_refused_when_a_join_on_the_table_cannot_be_resolved(tmp_path):
    # the cross-area edge says its `orders` is in catalog, which has none, and two schemas have one
    root = _with_archive(_model(tmp_path), cross_from="catalog")
    before = {p: p.read_bytes() for p in root.rglob("*.yaml")}
    res = _move(root, "orders", "people", area="archive", schema="archive")
    assert not res.applied and "can't move 'orders'" in res.skipped[0]["reason"], res.as_dict()
    assert {p: p.read_bytes() for p in root.rglob("*.yaml")} == before
    assert not (root / "subject_areas" / "people").exists()


def test_a_join_to_a_table_no_area_defines_stays_and_is_noted(tmp_path):
    root = _model(tmp_path)
    rp = root / "subject_areas" / "sales" / "relationships.yaml"
    doc = yaml.safe_load(rp.read_text())
    doc["relationships"].append(_join("customers", "region_id", "regions"))
    rp.write_text(yaml.safe_dump(doc))
    res = curate.apply(root, [{"op": "move", "kind": "table", "name": "customers", "to_area": "catalog"}])
    assert res.applied, res.as_dict()
    assert "left in sales: join customers->regions points at a table no area defines" in res.notes
    kept = yaml.safe_load(rp.read_text())["relationships"]
    assert any(r["to_table"] == "regions" for r in kept)


def test_a_failed_move_into_a_new_area_can_be_retried(tmp_path, monkeypatch):
    import types

    root = _model(tmp_path)
    real = curate.V.validate
    monkeypatch.setattr(curate.V, "validate", lambda *a, **k: types.SimpleNamespace(ok=False, errors=["forced"]))
    assert not _move(root, "customers", "people").applied
    assert not (root / "subject_areas" / "people").exists()   # its folders are removed too
    (root / "subject_areas" / "people" / "metrics").mkdir(parents=True)   # an empty leftover doesn't block
    monkeypatch.setattr(curate.V, "validate", real)
    res = _move(root, "customers", "people")
    assert res.validated and res.applied, res.as_dict()
    assert _where(loader.load_datasource(root), "customers") == ["people"]


def test_a_metric_follows_its_tables_when_they_arrive_by_separate_moves(tmp_path):
    root = _model(tmp_path)
    assert _move(root, "order_items", "catalog").applied
    assert _move(root, "orders", "fulfilment").applied
    res = _move(root, "order_items", "fulfilment")
    assert res.validated, res.as_dict()
    org = loader.load_datasource(root)
    assert "units_per_order" in {m.name for m in org.subject_area("fulfilment").metrics}
    assert "order" in {e.name for e in org.subject_area("fulfilment").entities}
    assert "units_per_order" not in {m.name for m in org.subject_area("sales").metrics}
    assert "metric units_per_order moved from sales: every table it reads is now in fulfilment" in res.notes


def test_a_batch_undone_by_a_later_op_reports_no_notes(tmp_path):
    root = _model(tmp_path)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    res = curate.apply(root, [{"op": "move", "kind": "table", "name": "order_items", "to_area": "lines"},
                              {"op": "approve", "kind": "metric", "name": "no such metric"}])
    assert res.errors and not res.applied and not res.notes, res.as_dict()
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


_SIGNED = {"review_state": "approved", "signed_off_by": "you@example.com", "signed_off_role": "owner",
           "signed_off_at": "2026-01-01T00:00:00Z"}


def _catalog_holds(root: Path, **extra) -> None:
    (root / "subject_areas" / "catalog" / "relationships.yaml").write_text(yaml.safe_dump(
        {"relationships": [_join("order_items", "product_id", "products", **extra)]}))


@pytest.mark.parametrize("cross_extra,area_extra,kept", [
    ({}, {}, "unreviewed"),
    (_SIGNED, {}, "approved"),            # the moved copy is signed: it replaces the one in place
    ({}, _SIGNED, "approved"),            # the copy in place is signed: it stays
])
def test_a_move_never_lands_a_join_the_area_already_holds(tmp_path, cross_extra, area_extra, kept):
    root = _model(tmp_path)
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["cross_subject_area_relationships"][0].update(cross_extra)
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds))
    _catalog_holds(root, **area_extra)
    res = _move(root, "order_items", "catalog")
    assert res.validated, res.as_dict()
    rels = loader.load_datasource(root, include_rejected=True).subject_area("catalog").relationships
    same = [r for r in rels if (r.from_table, r.to_table) == ("order_items", "products")]
    assert len(same) == 1 and same[0].review_state == kept
    assert any(n.startswith("join order_items->products was already in area 'catalog'; kept one copy: ")
               for n in res.notes), res.notes


def test_a_move_is_refused_when_two_copies_of_a_join_are_signed_differently(tmp_path):
    root = _model(tmp_path)
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["cross_subject_area_relationships"][0].update(_SIGNED)
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds))
    _catalog_holds(root, **{**_SIGNED, "relationship": "one_to_one"})
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    res = _move(root, "order_items", "catalog")
    assert not res.applied and "signed off differently" in res.skipped[0]["reason"], res.as_dict()
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


_ENGINE = {"review_state": "approved", "confidence": "confirmed", "signed_off_by": "agami_introspect",
           "signed_off_role": "system", "signed_off_at": "2026-01-01T00:00:00Z"}


def test_a_person_signed_copy_outranks_an_engine_approved_one(tmp_path):
    root = _model(tmp_path)
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["cross_subject_area_relationships"][0].update(_SIGNED)
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds))
    _catalog_holds(root, **_ENGINE, description="each line's product")
    res = _move(root, "order_items", "catalog")
    assert res.validated, res.as_dict()
    rels = loader.load_datasource(root).subject_area("catalog").relationships
    same = [r for r in rels if (r.from_table, r.to_table) == ("order_items", "products")]
    assert [(r.signed_off_by, r.description) for r in same] == [("you@example.com", "each line's product")]
    assert ("join order_items->products was already in area 'catalog'; kept one copy: "
            "the moved copy, signed off by a person") in res.notes


def test_an_engine_approved_copy_outranks_an_unreviewed_one(tmp_path):
    root = _model(tmp_path)
    _catalog_holds(root, **_ENGINE)
    res = _move(root, "order_items", "catalog")
    assert res.validated, res.as_dict()
    rels = loader.load_datasource(root).subject_area("catalog").relationships
    assert [r.signed_off_by for r in rels if r.from_table == "order_items"] == ["agami_introspect"]


_COMPOSITE = "shipments.order_id = order_items.order_id AND shipments.product_id = order_items.product_id"


def _with_shipments(root: Path, on: str) -> Path:
    """sales also owns shipments, joined to order_items by line id and by an `on:` over two columns."""
    sa = yaml.safe_load((root / "subject_areas" / "sales" / "subject_area.yaml").read_text())
    sa["tables"].append({"storage_connection": "c", "schema": "public", "table": "shipments"})
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(yaml.safe_dump(sa))
    _table(root, "sales", "shipments", ["line_id", "order_id", "product_id"])
    rels_file = root / "subject_areas" / "sales" / "relationships.yaml"
    rels = yaml.safe_load(rels_file.read_text())
    rels["relationships"] += [
        {**_join("shipments", "line_id", "order_items"), **_ENGINE},
        {"from_table": "shipments", "to_table": "order_items", "on": on, "relationship": "many_to_one",
         **_ENGINE}]
    rels_file.write_text(yaml.safe_dump(rels))
    return root


def _shipment_joins(org) -> list:
    rels = [r for sa in org.subject_areas for r in sa.relationships] + list(org.cross_subject_area_relationships)
    return sorted(((r.on or "", r.from_column or "") for r in rels if r.from_table == "shipments"))


@pytest.mark.parametrize("on", [_COMPOSITE, "shipments.order_id = order_items.order_id AND order_items.qty = 1"])
def test_a_table_with_an_on_join_beside_a_column_join_moves_and_back(tmp_path, on):
    root = _with_shipments(_model(tmp_path), on)
    want = sorted([(on, ""), ("", "line_id")])
    res = _move(root, "shipments", "logistics")
    assert res.validated, res.as_dict()
    org = loader.load_datasource(root)
    assert _shipment_joins(org) == want
    assert {(r.from_subject_area, r.to_subject_area) for r in org.cross_subject_area_relationships
            if r.from_table == "shipments"} == {("logistics", "sales")}
    res = _move(root, "shipments", "sales")
    assert res.validated, res.as_dict()
    org = loader.load_datasource(root)
    assert _shipment_joins(org) == want and V.validate(org).ok


def test_moving_back_into_an_area_that_holds_the_on_join_keeps_one_copy(tmp_path):
    root = _with_shipments(_model(tmp_path), _COMPOSITE)
    assert _move(root, "shipments", "logistics").validated
    rels_file = root / "subject_areas" / "sales" / "relationships.yaml"
    rels = yaml.safe_load(rels_file.read_text())
    rels["relationships"].append({"from_table": "shipments", "to_table": "order_items",
                                  "on": _COMPOSITE,
                                  "relationship": "many_to_one", **_ENGINE})
    rels_file.write_text(yaml.safe_dump(rels))
    res = _move(root, "shipments", "sales")
    assert res.validated, res.as_dict()
    org = loader.load_datasource(root)
    on_joins = [r for r in org.subject_area("sales").relationships if r.from_table == "shipments" and r.on]
    assert len(on_joins) == 1 and len(_shipment_joins(org)) == 2
    assert " ".join(on_joins[0].on.lower().split()) == _COMPOSITE.lower()
    assert V.validate(org).ok


def test_a_join_that_turns_cross_area_is_not_added_twice(tmp_path):
    root = _model(tmp_path)
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["cross_subject_area_relationships"].append(   # a stray copy of sales' own join
        _join("order_items", "order_id", "orders", from_subject_area="sales", to_subject_area="sales"))
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds))
    res = _move(root, "order_items", "catalog")
    assert res.validated, res.as_dict()
    cross = loader.load_datasource(root).cross_subject_area_relationships
    assert [(r.from_subject_area, r.to_subject_area) for r in cross
            if (r.from_table, r.to_table) == ("order_items", "orders")] == [("catalog", "sales")]


# A join a person edited after its approval is `stale` and keeps the approval's signer (#537).
_STALE_ENGINE = {**_ENGINE, "review_state": "stale", "relationship": "one_to_one"}


def test_a_stale_copy_outranks_an_engine_approved_one(tmp_path):
    root = _model(tmp_path)
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["cross_subject_area_relationships"][0].update(_STALE_ENGINE)
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds))
    _catalog_holds(root, **_ENGINE)
    res = _move(root, "order_items", "catalog")
    assert res.validated, res.as_dict()
    rels = loader.load_datasource(root).subject_area("catalog").relationships
    same = [r for r in rels if (r.from_table, r.to_table) == ("order_items", "products")]
    assert [(r.review_state, r.relationship) for r in same] == [("stale", "one_to_one")]
    assert ("join order_items->products was already in area 'catalog'; kept one copy: "
            "the moved copy, edited by a person since its approval") in res.notes


def test_a_move_is_refused_when_a_stale_copy_and_a_signed_copy_disagree(tmp_path):
    root = _model(tmp_path)
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["cross_subject_area_relationships"][0].update({**_SIGNED, "review_state": "stale",
                                                      "relationship": "one_to_one"})
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds))
    _catalog_holds(root, **_SIGNED)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    res = _move(root, "order_items", "catalog")
    assert not res.applied and "signed off differently" in res.skipped[0]["reason"], res.as_dict()
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("kind,name,edit,rule", [
    ("relationship", "order_items->orders", {"from_column": "order_id", "field": "relationship",
                                             "value": "one_to_one"}, "rule_2"),
    ("metric", "units_sold", {"field": "bindings", "value": {"PostgreSQL": "SUM(order_items.qty) * 2"}},
     "rule_1"),
])
def test_a_moved_stale_item_stays_stale_and_shows_what_changed(tmp_path, kind, name, edit, rule):
    root = _model(tmp_path)
    who = {"signer": "you@example.com", "role": "owner"}
    op = {"kind": kind, "area": "sales", "name": name}
    which = {k: v for k, v in edit.items() if k == "from_column"}
    assert curate.apply(root, [{**op, **which, "op": "approve", "at": "2026-01-01T00:00:00Z"}], **who).validated
    assert curate.apply(root, [{**op, "op": "edit", **edit}], **who).stale
    for to in ("catalog", "sales"):
        assert _move(root, "order_items", to).validated
        (it,) = [it for it in curate.review_queue(loader.load_datasource(root), root=root)[rule]
                 if it["name"] == name]
        assert (it["review_state"], it.get("signed_off_by")) == ("stale", "you@example.com"), it
        changed = it["changes_since_approval"]
        assert changed["recorded"] and [c["field"] for c in changed["changes"]] == [edit["field"]], (to, it)


_WHO = {"signer": "you@example.com", "role": "owner"}
_UNITS = {"kind": "metric", "name": "units_sold"}
_LINE_JOIN = {"kind": "relationship", "name": "order_items->orders", "from_column": "order_id"}


def _approved_then_edited(root: Path, *then: dict):
    """Approve units_sold and order_items->orders, then edit both, move order_items to catalog and
    run `then`, all in one batch."""
    at = {"at": "2026-01-01T00:00:00Z"}
    assert curate.apply(root, [{**_UNITS, "op": "approve", "area": "sales", **at},
                               {**_LINE_JOIN, "op": "approve", "area": "sales", **at}], **_WHO).validated
    return curate.apply(root, [
        {**_UNITS, "op": "edit", "area": "sales", "field": "unit", "value": "items"},
        {**_LINE_JOIN, "op": "edit", "area": "sales", "field": "relationship", "value": "one_to_one"},
        {"op": "move", "kind": "table", "name": "order_items", "to_area": "catalog"}, *then], **_WHO)


def test_a_moved_stale_item_is_reported_where_it_now_is(tmp_path):
    res = _approved_then_edited(_model(tmp_path))
    assert res.validated, res.as_dict()
    assert res.stale == ["metric catalog/units_sold", "relationship _cross/order_items->orders"]


def test_approving_a_moved_item_in_the_same_batch_clears_its_stale_notice(tmp_path):
    root = _model(tmp_path)
    at = {"op": "approve", "area": "catalog", "at": "2026-01-02T00:00:00Z"}
    res = _approved_then_edited(root, {**_UNITS, **at}, {**_LINE_JOIN, **at})
    assert res.validated and res.stale == [], res.as_dict()
    org = loader.load_datasource(root)
    assert next(m for m in org.subject_area("catalog").metrics if m.name == "units_sold").review_state == "approved"
    assert {(r.from_table, r.review_state) for r in org.cross_subject_area_relationships} == \
        {("order_items", "approved")}


def _revenue(root: Path, area: str, table: str) -> None:
    _item(root, area, "metrics", {"name": "revenue", "calculation": f"{table} count",
                                  "bindings": {"PostgreSQL": f"COUNT({table}.id)"}, "source_tables": [table],
                                  "confidence": "proposed", "review_state": "approved",
                                  "signed_off_by": "you@example.com", "signed_off_role": "owner",
                                  "signed_off_at": "2026-01-01T00:00:00Z"})


def _revenue_history(root: Path, area: str) -> dict:
    (it,) = [it for it in curate.review_queue(loader.load_datasource(root), root=root)["rule_1"]
             if (it["name"], it["area"]) == ("revenue", area)]
    return it["changes_since_approval"]


def test_a_move_does_not_give_a_destination_metric_another_areas_history(tmp_path):
    root = _model(tmp_path)
    _revenue(root, "catalog", "products")
    _revenue(root, "sales", "orders")
    for area, unit in (("catalog", "USD"), ("sales", "EUR")):
        assert curate.apply(root, [{"op": "edit", "kind": "metric", "area": area, "name": "revenue",
                                    "field": "unit", "value": unit}], **_WHO).stale
    assert _move(root, "customers", "catalog").validated
    since = _revenue_history(root, "catalog")
    assert [c["text"] for c in since["changes"]] == ["unit changed from (none) to USD"] and since["complete"]
    log = [json.loads(ln) for ln in (root / curate.CURATION_LOG).read_text().splitlines()]
    assert log[-1]["carried"] == [{"kind": "metric", "name": "customer_count", "from": "sales"}]


def test_a_move_logged_without_what_it_carried_says_changes_not_recorded(tmp_path):
    root = _model(tmp_path)
    _revenue(root, "catalog", "products")
    assert curate.apply(root, [{"op": "edit", "kind": "metric", "area": "catalog", "name": "revenue",
                                "field": "unit", "value": "USD"}], **_WHO).stale
    assert _move(root, "customers", "catalog").validated
    path = root / curate.CURATION_LOG
    lines = [json.loads(ln) for ln in path.read_text().splitlines()]
    del lines[-1]["carried"]
    path.write_text("".join(json.dumps(e) + "\n" for e in lines))
    assert _revenue_history(root, "catalog")["summary"] == "changes not recorded"
