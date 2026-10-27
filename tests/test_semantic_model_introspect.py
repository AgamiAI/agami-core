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

from catalog_helpers import col, make_catalog_runner  # noqa: E402

# ---------------------------------------------------------------------------
# Canned runners
# ---------------------------------------------------------------------------


_catalog_runner = make_catalog_runner(
    tables=["customers", "orders"],
    columns={
        "customers": [col("id", "integer", nullable=False), col("email", "varchar")],
        "orders": [col("id", "integer", nullable=False), col("customer_id", "integer"),
                   col("total", "numeric", scale=2)],
    },
    fks=[{"from_table": "orders", "from_column": "customer_id", "to_table": "customers", "to_column": "id"}],
)


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
    assert not any("no declared foreign keys" in n for n in rep.notes)


def test_no_catalog_fks_says_joins_are_inferred(tmp_path):
    # An empty FK result falls back to guessing joins; the report must say so rather than stay silent.
    runner = make_catalog_runner(tables=["customers", "orders"], columns={
        "customers": [col("id", "integer", nullable=False)],
        "orders": [col("id", "integer", nullable=False), col("customer_id", "integer")],
    })
    _, rep = I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path, dry_run=True)
    assert rep.mode_per_capability["relationships"] == "probe"
    assert any("no declared foreign keys were visible" in n for n in rep.notes)


def test_declared_fks_to_tables_outside_the_model_are_skipped(tmp_path):
    # The catalog lists every FK in the schema, so one to a pruned table or to a schema that was not
    # introspected must not become a join to a table the model does not have.
    runner = make_catalog_runner(
        tables=["customers", "orders", "regions"],
        columns={
            "customers": [col("id", "integer", nullable=False), col("region_id", "integer")],
            "orders": [col("id", "integer", nullable=False), col("customer_id", "integer")],
            "regions": [col("id", "integer", nullable=False)],
        },
        fks=[
            {"from_table": "orders", "from_column": "customer_id", "to_table": "customers", "to_column": "id",
             "from_schema": "public", "to_schema": "public"},
            {"from_table": "customers", "from_column": "region_id", "to_table": "regions", "to_column": "id",
             "from_schema": "public", "to_schema": "public"},
            {"from_table": "orders", "from_column": "customer_id", "to_table": "customers", "to_column": "id",
             "from_schema": "public", "to_schema": "archive"},
        ],
    )
    org, rep = I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path, dry_run=True,
                            tables=["public.customers", "public.orders"])
    rels = [r for a in org.subject_areas for r in a.relationships] + list(org.cross_subject_area_relationships)
    assert [(r.from_table, r.to_table) for r in rels] == [("orders", "customers")]
    assert any("skipped 2 declared foreign key(s) to tables outside the model" in n for n in rep.notes)


def test_exclude_columns_marks_them_rejected(tmp_path):
    # The prune step dropped customers.email — full introspect should mark it excluded.
    org, _ = I.introspect("shop", "postgres", runner=_catalog_runner,
                          artifacts_dir=tmp_path, dry_run=True,
                          exclude_columns=["public.customers.email"])
    assert V.validate(org).ok
    customers = org.subject_areas[0].defined_table("customers")
    assert customers.get_column("email").review_state == "rejected"
    # a column NOT in the exclude list is untouched
    assert customers.get_column("id").review_state != "rejected"


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


def test_areas_listing_is_a_complete_one_call_map(tmp_path):
    # `sm areas` should expose the whole shape of each area — including
    # relationship/entity/metric counts — so a caller never has to cat the YAML
    # tree to discover e.g. where relationships live (area-level, not per-table).
    from semantic_model import runtime as RT
    org, _ = I.introspect("shop", "postgres", runner=_catalog_runner,
                          artifacts_dir=tmp_path, dry_run=True)
    a = RT.list_subject_areas(org)[0]
    assert {"table_count", "entity_count", "metric_count", "relationship_count"} <= set(a)
    assert a["table_count"] == 2 and a["relationship_count"] == 1


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


def test_guid_valued_column_not_choice_field():
    # a low-cardinality FK column (few distinct sys_id GUIDs) must NOT become an enum — this is
    # the choice-field-pollution regression (resolved_by / child_incidents filled with GUIDs).
    sys_ids = ["a" * 32, "b" * 32, "c" * 32]
    vals = (sys_ids * 6)
    assert I._maybe_choice(vals) is None
    # dashed UUIDs too
    uuids = ["12345678-1234-1234-1234-123456789abc", "abcdef00-0000-0000-0000-000000000000"]
    assert I._maybe_choice(uuids * 8) is None
    # a genuine small-int enum is still detected
    assert I._maybe_choice([str(i % 3 + 1) for i in range(30)]) == {"1": "", "2": "", "3": ""}


@pytest.mark.parametrize("name,is_choice_candidate", [
    ("severity", True), ("state", True), ("priority", True),   # real enums
    ("resolved_by", False), ("opened_by", False),              # reference actors (FK, GUID)
    ("reopen_count", False), ("reassignment_count", False),    # counts (measures)
    ("caller_id", False), ("record_seq", False),               # id / sequence
])
def test_choice_candidate_name_exclusions(name, is_choice_candidate):
    excluded = bool(I._NOT_CHOICE_NAME_RE.search(name))
    assert excluded == (not is_choice_candidate)


# ---------------------------------------------------------------------------
# Writing + legacy backup
# ---------------------------------------------------------------------------


def test_introspect_writes_canonical_tree_and_loads_back(tmp_path):
    from semantic_model import loader as L
    org, rep = I.introspect("shop", "postgres", runner=_catalog_runner,
                            artifacts_dir=tmp_path, dry_run=False)
    root = tmp_path / "shop"
    assert (root / "datasource.yaml").exists()
    reloaded = L.load_datasource(root)
    assert V.validate(reloaded).ok
    assert {t.name for sa in reloaded.subject_areas for t in sa.tables_defined} == {"customers", "orders"}


def test_legacy_model_backed_up_on_reonboard(tmp_path):
    # simulate an existing legacy (v1) profile at the root
    root = tmp_path / "shop"
    (root / "PUBLIC").mkdir(parents=True)
    (root / "index.yaml").write_text("profile: shop\n")
    (root / "PUBLIC" / "_schema.yaml").write_text("schema: PUBLIC\n")
    I.introspect("shop", "postgres", runner=_catalog_runner, artifacts_dir=tmp_path, dry_run=False)
    assert (root / ".legacy_backup" / "index.yaml").exists()
    assert (root / ".legacy_backup" / "PUBLIC" / "_schema.yaml").exists()
    assert (root / "datasource.yaml").exists()  # new model written at root


# ---------------------------------------------------------------------------
# build.py helpers
# ---------------------------------------------------------------------------


def test_cluster_by_family_groups_by_the_whole_first_word():
    mapping = build.cluster_by_family(["events", "event_types", "pro_x", "products", "projects",
                                       "promotions", "member_visits", "member_plans"])
    # one name starting with another's word is not a family (#513): pro_x isn't products
    assert len({mapping[n] for n in ("pro_x", "products", "projects", "promotions")}) == 4
    assert mapping["events"] != mapping["event_types"]
    assert mapping["member_visits"] == mapping["member_plans"] == "member"


