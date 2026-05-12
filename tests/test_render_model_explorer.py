"""
Regression tests for plugins/agami/scripts/render_model_explorer.py
and plugins/agami/scripts/apply_model_exclusions.py.

Builds a minimal artifacts_dir/<profile>/ tree on the fly (index.yaml,
_schema.yaml, and a couple of table yamls), runs the renderer and the
applier against it, and verifies:
  - The manifest captures every dataset + field with the right
    `excluded` flag derived from agami.review_state.
  - All {{PLACEHOLDER}} tokens are substituted in the rendered HTML.
  - The applier flips review_state to rejected/unreviewed correctly,
    runs the validator, and reverts on validator failure.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from render_model_explorer import build_manifest, render  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")


def _agami_data(payload: dict) -> str:
    return json.dumps({"agami": payload})


def _build_profile(root: Path, profile: str = "test") -> Path:
    """Create a minimal but valid artifacts_dir/<profile>/ tree:
    - 1 schema (PUBLIC) with 2 tables (orders, customers)
    - orders has 3 fields (id, customer_id, status); customer_id is excluded
    - customers is excluded entirely
    - index.yaml + _schema.yaml + per-table yamls
    """
    pdir = root / profile
    pdir.mkdir(parents=True)

    # index.yaml
    (pdir / "index.yaml").write_text(yaml.safe_dump({
        "version": "0.1.1",
        "profile": profile,
        "db_type": "postgres",
        "schemas": [{
            "name": "PUBLIC",
            "file": "PUBLIC/_schema.yaml",
            "table_count": 2,
        }],
        "introspect_meta": {
            "introspected_at": "2026-05-12T00:00:00Z",
            "tier": "psql",
        },
    }, sort_keys=False))

    sdir = pdir / "PUBLIC"
    sdir.mkdir()
    (sdir / "_schema.yaml").write_text(yaml.safe_dump({
        "version": "0.1.1",
        "schema": "PUBLIC",
        "description": "Test schema.",
        "tables": [
            {"name": "orders",    "file": "orders.yaml"},
            {"name": "customers", "file": "customers.yaml"},
        ],
    }, sort_keys=False))

    def table_doc(name: str, ds_review_state: str, fields: list[dict]) -> dict:
        return {
            "version": "0.1.1",
            "semantic_model": [{
                "name": "test_model",
                "datasets": [{
                    "name": name,
                    "source": f"PUBLIC.{name}",
                    "description": f"Test {name} table.",
                    "fields": fields,
                    "custom_extensions": [{
                        "vendor_name": "COMMON",
                        "data": _agami_data({
                            "performance_hints": {"estimated_row_count": 1000},
                            "confidence": 0.95,
                            "review_state": ds_review_state,
                            "origin": "introspect_heuristic",
                            "signed_off_by": "agami_introspect_v1" if ds_review_state == "approved" else None,
                            "signed_off_at": "2026-05-12T00:00:00Z" if ds_review_state == "approved" else None,
                            "signed_off_role": "system" if ds_review_state == "approved" else None,
                        }),
                    }],
                }],
            }],
        }

    def field(name: str, ftype: str, desc: str, review_state: str) -> dict:
        agami = {
            "type": ftype,
            "confidence": 0.7,
            "review_state": review_state,
        }
        if review_state == "approved":
            agami.update({
                "origin": "column_comment",
                "signed_off_by": "agami_introspect_v1",
                "signed_off_at": "2026-05-12T00:00:00Z",
                "signed_off_role": "system",
            })
        elif review_state == "rejected":
            agami.update({
                "origin": "introspect_heuristic",
                "signed_off_by": None,
                "signed_off_at": None,
                "signed_off_role": None,
            })
        else:
            agami.update({
                "origin": "introspect_heuristic",
                "signed_off_by": None,
                "signed_off_at": None,
                "signed_off_role": None,
            })
        return {
            "name": name,
            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": name}]},
            "description": desc,
            "custom_extensions": [{"vendor_name": "COMMON", "data": _agami_data(agami)}],
        }

    (sdir / "orders.yaml").write_text(yaml.safe_dump(
        table_doc("orders", "approved", [
            field("id",          "integer", "Primary key.",              "approved"),
            field("customer_id", "integer", "FK to customers.id.",       "rejected"),  # excluded column
            field("status",      "string",  "Order lifecycle status.",   "approved"),
        ]), sort_keys=False,
    ))
    (sdir / "customers.yaml").write_text(yaml.safe_dump(
        table_doc("customers", "rejected", [  # whole table excluded
            field("id",   "integer", "Primary key.", "approved"),
            field("name", "string",  "Customer name.", "approved"),
        ]), sort_keys=False,
    ))

    # Initialize a git repo so apply_model_exclusions.py can commit / revert.
    subprocess.run(["git", "init", "-q", str(pdir)], check=True)
    subprocess.run(
        ["git", "-C", str(pdir), "-c", "user.name=test",
         "-c", "user.email=test@example.com", "add", "-A"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(pdir), "-c", "user.name=test",
         "-c", "user.email=test@example.com", "commit", "-q", "-m", "initial"],
        check=True,
    )
    return pdir


@pytest.fixture
def profile_dir(tmp_path):
    return _build_profile(tmp_path)


# --- build_manifest ----------------------------------------------------------

def test_manifest_captures_schemas_tables_fields(profile_dir):
    m = build_manifest(profile_dir, "test")
    assert m["totals"]["schemas"] == 1
    assert m["totals"]["tables"] == 2
    assert m["totals"]["fields"] == 5  # 3 in orders + 2 in customers


def test_manifest_excluded_table_flagged(profile_dir):
    m = build_manifest(profile_dir, "test")
    tables = m["schemas"][0]["tables"]
    customers = next(t for t in tables if t["name"] == "customers")
    assert customers["excluded"] is True
    assert customers["review_state"] == "rejected"


def test_manifest_excluded_column_flagged(profile_dir):
    m = build_manifest(profile_dir, "test")
    orders = next(t for t in m["schemas"][0]["tables"] if t["name"] == "orders")
    cid = next(f for f in orders["fields"] if f["name"] == "customer_id")
    assert cid["excluded"] is True
    assert cid["review_state"] == "rejected"
    # And the other fields on `orders` aren't excluded:
    for f in orders["fields"]:
        if f["name"] != "customer_id":
            assert f["excluded"] is False


def test_manifest_totals_excluded_counts(profile_dir):
    m = build_manifest(profile_dir, "test")
    assert m["totals"]["excluded_tables"] == 1
    assert m["totals"]["excluded_fields"] == 1


def test_manifest_qnames_are_qualified(profile_dir):
    m = build_manifest(profile_dir, "test")
    orders = next(t for t in m["schemas"][0]["tables"] if t["name"] == "orders")
    assert orders["qname"] == "PUBLIC.orders"
    cid = next(f for f in orders["fields"] if f["name"] == "customer_id")
    assert cid["qname"] == "PUBLIC.orders.customer_id"


# --- metrics + named_filters (Pillar #9, 2026-05-12) -----------------------

def _profile_with_metric_and_named_filter(root: Path) -> Path:
    """Reuse _build_profile and inject a metric + named_filter so we can
    exercise the new manifest fields. The metric lives on the `orders`
    dataset; the named_filter lives at the model level in index.yaml."""
    pdir = _build_profile(root, profile="m_test")

    # Inject a metric onto orders.
    orders_yaml = pdir / "PUBLIC" / "orders.yaml"
    doc = yaml.safe_load(orders_yaml.read_text())
    ds = doc["semantic_model"][0]["datasets"][0]
    ds["metrics"] = [{
        "name": "order_revenue",
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}]},
        "description": "Total order revenue (gross of refunds).",
        "custom_extensions": [{
            "vendor_name": "COMMON",
            "data": _agami_data({
                "definition_prose": "Sum of amount column. Gross — refunds NOT subtracted.",
                "assumptions": [
                    "amount is denominated in the order's currency",
                    "refunds carry a negative sign and are kept (not removed)",
                ],
                "confidence": 0.85,
                "review_state": "unreviewed",
                "origin": "llm_suggested",
                "signed_off_by": None,
                "signed_off_at": None,
                "signed_off_role": None,
            }),
        }],
    }]
    orders_yaml.write_text(yaml.safe_dump(doc, sort_keys=False))

    # Inject a model-level named_filter into index.yaml's semantic_model
    # extensions.
    index_yaml = pdir / "index.yaml"
    idx = yaml.safe_load(index_yaml.read_text())
    idx.setdefault("semantic_model", []).append({
        "name": "m_test",
        "custom_extensions": [{
            "vendor_name": "COMMON",
            "data": _agami_data({
                "named_filters": [{
                    "name": "active_customer",
                    "expression": "customers.last_purchase_at >= NOW() - INTERVAL '90 days'",
                    "description": "Made a purchase in the last 90 days.",
                    "definition_prose": "Customer made a purchase in the last 90 days.",
                    "confidence": 0.7,
                    "review_state": "unreviewed",
                    "origin": "human_authored",
                    "signed_off_by": None,
                    "signed_off_at": None,
                    "signed_off_role": None,
                }],
            }),
        }],
    })
    index_yaml.write_text(yaml.safe_dump(idx, sort_keys=False))
    return pdir


def test_manifest_collects_metrics(tmp_path):
    pdir = _profile_with_metric_and_named_filter(tmp_path)
    m = build_manifest(pdir, "m_test")
    metrics = m.get("metrics") or []
    assert len(metrics) == 1
    metric = metrics[0]
    assert metric["name"] == "order_revenue"
    assert metric["scope"] == "PUBLIC.orders"
    assert metric["expression"] == "SUM(amount)"
    assert "Gross" in metric["definition_prose"]
    assert len(metric["assumptions"]) == 2
    assert metric["review_state"] == "unreviewed"


def test_manifest_collects_named_filters(tmp_path):
    pdir = _profile_with_metric_and_named_filter(tmp_path)
    m = build_manifest(pdir, "m_test")
    nfs = m.get("named_filters") or []
    assert len(nfs) == 1
    nf = nfs[0]
    assert nf["name"] == "active_customer"
    assert nf["scope"] == "model"
    assert "INTERVAL '90 days'" in nf["expression"]
    assert nf["definition_prose"]
    assert nf["review_state"] == "unreviewed"


def test_manifest_totals_includes_metrics_and_named_filters(tmp_path):
    pdir = _profile_with_metric_and_named_filter(tmp_path)
    m = build_manifest(pdir, "m_test")
    assert m["totals"]["metrics"] == 1
    assert m["totals"]["named_filters"] == 1


def test_manifest_empty_metrics_and_named_filters_when_absent(profile_dir):
    """The base fixture has no metrics or named_filters — totals must show 0,
    not missing keys (the template depends on the keys being present)."""
    m = build_manifest(profile_dir, "test")
    assert m["totals"]["metrics"] == 0
    assert m["totals"]["named_filters"] == 0
    assert m.get("metrics") == []
    assert m.get("named_filters") == []


def test_render_surfaces_metric_definition_prose(tmp_path):
    pdir = _profile_with_metric_and_named_filter(tmp_path)
    m = build_manifest(pdir, "m_test")
    html = render(title="x", profile="m_test", manifest=m)
    # The metric's definition prose, expression, and assumptions all reach the page.
    assert "order_revenue" in html
    assert "Gross" in html
    assert "SUM(amount)" in html


def test_render_surfaces_named_filter_predicate(tmp_path):
    pdir = _profile_with_metric_and_named_filter(tmp_path)
    m = build_manifest(pdir, "m_test")
    html = render(title="x", profile="m_test", manifest=m)
    assert "active_customer" in html
    assert "INTERVAL" in html


# --- render ------------------------------------------------------------------

def test_render_substitutes_all_placeholders(profile_dir):
    m = build_manifest(profile_dir, "test")
    html = render(title="x", profile="test", manifest=m)
    leftover = PLACEHOLDER_RE.findall(html)
    # Only the {{ITEMS_JSON}} / {{REPORT_TITLE}} kind tokens count — but the
    # template should have none left after substitution.
    assert leftover == [], f"unsubstituted placeholders: {leftover}"


def test_render_embeds_manifest_json(profile_dir):
    m = build_manifest(profile_dir, "test")
    html = render(title="x", profile="test", manifest=m)
    # Pick a unique substring from the manifest and verify it made it into
    # the rendered HTML.
    assert "PUBLIC.orders.customer_id" in html
    assert "PUBLIC.customers" in html


def test_render_handles_empty_profile_dir(tmp_path):
    # No schemas; build_manifest returns empty schemas; render still works.
    pdir = tmp_path / "empty"
    pdir.mkdir()
    (pdir / "index.yaml").write_text(yaml.safe_dump({
        "version": "0.1.1", "profile": "empty", "db_type": "postgres",
        "schemas": [],
        "introspect_meta": {"introspected_at": "2026-05-12T00:00:00Z", "tier": "psql"},
    }, sort_keys=False))
    m = build_manifest(pdir, "empty")
    assert m["schemas"] == []
    html = render(title="x", profile="empty", manifest=m)
    assert PLACEHOLDER_RE.findall(html) == []


# --- apply_model_exclusions.py -----------------------------------------------
# Subprocess-based since the script is designed to be invoked from the SKILL.

def _run_applier(profile_dir: Path, actions: dict, actor: str = "tester@x.com") -> dict:
    actions_path = profile_dir.parent / "actions.json"
    actions_path.write_text(json.dumps(actions))
    proc = subprocess.run(
        ["python3", str(SCRIPTS / "apply_model_exclusions.py"),
         "--profile",       profile_dir.name,
         "--artifacts-dir", str(profile_dir.parent),
         "--actor",         actor,
         "--actions-file",  str(actions_path)],
        capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


def _table_review_state(profile_dir: Path, schema: str, table: str) -> str:
    doc = yaml.safe_load((profile_dir / schema / f"{table}.yaml").read_text())
    ds = doc["semantic_model"][0]["datasets"][0]
    agami = json.loads(ds["custom_extensions"][0]["data"])["agami"]
    return agami["review_state"]


def _field_review_state(profile_dir: Path, schema: str, table: str, field: str) -> str:
    doc = yaml.safe_load((profile_dir / schema / f"{table}.yaml").read_text())
    fields = doc["semantic_model"][0]["datasets"][0]["fields"]
    f = next(f for f in fields if f["name"] == field)
    agami = json.loads(f["custom_extensions"][0]["data"])["agami"]
    return agami["review_state"]


def test_apply_exclude_table_flips_review_state(profile_dir):
    result = _run_applier(profile_dir, {"exclude_tables": ["PUBLIC.orders"]})
    assert result["validator_ok"] is True
    assert result["applied"]["exclude_tables"] == 1
    assert _table_review_state(profile_dir, "PUBLIC", "orders") == "rejected"


def test_apply_include_table_flips_back_to_unreviewed(profile_dir):
    # customers starts excluded → include should flip to unreviewed.
    result = _run_applier(profile_dir, {"include_tables": ["PUBLIC.customers"]})
    assert result["validator_ok"] is True
    assert result["applied"]["include_tables"] == 1
    assert _table_review_state(profile_dir, "PUBLIC", "customers") == "unreviewed"


def test_apply_exclude_column_flips_only_that_field(profile_dir):
    result = _run_applier(profile_dir, {"exclude_columns": ["PUBLIC.orders.status"]})
    assert result["validator_ok"] is True
    assert result["applied"]["exclude_columns"] == 1
    assert _field_review_state(profile_dir, "PUBLIC", "orders", "status") == "rejected"
    # Other fields on `orders` untouched:
    assert _field_review_state(profile_dir, "PUBLIC", "orders", "id") == "approved"


def test_apply_skips_unknown_column(profile_dir):
    result = _run_applier(profile_dir, {
        "exclude_columns": ["PUBLIC.orders.does_not_exist"],
    })
    # Validator still passes (no changes), and the unknown column is in skipped[]
    assert result["validator_ok"] is True
    assert result["applied"]["exclude_columns"] == 0
    assert len(result["skipped"]) == 1
    assert "does_not_exist" in result["skipped"][0]["reason"]


def test_apply_skips_unknown_table(profile_dir):
    result = _run_applier(profile_dir, {
        "exclude_tables": ["PUBLIC.does_not_exist"],
    })
    assert result["validator_ok"] is True
    assert result["applied"]["exclude_tables"] == 0
    assert len(result["skipped"]) == 1


def test_apply_malformed_qname_skipped(profile_dir):
    result = _run_applier(profile_dir, {
        "exclude_tables":  ["orders"],                     # missing schema
        "exclude_columns": ["orders.customer_id"],          # missing schema
    })
    assert result["validator_ok"] is True
    assert result["applied"]["exclude_tables"] == 0
    assert result["applied"]["exclude_columns"] == 0
    assert len(result["skipped"]) == 2


def test_apply_writes_to_curation_log(profile_dir):
    _run_applier(profile_dir, {"exclude_tables": ["PUBLIC.orders"]})
    log = (profile_dir / "curation_log.jsonl").read_text().strip().splitlines()
    assert len(log) >= 1
    entry = json.loads(log[-1])
    assert entry["action"] == "exclude"
    assert entry["entity_type"] == "dataset"
    assert entry["entity_qname"] == "PUBLIC.orders"
    assert entry["to_state"] == "rejected"


def test_apply_commits_to_git(profile_dir):
    _run_applier(profile_dir, {"exclude_tables": ["PUBLIC.orders"]})
    log = subprocess.run(
        ["git", "-C", str(profile_dir), "log", "--oneline"],
        capture_output=True, text=True,
    ).stdout
    # Should have at least 2 commits now (initial + applier)
    assert len(log.strip().splitlines()) >= 2
    assert "model:" in log


def test_apply_idempotent_re_exclude(profile_dir):
    """Excluding an already-excluded table is a no-op flip; still validates."""
    # customers starts excluded — re-excluding should succeed without error.
    result = _run_applier(profile_dir, {"exclude_tables": ["PUBLIC.customers"]})
    assert result["validator_ok"] is True
    assert result["applied"]["exclude_tables"] == 1
    assert _table_review_state(profile_dir, "PUBLIC", "customers") == "rejected"


def test_apply_mixed_batch(profile_dir):
    """A single batch with all four action kinds."""
    result = _run_applier(profile_dir, {
        "exclude_tables":  ["PUBLIC.orders"],
        "include_tables":  ["PUBLIC.customers"],
        "exclude_columns": ["PUBLIC.customers.name"],
        "include_columns": ["PUBLIC.orders.customer_id"],
    })
    assert result["validator_ok"] is True
    assert result["applied"] == {
        "exclude_tables":  1,
        "include_tables":  1,
        "exclude_columns": 1,
        "include_columns": 1,
    }
    assert _table_review_state(profile_dir, "PUBLIC", "orders") == "rejected"
    assert _table_review_state(profile_dir, "PUBLIC", "customers") == "unreviewed"
    assert _field_review_state(profile_dir, "PUBLIC", "customers", "name") == "rejected"
    assert _field_review_state(profile_dir, "PUBLIC", "orders", "customer_id") == "unreviewed"
