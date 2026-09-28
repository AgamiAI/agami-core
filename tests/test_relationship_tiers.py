"""A `dataset_names` call sends every edge touching a requested table, in two tiers.

Full detail where BOTH ends were requested — that is where the agent writes an explicit join and
wants the cardinality and the caveat. The join alone everywhere else: the caller has no columns for
the other side, so it cannot write that join as a SELECT, but the scope gate admits any table the
model declares and a correlated `EXISTS` needs only the join key. Dropping those edges would force
a round trip for the subquery case; sending them in full made a two-table request on a hub table
run to 98,724 chars for four usable edges.

The load-bearing assertions here are that NO edge is lost and that every edge keeps a complete
join, in both tiers. The size win is a consequence, not the contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from semantic_model import loader as L  # noqa: E402

# Present on a full edge, absent from a lean one. `description` is here because it only restates
# the two columns the same object already carries.
GOVERNANCE = ("description", "review_state", "confidence", "signed_off_by", "signed_off_at",
              "signed_off_role", "from_subject_area", "to_subject_area", "for_questions_about")
# Always present on a usable edge. Schemas are optional (schema-less DBs, and models written
# before schema-qualified relationships shipped), and the join condition is EITHER the column pair
# OR the `on:` expression — so those are checked separately, by `_has_a_join_condition`.
JOIN = ("from_table", "to_table", "join_type", "relationship")


def _has_a_join_condition(e: dict) -> bool:
    return bool(e.get("on")) or ("from_column" in e and "to_column" in e)


def _model(root: Path, *, executable: str = "same_engine", second_connection: bool = False) -> None:
    """`orders`/`returns` in sales, `users` in people. Edges: orders→returns (intra-area, both
    requestable) and orders→users plus returns→users (cross-area, the hub case)."""
    import yaml

    conns = [("c", "PostgreSQL")] + ([("c2", "Snowflake")] if second_connection else [])
    for name, kind in conns:
        (root / "datasources" / name).mkdir(parents=True)
        (root / "datasources" / name / "storage.yaml").write_text(
            yaml.safe_dump({"name": name, "storage_type": kind}))

    people_conn = "c2" if second_connection else "c"
    for area, tables, conn in (("sales", ["orders", "returns"], "c"),
                               ("people", ["users"], people_conn)):
        adir = root / "subject_areas" / area
        (adir / "tables").mkdir(parents=True)
        for t in tables:
            (adir / "tables" / f"{t}.yaml").write_text(yaml.safe_dump({
                "name": t, "schema": "public", "storage_connection": conn, "grain": ["id"],
                "description": f"{t} table",
                "columns": [{"name": "id", "type": "integer", "primary_key": True},
                            {"name": "order_id", "type": "integer"},
                            {"name": "assigned_to", "type": "integer"}]}))
        (adir / "subject_area.yaml").write_text(yaml.safe_dump({
            "name": area, "description": f"{area} area",
            "tables": [{"storage_connection": conn, "schema": "public", "table": t}
                       for t in tables]}))
        if area == "sales":
            (adir / "relationships.yaml").write_text(yaml.safe_dump({"relationships": [{
                "from_table": "returns", "to_table": "orders", "from_column": "order_id",
                "to_column": "id", "join_type": "LEFT", "relationship": "many_to_one",
                "description": "returns.order_id references orders.id",
                "confidence": "confirmed", "review_state": "approved"}]}))

    def _edge(from_table):
        return {"from_table": from_table, "to_table": "users", "from_column": "assigned_to",
                "to_column": "id", "join_type": "LEFT", "relationship": "many_to_one",
                "executable": executable, "description": f"{from_table}.assigned_to -> users.id",
                "confidence": "confirmed", "review_state": "approved",
                "from_subject_area": "sales", "to_subject_area": "people"}

    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "acme", "version": 1,
        "storage_connections": [{"name": n, "ref": f"datasources/{n}/storage.yaml"}
                                for n, _ in conns],
        "subject_areas": ["subject_areas/sales", "subject_areas/people"],
        "cross_subject_area_relationships": [_edge("orders"), _edge("returns")]}))


@pytest.fixture()
def org(tmp_path):
    _model(tmp_path / "acme")
    return L.load_datasource(tmp_path / "acme")


def _rels(org, tables):
    return L.get_table_context(org, tables, include=["relationships"])["relationships"]


# --- no edge is lost -----------------------------------------------------------------------


@pytest.mark.parametrize("tables,expected", [
    (["orders"], 2),                      # orders→returns, orders→users
    (["orders", "returns"], 3),           # + returns→users
    (["users"], 2),                       # the hub: both cross-area edges
    (["orders", "returns", "users"], 3),
])
def test_every_touching_edge_is_still_returned(org, tables, expected):
    assert len(_rels(org, tables)) == expected


def test_a_hub_table_still_names_every_edge_that_reaches_it(org):
    """The case this change exists for: asking for a hub returns the whole neighbourhood, and
    must go on doing so — only the detail per edge changes."""
    reached = {(e["from_table"], e["to_table"]) for e in _rels(org, ["users"])}
    assert reached == {("orders", "users"), ("returns", "users")}


# --- both tiers stay usable ----------------------------------------------------------------


@pytest.mark.parametrize("tables", [["orders"], ["users"], ["orders", "returns"]])
def test_the_join_is_complete_on_every_edge_in_both_tiers(org, tables):
    """The property the subquery case depends on: a lean edge is still enough to write
    `EXISTS (SELECT 1 FROM other o WHERE o.fk = t.pk)`."""
    for e in _rels(org, tables):
        assert all(k in e for k in JOIN), f"incomplete join on {e}"
        assert _has_a_join_condition(e), f"no join condition on {e}"


def test_an_edge_between_two_requested_tables_keeps_its_full_detail(org):
    edge = [e for e in _rels(org, ["orders", "returns"])
            if {e["from_table"], e["to_table"]} == {"orders", "returns"}]
    assert len(edge) == 1
    assert edge[0]["description"] and edge[0]["review_state"] == "approved"


def test_the_full_tier_drops_the_dead_field_and_the_default_executable_too(org):
    """One response must not omit `same_engine` on one edge and state it on the next, and the
    deprecated field rides along as an empty list on every edge that does not exclude it."""
    for e in _rels(org, ["orders", "returns"]):
        assert "for_questions_about" not in e
        assert e.get("executable") != "same_engine"


def test_an_edge_to_a_table_the_caller_did_not_ask_for_is_the_join_alone(org):
    lean = [e for e in _rels(org, ["orders"]) if e["to_table"] == "users"]
    assert len(lean) == 1
    assert not set(lean[0]) & set(GOVERNANCE), "the lean tier must carry no governance fields"
    assert _has_a_join_condition(lean[0])


def test_the_tiers_are_assigned_by_whether_both_ends_were_requested(org):
    """Checked against the governance list above rather than by diffing the two dumps, so a
    field newly added to the model is invisible here until someone adds it to that list."""
    for e in _rels(org, ["orders", "returns"]):
        both = {e["from_table"], e["to_table"]} <= {"orders", "returns"}
        carries = [k for k in GOVERNANCE if k in e]
        assert bool(carries) is both, f"tier mismatch on {e['from_table']}->{e['to_table']}"


def test_the_schemas_survive_the_lean_tier_when_they_are_set(tmp_path):
    """`from_schema`/`to_schema` are optional — None on schema-less DBs and on models written
    before schema-qualified relationships shipped — so the other fixtures leave them off and
    `exclude_none` strips them. Dropping them from the projection would therefore pass every
    other test here, and would tell an agent to write `FROM users` for `analytics.users`."""
    import yaml

    root = tmp_path / "acme"
    _model(root)
    doc = yaml.safe_load((root / "datasource.yaml").read_text())
    for rel in doc["cross_subject_area_relationships"]:
        rel["from_schema"], rel["to_schema"] = "public", "analytics"
    (root / "datasource.yaml").write_text(yaml.safe_dump(doc))

    lean = [e for e in _rels(L.load_datasource(root), ["orders"]) if e["to_table"] == "users"]
    assert lean and lean[0]["from_schema"] == "public" and lean[0]["to_schema"] == "analytics"


def test_a_schema_qualified_request_still_reaches_the_full_tier(tmp_path):
    """The tier test normalises both endpoints through `_table_alias`, the same way the
    membership filter above it does. Without that, a schema-qualified `from_table` against a bare
    request would silently fall to the lean tier — and every other fixture here uses bare names
    on both sides, so nothing would say so."""
    import yaml

    root = tmp_path / "acme"
    _model(root)
    doc = yaml.safe_load((root / "datasource.yaml").read_text())
    doc["cross_subject_area_relationships"] = [
        {**e, "from_table": f"public.{e['from_table']}", "to_table": "public.users"}
        for e in doc["cross_subject_area_relationships"]]
    (root / "datasource.yaml").write_text(yaml.safe_dump(doc))

    rels = _rels(L.load_datasource(root), ["orders", "users"])
    by_pair = {(e["from_table"], e["to_table"]): e for e in rels}
    assert "description" in by_pair[("public.orders", "public.users")], \
        "a schema-qualified edge between two requested tables must still be full detail"
    # And the control: `returns` was not requested, so its edge stays lean even though both
    # endpoints are spelled the same way.
    assert "description" not in by_pair[("public.returns", "public.users")]


def test_the_on_expression_survives_the_lean_tier(tmp_path):
    """An edge carries EITHER a column pair OR the `on:` SQL-expression escape hatch (CAST,
    compound and function-based joins), never both. A projection listing only the column pair
    leaves such an edge with no join condition at all — unusable rather than merely terse."""
    import yaml

    root = tmp_path / "acme"
    _model(root)
    doc = yaml.safe_load((root / "datasource.yaml").read_text())
    for rel in doc["cross_subject_area_relationships"]:
        rel.pop("from_column"), rel.pop("to_column")
        rel["on"] = f"CAST({rel['from_table']}.assigned_to AS text) = users.id::text"
    (root / "datasource.yaml").write_text(yaml.safe_dump(doc))

    lean = [e for e in _rels(L.load_datasource(root), ["orders"]) if e["to_table"] == "users"]
    assert lean and lean[0]["on"].startswith("CAST(")
    assert _has_a_join_condition(lean[0])


# --- executable is the one field that must survive the projection --------------------------


def test_executable_is_omitted_when_the_join_is_ordinary(org):
    """Every edge in a single-warehouse model is `same_engine`, so carrying it would be pure
    repetition — and a model like that is exactly the fixture that would let a regression ship."""
    assert all("executable" not in e for e in _rels(org, ["orders"]) if e["to_table"] == "users")


@pytest.mark.parametrize("executable", ["split", "informational"])
def test_executable_survives_the_lean_tier_when_the_join_cannot_run_in_one_statement(
        tmp_path, executable):
    """A `split` edge written as a JOIN cannot execute. Dropping the field on the lean tier would
    hide that from the one caller who needs it."""
    root = tmp_path / "acme"
    _model(root, executable=executable, second_connection=True)
    org = L.load_datasource(root)
    lean = [e for e in _rels(org, ["orders"]) if e["to_table"] == "users"]
    assert lean and lean[0]["executable"] == executable


def test_a_cross_engine_edge_keeps_a_complete_join_too(tmp_path):
    root = tmp_path / "acme"
    _model(root, executable="split", second_connection=True)
    org = L.load_datasource(root)
    for e in _rels(org, ["orders"]):
        assert all(k in e for k in JOIN) and _has_a_join_condition(e)


# --- the size win, asserted so it cannot silently regress -----------------------------------


def test_the_hub_request_is_smaller_than_sending_every_edge_in_full(org):
    import json

    rels = _rels(org, ["users"])
    full = sum(len(json.dumps(r.model_dump(exclude_none=True)))
               for r in org.cross_subject_area_relationships)
    assert len(json.dumps(rels)) < full


# --- the identity comparison both tiers depend on -------------------------------------------


@pytest.mark.parametrize("name,schema,expected", [
    ("public.orders", None, ("public", "orders")),      # qualifier embedded in the name
    ("public.orders", "archive", ("archive", "orders")),  # the field wins over the embedded one
    ("Orders", "PUBLIC", ("public", "orders")),          # both halves fold
    ("orders", "", ("", "orders")),                      # empty schema
    ("orders", None, ("", "orders")),                    # and a missing one are the same
    ("catalog.schema.table", None, ("schema", "table")),  # the segment BEFORE the bare name
])
def test_table_key_is_one_comparison(name, schema, expected):
    """Every claim its docstring makes, asserted. Comparing table references two different ways
    is how one half of a comparison came to disagree with the other — three review rounds on this
    PR were that, in three places."""
    from semantic_model.models import table_key

    assert table_key(name, schema) == expected