def test_area_names_keep_the_plural():
    # stripping a trailing "s" made supplie, facilitie, statu, analysi, new (#513)
    names = ["supplies", "facilities", "status", "analysis", "news", "series"]
    assert build.cluster_by_family(names) == {n: n for n in names}


def _t(name: str, description: str = "") -> m.Table:
    return m.Table(name=name, description=description,
                   columns=[m.Column(name="id", type="integer", primary_key=True)])


def _j(frm: str, to: str) -> m.Relationship:
    return m.Relationship(from_table=frm, from_column=f"{to}_id", to_table=to, to_column="id",
                          relationship="many_to_one")


def _proposed(tables, rels):
    areas, _ = build.propose_subject_areas(tables, rels, "c", "demo")
    return {t.name: sa.name for sa in areas for t in sa.tables_defined}


def test_grouping_follows_joins_not_name_prefixes():
    # pro_tasks joins projects' world, products joins orders': a shared prefix doesn't decide it,
    # and joined tables with different names land together
    names = ["orders", "order_lines", "products", "customers", "invoices",
             "projects", "pro_tasks", "timesheets", "staff",
             *(f"misc{i}_log" for i in range(20))]
    rels = [_j("order_lines", "orders"), _j("order_lines", "products"), _j("orders", "customers"),
            _j("invoices", "orders"), _j("pro_tasks", "projects"), _j("timesheets", "pro_tasks"),
            _j("timesheets", "staff")]
    area = _proposed([_t(n) for n in names], rels)
    assert area["orders"] == area["order_lines"] == area["products"] == area["customers"] == area["invoices"]
    assert area["projects"] == area["pro_tasks"] == area["timesheets"] == area["staff"]
    assert area["products"] != area["pro_tasks"]
    assert area["orders"] == "orders"   # named by its most-joined table, in the plural
    assert area["misc0_log"] == "misc0"   # no joins: grouped by its whole first word


def test_one_join_between_two_clusters_does_not_merge_them():
    # connected components would make this one area: every table reaches every other
    shop = ["orders", "order_items", "products", "payments", "returns", "promotions"]
    people = ["staff", "departments", "projects", "pro_tasks", "timesheets", "payroll_runs"]
    rels = [_j("order_items", "orders"), _j("order_items", "products"), _j("payments", "orders"),
            _j("returns", "order_items"), _j("promotions", "products"),
            _j("staff", "departments"), _j("pro_tasks", "projects"), _j("pro_tasks", "staff"),
            _j("timesheets", "pro_tasks"), _j("timesheets", "staff"), _j("payroll_runs", "staff"),
            _j("returns", "staff")]   # the one bridge
    filler = [_t(f"x{i}_log") for i in range(15)]
    area = _proposed([_t(n) for n in shop + people] + filler, rels)
    assert len({area[t] for t in shop}) == 1 and len({area[t] for t in people}) == 1
    assert area["orders"] != area["staff"]
    assert (area["orders"], area["staff"]) == ("orders", "staff")


def test_a_table_with_no_joins_goes_with_tables_sharing_its_first_word():
    names = ["orders", "order_lines", "order_notes", "suppliers", "supplier_ratings", "status",
             *(f"t{i}" for i in range(20))]
    rels = [_j("order_lines", "orders"), *(_j(f"t{i}", "suppliers") for i in range(3))]
    area = _proposed([_t(n) for n in names], rels)
    assert area["order_notes"] == area["order_lines"]       # joinless, same whole first word
    assert area["supplier_ratings"] != area["suppliers"]    # "supplier" is not "suppliers"
    assert area["status"] == "status" and "suppliers" in set(area.values())


def test_an_oversized_join_cluster_is_split_below_the_area_limit():
    hub = [_t("customers")]
    leaves = [_t(f"leaf{i}_events") for i in range(40)]
    pairs = [_t(f"orders{i}") for i in range(5)]
    rels = [*(_j(t.name, "customers") for t in leaves), *(_j(t.name, "customers") for t in pairs),
            *(_j(t.name, "customers") for t in pairs)]   # two joins each: the strongest links
    areas, _ = build.propose_subject_areas(hub + leaves + pairs, rels, "c", "demo")
    assert all(len(sa.tables) <= build.SINGLE_AREA_MAX for sa in areas)
    owner = {t.name: sa.name for sa in areas for t in sa.tables_defined}
    assert all(owner[f"orders{i}"] == owner["customers"] for i in range(5))
    assert len(areas) == 2   # the leaves cut off by the limit are pooled, not one area each
    org = m.Datasource(datasource="demo", storage_connections=[m.StorageConnection(
        name="c", storage_type="PostgreSQL")], subject_areas=areas,
        cross_subject_area_relationships=build.extract_cross_area_relationships(areas, rels))
    assert V.validate(org).ok


def test_two_hubs_beside_full_clusters_do_not_raise():
    # Both hubs end up alone with every neighbour group full; the first is pooled (its group
    # removed) before the second looks at it as a neighbour.
    clusters = [[f"c{k}_t{i}" for i in range(build.SINGLE_AREA_MAX)] for k in range(3)]
    rels = [_j("hub_a", "hub_b")]
    for names in clusters:
        rels += [_j(a, b) for i, a in enumerate(names) for b in names[i + 1:]]
        rels += [_j(n, h) for n in names for h in ("hub_a", "hub_b")]
    tables = [_t("hub_a"), _t("hub_b"), *(_t(n) for names in clusters for n in names)]
    groups = build.group_by_joins(tables, rels)
    assert sum(len(g) for g in groups) == len(tables)
    assert all(len(g) <= build.SINGLE_AREA_MAX for g in groups)


def test_shared_words_break_ties_between_equally_joined_links():
    tables = [_t("a_hub", "warehouse stock"), _t("b_x", "warehouse bins"), _t("c_y", "invoice lines")]
    rels = [_j("b_x", "a_hub"), _j("c_y", "a_hub")]
    groups = build.group_by_joins(tables, rels, max_size=2)
    assert sorted(sorted(t.name for t in g) for g in groups) == [["a_hub", "b_x"], ["c_y"]]


def test_deep_table_column_groups_no_orphans():
    cols = [m.Column(name="ID", type="integer", primary_key=True)] + \
           [m.Column(name=f"AL_{i}", type="decimal") for i in range(20)] + \
           [m.Column(name=f"PL_{i}", type="decimal") for i in range(20)]
    groups = build.maybe_column_groups(cols)
    assert groups  # deep table -> groups derived
    grouped = {c for g in groups.values() for c in g}
    assert grouped == {c.name for c in cols}  # every column covered


