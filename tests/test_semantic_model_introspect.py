"""Unit tests for semantic_model/introspect.py — catalog + probe modes via canned
runners (no live DB), plus build.py assembly helpers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import build  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import validator as V  # noqa: E402


# ---------------------------------------------------------------------------
# Canned runners
# ---------------------------------------------------------------------------


def _catalog_runner(sql):
    s = " ".join(sql.split())
    if "information_schema.schemata" in s:
        return [{"schema_name": "public"}]
    if "information_schema.tables" in s and "table_type" in s:
        return [{"schema_name": "public", "table_name": "customers", "table_type": "BASE TABLE"},
                {"schema_name": "public", "table_name": "orders", "table_type": "BASE TABLE"}]
    if "information_schema.columns" in s:
        if "'customers'" in s:
            return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                    {"column_name": "email", "data_type": "varchar", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""}]
        return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                {"column_name": "customer_id", "data_type": "integer", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""},
                {"column_name": "total", "data_type": "numeric", "is_nullable": "YES", "ordinal_position": "3", "numeric_scale": "2"}]
    if "PRIMARY KEY" in s:
        return [{"column_name": "id"}]
    if "FOREIGN KEY" in s:
        return [{"from_table": "orders", "from_column": "customer_id", "to_table": "customers", "to_column": "id"}]
    if "reltuples" in s:
        return [{"estimated_rows": "1000"}]
    return []


def _probe_runner(sql):
    s = " ".join(sql.split())
    if "information_schema" in s or "PRIMARY KEY" in s or "FOREIGN KEY" in s or "reltuples" in s:
        raise RuntimeError("permission denied")
    if "WHERE 1=0" in s:
        return []
    if "COUNT(DISTINCT" in s:
        return [{"total": "3", "distinct_count": "3", "null_count": "0"}]
    if "EXISTS" in s:
        return [{"matched": "2"}]
    if "LIMIT" in s:
        if "orders" in s:
            return [{"id": "1", "customer_id": "10", "total": "9.50"},
                    {"id": "2", "customer_id": "11", "total": "3.00"}]
        return [{"id": "10", "email": "a@x.com"}, {"id": "11", "email": "b@x.com"}]
    return []


# ---------------------------------------------------------------------------
# Catalog mode
# ---------------------------------------------------------------------------


def test_catalog_mode_builds_valid_model(tmp_path):
    org, rep = I.introspect("shop", "postgres", runner=_catalog_runner,
                            artifacts_dir=tmp_path, dry_run=True)
    assert rep.mode_per_capability["columns"] == "catalog"
    assert rep.table_count == 2 and rep.relationship_count == 1
    assert V.validate(org).ok
    rel = org.subject_areas[0].relationships[0]
    assert (rel.from_table, rel.to_table, rel.relationship) == ("orders", "customers", "many_to_one")
    assert rel.confidence == "confirmed"  # postgres FKs are enforced


def test_catalog_mode_grain_from_pk(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_catalog_runner,
                          artifacts_dir=tmp_path, dry_run=True)
    customers = org.subject_areas[0].defined_table("customers")
    assert customers.grain == ["id"]
    assert customers.get_column("id").primary_key


def test_numeric_scale_maps_to_decimal(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_catalog_runner,
                          artifacts_dir=tmp_path, dry_run=True)
    orders = org.subject_areas[0].defined_table("orders")
    assert orders.get_column("total").type == "decimal"


# ---------------------------------------------------------------------------
# Uppercasing dialects (Snowflake / Oracle return COLUMN_NAME, not column_name)
# ---------------------------------------------------------------------------


def _uppercase_row_runner(sql):
    # Simulate an uppercasing dialect: same catalog data, but the header comes back
    # UPPERCASE and wrapped in _Row exactly as the real make_execute_sql_runner does.
    return [I._Row({k.upper(): v for k, v in r.items()}) for r in _catalog_runner(sql)]


def test_row_case_insensitive_lookup_preserves_key_casing():
    r = I._Row({"COLUMN_NAME": "id", "DATA_TYPE": "integer"})
    # fixed lowercase lookups the engine uses must hit the uppercase header
    assert r["column_name"] == "id" and r.get("data_type") == "integer"
    assert "column_name" in r and r["COLUMN_NAME"] == "id"   # exact hit still works
    assert r.get("missing") is None
    # iteration / keys must keep the DB's true casing (probe mode reads real names here)
    assert set(r.keys()) == {"COLUMN_NAME", "DATA_TYPE"}


def test_catalog_mode_handles_uppercase_headers(tmp_path):
    # Regression: Snowflake returns COLUMN_NAME/TABLE_NAME/etc.; the engine reads
    # fixed lowercase keys. _Row folds case so catalog introspection still works —
    # a blanket lowercase would instead break probe mode's real-name discovery.
    org, rep = I.introspect("shop", "postgres", runner=_uppercase_row_runner,
                            artifacts_dir=tmp_path, dry_run=True)
    assert rep.table_count == 2 and rep.relationship_count == 1
    assert V.validate(org).ok
    assert org.subject_areas[0].defined_table("customers").grain == ["id"]


# ---------------------------------------------------------------------------
# Probe mode
# ---------------------------------------------------------------------------


def test_probe_mode_infers_structure_from_data(tmp_path):
    org, rep = I.introspect("shop", "postgres", runner=_probe_runner,
                            artifacts_dir=tmp_path, dry_run=True,
                            tables=["public.customers", "public.orders"])
    assert rep.mode_per_capability["columns"] == "probe"
    assert rep.mode_per_capability["grain"] == "probe"
    assert rep.mode_per_capability["relationships"] == "probe"
    assert V.validate(org).ok
    orders = org.subject_areas[0].defined_table("orders")
    assert {c.name for c in orders.columns} == {"id", "customer_id", "total"}
    assert orders.get_column("total").type == "decimal"
    assert orders.grain == ["id"]
    # FK inferred by name+overlap -> proposed confidence
    rels = org.subject_areas[0].relationships
    assert rels and rels[0].confidence == "proposed"


def test_probe_requires_allowlist_without_catalog(tmp_path):
    with pytest.raises(RuntimeError):
        I.introspect("shop", "postgres", runner=_probe_runner,
                     artifacts_dir=tmp_path, dry_run=True)  # no tables given


def test_email_column_flagged_sensitive(tmp_path):
    org, rep = I.introspect("shop", "postgres", runner=_probe_runner,
                            artifacts_dir=tmp_path, dry_run=True,
                            tables=["public.customers", "public.orders"])
    customers = org.subject_areas[0].defined_table("customers")
    assert customers.get_column("email").sensitive
    assert rep.sensitive_columns >= 1


# ---------------------------------------------------------------------------
# Value-based inference units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("values,expected", [
    (["1", "2", "3"], "integer"),
    (["1.5", "2.0"], "decimal"),
    (["2026-01-02", "2026-03-04"], "date"),
    (["2026-01-02 10:00:00"], "timestamp"),
    (["true", "false"], "boolean"),
    (["alice", "bob"], "string"),
])
def test_value_type_inference(values, expected):
    assert I._infer_value_type(values) == expected


def test_low_cardinality_becomes_choice_field():
    vals = ["A", "B", "A", "B", "A", "B", "A", "B", "A", "B", "A", "B"]
    cf = I._maybe_choice(vals)
    assert cf and set(cf) == {"A", "B"}


def test_high_cardinality_not_choice_field():
    assert I._maybe_choice([str(i) for i in range(100)]) is None


# ---------------------------------------------------------------------------
# Writing + legacy backup
# ---------------------------------------------------------------------------


def test_introspect_writes_canonical_tree_and_loads_back(tmp_path):
    from semantic_model import loader as L
    org, rep = I.introspect("shop", "postgres", runner=_catalog_runner,
                            artifacts_dir=tmp_path, dry_run=False)
    root = tmp_path / "shop"
    assert (root / "org.yaml").exists()
    reloaded = L.load_organization(root)
    assert V.validate(reloaded).ok
    assert {t.name for sa in reloaded.subject_areas for t in sa.tables_defined} == {"customers", "orders"}


def test_legacy_osi_backed_up_on_reonboard(tmp_path):
    # simulate an existing OSI profile at the root
    root = tmp_path / "shop"
    (root / "PUBLIC").mkdir(parents=True)
    (root / "index.yaml").write_text("profile: shop\n")
    (root / "PUBLIC" / "_schema.yaml").write_text("schema: PUBLIC\n")
    I.introspect("shop", "postgres", runner=_catalog_runner, artifacts_dir=tmp_path, dry_run=False)
    assert (root / ".osi_backup" / "index.yaml").exists()
    assert (root / ".osi_backup" / "PUBLIC" / "_schema.yaml").exists()
    assert (root / "org.yaml").exists()  # new model written at root


# ---------------------------------------------------------------------------
# build.py helpers
# ---------------------------------------------------------------------------


def test_cluster_by_family_merges_plural_and_prefix():
    mapping = build.cluster_by_family(["events", "event_types", "event_registrations",
                                       "members", "membership_plans", "member_visits", "sales", "sale_items"])
    # event* collapse together; member*/membership* collapse; sale*/sale_items collapse
    assert mapping["events"] == mapping["event_types"] == mapping["event_registrations"]
    assert mapping["members"] == mapping["membership_plans"] == mapping["member_visits"]
    assert mapping["sales"] == mapping["sale_items"]


def test_deep_table_column_groups_no_orphans():
    cols = [m.Column(name="ID", type="integer", primary_key=True)] + \
           [m.Column(name=f"AL_{i}", type="decimal") for i in range(20)] + \
           [m.Column(name=f"PL_{i}", type="decimal") for i in range(20)]
    groups = build.maybe_column_groups(cols)
    assert groups  # deep table -> groups derived
    grouped = {c for g in groups.values() for c in g}
    assert grouped == {c.name for c in cols}  # every column covered
