"""Unit tests for semantic_model/curate.py — review queue, model tree, and the
apply (exclude/include/approve/reject) write path with validation gating."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# case/whitespace-insensitive star projection — mirrors runtime.check_no_select_star (AST-based),
# so the test isn't fooled by `SELECT  *` / `select *` the way a plain substring check would be.
_STAR_PROJECTION = re.compile(r"(?i)\bselect\s*\*")

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate, loader  # noqa: E402


def _write_model(root: Path, *, git: bool = True) -> None:
    """A tiny two-table model with a metric + a relationship, written to disk."""
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "metrics").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "shop", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/sales"],
    }))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sales",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"},
                   {"storage_connection": "c", "schema": "public", "table": "customers"}],
    }))
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "orders",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "customer_id", "type": "integer"},
                    {"name": "ssn", "type": "string", "sensitive": True}],
    }))
    (root / "subject_areas" / "sales" / "tables" / "customers.yaml").write_text(yaml.safe_dump({
        "name": "customers", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "customers",
        "columns": [{"name": "id", "type": "integer", "primary_key": True}],
    }))
    (root / "subject_areas" / "sales" / "metrics" / "order_count.yaml").write_text(yaml.safe_dump({
        "name": "order_count", "calculation": "count of orders",
        "bindings": {"PostgreSQL": "COUNT(*)"}, "source_tables": ["orders"],
        "confidence": "proposed", "review_state": "unreviewed",
    }))
    (root / "subject_areas" / "sales" / "relationships.yaml").write_text(yaml.safe_dump({
        "relationships": [{"from_table": "orders", "from_column": "customer_id",
                           "to_table": "customers", "to_column": "id",
                           "relationship": "many_to_one", "confidence": "inferred",
                           "review_state": "unreviewed"}],
    }))
    if git:
        subprocess.run(["git", "-C", str(root), "init", "-q"])
        subprocess.run(["git", "-C", str(root), "add", "-A"])
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "-m", "init"])


def test_review_queue_partitions_rule1_rule2(tmp_path):
    _write_model(tmp_path, git=False)
    org = loader.load_datasource(tmp_path)
    q = curate.review_queue(org)
    assert q["counts"]["rule_1"] == 1  # the proposed metric
    assert q["counts"]["rule_2"] == 1  # the inferred relationship
    assert q["rule_1"][0]["kind"] == "metric"
    assert q["rule_2"][0]["kind"] == "relationship"


def test_model_tree_shows_columns_and_state(tmp_path):
    _write_model(tmp_path, git=False)
    org = loader.load_datasource(tmp_path, include_rejected=True)
    tree = curate.model_tree(org)
    orders = next(t for t in tree["subject_areas"][0]["tables"] if t["table"] == "orders")
    assert orders["review_state"] == "approved"
    ssn = next(c for c in orders["columns"] if c["name"] == "ssn")
    assert ssn["sensitive"] is True


def test_exclude_table_hides_from_runtime(tmp_path):
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "exclude", "kind": "table",
                                   "area": "sales", "name": "orders"}])
    assert res.validated and res.applied
    runtime = loader.load_datasource(tmp_path)  # drops rejected
    assert not any(t.name == "orders" for sa in runtime.subject_areas for t in sa.tables_defined)
    # still visible with include_rejected (so the explorer can toggle it back)
    full = loader.load_datasource(tmp_path, include_rejected=True)
    assert any(t.name == "orders" for sa in full.subject_areas for t in sa.tables_defined)


def test_include_restores(tmp_path):
    _write_model(tmp_path)
    curate.apply(tmp_path, [{"op": "exclude", "kind": "table", "area": "sales", "name": "orders"}])
    curate.apply(tmp_path, [{"op": "include", "kind": "table", "area": "sales", "name": "orders"}])
    runtime = loader.load_datasource(tmp_path)
    assert any(t.name == "orders" for sa in runtime.subject_areas for t in sa.tables_defined)


def test_exclude_column(tmp_path):
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "exclude", "kind": "table", "area": "sales",
                                   "name": "orders", "column": "ssn"}])
    assert res.validated
    orders = loader.load_datasource(tmp_path).subject_areas[0].defined_table("orders")
    assert not any(c.name == "ssn" for c in orders.columns)


def test_approve_metric_records_signoff(tmp_path):
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "approve", "kind": "metric", "area": "sales",
                                   "name": "order_count", "at": "2026-06-09T00:00:00Z"}],
                       signer="reviewer@example.com", role="cto")
    assert res.validated
    org = loader.load_datasource(tmp_path)
    mm = org.subject_areas[0].metrics[0]
    assert mm.review_state == "approved" and mm.signed_off_by == "reviewer@example.com"
    # no longer in the Rule 1 queue
    assert curate.review_queue(org)["counts"]["rule_1"] == 0


def test_approve_relationship(tmp_path):
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "approve", "kind": "relationship", "area": "sales",
                                   "name": "orders->customers", "at": "2026-06-09T00:00:00Z"}],
                       signer="reviewer@example.com", role="cto")
    assert res.validated
    rel = loader.load_datasource(tmp_path).subject_areas[0].relationships[0]
    assert rel.review_state == "approved" and rel.signed_off_by == "reviewer@example.com"


def test_edit_relationship_on_clause(tmp_path):
    _write_model(tmp_path)
    # the "approve with fix" flow: set on: to a CAST, drop the simple columns
    res = curate.apply(tmp_path, [
        {"op": "edit", "kind": "relationship", "area": "sales", "name": "orders->customers",
         "field": "on", "value": "CAST(orders.customer_id AS TEXT) = customers.id"},
        {"op": "edit", "kind": "relationship", "area": "sales", "name": "orders->customers",
         "field": "from_column", "value": None},
        {"op": "edit", "kind": "relationship", "area": "sales", "name": "orders->customers",
         "field": "to_column", "value": None},
    ])
    assert res.validated, res.errors
    rel = loader.load_datasource(tmp_path).subject_areas[0].relationships[0]
    assert rel.on and "CAST" in rel.on


def test_approve_resolves_slugged_metric_name(tmp_path):
    """Regression: a multi-word metric name is stored slugged (`total event sales` →
    total_event_sales.yaml). `apply(approve)` must resolve the slug — looking for a literal
    `total event sales.yaml` was caught into res.skipped, so the approval silently no-op'd."""
    _write_model(tmp_path)
    curate.write_items(tmp_path, "sales", "metric", [
        {"name": "total event sales", "calculation": "count of event sales",
         "bindings": {"PostgreSQL": "COUNT(*)"}, "source_tables": ["orders"],
         "confidence": "inferred", "review_state": "unreviewed"}])
    assert (tmp_path / "subject_areas" / "sales" / "metrics" / "total_event_sales.yaml").exists()

    res = curate.apply(tmp_path, [{"op": "approve", "kind": "metric", "area": "sales",
                                   "name": "total event sales", "at": "2026-06-12T00:00:00Z"}],
                       signer="x@y.com", role="cto")
    assert res.applied and not res.skipped, res.skipped
    m2 = next(x for x in loader.load_datasource(tmp_path).subject_areas[0].metrics
              if x.name == "total event sales")
    assert m2.review_state == "approved"

    # a genuinely-missing metric now fails with a CLEAR reason (not a cryptic FileNotFoundError)
    res2 = curate.apply(tmp_path, [{"op": "approve", "kind": "metric", "area": "sales",
                                    "name": "no such metric"}])
    assert res2.skipped and "no metric named" in res2.skipped[0]["reason"]


