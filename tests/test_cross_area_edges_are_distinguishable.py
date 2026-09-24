"""The schema payload's cross-area block must say WHICH tables bridge two areas, once each.

It used to project `{from, to, for_questions_about}`, and nothing anywhere populates
`for_questions_about` — it was `setdefault`-ed empty when an edge was generated and never filled,
including in the sample model this product ships. So each entry was a bare pair of AREA names, and
a model whose `sales` area reaches `people` through a dozen different columns (assigned_to,
created_by, approved_by, …) emitted `sales → people` a dozen identical times.

Naming the endpoint tables fixed that for edges between DIFFERENT table pairs. It did not fix the
several edges that reach the SAME pair through different columns: the column is the only thing
telling those apart and this tier carries no columns, so they still serialized identically. An
adjacency map states each bridge once.

The join mechanics stay off this tier on purpose — a `dataset_names` call returns each edge in full
(columns, `on`, cardinality, trust block) via `loader._relationships_among`. This tier answers the
routing question: which table do I ask for next.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

import tools  # noqa: E402


def _model(root: Path) -> None:
    """Two areas; two edges to distinct tables, and three that reach ONE pair by three columns."""
    import yaml

    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    for area, tables in (("sales", ["orders", "returns"]), ("people", ["users"])):
        adir = root / "subject_areas" / area
        (adir / "tables").mkdir(parents=True)
        for t in tables:
            (adir / "tables" / f"{t}.yaml").write_text(yaml.safe_dump({
                "name": t, "schema": "public", "storage_connection": "c", "grain": ["id"],
                "description": f"{t} table",
                "columns": [{"name": "id", "type": "integer", "primary_key": True},
                            {"name": "assigned_to", "type": "integer"},
                            {"name": "created_by", "type": "integer"},
                            {"name": "approved_by", "type": "integer"}]}))
        (adir / "subject_area.yaml").write_text(yaml.safe_dump({
            "name": area, "description": f"{area} area",
            "tables": [{"storage_connection": "c", "schema": "public", "table": t}
                       for t in tables]}))

    def _edge(from_table, column):
        return {"from_table": from_table, "to_table": "users", "from_column": column,
                "to_column": "id", "join_type": "LEFT", "relationship": "many_to_one",
                "confidence": "proposed", "review_state": "unreviewed",
                "from_subject_area": "sales", "to_subject_area": "people"}

    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "acme", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/sales", "subject_areas/people"],
        # orders→users three times, by three different columns: the case that repeated.
        "cross_subject_area_relationships": [_edge("orders", "assigned_to"),
                                             _edge("orders", "created_by"),
                                             _edge("orders", "approved_by"),
                                             _edge("returns", "created_by")]}))


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    art = tmp_path / "art"
    _model(art / "acme")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    return "acme"


def _block(profile: str) -> dict:
    out = tools.tool_get_datasource_schema({"datasource": profile, "mode": "index"})
    return json.JSONDecoder().raw_decode(out)[0]["cross_area_relationships"]


def test_each_bridge_is_named_once(profile):
    assert _block(profile)["joins"] == {"orders": ["users"], "returns": ["users"]}


def test_four_declared_edges_collapse_to_two_bridges(profile):
    """Three of the four reach orders→users by different columns. The routing answer is one."""
    joins = _block(profile)["joins"]
    assert sum(len(v) for v in joins.values()) == 2, \
        "edges differing only by join column must not repeat the bridge they name"


def test_the_area_each_table_belongs_to_survives(profile):
    """The per-edge form carried `from`/`to` area names. Dropping them would be a regression —
    and this is the only place a response says which area a table is in."""
    assert _block(profile)["areas"] == {"orders": "sales", "returns": "sales", "users": "people"}


def test_every_table_named_in_joins_has_an_area(profile):
    block = _block(profile)
    named = set(block["joins"]) | {t for v in block["joins"].values() for t in v}
    assert named <= set(block["areas"]), "a bridge names a table with no declared area"


def test_an_area_scope_keeps_only_that_areas_bridges(tmp_path, monkeypatch):
    art = tmp_path / "art"
    _model(art / "acme")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    out = tools.tool_get_datasource_schema(
        {"datasource": "acme", "mode": "index", "area": "people"})
    block = json.JSONDecoder().raw_decode(out)[0]["cross_area_relationships"]
    assert block["joins"] == {"orders": ["users"], "returns": ["users"]}


def test_the_dead_field_is_no_longer_projected(profile):
    assert "for_questions_about" not in json.dumps(_block(profile))


def test_a_model_still_loads_when_it_declares_the_deprecated_field(tmp_path, monkeypatch):
    """The field stays DECLARED on the model. These models `forbid` unknown keys, and generation
    wrote `for_questions_about: []` into edges on disk — the shipped sample carries it twelve times
    — so removing it would fail every such model at load. Dropping it needs a format migration."""
    import yaml
    from semantic_model import loader as L

    art = tmp_path / "art"
    root = art / "acme"
    _model(root)
    doc = yaml.safe_load((root / "datasource.yaml").read_text())
    for rel in doc["cross_subject_area_relationships"]:
        rel["for_questions_about"] = []
    (root / "datasource.yaml").write_text(yaml.safe_dump(doc))
    org = L.load_datasource(root)
    assert len(org.cross_subject_area_relationships) == 4