def test_column_groups_are_role_aware_and_shrink_misc():
    # A wide table where most columns have a UNIQUE prefix — the old prefix-only grouping
    # dumped all of them into `misc`. Role grouping (FK / choice / flag / audit / measure /
    # date from structural signals) should absorb them, leaving misc tiny.
    cols = [
        m.Column(name="sys_id", type="string", primary_key=True),
        m.Column(name="caller_id", type="string",
                 foreign_key=m.ForeignKey(table="sys_user", column="sys_id")),
        m.Column(name="assignment_group", type="string",
                 foreign_key=m.ForeignKey(table="sys_user_group", column="sys_id")),
        m.Column(name="priority", type="string", choice_field={"1": "Critical", "2": "High"}),
        m.Column(name="state", type="string", choice_field={"1": "New", "6": "Resolved"}),
        m.Column(name="active", type="boolean"),
        m.Column(name="made_sla", type="boolean"),
        m.Column(name="created_at", type="timestamp"),
        m.Column(name="closed_by", type="string"),     # audit by-suffix
        m.Column(name="reassignment_count", type="integer", aggregation="additive"),
        m.Column(name="business_duration", type="decimal", aggregation="additive"),
        m.Column(name="due_date", type="date"),         # date role (not audit)
        m.Column(name="short_description", type="string"),   # unique prefix -> misc
        m.Column(name="urgency_reason", type="string"),      # unique prefix -> misc
    ]
    g = build.derive_column_groups(cols)
    assert g["identity"] == ["sys_id"]
    assert set(g["references"]) == {"caller_id", "assignment_group"}
    assert set(g["codes"]) == {"priority", "state"}
    assert set(g["flags"]) == {"active", "made_sla"}
    assert set(g["audit"]) == {"created_at", "closed_by"}
    assert set(g["measures"]) == {"reassignment_count", "business_duration"}
    assert g["dates"] == ["due_date"]
    # only the two genuinely-singleton, role-less columns land in misc
    assert set(g["misc"]) == {"short_description", "urgency_reason"}
    # still exactly one group per column, no orphans
    grouped = [c for cols_ in g.values() for c in cols_]
    assert sorted(grouped) == sorted(c.name for c in cols)
    assert len(grouped) == len(set(grouped))


def test_references_grouped_from_relationship_not_just_foreign_key():
    # The introspection pipeline records joins as Relationships, never on column.foreign_key,
    # so references must be classifiable from the relationship's FROM columns. caller_id has no
    # foreign_key set, but passing it as a reference column should file it under `references`.
    cols = [m.Column(name="sys_id", type="string", primary_key=True)] + \
           [m.Column(name="caller_id", type="string")] + \
           [m.Column(name=f"f_{i}", type="decimal") for i in range(35)]
    g_no = build.derive_column_groups(cols)
    assert "caller_id" in g_no.get("misc", [])                      # singleton -> misc without ref info
    g_ref = build.derive_column_groups(cols, reference_columns={"caller_id"})
    assert g_ref["references"] == ["caller_id"]                     # known FROM column -> references
    assert "caller_id" not in g_ref.get("misc", [])


def test_column_group_descriptions_role_and_prefix():
    groups = {"references": ["caller_id"], "measures": ["amount"], "discount": ["a", "b"]}
    d = build.column_group_descriptions(groups)
    assert d["references"] == build._ROLE_GROUP_DESCRIPTIONS["references"]
    assert d["measures"] == build._ROLE_GROUP_DESCRIPTIONS["measures"]
    # a prefix-token group (not a known role) gets a generated gloss
    assert "discount" in d["discount"].lower()
    assert set(d) == set(groups)


def test_sniff_date_detects_epoch_yyyymmdd_iso_and_rejects_ids():
    # epoch + yyyymmdd only when the column is time-named AND values fit the shape
    assert I._sniff_date("created_ts", "integer", [1704067200, 1709000000]) == ("epoch_s", "UTC")
    assert I._sniff_date("updated_at", "integer", [1704067200000, 1709000000000]) == ("epoch_ms", "UTC")
    assert I._sniff_date("order_date", "integer", [20240115, 20240116]) == ("yyyymmdd", None)
    assert I._sniff_date("event_at", "string", ["2024-01-15T10:00:00Z"]) == ("iso8601", "offset-aware")
    # a non-time-named integer in epoch range is NOT mistaken for a timestamp
    assert I._sniff_date("user_id", "integer", [1704067200, 1709000000]) == (None, None)
    # native timestamp: no re-encoding; tz only when the sample carries an offset
    assert I._sniff_date("updated_at", "timestamp", ["2024-01-01 10:00:00+05:30"]) == (None, "offset-aware")
    assert I._sniff_date("updated_at", "timestamp", ["2024-01-01 10:00:00"]) == (None, None)


# ---------------------------------------------------------------------------
# Large-table scan hints: recommended_filters seeded from date columns
# ---------------------------------------------------------------------------


def _large_runner(sql):
    """Two large tables: `events` (one clear date column) and `wide_mart` (7 date columns)."""
    s = " ".join(sql.split())
    if "information_schema.schemata" in s:
        return [{"schema_name": "public"}]
    if "information_schema.tables" in s and "table_type" in s:
        return [{"schema_name": "public", "table_name": "events", "table_type": "BASE TABLE"},
                {"schema_name": "public", "table_name": "wide_mart", "table_type": "BASE TABLE"}]
    if "information_schema.columns" in s or "FROM svv_columns" in s:
        if "'events'" in s:
            return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                    {"column_name": "created_at", "data_type": "timestamp", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""},
                    {"column_name": "label", "data_type": "varchar", "is_nullable": "YES", "ordinal_position": "3", "numeric_scale": ""}]
        cols = [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""}]
        for i in range(7):  # 7 date columns → too many to be a clear scan key
            cols.append({"column_name": f"d{i}_date", "data_type": "date", "is_nullable": "YES", "ordinal_position": str(i + 2), "numeric_scale": ""})
        return cols
    if "PRIMARY KEY" in s:
        return [{"column_name": "id"}]
    if "FOREIGN KEY" in s:
        return []
    if "reltuples" in s:
        return [{"estimated_rows": "5000000"}]   # 5M → large
    return []


def test_large_table_recommends_its_date_columns(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_large_runner, artifacts_dir=tmp_path, dry_run=True)
    events = org.subject_areas[0].defined_table("events")
    assert events.performance_hints.estimated_row_count == 5_000_000
    # a clear single date column → the narrow-by-date hint, so the scan warning can name it
    assert events.performance_hints.recommended_filters == ["created_at"]


def test_wide_mart_skips_noisy_date_columns(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_large_runner, artifacts_dir=tmp_path, dry_run=True)
    wide = org.subject_areas[0].defined_table("wide_mart")
    # 7 date columns is too many to be a clear scan key — left empty for the index/partition pass
    assert wide.performance_hints.recommended_filters == []


# ---------------------------------------------------------------------------
# Grain-probe size guard: a giant fact table with no catalog PK must NOT be
# COUNT(DISTINCT)-full-scanned once per id column to guess a grain it doesn't have.
# ---------------------------------------------------------------------------