def test_edit_op_auto_resolves_area_when_omitted(tmp_path):
    """No `area` on the op -> curate resolves which area owns the table by name (so callers
    never hand-maintain a table->area map / generator script)."""
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "edit", "kind": "table", "name": "orders",
                                   "field": "description", "value": "all orders", "source": "ai"}])
    assert res.validated and res.applied and not res.skipped, res.skipped
    orders = loader.load_datasource(tmp_path).subject_areas[0].defined_table("orders")
    assert orders.description == "all orders"
    assert orders.description_source == "ai_unvalidated"  # source:"ai" stamped


def test_edit_op_ambiguous_area_errors_clearly(tmp_path):
    """A bare table name that exists in two schema areas can't be auto-resolved -> the op is
    skipped with a clear 'pass an explicit area' reason rather than editing the wrong one."""
    yaml = __import__("yaml")
    _write_model(tmp_path)
    # add a second area 'crm' that ALSO has an `orders` table (the cross-schema collision shape)
    crm = tmp_path / "subject_areas" / "crm" / "tables"
    crm.mkdir(parents=True)
    (tmp_path / "subject_areas" / "crm" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "crm", "tables": [{"storage_connection": "c", "schema": "crm", "table": "orders"}]}))
    (crm / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "crm", "storage_connection": "c", "grain": ["id"],
        "description": "crm orders", "columns": [{"name": "id", "type": "integer", "primary_key": True}]}))
    res = curate.apply(tmp_path, [{"op": "edit", "kind": "table", "name": "orders",
                                   "field": "description", "value": "x", "source": "ai"}])
    assert res.skipped and "multiple areas" in res.skipped[0]["reason"]
    # with an explicit area it resolves fine
    res2 = curate.apply(tmp_path, [{"op": "edit", "kind": "table", "area": "crm", "name": "orders",
                                    "field": "description", "value": "crm only", "source": "ai"}])
    assert res2.applied and not res2.skipped, res2.skipped


