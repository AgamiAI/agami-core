"""Tests for the model CLI `prepare` command (tier-independent safety pass) and the
`sm` wrapper (interpreter resolution + running the CLI)."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from semantic_model import cli  # noqa: E402


def _model(root: Path) -> None:
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "org.yaml").write_text(yaml.safe_dump({
        "organization": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"},
            {"storage_connection": "c", "schema": "public", "table": "order_items"}]}))
    (root / "subject_areas" / "s" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "o", "default_filters": ["{alias}.deleted_at IS NULL"],
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "deleted_at", "type": "timestamp"},
                    {"name": "total", "type": "decimal"}]}))
    (root / "subject_areas" / "s" / "tables" / "order_items.yaml").write_text(yaml.safe_dump({
        "name": "order_items", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "oi",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "order_id", "type": "integer"}, {"name": "qty", "type": "integer"}]}))
    (root / "subject_areas" / "s" / "relationships.yaml").write_text(yaml.safe_dump({
        "relationships": [{"from_table": "order_items", "from_column": "order_id",
                           "to_table": "orders", "to_column": "id", "relationship": "many_to_one",
                           "review_state": "approved", "confidence": "confirmed",
                           "signed_off_by": "x", "signed_off_role": "system",
                           "signed_off_at": "t"}]}))


def _run(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(args)
    return rc, buf.getvalue()


def test_prepare_allow_applies_default_filters(tmp_path):
    _model(tmp_path)
    rc, out = _run(["prepare", str(tmp_path), "--area", "s", "--sql", "SELECT SUM(total) FROM orders"])
    d = json.loads(out)
    assert rc == 0 and d["action"] == "allow"
    assert d["applied_filters"] == ["orders.deleted_at IS NULL"]
    assert "deleted_at IS NULL" in d["sql"]


def test_prepare_auto_rewrites_fan_trap(tmp_path):
    _model(tmp_path)
    rc, out = _run(["prepare", str(tmp_path), "--area", "s", "--sql",
                    "SELECT SUM(orders.total) FROM orders JOIN order_items ON order_items.order_id=orders.id"])
    d = json.loads(out)
    assert rc == 0 and d["action"] == "auto_rewrite" and d["rewritten"] is True
    assert "order_items" not in d["sql"]              # fan-out join dropped
    assert "deleted_at IS NULL" in d["sql"]           # + default_filter applied


def test_add_writes_metrics_and_skips_invalid(tmp_path):
    # packaged creation path (replaces hand-writing YAML / a throwaway loop script):
    # writes validated metric files, and an invalid item is skipped — never written
    _model(tmp_path)
    from semantic_model import curate
    res = curate.write_items(tmp_path, "s", "metric", [
        {"name": "Total Outstanding", "calculation": "sum of balance",
         "bindings": {"PostgreSQL": "SUM(orders.total)"}, "source_tables": ["orders"],
         "other_names": ["exposure"], "confidence": "inferred", "review_state": "unreviewed"}])
    assert res.validated and res.applied
    assert (tmp_path / "subject_areas" / "s" / "metrics" / "total_outstanding.yaml").exists()

    res2 = curate.write_items(tmp_path, "s", "metric",
                              [{"name": "No Calc", "bindings": {"PostgreSQL": "SUM(x)"}}])
    assert not res2.applied and res2.skipped              # missing required `calculation`
    assert not (tmp_path / "subject_areas" / "s" / "metrics" / "no_calc.yaml").exists()


def test_review_item_matches_dashboard_contract(tmp_path):
    # the dashboard reads rule_1 (bool), signals[], extra_lines[] — a metric item must
    # carry them, else the card shows no description (bug 2) and the feedback generator
    # omits `by <email> role=` for sign-off (bug 3, which buckets by rule_1).
    from semantic_model import curate
    from semantic_model.loader import load_organization
    _model(tmp_path)
    curate.write_items(tmp_path, "s", "metric", [
        {"name": "revenue", "calculation": "Total revenue (USD)",
         "bindings": {"PostgreSQL": "SUM(orders.total)"}, "source_tables": ["orders"],
         "confidence": "inferred", "review_state": "unreviewed"}])
    org = load_organization(tmp_path, include_rejected=True)
    m = next(it for it in curate.all_items(org, scope="all") if it["entity_type"] == "metric")
    assert m["rule_1"] is True
    assert any(s["text"] == "Total revenue (USD)" for s in m["signals"])
    assert any(l["label"] == "Definition" and l["text"] == "Total revenue (USD)" for l in m["extra_lines"])
    # the system-approved relationship is origin=fk on the auto tab (not a phantom count)
    rel = next(it for it in curate.all_items(org, scope="all") if it["entity_type"] == "join")
    assert rel["rule_1"] is False and rel["origin"] == "fk" and rel["tab"] == "auto"


def test_review_items_scope_preseed_covers_metrics_and_entities_not_joins(tmp_path):
    # the "curate before examples" gate: seeds depend on metrics + entities, so preseed
    # includes both — but NOT relationships (those stay lazy / auto-approved FKs)
    from semantic_model import curate
    from semantic_model.loader import load_organization
    _model(tmp_path)  # has a system-approved relationship (auto, FK)
    curate.write_items(tmp_path, "s", "metric", [
        {"name": "rev", "calculation": "sum", "bindings": {"PostgreSQL": "SUM(orders.total)"},
         "source_tables": ["orders"], "confidence": "inferred", "review_state": "unreviewed"}])
    curate.write_items(tmp_path, "s", "entity", [
        {"name": "order", "plural": "orders", "maps_to": [{"table": "orders", "column": "id", "primary": True}],
         "confidence": "inferred", "review_state": "unreviewed"}])
    org = load_organization(tmp_path, include_rejected=True)
    preseed = curate.all_items(org, scope="preseed")
    types = {it["entity_type"] for it in preseed}
    assert "metric" in types and "entity" in types
    assert "join" not in types  # relationships are NOT in the pre-seed gate
    assert all(it["tab"] == "review" for it in preseed)


def test_review_items_scope_rule1_returns_only_signoff_items(tmp_path):
    # the Phase 4 gate uses --scope rule1 so the rendered count == the sign-off count;
    # no skill-side hand-filtering, no env var. rule1 = metrics/named-filters in review tab.
    from semantic_model import curate
    from semantic_model.loader import load_organization
    _model(tmp_path)  # has a system-approved relationship (tab=auto, not in review)
    curate.write_items(tmp_path, "s", "metric", [
        {"name": "Revenue", "calculation": "sum", "bindings": {"PostgreSQL": "SUM(orders.total)"},
         "source_tables": ["orders"], "confidence": "inferred", "review_state": "unreviewed"}])
    org = load_organization(tmp_path, include_rejected=True)
    rule1 = curate.all_items(org, scope="rule1")
    assert rule1 and all(it["rule"] == 1 and it["tab"] == "review" for it in rule1)
    assert any(it["entity_type"] == "metric" for it in rule1)
    # the approved relationship is NOT in the rule1 set, and "all" is a superset
    assert len(curate.all_items(org, scope="all")) >= len(rule1)


def test_add_examples_appends_dedups_and_skips_invalid(tmp_path):
    # packaged examples writer (so skills don't hand-edit YAML or grep its schema):
    # appends, dedups by question, skips an entry missing sql
    from semantic_model import curate
    from semantic_model.loader import list_prompt_examples
    r1 = curate.add_examples(tmp_path, "sales", [
        {"question": "revenue", "sql": "SELECT SUM(total) FROM orders", "tables": ["orders"],
         "source": "seed", "status": "confirmed"},
        {"question": "customers", "sql": "SELECT COUNT(*) FROM customers", "source": "seed"}])
    assert len(r1.applied) == 2 and r1.validated
    r2 = curate.add_examples(tmp_path, "sales", [
        {"question": "revenue", "sql": "SELECT SUM(amount) FROM orders", "source": "correction"},
        {"question": "no sql"}])
    assert any("replaced" in a for a in r2.applied) and r2.skipped
    ex = list_prompt_examples(tmp_path, "sales")
    assert len(ex) == 2  # dedup by question — not 3
    assert next(e for e in ex if e["question"] == "revenue")["sql"] == "SELECT SUM(amount) FROM orders"


def test_curate_edit_op_sets_enrichment_fields(tmp_path):
    # enrichment edits (descriptions / caveats / default_filters / value_transform) go
    # through `sm curate` edit ops — not a hand-edited or scripted table YAML
    from semantic_model import curate
    from semantic_model.loader import load_organization
    _model(tmp_path)
    res = curate.apply(tmp_path, [
        {"op": "edit", "kind": "table", "area": "s", "name": "orders",
         "field": "caveats", "value": ["Excludes test orders."]},
        {"op": "edit", "kind": "table", "area": "s", "name": "orders",
         "field": "default_filters", "value": ["{alias}.deleted_at IS NOT NULL"]},
        {"op": "edit", "kind": "table", "area": "s", "name": "orders",
         "field": "description", "value": "One row per order."},
        {"op": "edit", "kind": "table", "area": "s", "name": "orders",
         "column": "total", "field": "value_transform", "value": "ABS(total)"}])
    assert res.validated and len(res.applied) == 4
    t = load_organization(tmp_path).subject_areas[0].defined_table("orders")
    assert t.caveats == ["Excludes test orders."]
    assert t.default_filters == ["{alias}.deleted_at IS NOT NULL"]
    assert t.description == "One row per order."
    assert t.get_column("total").value_transform == "ABS(total)"


def test_curate_edit_op_sets_structured_fields(tmp_path):
    # the model-explorer's structured editors emit list/object edit-ops; curate applies
    # them (caveats list, entity maps_to, relationship cardinality)
    from semantic_model import curate
    from semantic_model.loader import load_organization
    _model(tmp_path)
    # _model has an entity? no — add one + use the existing rel/columns
    (tmp_path / "subject_areas" / "s" / "entities").mkdir(parents=True, exist_ok=True)
    import yaml
    (tmp_path / "subject_areas" / "s" / "entities" / "buyer.yaml").write_text(yaml.safe_dump({
        "name": "buyer", "maps_to": [{"table": "orders", "column": "id", "primary": True}],
        "confidence": "inferred", "review_state": "unreviewed"}))
    res = curate.apply(tmp_path, [
        {"op": "edit", "kind": "table", "area": "s", "name": "orders", "column": "total",
         "field": "caveats", "value": ["excludes refunds", "net of tax"]},
        {"op": "edit", "kind": "entity", "area": "s", "name": "buyer",
         "field": "maps_to", "value": [{"table": "order_items", "column": "order_id", "primary": True}]},
        {"op": "edit", "kind": "relationship", "area": "s", "name": "order_items->orders",
         "field": "relationship", "value": "one_to_many"}])
    assert res.validated and len(res.applied) == 3
    sa = load_organization(tmp_path).subject_areas[0]
    assert sa.defined_table("orders").get_column("total").caveats == ["excludes refunds", "net of tax"]
    assert [(m.table, m.column) for m in next(e for e in sa.entities if e.name == "buyer").maps_to] == [("order_items", "order_id")]
    assert next(r for r in sa.relationships if r.from_table == "order_items").relationship == "one_to_many"


def test_validate_seeds_splits_pass_fail_via_runner():
    # the packaged Phase-5 loop: each candidate SQL is wrapped to return zero rows and
    # run via the live-DB runner; passing get seed/confirmed defaults, rejects carry the error
    from semantic_model import curate

    def fake_runner(sql):
        assert "WHERE 1=0" in sql  # validated as a zero-row probe, never scans data
        if "BADCOL" in sql:
            raise RuntimeError("column BADCOL does not exist")
        return []

    passing, rejected = curate.validate_seeds([
        {"question": "good", "sql": "SELECT 1 FROM orders"},
        {"question": "bad", "sql": "SELECT BADCOL FROM orders"},
        {"question": "no sql"}], fake_runner)
    assert [p["question"] for p in passing] == ["good"]
    assert passing[0]["source"] == "seed" and passing[0]["status"] == "confirmed"
    assert {r["question"] for r in rejected} == {"bad", "no sql"}


def _seeds_file(tmp_path):
    f = tmp_path / "seeds.json"
    f.write_text(json.dumps([{"question": "how many orders?", "sql": "SELECT 1 FROM orders"}]))
    return f


def test_seed_examples_refuses_until_preseed_reviewed(tmp_path):
    # Phase-4 gate in code: with an unreviewed metric the seeds would reference, seed-examples
    # REFUSES (exit 2) and writes nothing — the model can't skip the explorer-first review.
    from semantic_model import curate
    from semantic_model.loader import list_prompt_examples
    _model(tmp_path)
    curate.write_items(tmp_path, "s", "metric", [
        {"name": "rev", "calculation": "sum", "bindings": {"PostgreSQL": "SUM(orders.total)"},
         "source_tables": ["orders"], "confidence": "inferred", "review_state": "unreviewed"}])
    rc, out = _run(["seed-examples", str(tmp_path), "--area", "s", "--profile", "p",
                    "--file", str(_seeds_file(tmp_path))])
    d = json.loads(out)
    assert rc == 2 and d["refused"] == "preseed_review_pending" and d["pending_count"] == 1
    assert list_prompt_examples(tmp_path, "s") == []   # nothing written


def test_seed_examples_after_review_bypasses_gate(tmp_path, monkeypatch):
    # --after-review is the Phase-4c bypass: the user has been in the explorer and chose to
    # proceed with items still unreviewed. The seed then validates + writes normally.
    from semantic_model import curate, introspect
    from semantic_model.loader import list_prompt_examples
    _model(tmp_path)
    curate.write_items(tmp_path, "s", "metric", [
        {"name": "rev", "calculation": "sum", "bindings": {"PostgreSQL": "SUM(orders.total)"},
         "source_tables": ["orders"], "confidence": "inferred", "review_state": "unreviewed"}])
    monkeypatch.setattr(introspect, "make_execute_sql_runner", lambda profile: (lambda sql: []))
    rc, out = _run(["seed-examples", str(tmp_path), "--area", "s", "--profile", "p",
                    "--file", str(_seeds_file(tmp_path)), "--after-review"])
    d = json.loads(out)
    assert rc == 0 and "refused" not in d and d["written"]
    assert [e["question"] for e in list_prompt_examples(tmp_path, "s")] == ["how many orders?"]


def test_seed_examples_runs_clean_when_no_preseed_pending(tmp_path, monkeypatch):
    # the common path: nothing unreviewed (base model has only a system-approved FK) → the
    # gate is transparent, no --after-review needed.
    from semantic_model import introspect
    from semantic_model.loader import list_prompt_examples
    _model(tmp_path)
    monkeypatch.setattr(introspect, "make_execute_sql_runner", lambda profile: (lambda sql: []))
    rc, out = _run(["seed-examples", str(tmp_path), "--area", "s", "--profile", "p",
                    "--file", str(_seeds_file(tmp_path))])
    d = json.loads(out)
    assert rc == 0 and "refused" not in d
    assert [e["question"] for e in list_prompt_examples(tmp_path, "s")] == ["how many orders?"]


def test_no_model_root_exits_3_cleanly(tmp_path):
    # an empty root has no org.yaml — the CLI returns a clean no_model signal (exit 3),
    # not a traceback, so callers fold the existence check into their first real call
    rc, out = _run(["areas", str(tmp_path)])
    assert rc == 3
    assert json.loads(out)["error"] == "no_model"


def test_prepare_refuses_shape_changing_trap(tmp_path):
    _model(tmp_path)
    rc, out = _run(["prepare", str(tmp_path), "--area", "s", "--sql",
                    "SELECT orders.id, orders.deleted_at, SUM(orders.total) FROM orders "
                    "JOIN order_items ON order_items.order_id=orders.id "
                    "GROUP BY orders.id, orders.deleted_at"])
    d = json.loads(out)
    assert rc == 1 and d["action"] == "refuse" and d["suggestion"]


# --- sm wrapper ---


def test_sm_wrapper_resolves_and_runs(tmp_path):
    """The wrapper runs the CLI through a resolved, deps-present interpreter even
    though bare `python3` on PATH may lack the model deps. We point it at the
    current test interpreter (which has them) via AGAMI_PYTHON."""
    _model(tmp_path)
    sm = SCRIPTS / "sm"
    proc = subprocess.run(
        ["bash", str(sm), "validate", str(tmp_path)],
        capture_output=True, text=True, env={**__import__("os").environ, "AGAMI_PYTHON": sys.executable},
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout or "error" not in proc.stdout.lower()


def test_validate_tolerates_excluded_table_but_flags_genuine_orphan(tmp_path):
    """Regression: excluding a table flips its tables/<T>.yaml to review_state:
    rejected but intentionally leaves the subject_area.yaml `tables:` ref in place.
    `cmd_validate` loads with include_rejected=True (matching curate), so an
    intentional exclusion does NOT false-flag as orphan_table_ref — while a ref to a
    table that was never defined still fails."""
    from semantic_model import curate
    _model(tmp_path)
    # exclude a table; its subject_area.yaml ref stays in place
    res = curate.apply(tmp_path, [{"op": "exclude", "kind": "table", "area": "s", "name": "order_items"}])
    assert res.validated, "curate's own pre-write validation should pass on an exclusion"
    rc, out = _run(["validate", str(tmp_path)])
    assert rc == 0, f"validate should tolerate the excluded table's dangling ref, got:\n{out}"
    assert "orphan_table_ref" not in out

    # a GENUINE orphan — a ref to a table that was never defined — must still fail
    sa = tmp_path / "subject_areas" / "s" / "subject_area.yaml"
    doc = yaml.safe_load(sa.read_text())
    doc["tables"].append({"storage_connection": "c", "schema": "public", "table": "never_defined"})
    sa.write_text(yaml.safe_dump(doc))
    rc2, out2 = _run(["validate", str(tmp_path)])
    assert rc2 == 1 and "orphan_table_ref" in out2


def test_seed_validate_runs_through_safety_and_shapes_items(tmp_path, monkeypatch):
    """`sm seed-validate` runs each written seed and emits examples-validation items.
    The guarantees we lock: (1) every seed is executed via execute_sql.py WITH `--area`
    and AGAMI_ARTIFACTS_DIR set — so the fan/chasm pre-flight + default_filters always
    run (a raw driver could skip them); (2) results shape into {n, question, sql,
    row_headers, row_preview, row_count, state}; (3) a failing seed surfaces its `error`
    instead of faking a result. The live-DB call is mocked so the test needs no DB."""
    import subprocess
    from semantic_model import curate
    _model(tmp_path)
    curate.add_examples(tmp_path, "s", [
        {"question": "how many orders?", "sql": "SELECT COUNT(*) AS n FROM orders"},
        {"question": "broken seed", "sql": "SELECT * FROM does_not_exist"},
    ])

    calls = []

    def fake_run(cmd, capture_output, text, env):
        calls.append((cmd, env))
        sql = cmd[cmd.index("--sql") + 1]

        class R:
            pass
        r = R()
        if "does_not_exist" in sql:
            r.returncode, r.stdout, r.stderr = 1, "", "relation does_not_exist does not exist"
        else:
            r.returncode, r.stdout, r.stderr = 0, "n\n42\n", ""
        return r

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc, out = _run(["seed-validate", str(tmp_path), "--area", "s", "--profile", "p"])
    assert rc == 0
    items = json.loads(out)
    assert len(items) == 2

    ok, bad = items[0], items[1]
    assert ok["n"] == 1 and ok["question"] == "how many orders?"
    assert ok["state"] == "unreviewed"
    assert ok["row_headers"] == ["n"] and ok["row_preview"] == [["42"]] and ok["row_count"] == 1
    assert "error" not in ok
    assert bad["error"] and bad["row_count"] == 0 and bad["row_preview"] == []

    # (1) safety: EVERY seed ran with --area + AGAMI_ARTIFACTS_DIR so execute_sql's
    # fan/chasm pre-flight + default_filters apply — never bypassed.
    assert len(calls) == 2
    for cmd, env in calls:
        assert "--area" in cmd and cmd[cmd.index("--area") + 1] == "s"
        assert "--no-safety" not in cmd
        assert env.get("AGAMI_ARTIFACTS_DIR") == str(tmp_path.resolve().parent)


def test_seed_validate_formats_numbers_with_model_units(tmp_path, monkeypatch):
    """The validation preview must show numbers formatted by the SAME units.py the query
    path uses — a column with a currency unit shows its symbol + grouping here too, not a
    bare number (the gap that made users re-type 'format as currency' on every example)."""
    import subprocess
    from semantic_model import curate, runtime as RT
    _model(tmp_path)
    curate.add_examples(tmp_path, "s", [{"question": "total billed?", "sql": "SELECT SUM(total) AS total FROM orders"}])

    def fake_run(cmd, capture_output, text, env):
        class R:
            pass
        r = R()
        # UPPERCASE header — mimics Snowflake re-casing the `total` alias to TOTAL. The
        # unit must still attach via the positional key, like the live query path does.
        r.returncode, r.stdout, r.stderr = 0, "TOTAL\n1234567\n", ""
        return r

    monkeypatch.setattr(subprocess, "run", fake_run)
    # resolve_result_units (exercised elsewhere) keys off the SQL alias as written (lowercase)
    # AND a positional `#i` — exactly what it returns in practice. The header here is
    # UPPERCASE, so only the positional `#0` matches: this pins the fallback that was missing.
    monkeypatch.setattr(RT, "resolve_result_units", lambda org, sql: {"total": "USD", "#0": "USD"})

    rc, out = _run(["seed-validate", str(tmp_path), "--area", "s", "--profile", "p"])
    assert rc == 0
    item = json.loads(out)[0]
    assert item["row_preview"] == [["$1,234,567.00"]], item["row_preview"]  # not the bare "1234567"


def test_remove_example_rejects_for_audit_and_drops_from_runtime(tmp_path):
    """`remove-example` rejects by question: the example stays in examples.yaml flagged
    `status: rejected` (audit) but is dropped from the default runtime ranker view — the
    same keep-for-audit/exclude-from-runtime contract as a rejected table/column/metric,
    instead of a skill hand-deleting the YAML."""
    from semantic_model import curate
    from semantic_model.loader import list_prompt_examples
    _model(tmp_path)
    curate.add_examples(tmp_path, "s", [
        {"question": "how many orders?", "sql": "SELECT COUNT(*) FROM orders"},
        {"question": "total billed?", "sql": "SELECT SUM(total) FROM orders"},
    ])

    rc, out = _run(["remove-example", str(tmp_path), "--area", "s",
                    "--question", "total billed?", "--signer", "x@y.com", "--role", "cto"])
    assert rc == 0
    res = json.loads(out)
    assert len(res["rejected"]) == 1 and res["validated"]  # (committed is False in the non-git test fixture)

    # runtime view: the rejected example no longer anchors the ranker
    runtime = list_prompt_examples(tmp_path, "s")
    assert [e["question"] for e in runtime] == ["how many orders?"]

    # audit view: it's still there, flagged rejected
    audit = list_prompt_examples(tmp_path, "s", include_rejected=True)
    rej = [e for e in audit if e["question"] == "total billed?"]
    assert len(rej) == 1 and rej[0]["status"] == "rejected"

    # a question that doesn't exist is reported (skipped), not silently swallowed
    rc2, out2 = _run(["remove-example", str(tmp_path), "--area", "s", "--question", "no such q?"])
    assert rc2 == 1 and json.loads(out2)["skipped"]


def test_suggest_units_finds_money_columns(tmp_path):
    """`sm suggest-units` returns the numeric money columns via the tested matcher — so the
    skill never hand-rolls a regex that drops `discount_amount` by matching `count`."""
    _model(tmp_path)
    rc, out = _run(["suggest-units", str(tmp_path)])
    assert rc == 0
    cols = json.loads(out)["money_columns"]
    names = {(c["table"], c["column"]) for c in cols}
    assert ("orders", "total") in names            # numeric + money-named
    assert ("order_items", "qty") not in names      # a count, not money
    assert all(c["column"] != "id" for c in cols)   # ids excluded