def _no_pk_runner(table, est_rows, *, seen):
    """Catalog-mode runner for ONE table with no PK/FK; `est_rows` row estimate.
    Records every SQL it sees in `seen` so a test can assert the probe ran or not."""
    cols = [
        {"column_name": "event_id", "data_type": "integer", "is_nullable": "YES", "ordinal_position": "1", "numeric_scale": ""},
        {"column_name": "asset_id", "data_type": "integer", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""},
        {"column_name": "ts", "data_type": "timestamp", "is_nullable": "YES", "ordinal_position": "3", "numeric_scale": ""},
    ]

    def run(sql):
        s = " ".join(sql.split())
        seen.append(s)
        if "information_schema.schemata" in s:
            return [{"schema_name": "public"}]
        if "information_schema.tables" in s and "table_type" in s:
            return [{"schema_name": "public", "table_name": table, "table_type": "BASE TABLE"}]
        if "information_schema.columns" in s or "FROM svv_columns" in s:
            return cols
        if "PRIMARY KEY" in s:
            return []  # no catalog PK → without the guard this would fall into the scan probe
        if "FOREIGN KEY" in s:
            return []
        if "reltuples" in s:
            return [{"estimated_rows": str(est_rows)}]
        if "COUNT(DISTINCT" in s:           # the expensive uniqueness probe — unique id
            return [{"total": "1000", "distinct_count": "1000", "null_count": "0"}]
        return []

    return run


def test_giant_table_skips_grain_probe(tmp_path):
    seen: list[str] = []
    run = _no_pk_runner("sm_asset_mgmt", 40_000_000, seen=seen)  # 40M rows, no PK
    org, rep = I.introspect("ops", "postgres", runner=run, artifacts_dir=tmp_path, dry_run=True)
    fact = org.subject_areas[0].defined_table("sm_asset_mgmt")
    assert fact.grain == []                                   # left empty, not guessed
    assert rep.mode_per_capability["grain"] == "probe_skipped_large"
    assert not any("COUNT(DISTINCT" in s for s in seen)       # the full scans never ran
    assert any("skipped grain probe" in n for n in rep.notes)  # and the skip is surfaced
    # the catalog row estimate is still recorded (it's a cheap stat, fetched once)
    assert fact.performance_hints.estimated_row_count == 40_000_000


def test_small_table_without_pk_still_probes_grain(tmp_path):
    seen: list[str] = []
    run = _no_pk_runner("dim_small", 1000, seen=seen)  # well under the guard
    org, rep = I.introspect("ops", "postgres", runner=run, artifacts_dir=tmp_path, dry_run=True)
    fact = org.subject_areas[0].defined_table("dim_small")
    assert fact.grain == ["event_id"]                        # probe found the unique id
    assert rep.mode_per_capability["grain"] == "probe"
    assert any("COUNT(DISTINCT" in s for s in seen)          # the guard did NOT over-trigger


# ---------------------------------------------------------------------------
# Supabase: drop system schemas (auth/storage/vault/…), keep the app schemas
# ---------------------------------------------------------------------------


def test_supabase_system_schemas_are_dropped():
    rep = I.IntrospectReport(profile="p", db_type="postgres", out_dir=None, dry_run=True)
    schemas = ["auth", "storage", "vault", "realtime", "extensions", "public", "analytics"]
    kept = I._filter_supabase_system_schemas(schemas, rep)
    assert kept == ["public", "analytics"]                       # only the user's app schemas
    assert any("Supabase detected" in n for n in rep.notes)      # and it's surfaced, not silent


def test_plain_postgres_auth_schema_is_not_filtered():
    # A plain Postgres DB with its OWN `auth` schema (no other Supabase signature) is left alone —
    # the filter only triggers on the full Supabase signature, so it never eats real app schemas.
    rep = I.IntrospectReport(profile="p", db_type="postgres", out_dir=None, dry_run=True)
    assert I._filter_supabase_system_schemas(["public", "auth"], rep) == ["public", "auth"]
    assert rep.notes == []


# ---------------------------------------------------------------------------
# Money-column detection (word boundaries — `count` must not match `discount`)
# ---------------------------------------------------------------------------


def test_detect_money_column_handles_word_boundaries():
    money = ["amount", "price", "total_revenue", "discount_amount", "member_discount",
             "tax_amount", "account_balance", "salary", "refund_amt", "subtotal"]
    not_money = ["order_count", "discount_rate", "total_count", "num_payments",
                 "credit_score", "customer_id", "created_at", "status", "quantity"]
    for c in money:
        assert build.detect_money_column(c), f"{c} should be money"
    for c in not_money:
        assert not build.detect_money_column(c), f"{c} should NOT be money"


@pytest.mark.parametrize("name", ["duration_minutes", "total_minutes", "total_hours",
                                  "credit_hours", "points_balance", "total_weight",
                                  "totalMinutes", "reading_units", "balance_pts",
                                  "total_seconds", "duration_secs"])
def test_non_money_unit_words_are_not_money(name):
    assert not build.detect_money_column(name)


@pytest.mark.parametrize("name", ["gross_usd", "value_usd", "balance_usd", "fx_dollar",
                                  "amount_total", "total_amount", "revenueUSDMillions",
                                  "revenue_per_day", "cost_per_kg", "fare_try",
                                  "second_payment_amount", "second_installment_fee",
                                  "hour_cost", "cost_per_hour"])
def test_currency_named_and_money_named_columns_are_money(name):
    assert build.detect_money_column(name)


@pytest.mark.parametrize("name,unit", [
    ("revenue_usd_millions", "USD_millions"), ("revenueUSDMillions", "USD_millions"),
    ("gross_usd", "USD"), ("fx_dollar", "USD"), ("cost_usd_per_day", "USD/day"),
    ("monthly_fee_inr_thousands", "INR_thousands/month"), ("total_amount", None),
    ("try_count", None),   # a leading ISO-looking word is not a currency
])
def test_suggest_unit_reads_currency_scale_and_period_from_the_name(name, unit):
    assert build.suggest_unit(name) == unit


# ---------------------------------------------------------------------------
# Cross-schema relationships (Case 1): schema is stamped on every edge, declared
# cross-schema FKs are surfaced for review (not auto-approved), and the inferred
# probe binds to the same-schema target instead of a same-named decoy in another schema.
# ---------------------------------------------------------------------------


def _xschema_fk_runner(sql):
    """Two schemas; a FK declared in `sales` that REFERENCES `billing` (cross-schema)."""
    s = " ".join(sql.split())
    if "information_schema.schemata" in s:
        return [{"schema_name": "sales"}, {"schema_name": "billing"}]
    if "information_schema.tables" in s and "table_type" in s:
        if "'sales'" in s:
            return [{"schema_name": "sales", "table_name": "invoices", "table_type": "BASE TABLE"}]
        return [{"schema_name": "billing", "table_name": "customers", "table_type": "BASE TABLE"}]
    if "information_schema.columns" in s or "FROM svv_columns" in s:
        if "'invoices'" in s:
            return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                    {"column_name": "customer_id", "data_type": "integer", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""}]
        return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                {"column_name": "email", "data_type": "varchar", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""}]
    if "PRIMARY KEY" in s:
        return [{"column_name": "id"}]
    if "FOREIGN KEY" in s:
        if "'sales'" in s:
            return [{"from_table": "invoices", "from_column": "customer_id", "from_schema": "sales",
                     "to_table": "customers", "to_column": "id", "to_schema": "billing"}]
        return []
    if "reltuples" in s:
        return [{"estimated_rows": "1000"}]
    return []