def test_approve_cross_area_relationship_writes_org_yaml(tmp_path):
    """A cross-schema/cross-area join lives in datasource.yaml (not an area's relationships.yaml),
    so approving it must update datasource.yaml via the org-level fallback."""
    root = tmp_path
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    for area, tbl in [("billing", "invoices"), ("crm", "customers")]:
        (root / "subject_areas" / area / "tables").mkdir(parents=True)
        (root / "subject_areas" / area / "subject_area.yaml").write_text(yaml.safe_dump({
            "name": area, "tables": [{"storage_connection": "c", "schema": area, "table": tbl}]}))
        (root / "subject_areas" / area / "tables" / f"{tbl}.yaml").write_text(yaml.safe_dump({
            "name": tbl, "schema": area, "storage_connection": "c", "grain": ["id"], "description": tbl,
            "columns": [{"name": "id", "type": "integer", "primary_key": True},
                        {"name": "customer_id", "type": "integer"}]}))
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "shop", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/billing", "subject_areas/crm"],
        "cross_subject_area_relationships": [{
            "from_table": "invoices", "from_column": "customer_id", "to_table": "customers", "to_column": "id",
            "from_subject_area": "billing", "to_subject_area": "crm", "from_schema": "billing", "to_schema": "crm",
            "relationship": "many_to_one", "confidence": "inferred", "review_state": "unreviewed"}]}))
    subprocess.run(["git", "-C", str(root), "init", "-q"])
    subprocess.run(["git", "-C", str(root), "add", "-A"])
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "i"])

    res = curate.apply(root, [{"op": "approve", "kind": "relationship", "area": "billing",
                               "name": "invoices->customers", "at": "2026-06-12T00:00:00Z"}],
                       signer="reviewer@example.com", role="data_lead")
    assert res.validated and res.applied and not res.skipped, res.skipped
    o = yaml.safe_load((root / "datasource.yaml").read_text())
    cr = o["cross_subject_area_relationships"][0]
    assert cr["review_state"] == "approved" and cr["signed_off_by"] == "reviewer@example.com"

    # editing a cross-area join's field also persists to datasource.yaml (the UI edit path) —
    # not just approve/reject. So cross joins are as editable as regular joins.
    res2 = curate.apply(root, [{"op": "edit", "kind": "relationship", "area": "billing",
                                "name": "invoices->customers", "field": "description",
                                "value": "invoice to its CRM account"}])
    assert res2.validated and res2.applied and not res2.skipped, res2.skipped
    cr2 = yaml.safe_load((root / "datasource.yaml").read_text())["cross_subject_area_relationships"][0]
    assert cr2["description"] == "invoice to its CRM account"


