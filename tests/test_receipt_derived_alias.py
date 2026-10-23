"""The receipt names the table behind a derived-table or CTE alias, and reads a FILTER clause (#521).

`COUNT(*) FILTER (WHERE s.x > 0)` over a subquery `s` labelled the reference `s.x`, and the
aggregate said it names no column. The receipt now follows `s` to `public.t.x` through a plain
projection, and an aggregate's FILTER predicate counts as a column it reads. What the scope gate
refuses does not change.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import loader as L  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402


@pytest.fixture()
def org(tmp_path):
    p = tmp_path / "p"
    (p / "datasources" / "c").mkdir(parents=True)
    tables = p / "subject_areas" / "s" / "tables"
    tables.mkdir(parents=True)
    (p / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (p / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (p / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": "public", "table": "t"},
                                {"storage_connection": "c", "schema": "public", "table": "u"}]}))
    (tables / "t.yaml").write_text(yaml.safe_dump({
        "name": "t", "schema": "public", "storage_connection": "c", "grain": ["id"], "description": "t",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "x", "type": "integer"}, {"name": "cat", "type": "string"}]}))
    (tables / "u.yaml").write_text(yaml.safe_dump({
        "name": "u", "schema": "public", "storage_connection": "c", "grain": ["id"], "description": "u",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "t_id", "type": "integer"}, {"name": "y", "type": "integer"}]}))
    (p / "subject_areas" / "s" / "relationships.yaml").write_text(yaml.safe_dump({
        "relationships": [{"from_table": "u", "from_column": "t_id", "to_table": "t", "to_column": "id",
                           "from_schema": "public", "to_schema": "public", "relationship": "many_to_one",
                           "confidence": "confirmed"}]}))
    return L.load_datasource(p)


ACCEPTANCE = "SELECT COUNT(*) FILTER (WHERE s.x > 0) AS n FROM (SELECT id, x FROM public.t) s"
OVER_CTE = "WITH s AS (SELECT id, x AS y FROM public.t) SELECT COUNT(*) FILTER (WHERE s.y > 0) AS n FROM s"
PLAIN = "SELECT COUNT(*) FILTER (WHERE x > 0) AS n FROM public.t"
COMPUTED = "SELECT s.r FROM (SELECT x / id AS r FROM public.t) s"
NESTED = "SELECT s.x FROM (SELECT k.x FROM (SELECT x FROM public.t) k) s"
SHADOWED_DERIVED = ("SELECT s.x FROM (SELECT x FROM public.t) s "
                    "WHERE s.x IN (SELECT s.y FROM public.u s)")
SHADOWED_TABLE = "SELECT s.x FROM public.t s WHERE s.id IN (SELECT s.id FROM public.u s WHERE s.y > 0)"


def _references(org, sql: str) -> list[str]:
    receipt = rt.assemble_receipt(org, sql)
    return [i["column"] for i in receipt["columns"]["items"] if i["kind"] == "reference"]


def test_a_filter_over_a_subquery_alias_names_the_column_and_its_table(org):
    refs = _references(org, ACCEPTANCE)
    assert "public.t.x" in refs
    assert "s.x" not in refs


def test_a_cte_alias_is_followed_through_a_renamed_projection(org):
    refs = _references(org, OVER_CTE)
    assert "public.t.x" in refs
    assert "s.y" not in refs


def test_a_chain_of_derived_tables_is_followed_to_the_table(org):
    assert _references(org, NESTED) == ["public.t.x"]


def test_a_computed_projection_keeps_the_name_it_was_written_with(org):
    refs = _references(org, COMPUTED)
    assert "s.r" in refs
    assert not any(ref.endswith(".r") and ref.startswith("public.") for ref in refs)


@pytest.mark.parametrize("sql", [SHADOWED_DERIVED, SHADOWED_TABLE], ids=["derived", "table"])
def test_a_reused_alias_resolves_in_the_scope_it_was_written_in(org, sql):
    refs = _references(org, sql)
    assert "public.t.x" in refs
    assert "public.u.y" in refs
    # The inner `s` is `u`, which has no `x`: the outer `s.x` must not be credited to it.
    assert "public.u.x" not in refs


def test_a_filter_predicate_is_a_column_the_aggregate_reads(org):
    (item,) = rt.assemble_receipt(org, PLAIN)["aggregates"]["items"]
    assert item["status"] == "not_multiplied"
    assert item["reason"] is None


def test_a_filter_over_a_join_reads_like_the_same_test_inside_a_case(org):
    # Counted rows are `u` rows, so a predicate on the one side `t` is multiplied by the join.
    join = "FROM public.t t JOIN public.u u ON u.t_id = t.id"
    filtered = rt.assemble_receipt(org, f"SELECT COUNT(*) FILTER (WHERE t.x > 0) AS n {join}")
    cased = rt.assemble_receipt(org, f"SELECT COUNT(CASE WHEN t.x > 0 THEN 1 END) AS n {join}")
    (f_item,) = filtered["aggregates"]["items"]
    (c_item,) = cased["aggregates"]["items"]
    assert f_item["status"] == c_item["status"] == "multiplied"
    assert [f["risk"] for f in f_item["findings"]] == [f["risk"] for f in c_item["findings"]] == ["fan_trap"]


def test_count_star_without_a_filter_still_names_no_column(org):
    (item,) = rt.assemble_receipt(org, "SELECT COUNT(*) AS n FROM public.t")["aggregates"]["items"]
    assert item["status"] == "undetermined"
    assert "names no column" in item["reason"]


def test_a_value_from_a_derived_table_is_still_not_matched_to_a_metric(org):
    # ACE-058/060 keep this undetermined: matching a value through a subquery is not done here.
    receipt = rt.assemble_receipt(org, ACCEPTANCE)
    (output,) = [i for i in receipt["columns"]["items"] if i["kind"] == "output"]
    assert output["status"] == "undetermined"


@pytest.mark.parametrize("sql", [ACCEPTANCE, OVER_CTE, PLAIN, COMPUTED, NESTED,
                                 SHADOWED_DERIVED, SHADOWED_TABLE])
def test_the_scope_gate_still_admits_these_statements(org, sql):
    assert rt.check_column_scope(sql, org) is None


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FILTER (WHERE s.z > 0) AS n FROM (SELECT id, z FROM public.t) s",
    "WITH s AS (SELECT id, z FROM public.t) SELECT COUNT(*) FILTER (WHERE s.z > 0) AS n FROM s",
    "SELECT COUNT(*) FILTER (WHERE z > 0) AS n FROM public.t",
])
def test_the_scope_gate_still_refuses_an_undeclared_column(org, sql):
    refusal = rt.check_column_scope(sql, org)
    assert refusal is not None
    assert refusal.rule == "column_scope"
