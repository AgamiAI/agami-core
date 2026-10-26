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
    assert _move(root, "order_items", "catalog").validated
    assert _move(root, "order_items", "sales").validated
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