def test_validate_seeds_probe_is_star_free(tmp_path):
    """Regression (ACE-062 A1): the zero-row validation wrapper must not project `SELECT *`.
    The executor's SELECT-* ban walks every SELECT in the tree, so a `SELECT *` wrapper would
    trip the ban on itself and reject every seed. We simulate that ban with a runner that
    raises on any star in the probe, and assert a legit named-column seed still passes."""
    seen_probes: list[str] = []

    def runner(sql: str) -> None:
        seen_probes.append(sql)
        # mimic runtime.check_no_select_star: a projected star anywhere is refused
        if _STAR_PROJECTION.search(sql):
            raise RuntimeError('{"kind": "select_star"}')

    seed = {"question": "how many orders?", "sql": "SELECT COUNT(id) AS n FROM orders"}
    passing, rejected = curate.validate_seeds([seed], runner)

    assert not rejected, rejected
    assert len(passing) == 1 and passing[0]["source"] == "seed"
    assert seen_probes and not _STAR_PROJECTION.search(seen_probes[0])  # wrapper is star-free


# --- add_relationships: on-form joins and an all-or-nothing batch -----------------------------------

_ON_JOIN = {"from_table": "orders", "to_table": "customers", "relationship": "many_to_one",
            "on": "orders.customer_id = customers.id AND customers.id > 0",
            "confidence": "proposed", "review_state": "unreviewed"}


def test_add_relationships_accepts_an_on_form_join(tmp_path):
    _write_model(tmp_path, git=False)
    res = curate.add_relationships(tmp_path, intra={"sales": [_ON_JOIN]})
    assert not res.errors and res.applied == ["rel sales/orders→customers (on)"]
    rels = loader.load_datasource(tmp_path).subject_areas[0].relationships
    assert any(r.on and r.from_column is None for r in rels)


def test_add_relationships_restores_every_file_when_the_batch_fails_part_way(tmp_path):
    _write_model(tmp_path, git=False)
    before = (tmp_path / "subject_areas" / "sales" / "relationships.yaml").read_text()
    # the second area has no directory, so writing its file fails after `sales` was already written
    res = curate.add_relationships(tmp_path, intra={"sales": [_ON_JOIN], "missing_area": [_ON_JOIN]})
    assert res.errors and not res.applied
    assert (tmp_path / "subject_areas" / "sales" / "relationships.yaml").read_text() == before


def test_a_generated_description_never_replaces_a_human_one(tmp_path):
    _write_model(tmp_path, git=False)
    human = {"op": "edit", "kind": "table", "name": "orders", "column": "customer_id",
             "field": "description", "value": "Who placed the order"}
    assert curate.apply(tmp_path, [human]).applied
    ai = {**human, "value": "Customer identifier", "source": "ai"}
    res = curate.apply(tmp_path, [ai])
    assert not res.applied and "kept the existing human description" in res.skipped[0]["reason"]
    col = next(c for c in loader.load_datasource(tmp_path).subject_areas[0].defined_table("orders").columns
               if c.name == "customer_id")
    assert col.description == "Who placed the order" and col.description_source == "human"
    # a person can still change it
    assert curate.apply(tmp_path, [{**human, "value": "Ordering customer"}]).applied


# --- #528: a model outside git keeps a reviewable history in its curation log ---------------------


def _no_repo_above(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))


