"""Phase 1 (scorecard #2): column-intrinsic aggregation semantics.

`Column.aggregation` classifies how a column may be aggregated as a measure
(additive / averageable / dimension / unknown). Set by a name+type heuristic at
introspection, refined by the curator, never enforced against when `unknown`.
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
from semantic_model import curate as C  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import validator as V  # noqa: E402


# --- classify_aggregation heuristic ----------------------------------------

@pytest.mark.parametrize("name,ctype,is_key,expected", [
    # keys / non-numeric → dimension
    ("id", "integer", True, "dimension"),
    ("customer_id", "integer", False, "dimension"),
    ("order_no", "integer", False, "dimension"),
    ("zip_code", "integer", False, "dimension"),
    ("fiscal_year", "integer", False, "dimension"),
    ("status", "string", False, "dimension"),         # non-numeric
    ("created_at", "timestamp", False, "dimension"),   # non-numeric
    ("is_active", "boolean", False, "dimension"),      # non-numeric
    # averageable (rates/prices/ratios) — even when numeric & money-ish
    ("unit_price", "decimal", False, "averageable"),
    ("discount_rate", "decimal", False, "averageable"),
    ("conversion_pct", "float", False, "averageable"),
    ("avg_balance", "decimal", False, "averageable"),  # 'avg' beats 'balance'
    ("cost_per_unit", "decimal", False, "averageable"),  # 'per' beats 'cost'
    ("credit_score", "integer", False, "averageable"),
    # additive (money / quantity / stocks)
    ("order_amount", "decimal", False, "additive"),
    ("quantity", "integer", False, "additive"),
    ("revenue", "decimal", False, "additive"),
    ("total_cost", "decimal", False, "additive"),
    ("account_balance", "decimal", False, "additive"),   # stock: summable across accounts
    ("inventory_on_hand", "integer", False, "additive"),
    # unrecognized numeric → unknown (safe; never enforced)
    ("xyz", "decimal", False, "unknown"),
    ("v_1", "float", False, "unknown"),
])
def test_classify_aggregation(name, ctype, is_key, expected):
    assert build.classify_aggregation(name, ctype, is_key=is_key) == expected


def test_default_is_unknown_on_model():
    col = m.Column(name="foo", type="decimal")
    assert col.aggregation == "unknown"  # back-compat: legacy models never falsely enforced


def test_invalid_aggregation_value_rejected():
    with pytest.raises(Exception):
        m.Column(name="foo", type="decimal", aggregation="summable")  # not in the Literal


# --- introspection stamps the class ----------------------------------------

def _catalog_runner(sql):
    s = " ".join(sql.split())
    if "information_schema.schemata" in s:
        return [{"schema_name": "public"}]
    if "information_schema.tables" in s and "table_type" in s:
        return [{"schema_name": "public", "table_name": "orders", "table_type": "BASE TABLE"}]
    if "information_schema.columns" in s:
        return [
            {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
            {"column_name": "customer_id", "data_type": "integer", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""},
            {"column_name": "amount", "data_type": "numeric", "is_nullable": "YES", "ordinal_position": "3", "numeric_scale": "2"},
            {"column_name": "discount_rate", "data_type": "numeric", "is_nullable": "YES", "ordinal_position": "4", "numeric_scale": "4"},
        ]
    if "PRIMARY KEY" in s:
        return [{"column_name": "id"}]
    return []


def test_introspect_stamps_aggregation(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_catalog_runner,
                          artifacts_dir=tmp_path, dry_run=True)
    assert V.validate(org).ok
    t = org.subject_areas[0].defined_table("orders")
    cls = {c.name: c.aggregation for c in t.columns}
    assert cls["id"] == "dimension"            # primary key
    assert cls["customer_id"] == "dimension"   # *_id
    assert cls["amount"] == "additive"
    assert cls["discount_rate"] == "averageable"


# --- curator can correct it (and the strict schema guards the value) --------

def test_curator_can_edit_aggregation(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_catalog_runner,
                          artifacts_dir=tmp_path)  # writes the tree
    root = tmp_path / "shop"
    area = org.subject_areas[0].name
    res = C.apply(root, [{
        "op": "edit", "kind": "table", "area": area, "name": "orders",
        "column": "amount", "field": "aggregation", "value": "averageable",
    }])
    assert not res.errors, res.errors
    reloaded = __import__("semantic_model.loader", fromlist=["load_organization"]).load_organization(root)
    t = reloaded.subject_areas[0].defined_table("orders")
    assert t.get_column("amount").aggregation == "averageable"


def test_curator_edit_rejects_bad_value(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_catalog_runner, artifacts_dir=tmp_path)
    root = tmp_path / "shop"
    area = org.subject_areas[0].name
    res = C.apply(root, [{
        "op": "edit", "kind": "table", "area": area, "name": "orders",
        "column": "amount", "field": "aggregation", "value": "bogus",
    }])
    # strict schema rejects the batch (reverted); the model on disk is unchanged
    assert res.errors