def test_cross_schema_fk_is_flagged_and_not_auto_approved(tmp_path):
    org, rep = I.introspect("shop", "postgres", runner=_xschema_fk_runner,
                            artifacts_dir=tmp_path, dry_run=True)
    # two schemas -> two areas; the join spans them, so it's a cross-AREA relationship.
    assert {sa.name for sa in org.subject_areas} == {"sales", "billing"}
    assert not [r for sa in org.subject_areas for r in sa.relationships]  # none intra-area
    cross = org.cross_subject_area_relationships
    assert len(cross) == 1, cross
    r = cross[0]
    assert (r.from_table, r.to_table) == ("invoices", "customers")
    assert r.from_schema == "sales" and r.to_schema == "billing" and r.cross_schema is True
    assert (r.from_subject_area, r.to_subject_area) == ("sales", "billing")
    # enforced Postgres FK, but it spans schemas -> surfaced for review, NOT auto-signed-off
    assert r.review_state == "unreviewed" and r.signed_off_by is None
    assert any("cross-schema" in n for n in rep.notes), rep.notes


def _collision_probe_runner(sql):
    """`customers` exists in BOTH s1 and s2. `s1.orders.customer_id` must bind to
    s1.customers (same schema), not the s2 decoy. Empty FK catalog -> probe path."""
    s = " ".join(sql.split())
    if "information_schema.schemata" in s:
        return [{"schema_name": "s1"}, {"schema_name": "s2"}]
    if "information_schema.tables" in s and "table_type" in s:
        if "'s1'" in s:
            return [{"schema_name": "s1", "table_name": "orders", "table_type": "BASE TABLE"},
                    {"schema_name": "s1", "table_name": "customers", "table_type": "BASE TABLE"}]
        return [{"schema_name": "s2", "table_name": "customers", "table_type": "BASE TABLE"}]
    if "information_schema.columns" in s or "FROM svv_columns" in s:
        if "'orders'" in s:
            return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                    {"column_name": "customer_id", "data_type": "integer", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""}]
        return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                {"column_name": "email", "data_type": "varchar", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""}]
    if "PRIMARY KEY" in s:
        return [{"column_name": "id"}]
    if "FOREIGN KEY" in s:
        return []                      # no catalog FKs -> force the probe
    if "matched" in s:                 # _overlaps EXISTS probe -> overlap confirmed
        return [{"matched": "2"}]
    if "reltuples" in s:
        return [{"estimated_rows": "1000"}]
    return []


def test_probe_binds_same_schema_target_on_name_collision(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_collision_probe_runner,
                          artifacts_dir=tmp_path, dry_run=True)
    rels = [r for sa in org.subject_areas for r in sa.relationships]
    match = [r for r in rels if r.from_table == "orders" and r.to_table == "customers"]
    assert len(match) == 1, rels
    r = match[0]
    # the fix: resolve the target schema-aware (same schema first), not the last bare-name write
    assert r.from_schema == "s1" and r.to_schema == "s1"
    assert r.cross_schema is False


def test_relationship_cross_schema_property():
    same = m.Relationship(from_table="a", to_table="b", from_column="b_id", to_column="id",
                          from_schema="x", to_schema="x", relationship="many_to_one")
    assert same.cross_schema is False
    diff = m.Relationship(from_table="a", to_table="b", from_column="b_id", to_column="id",
                          from_schema="x", to_schema="y", relationship="many_to_one")
    assert diff.cross_schema is True
    # schema-less (SQLite / legacy) -> never flagged
    none = m.Relationship(from_table="a", to_table="b", from_column="b_id", to_column="id",
                          relationship="many_to_one")
    assert none.cross_schema is False


def _collision_schemas_runner(sql):
    """billing + crm both contain a `products` table — the bare-name collision that used to
    drop billing.products on write. Plus billing.invoices.product_id (probe join)."""
    s = " ".join(sql.split())
    if "information_schema.schemata" in s:
        return [{"schema_name": "billing"}, {"schema_name": "crm"}]
    if "information_schema.tables" in s and "table_type" in s:
        if "'billing'" in s:
            return [{"schema_name": "billing", "table_name": "products", "table_type": "BASE TABLE"},
                    {"schema_name": "billing", "table_name": "invoices", "table_type": "BASE TABLE"}]
        return [{"schema_name": "crm", "table_name": "products", "table_type": "BASE TABLE"},
                {"schema_name": "crm", "table_name": "accounts", "table_type": "BASE TABLE"}]
    if "information_schema.columns" in s or "FROM svv_columns" in s:
        if "'invoices'" in s:
            return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                    {"column_name": "product_id", "data_type": "integer", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""}]
        return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                {"column_name": "name", "data_type": "varchar", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""}]
    if "PRIMARY KEY" in s:
        return [{"column_name": "id"}]
    if "FOREIGN KEY" in s:
        return []
    if "matched" in s:
        return [{"matched": "2"}]
    if "reltuples" in s:
        return [{"estimated_rows": "100"}]
    return []


def test_same_named_tables_in_two_schemas_both_survive(tmp_path):
    """Regression: `billing.products` and `crm.products` must BOTH be modeled (the old engine
    keyed tables by bare name and dropped one on write). One area per schema keeps them apart."""
    org, rep = I.introspect("shop", "postgres", runner=_collision_schemas_runner,
                            artifacts_dir=tmp_path, dry_run=False)
    # one area per schema, not per-table fragmentation
    assert {sa.name for sa in org.subject_areas} == {"billing", "crm"}
    # all 4 tables survive — neither products dropped
    tabs = {(t.schema_name, t.name) for sa in org.subject_areas for t in sa.tables_defined}
    assert tabs == {("billing", "products"), ("billing", "invoices"),
                    ("crm", "products"), ("crm", "accounts")}
    # both products.yaml files exist on disk (the collision used to overwrite one)
    root = tmp_path / "shop"
    assert (root / "subject_areas" / "billing" / "tables" / "products.yaml").exists()
    assert (root / "subject_areas" / "crm" / "tables" / "products.yaml").exists()
    # reload from disk -> still 4 tables (write+read round-trips without loss)
    from semantic_model import loader as L
    reloaded = L.load_datasource(root)
    assert sum(len(sa.tables_defined) for sa in reloaded.subject_areas) == 4
    # the probed join binds within billing (same-schema), not across to crm.products
    bil = next(sa for sa in reloaded.subject_areas if sa.name == "billing")
    inv_join = [r for r in bil.relationships if r.from_table == "invoices"]
    assert inv_join and inv_join[0].to_table == "products" and inv_join[0].cross_schema is False