def test_two_edits_outside_git_leave_a_history_showing_both(tmp_path, monkeypatch):
    from semantic_model import snapshot

    _no_repo_above(tmp_path, monkeypatch)
    _write_model(tmp_path, git=False)
    first = curate.apply(tmp_path, [{"op": "edit", "kind": "table", "name": "orders",
                                     "field": "description", "value": "One row per order"}])
    v1 = snapshot.newest_version(tmp_path)
    second = curate.apply(tmp_path, [{"op": "edit", "kind": "table", "name": "orders",
                                      "column": "customer_id", "field": "description",
                                      "value": "Who placed the order"}])
    assert first.applied and second.applied and not second.committed
    assert "sm history" in second.commit_note

    newest, oldest = curate.history(tmp_path)
    assert (oldest["field"], oldest["before"], oldest["value"]) == ("description", "orders", "One row per order")
    assert (newest["column"], newest["before"], newest["value"]) == ("customer_id", None, "Who placed the order")
    assert oldest["version"] == v1 and newest["version"] == snapshot.newest_version(tmp_path) != v1
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", newest["at"])


def test_a_review_state_change_logs_the_state_it_replaced(tmp_path):
    _write_model(tmp_path, git=False)
    curate.apply(tmp_path, [{"op": "approve", "kind": "metric", "name": "order_count",
                             "at": "2026-06-09T00:00:00Z"}], signer="you@example.com", role="cto")
    entry = curate.history(tmp_path)[0]
    assert entry["before"] == "unreviewed" and entry["signer"] == "you@example.com"


def test_only_the_ops_that_took_are_logged(tmp_path):
    _write_model(tmp_path, git=False)
    res = curate.apply(tmp_path, [
        {"op": "exclude", "kind": "table", "name": "orders", "column": "ssn"},
        {"op": "edit", "kind": "table", "name": "no_such_table", "field": "description", "value": "x"},
    ])
    assert len(res.applied) == 1 and len(res.skipped) == 1
    assert [e["name"] for e in curate.history(tmp_path)] == ["orders"]


def test_a_batch_that_fails_validation_logs_nothing(tmp_path):
    _write_model(tmp_path, git=False)
    res = curate.apply(tmp_path, [{"op": "edit", "kind": "relationship", "name": "orders->customers",
                                   "field": "relationship", "value": "not-a-cardinality"}])
    assert not res.validated
    assert curate.history(tmp_path) == []


def test_datasource_description_and_glossary_writes_are_logged_with_the_prior_value(tmp_path):
    _write_model(tmp_path, git=False)
    assert curate.set_datasource_description(tmp_path, "Orders and customers").validated
    assert curate.set_datasource_description(tmp_path, "Every order a customer placed").validated
    assert curate.set_key_terminology(tmp_path, {"AOV": "average order value"}).validated
    gloss, second, first = curate.history(tmp_path)
    assert (first["before"], first["value"]) == (None, "Orders and customers")
    assert (second["before"], second["value"]) == ("Orders and customers", "Every order a customer placed")
    assert gloss["field"] == "key_terminology" and gloss["before"] == {}
    assert gloss["value"] == {"AOV": "average order value"}
    # a datasource-level edit doesn't make introspection treat the areas as curated
    assert not curate.has_model_curation(tmp_path)


def test_has_model_curation_counts_any_area_edit_or_an_unreadable_line(tmp_path):
    log = tmp_path / "curation_log.jsonl"
    assert not curate.has_model_curation(tmp_path)
    log.write_text('{"op": "edit", "kind": "datasource"}\n', encoding="utf-8")
    assert not curate.has_model_curation(tmp_path)
    for extra in ('{"op": "exclude", "kind": "table"}\n', "{}\n", "not json\n"):
        log.write_text('{"op": "edit", "kind": "datasource"}\n' + extra, encoding="utf-8")
        assert curate.has_model_curation(tmp_path)


def test_history_reads_older_entries_and_skips_unreadable_lines(tmp_path):
    (tmp_path / "curation_log.jsonl").write_text(
        '{"op": "exclude", "kind": "table", "name": "a", "signer": null, "role": null}\n'
        "not json\n"
        '{"op": "edit", "kind": "table", "name": "b", "at": "2026-10-01T00:00:00Z"}\n', encoding="utf-8")
    assert [e["name"] for e in curate.history(tmp_path)] == ["b", "a"]
    assert [e["name"] for e in curate.history(tmp_path, limit=1)] == ["b"]
    assert curate.history(tmp_path / "missing") == []


