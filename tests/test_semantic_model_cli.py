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