def _redshift_fk_runner(overlap_matches: bool):
    """A single-schema Redshift catalog: orders.customer_id is a DECLARED (but unenforced)
    FK to customers.id. Redshift's catalog records the FK; the data may or may not honour it,
    which is what the value-overlap probe decides."""
    def run(sql):
        s = " ".join(sql.split())
        if "FROM pg_namespace" in s:  # Redshift's schema list (#558)
            return [{"schema_name": "public"}]
        if "information_schema.tables" in s and "table_type" in s:
            return [{"schema_name": "public", "table_name": "orders", "table_type": "BASE TABLE"},
                    {"schema_name": "public", "table_name": "customers", "table_type": "BASE TABLE"}]
        if "information_schema.columns" in s or "FROM svv_columns" in s:
            if "'orders'" in s:
                return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                        {"column_name": "customer_id", "data_type": "integer", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""}]
            return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                    {"column_name": "email", "data_type": "varchar", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""}]
        if "PRIMARY KEY" in s:
            return [{"column_name": "id"}]
        if "FOREIGN KEY" in s:
            return [{"from_table": "orders", "from_column": "customer_id", "from_schema": "public",
                     "to_table": "customers", "to_column": "id", "to_schema": "public"}]
        if "matched" in s:                       # _overlaps EXISTS probe
            return [{"matched": "5"}] if overlap_matches else [{"matched": "0"}]
        if "reltuples" in s:
            return [{"estimated_rows": "1000"}]
        return []
    return run


def test_unenforced_fk_confirmed_by_overlap_auto_approves(tmp_path):
    """Redshift declares but does not enforce FKs. A declared FK whose child values actually
    live in the parent key is sound structure -> confirmed + auto-approved (kept OUT of the
    review queue), exactly like an enforced Postgres FK. This is what stops inheritance-heavy
    Redshift schemas from flooding the trust queue with already-valid joins."""
    org, _ = I.introspect("shop", "redshift", runner=_redshift_fk_runner(True),
                          artifacts_dir=tmp_path, dry_run=True)
    rels = [r for sa in org.subject_areas for r in sa.relationships]
    match = [r for r in rels if r.from_table == "orders" and r.to_table == "customers"]
    assert len(match) == 1, rels
    r = match[0]
    assert r.confidence == "confirmed"
    assert r.review_state == "approved"
    assert r.signed_off_by == "agami_introspect" and r.signed_off_role == "system"


def test_unenforced_fk_without_overlap_stays_unreviewed(tmp_path):
    """The same declared FK, but the data does NOT back it (no value overlap) -> we can't
    confirm it, so it stays inferred/unreviewed for a human glance rather than being asserted."""
    org, _ = I.introspect("shop", "redshift", runner=_redshift_fk_runner(False),
                          artifacts_dir=tmp_path, dry_run=True)
    rels = [r for sa in org.subject_areas for r in sa.relationships]
    match = [r for r in rels if r.from_table == "orders" and r.to_table == "customers"]
    assert len(match) == 1, rels
    r = match[0]
    assert r.confidence == "inferred"
    assert r.review_state == "unreviewed" and r.signed_off_by is None


def test_unenforced_fk_overlap_probing_is_capped(tmp_path, monkeypatch):
    """A schema that declares MANY FKs on an unenforced dialect must not fire one sequential
    overlap COUNT per FK without bound. Past FK_OVERLAP_PROBE_CAP we stop probing and leave the
    rest unreviewed — the run stays bounded on inheritance-heavy Redshift/ServiceNow schemas."""
    monkeypatch.setattr(I, "FK_OVERLAP_PROBE_CAP", 3)
    N = 8
    probes = {"n": 0}

    def runner(sql):
        s = " ".join(sql.split())
        if "FROM pg_namespace" in s:  # Redshift's schema list (#558)
            return [{"schema_name": "public"}]
        if "information_schema.tables" in s and "table_type" in s:
            rows = [{"schema_name": "public", "table_name": "parent", "table_type": "BASE TABLE"}]
            rows += [{"schema_name": "public", "table_name": f"child{i}", "table_type": "BASE TABLE"} for i in range(N)]
            return rows
        if "information_schema.columns" in s or "FROM svv_columns" in s:
            if "'parent'" in s:
                return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""}]
            return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                    {"column_name": "parent_id", "data_type": "integer", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""}]
        if "PRIMARY KEY" in s:
            return [{"column_name": "id"}]
        if "FOREIGN KEY" in s:
            return [{"from_table": f"child{i}", "from_column": "parent_id", "from_schema": "public",
                     "to_table": "parent", "to_column": "id", "to_schema": "public"} for i in range(N)]
        if "matched" in s:
            probes["n"] += 1
            return [{"matched": "5"}]
        if "reltuples" in s:
            return [{"estimated_rows": "1000"}]
        return []

    org, rep = I.introspect("shop", "redshift", runner=runner, artifacts_dir=tmp_path, dry_run=True)
    # never probed more than the cap, even though there are N=8 declared FKs
    assert probes["n"] <= 3
    assert any("capped" in n for n in rep.notes), rep.notes
    # all FKs still modeled (the uncapped ones simply stay unreviewed rather than vanishing)
    rels = [r for sa in org.subject_areas for r in sa.relationships]
    assert len([r for r in rels if r.to_table == "parent"]) == N


def test_introspect_writes_progress_log(tmp_path):
    """A long introspection must emit a flushed heartbeat so a tailing skill doesn't read 'stuck'."""
    pp = tmp_path / "progress.log"
    I.introspect("shop", "postgres", runner=_catalog_runner,
                 artifacts_dir=tmp_path, dry_run=True, progress_path=pp)
    lines = pp.read_text().splitlines()
    assert any("discovered" in ln for ln in lines)
    assert any(ln.startswith("columns+grain 1/") for ln in lines)
    assert lines[-1].startswith("done:")


def test_normalize_table_list_splits_zsh_blob():
    from semantic_model import cli
    assert cli._normalize_table_list(["a", "b"]) == ["a", "b"]          # clean list untouched
    assert cli._normalize_table_list(["incident orders customers"]) == ["incident", "orders", "customers"]
    assert cli._normalize_table_list(["a,b, c"]) == ["a", "b", "c"]     # comma/space mix
    assert cli._normalize_table_list(None) is None


def test_introspect_fails_fast_on_bogus_allowlist(tmp_path):
    """A bogus allowlist entry (mis-joined names) describes nothing — introspect must raise, not
    persist a garbage zero-column table."""
    with pytest.raises(RuntimeError):
        I.introspect("shop", "postgres", runner=lambda sql: [],
                     artifacts_dir=tmp_path, tables=["public.nonexistent_blob"], dry_run=True)


def _append_runner(sql):
    s = " ".join(sql.split())
    if "information_schema.columns" in s or "FROM svv_columns" in s:
        if "'orders'" in s:
            return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                    {"column_name": "total", "data_type": "numeric", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": "2"}]
        if "'customers'" in s:
            return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                    {"column_name": "email", "data_type": "varchar", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""}]
        if "'order_items'" in s:
            return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                    {"column_name": "order_id", "data_type": "integer", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""}]
        return []
    if "PRIMARY KEY" in s:
        return [{"column_name": "id"}]
    if "FOREIGN KEY" in s:
        return [{"from_table": "order_items", "from_column": "order_id", "to_table": "orders",
                 "to_column": "id", "from_schema": "public", "to_schema": "public"}]
    if "reltuples" in s:
        return [{"estimated_rows": "100"}]
    if "matched" in s:
        return [{"matched": "5"}]
    return []