def test_a_line_that_is_not_utf8_is_skipped_not_fatal(tmp_path):
    (tmp_path / "curation_log.jsonl").write_bytes(
        b'{"op": "exclude", "kind": "table", "name": "a"}\n'
        b"\xff\n"
        b'{"op": "edit", "kind": "table", "name": "b"}\n')
    assert [e["name"] for e in curate.history(tmp_path)] == ["b", "a"]
    assert curate.has_model_curation(tmp_path)
    (tmp_path / "curation_log.jsonl").write_bytes(b'{"op": "edit", "kind": "datasource"}\n\xff\n')
    assert curate.has_model_curation(tmp_path)  # the unreadable line counts as curation


def test_sm_history_prints_the_log_newest_first(tmp_path, capsys):
    import json

    from semantic_model import cli

    _write_model(tmp_path, git=False)
    curate.set_datasource_description(tmp_path, "first")
    curate.set_datasource_description(tmp_path, "second")
    assert cli.main(["history", str(tmp_path), "--limit", "1"]) == 0
    (entry,) = json.loads(capsys.readouterr().out)
    assert (entry["before"], entry["value"]) == ("first", "second")


def test_a_profile_that_is_its_own_repository_commits_the_log_with_the_change(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "you@example.com", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "you@example.com", "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(k, v)
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "exclude", "kind": "table", "name": "orders", "column": "ssn"}])
    assert res.committed
    shown = subprocess.run(["git", "-C", str(tmp_path), "show", "--name-only", "HEAD"],
                           capture_output=True, text=True).stdout
    assert "curation_log.jsonl" in shown


def test_an_enrichment_entity_is_unreviewed_until_approved_with_a_signer(tmp_path):
    """#508: an entity written without a review_state (as enrichment does) is unreviewed and in the
    review queue, not trusted unseen; approving it through the review flow stamps who and when."""
    _write_model(tmp_path)
    res = curate.write_items(tmp_path, "sales", "entity", [
        {"name": "customer", "maps_to": [{"table": "customers", "column": "id", "primary": True}]}])
    assert res.applied and not res.errors, (res.skipped, res.errors)
    ent = loader.load_datasource(tmp_path).subject_areas[0].entities[0]
    assert ent.review_state == "unreviewed" and ent.signed_off_by is None
    queued = curate.review_queue(loader.load_datasource(tmp_path))["rule_2"]
    assert [it["name"] for it in queued if it["kind"] == "entity"] == ["customer"]

    res = curate.apply(tmp_path, [{"op": "approve", "kind": "entity", "area": "sales",
                                   "name": "customer", "at": "2026-10-22T00:00:00Z"}],
                       signer="you@example.com", role="analyst")
    assert res.applied and not res.skipped, res.skipped
    ent = loader.load_datasource(tmp_path).subject_areas[0].entities[0]
    assert ent.review_state == "approved"
    assert (ent.signed_off_by, ent.signed_off_role, ent.signed_off_at) == (
        "you@example.com", "analyst", "2026-10-22T00:00:00Z")
    assert not [it for it in curate.review_queue(loader.load_datasource(tmp_path))["rule_2"]
                if it["kind"] == "entity"]


# --- #549: a description edit without `source` keeps the description's provenance -----------------


def _set_provenance(root: Path, source: str, *, column: str | None = "customer_id",
                    description: str = "Who placd the order") -> None:
    path = root / "subject_areas" / "sales" / "tables" / "orders.yaml"
    doc = yaml.safe_load(path.read_text())
    target = next(c for c in doc["columns"] if c["name"] == column) if column else doc
    target["description"], target["description_source"] = description, source
    path.write_text(yaml.safe_dump(doc))


def _orders_provenance(root: Path, column: str | None = "customer_id") -> tuple:
    t = loader.load_datasource(root).subject_areas[0].defined_table("orders")
    item = next(c for c in t.columns if c.name == column) if column else t
    return item.description, item.description_source