def test_introspect_append_merges_batches(tmp_path):
    from semantic_model import loader as L
    # batch 1 → orders, customers
    I.introspect("shop", "postgres", runner=_append_runner, artifacts_dir=tmp_path,
                 tables=["public.orders", "public.customers"])
    # batch 2 (append) → order_items, which FK-references orders (a CROSS-batch edge)
    I.introspect("shop", "postgres", runner=_append_runner, artifacts_dir=tmp_path,
                 tables=["public.order_items"], append=True)

    org = L.load_datasource(tmp_path / "shop")
    tnames = {t.name for sa in org.subject_areas for t in sa.tables_defined}
    assert tnames == {"orders", "customers", "order_items"}   # union — nothing lost, no re-query
    allrels = [r for sa in org.subject_areas for r in sa.relationships] + list(org.cross_subject_area_relationships)
    oi = [r for r in allrels if r.from_table == "order_items" and r.to_table == "orders"]
    assert len(oi) == 1   # the cross-batch FK was built once (not lost, not duplicated)


def test_introspect_append_relisting_table_no_duplicate(tmp_path):
    """Re-listing a batch-1 table in a later --append batch must not duplicate it or its grain."""
    from semantic_model import loader as L
    I.introspect("shop", "postgres", runner=_append_runner, artifacts_dir=tmp_path,
                 tables=["public.orders", "public.customers"])
    # batch 2 re-lists orders (already built) + adds order_items
    I.introspect("shop", "postgres", runner=_append_runner, artifacts_dir=tmp_path,
                 tables=["public.orders", "public.order_items"], append=True)
    org = L.load_datasource(tmp_path / "shop")
    names = [t.name for sa in org.subject_areas for t in sa.tables_defined]
    assert sorted(names) == ["customers", "order_items", "orders"]   # each table exactly once


def test_a_failed_column_read_is_retried_once():
    # A dropped connection on one catalog read must not cost the table its columns.
    from semantic_model import dialects as D

    calls = {"n": 0}

    def flaky(sql: str):
        if "svv_columns" in sql:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("connection reset")
            return [{"column_name": "id", "data_type": "integer", "numeric_scale": None}]
        raise AssertionError("fell through to the probe")

    cols, mode = I._columns(D.get_dialect("redshift"), flaky, "s", "t")
    assert mode == "catalog" and [c.name for c in cols] == ["id"] and calls["n"] == 2


def test_an_empty_column_read_is_not_retried():
    # [] means the catalog answered and listed nothing; only a FAILED read (None) is retried.
    from semantic_model import dialects as D

    seen: list[str] = []

    def empty(sql: str):
        seen.append(sql)
        return []

    I._columns(D.get_dialect("redshift"), empty, "s", "t")
    assert sum("svv_columns" in s for s in seen) == 1


def test_one_value_seen_in_a_sample_is_not_a_value_list():
    # a sample of the first rows can show a single value of a column that holds several
    assert I._maybe_choice(["N"] * 50) is None
    assert I._maybe_choice(["N"] * 30 + ["Y"] * 20) == {"N": "", "Y": ""}


def test_warehouse_keys_are_not_money():
    from semantic_model import build as B_

    for key in (
        "invoice_key",
        "payment_key",
        "payment_method_type_key",
        "invoice_sk",
        "credit_fk",
        "deposit_account_key",
        "invoice_nbr",
    ):
        assert not B_.detect_money_column(key), key
    assert B_.detect_money_column("invoice_amount") and B_.detect_money_column("credit_amt")


def _status_col():
    return m.Column(name="status", type="string", description="")


def _rows(n):
    return [{"status": "Open" if i % 3 else "Closed"} for i in range(n)]


@pytest.mark.parametrize(
    "sample_rows, est_rows, expect_list",
    [
        (120, None, True),  # the sample is the whole table
        (I.SAMPLE_ROWS, 5_000, True),  # first rows of a small table, per the catalog
        (I.SAMPLE_ROWS, None, False),  # a view or no estimate: the first rows could be any corner
        (I.SAMPLE_ROWS, 15_000_000, False),  # a big table: an incomplete list would mislead
    ],
)
def test_a_value_list_is_built_only_from_a_representative_sample(
    sample_rows, est_rows, expect_list
):
    from semantic_model import dialects as D

    col = _status_col()
    I._enrich_from_sample(
        D.get_dialect("postgres"),
        lambda sql: _rows(sample_rows),
        "s",
        "t",
        [col],
        est_rows=est_rows,
    )
    assert (col.choice_field == {"Closed": "", "Open": ""}) is expect_list


def test_an_unknown_row_estimate_is_not_a_small_table():
    # a view or a never-analyzed table reports -1 rows; a full sample of it covers nothing
    full = [{}] * I.SAMPLE_ROWS
    assert not I._sample_covers_values(full, -1)
    assert I._sample_covers_values(full, I.SAMPLE_ROWS)


# ---------------------------------------------------------------------------
# Row counts for views and never-analysed tables (#451): the catalog keeps no usable estimate, so
# introspection counts instead, and a large view gets `large_tables` + `recommended_filters`.
# ---------------------------------------------------------------------------