def _desc_edit(value: str, column: str | None = "customer_id", **extra) -> dict:
    op = {"op": "edit", "kind": "table", "name": "orders", "field": "description", "value": value, **extra}
    return {**op, "column": column} if column else op


@pytest.mark.parametrize("column", ["customer_id", None])
def test_an_edit_without_source_keeps_a_metadata_description_metadata(tmp_path, column):
    _write_model(tmp_path, git=False)
    _set_provenance(tmp_path, "metadata", column=column)
    res = curate.apply(tmp_path, [_desc_edit("Who placed the order", column)])
    assert res.applied and not res.skipped, res.skipped
    assert _orders_provenance(tmp_path, column) == ("Who placed the order", "metadata")
    assert not res.provenance_changed and "provenance_changed" not in res.as_dict()


@pytest.mark.parametrize("column", ["customer_id", None])
def test_an_explicit_human_source_changes_the_provenance_and_says_so(tmp_path, column):
    _write_model(tmp_path, git=False)
    _set_provenance(tmp_path, "metadata", column=column)
    res = curate.apply(tmp_path, [_desc_edit("Who placed the order", column, source="human")])
    assert _orders_provenance(tmp_path, column) == ("Who placed the order", "human")
    item = "table sales/orders" + (f".{column}" if column else "")
    assert res.as_dict()["provenance_changed"] == [{"item": item, "from": "metadata", "to": "human"}]


def test_a_first_description_is_a_persons_and_is_not_reported_as_a_change(tmp_path):
    _write_model(tmp_path, git=False)
    res = curate.apply(tmp_path, [_desc_edit("Who placed the order")])
    assert _orders_provenance(tmp_path) == ("Who placed the order", "human")
    assert not res.provenance_changed


def test_an_explicit_metadata_source_stamps_metadata(tmp_path):
    _write_model(tmp_path, git=False)
    _set_provenance(tmp_path, "human")
    res = curate.apply(tmp_path, [_desc_edit("Who placed the order", source="metadata")])
    assert _orders_provenance(tmp_path) == ("Who placed the order", "metadata")
    assert res.provenance_changed == [{"item": "table sales/orders.customer_id",
                                       "from": "human", "to": "metadata"}]


def test_an_unknown_source_is_refused(tmp_path):
    _write_model(tmp_path, git=False)
    _set_provenance(tmp_path, "metadata")
    res = curate.apply(tmp_path, [_desc_edit("Who placed the order", source="person")])
    assert not res.applied and "is not one of: ai, metadata, human" in res.skipped[0]["reason"]
    assert _orders_provenance(tmp_path) == ("Who placd the order", "metadata")


@pytest.mark.parametrize("source", ["ai_unvalidated", "ai_validated"])
def test_an_edit_without_source_keeps_an_ai_description_ai(tmp_path, source):
    """Unconfirmed AI words stay unconfirmed: an edit without `source` never promotes them to a person's."""
    _write_model(tmp_path, git=False)
    _set_provenance(tmp_path, source)
    res = curate.apply(tmp_path, [_desc_edit("Who placed the order")])
    assert _orders_provenance(tmp_path) == ("Who placed the order", source)
    assert not res.provenance_changed
    res = curate.apply(tmp_path, [_desc_edit("The ordering customer", source="human")])
    assert res.provenance_changed == [{"item": "table sales/orders.customer_id", "from": source, "to": "human"}]


def test_a_generated_description_is_still_refused_over_a_metadata_one(tmp_path):
    _write_model(tmp_path, git=False)
    _set_provenance(tmp_path, "metadata")
    res = curate.apply(tmp_path, [_desc_edit("Customer identifier", source="ai")])
    assert not res.applied and "kept the existing metadata description" in res.skipped[0]["reason"]


def test_describing_a_column_agami_could_not_read_makes_it_a_persons(tmp_path):
    """`ai_unknown` marks a column with no description yet, so the text a person writes into it is theirs."""
    _write_model(tmp_path, git=False)
    _set_provenance(tmp_path, "ai_unknown", description="")
    res = curate.apply(tmp_path, [_desc_edit("Who placed the order")])
    assert _orders_provenance(tmp_path) == ("Who placed the order", "human")
    assert res.provenance_changed == [{"item": "table sales/orders.customer_id",
                                       "from": "ai_unknown", "to": "human"}]


def test_emptying_a_column_agami_could_not_read_keeps_the_mark(tmp_path):
    _write_model(tmp_path, git=False)
    _set_provenance(tmp_path, "ai_unknown", description="")
    res = curate.apply(tmp_path, [_desc_edit("")])
    assert res.applied and _orders_provenance(tmp_path) == ("", "ai_unknown")
    assert not res.provenance_changed


def test_a_description_edit_logs_the_provenance_before_and_after(tmp_path):
    _write_model(tmp_path, git=False)
    _set_provenance(tmp_path, "metadata")
    curate.apply(tmp_path, [_desc_edit("Who placed the order")])
    curate.apply(tmp_path, [_desc_edit("The ordering customer", source="human")])
    changed, kept = curate.history(tmp_path)
    assert (kept["description_source_before"], kept["description_source_after"]) == ("metadata", "metadata")
    assert (changed["description_source_before"], changed["description_source_after"]) == ("metadata", "human")
    # an edit of another field carries no provenance
    curate.apply(tmp_path, [{"op": "edit", "kind": "table", "name": "orders", "column": "customer_id",
                             "field": "unit", "value": "count"}])
    assert "description_source_before" not in curate.history(tmp_path)[0]


def test_a_direct_provenance_edit_is_reported(tmp_path):
    _write_model(tmp_path, git=False)
    _set_provenance(tmp_path, "ai_unvalidated")
    res = curate.apply(tmp_path, [{"op": "edit", "kind": "table", "name": "orders", "column": "customer_id",
                                   "field": "description_source", "value": "ai_validated"}])
    assert res.provenance_changed == [{"item": "table sales/orders.customer_id",
                                       "from": "ai_unvalidated", "to": "ai_validated"}]


def test_a_batch_that_fails_validation_reports_no_provenance_change(tmp_path):
    _write_model(tmp_path, git=False)
    _set_provenance(tmp_path, "metadata")
    res = curate.apply(tmp_path, [_desc_edit("Who placed the order", source="human"),
                                  {"op": "edit", "kind": "relationship", "name": "orders->customers",
                                   "field": "relationship", "value": "not-a-cardinality"}])
    assert res.errors and not res.provenance_changed
    assert _orders_provenance(tmp_path) == ("Who placd the order", "metadata")


@pytest.mark.parametrize("column", ["customer_id", None])
def test_an_approved_table_or_column_stays_approved_after_a_description_edit(tmp_path, column):
    _write_model(tmp_path, git=False)
    _set_provenance(tmp_path, "metadata", column=column)
    path = tmp_path / "subject_areas" / "sales" / "tables" / "orders.yaml"
    doc = yaml.safe_load(path.read_text())
    target = next(c for c in doc["columns"] if c["name"] == column) if column else doc
    target["review_state"] = "approved"
    path.write_text(yaml.safe_dump(doc))
    res = curate.apply(tmp_path, [_desc_edit("Who placed the order", column, source="human")])
    assert res.applied and not res.stale
    t = loader.load_datasource(tmp_path).subject_areas[0].defined_table("orders")
    item = next(c for c in t.columns if c.name == column) if column else t
    assert item.review_state == "approved"


def test_sm_curate_prints_the_provenance_change(tmp_path, capsys):
    import json

    from semantic_model import cli

    _write_model(tmp_path, git=False)
    _set_provenance(tmp_path, "metadata")
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([_desc_edit("Who placed the order", source="human")]))
    assert cli.main(["curate", str(tmp_path), "--ops-file", str(ops)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["provenance_changed"] == [{"item": "table sales/orders.customer_id",
                                          "from": "metadata", "to": "human"}]