def _estimate_runner(table, *, estimate, count, seen):
    """One table with a date column. `estimate` is what the catalog stat returns (None = no row, as
    `svv_table_info` gives for a view); `count` is the COUNT(*) answer (None = the count fails)."""
    cols = [
        {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
        {"column_name": "submitted_on", "data_type": "date", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""},
    ]

    def run(sql):
        s = " ".join(sql.split())
        seen.append(s)
        if "information_schema.schemata" in s or "pg_namespace" in s and "nspname AS schema_name" in s:
            return [{"schema_name": "public"}]
        if ("information_schema.tables" in s or "svv_tables" in s) and "table_type" in s:
            return [{"schema_name": "public", "table_name": table, "table_type": "VIEW"}]
        if "information_schema.columns" in s or "svv_columns" in s:
            return cols
        if "PRIMARY KEY" in s:
            return [{"column_name": "id"}]
        if "FOREIGN KEY" in s:
            return []
        if "reltuples" in s or "svv_table_info" in s:
            return [] if estimate is None else [{"estimated_rows": str(estimate)}]
        if "COUNT(*) AS estimated_rows" in s:
            if count is None:
                raise RuntimeError("statement timed out")
            return [{"estimated_rows": str(count)}]
        return []

    return run


@pytest.mark.parametrize("dialect, estimate", [("redshift", None), ("postgres", -1), ("postgres", 0)])
def test_a_table_the_catalog_cannot_size_is_counted(tmp_path, dialect, estimate):
    seen: list[str] = []
    run = _estimate_runner("orders_v", estimate=estimate, count=7_000_000, seen=seen)
    org, _ = I.introspect("fin", dialect, runner=run, artifacts_dir=tmp_path, dry_run=True)
    view = org.subject_areas[0].defined_table("orders_v")
    assert view.performance_hints.estimated_row_count == 7_000_000
    # Large, so it carries the filter the scan-risk check names.
    assert view.performance_hints.recommended_filters == ["submitted_on"]
    assert any("COUNT(*) AS estimated_rows" in s for s in seen)


def test_a_table_with_a_catalog_estimate_is_not_counted(tmp_path):
    seen: list[str] = []
    run = _estimate_runner("orders", estimate=5_000_000, count=1, seen=seen)
    org, _ = I.introspect("fin", "postgres", runner=run, artifacts_dir=tmp_path, dry_run=True)
    assert org.subject_areas[0].defined_table("orders").performance_hints.estimated_row_count == 5_000_000
    assert not any("COUNT(*) AS estimated_rows" in s for s in seen)


@pytest.mark.parametrize("dialect, estimate", [("redshift", None), ("postgres", -1), ("postgres", 0)])
def test_a_count_that_fails_leaves_the_size_unknown(tmp_path, dialect, estimate):
    seen: list[str] = []
    run = _estimate_runner("orders_v", estimate=estimate, count=None, seen=seen)
    org, _ = I.introspect("fin", dialect, runner=run, artifacts_dir=tmp_path, dry_run=True)
    hints = org.subject_areas[0].defined_table("orders_v").performance_hints
    # Unknown, not -1: a never-analysed Postgres table no longer stores the catalog's -1.
    assert hints is None or hints.estimated_row_count is None
    assert any("COUNT(*) AS estimated_rows" in s for s in seen)  # the fallback was tried


def test_a_spent_count_budget_leaves_the_rest_unknown(tmp_path, monkeypatch):
    """Counts run serially, so one run-level budget bounds them all; past it a table stays unsized
    and the report says how many were skipped."""
    monkeypatch.setattr(I, "_COUNT_BUDGET_S", 0.0)
    seen: list[str] = []
    run = _estimate_runner("orders_v", estimate=None, count=7_000_000, seen=seen)
    org, report = I.introspect("fin", "redshift", runner=run, artifacts_dir=tmp_path, dry_run=True)
    hints = org.subject_areas[0].defined_table("orders_v").performance_hints
    assert hints is None or hints.estimated_row_count is None
    assert not any("COUNT(*) AS estimated_rows" in s for s in seen)
    assert any("1 table(s) left unsized" in n for n in report.notes)


def test_a_count_spends_the_budget():
    from semantic_model import dialects as D

    budget = I._CountBudget(seconds_left=5.0)
    assert I._estimate_rows(D.get_dialect("redshift"), lambda sql: [], "s", "v", budget) is None
    assert budget.seconds_left < 5.0 and budget.skipped == 0


@pytest.mark.parametrize("rows", [0, 42])
def test_an_engine_whose_estimate_is_the_count_runs_it_once(rows):
    """SQLite has no catalog statistics, so its estimate query IS the exact count: a 0 (or any)
    answer must not run the same full scan a second time."""
    from semantic_model import dialects as D

    seen: list[str] = []

    def run(sql):
        seen.append(sql)
        return [{"estimated_rows": str(rows)}]

    assert I._estimate_rows(D.get_dialect("sqlite"), run, None, "t") == rows
    assert len(seen) == 1


@pytest.mark.parametrize("dialect", ["mysql", "bigquery"])
def test_engines_without_a_native_bound_are_not_counted(dialect):
    """Until #459 gives introspection reads BigQuery's byte cap and MySQL's server-side time limit,
    those engines never get the exact-count fallback: a view stays unsized, as before."""
    from semantic_model import dialects as D

    assert D.get_dialect(dialect).sql_row_count("s", "t") is None


# ---------------------------------------------------------------------------
# ISO dates kept as text under a name that says nothing about dates (#518)
# ---------------------------------------------------------------------------


def _iso_rows(n=25, **extra):
    return [{"id": i, "period": f"2024-01-{i % 28 + 1:02d}", **{k: v(i) for k, v in extra.items()}}
            for i in range(n)]


def test_sniff_iso_text_needs_enough_values_that_all_are_real_dates():
    dates = [f"2024-02-{d:02d}" for d in range(1, 26)]
    assert I._sniff_iso_text(dates) == ("iso8601", None)
    assert I._sniff_iso_text([f"{d} 10:00:00+05:30" for d in dates]) == ("iso8601", "offset-aware")
    assert I._sniff_iso_text([f"{d}T10:00:00.123Z" for d in dates]) == ("iso8601", "offset-aware")
    assert I._sniff_iso_text(dates[:19]) == (None, None)  # too few to claim without a date name
    assert I._sniff_iso_text(dates + ["2024-13-45"]) == (None, None)  # the shape, not a date
    assert I._sniff_iso_text(dates + ["2024-01-01 25:00"]) == (None, None)
    assert I._sniff_iso_text(dates + ["2024-01-01 shipped late"]) == (None, None)
    assert I._sniff_iso_text([f"2024.1.{d}" for d in range(1, 26)]) == (None, None)  # versions
    assert I._sniff_iso_text(dates + [None, ""]) == ("iso8601", None)  # nulls don't count against


def test_strict_iso_range_checks_the_utc_offset():
    assert I._is_strict_iso("2024-01-01 10:00:00+05:30")
    assert I._is_strict_iso("2024-01-01T10:00:00-2359")
    assert not I._is_strict_iso("2024-01-01 10:00:00+25:00")  # no such offset hour
    assert not I._is_strict_iso("2024-01-01 10:00:00+05:99")  # no such offset minute
    dates = [f"2024-01-{d:02d}" for d in range(1, 26)]
    assert I._sniff_iso_text(dates + ["2024-01-01 10:00+25:00"]) == (None, None)


def test_enrich_from_sample_dates_an_iso_text_column_with_a_plain_name():
    from semantic_model import dialects as D

    sample = _iso_rows(release=lambda i: f"2024.1.{i}", note=lambda i: f"2024-01-01 note {i}")
    cols = [m.Column(name="id", type="integer", primary_key=True),
            m.Column(name="period", type="string"), m.Column(name="release", type="string"),
            m.Column(name="note", type="string")]
    I._enrich_from_sample(D.get_dialect("sqlite"), lambda sql: sample, None, "t", cols)
    by = {c.name: c for c in cols}
    assert by["period"].date_format == "iso8601"
    assert by["period"].choice_field is None  # a column of dates is not a list of codes
    assert by["release"].date_format is None and by["note"].date_format is None


def test_enrich_from_sample_samples_a_table_whose_only_text_column_is_a_key():
    from semantic_model import dialects as D

    # No date-named or code-like column here, which used to skip the sample altogether.
    sample = [{"period_id": f"2024-03-{d:02d}"} for d in range(1, 26)]
    calls = []
    cols = [m.Column(name="period_id", type="string")]
    I._enrich_from_sample(D.get_dialect("sqlite"), lambda sql: calls.append(sql) or sample,
                          None, "t", cols)
    assert len(calls) == 1 and cols[0].date_format == "iso8601"
