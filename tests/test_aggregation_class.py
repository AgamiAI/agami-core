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

from catalog_helpers import col as _col  # noqa: E402
from catalog_helpers import make_catalog_runner

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

_catalog_runner = make_catalog_runner(
    tables=["orders"],
    columns={"orders": [
        _col("id", "integer", nullable=False),
        _col("customer_id", "integer"),
        _col("amount", "numeric", scale=2),
        _col("discount_rate", "numeric", scale=4),
    ]},
    estimate=None,
)


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
    reloaded = __import__("semantic_model.loader", fromlist=["load_datasource"]).load_datasource(root)
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


@pytest.mark.parametrize("why", [
    "unit", "column_caveat", "table_caveat", "composite_grain", "no_grain", "default_filter",
])
def test_suggest_metrics_never_proposes_a_plain_aggregate(why):
    """Each of these once kept a plain COUNT(*)/SUM/AVG alive (#404). None does now (#406): the
    unit, the caveats and the grain all reach the agent on the table it opens, the formatter carries
    a column's unit through SUM/AVG by itself, and execute_sql never applied a default filter that a
    bare binding does not embed. What is left is a name restating an aggregation class."""
    from semantic_model import dialects as D
    amount = m.Column(name="amount", type="decimal", aggregation="additive")
    rate = m.Column(name="discount_rate", type="decimal", aggregation="averageable")
    table = {"name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
             "description": "o"}
    if why == "unit":
        amount.unit, rate.unit = "USD", "percent"
    elif why == "column_caveat":
        amount.caveats, rate.caveats = ["Excludes tax"], ["Before returns"]
    elif why == "table_caveat":
        table["caveats"] = ["Voided orders are still rows"]
    elif why == "composite_grain":
        table["grain"] = ["id", "line_no"]
    elif why == "no_grain":
        table["grain"] = []
    else:
        table["default_filters"] = ["{alias}.deleted_at IS NULL"]
    t = m.Table(**table, columns=[
        m.Column(name="id", type="integer", primary_key=True, aggregation="dimension"),
        m.Column(name="line_no", type="integer", aggregation="dimension"),
        amount, rate])
    assert build.suggest_metrics(t, D.get_dialect("postgresql")) == []


def test_suggest_metrics_rate_and_duration_patterns():
    from semantic_model import dialects as D
    t = m.Table(name="incident", schema="public", storage_connection="c", grain=["id"],
                description="i", columns=[
                    m.Column(name="id", type="integer", primary_key=True),
                    m.Column(name="made_sla", type="boolean"),
                    m.Column(name="is_active", type="integer"),       # int flag → rate
                    m.Column(name="opened_at", type="timestamp"),
                    m.Column(name="resolved_at", type="timestamp")])
    mets = {x["name"]: x for x in build.suggest_metrics(t, D.get_dialect("redshift"))}
    assert mets["incident_made_sla_rate"]["bindings"]["Redshift"] == \
        "AVG(CASE WHEN made_sla THEN 1.0 ELSE 0.0 END)"
    assert mets["incident_is_active_rate"]["bindings"]["Redshift"] == \
        "AVG(CASE WHEN is_active <> 0 THEN 1.0 ELSE 0.0 END)"
    dur = mets["incident_avg_duration_days"]   # start+end timestamp pair → dialect DATEDIFF
    assert dur["bindings"]["Redshift"] == "AVG(DATEDIFF('day', opened_at, resolved_at))"
    assert dur["unit"] == "days"
    # A flag RATE picks a condition and a DURATION pairs two columns: a choice the engine could
    # get wrong, so every proposal waits for a person, and none carries a system sign-off.
    assert set(mets) == {"incident_made_sla_rate", "incident_is_active_rate",
                         "incident_avg_duration_days"}
    for met in mets.values():
        assert met["review_state"] == "unreviewed" and met["confidence"] == "proposed"
        assert "signed_off_by" not in met
        # What a metric is FOR is judgement the generator cannot derive; the skill writes it (#406).
        assert "description" not in met
        assert met["primary_table"] == "incident"


def test_suggest_metrics_skips_opaque_columns_until_described():
    from semantic_model import dialects as D
    # active_to is a boolean agami couldn't read; is_leased is a described one.
    cols = [
        m.Column(name="id", type="integer", primary_key=True),
        m.Column(name="active_to", type="boolean", description_source="ai_unknown"),
        m.Column(name="is_leased", type="boolean", description="leased rather than owned"),
    ]
    t = m.Table(name="alm_asset", schema="public", storage_connection="c", grain=["id"],
                description="a", columns=cols)
    names = {x["name"] for x in build.suggest_metrics(t, D.get_dialect("postgresql"))}
    assert "alm_asset_active_to_rate" not in names   # opaque column → no metric proposed
    assert "alm_asset_is_leased_rate" in names       # described column → proposed

    # once the column is described, the SAME call now proposes its metric (the re-pass).
    cols[1].description_source = "ai"
    cols[1].description = "whether the asset is still within its active period"
    names2 = {x["name"] for x in build.suggest_metrics(t, D.get_dialect("postgresql"))}
    assert "alm_asset_active_to_rate" in names2


@pytest.mark.parametrize("name,values,expected_substr", [
    ("currency", ["USD", "EUR", "INR", "USD"], "ISO 4217"),
    ("ccy_code", ["USD", "GBP"], "ISO 4217"),
    ("country", ["US", "IN", "GB"], "ISO 3166"),
    ("user_timezone", ["America/New_York", "Asia/Kolkata"], "IANA"),
    ("locale", ["en", "en_US", "pt-BR"], "locale"),
    # name signal but wrong value shape → no false description
    ("currency", ["dollars", "euros"], None),
    # value shape but no name signal → no guess
    ("status", ["USD", "EUR"], None),
])
def test_canonical_description(name, values, expected_substr):
    out = build.canonical_description(name, values)
    if expected_substr is None:
        assert out is None
    else:
        assert out and expected_substr in out


@pytest.mark.parametrize("cap,expected", [(2, 2), (1, 1), (0, 0), (-1, 0), (-5, 0)])
def test_the_cap_is_clamped_at_zero_not_floored_at_one(cap, expected):
    """`0` honestly means none, but a negative cap must not reach Python's negative slice, where
    `-1` returns every proposal but the last."""
    from semantic_model import dialects as D
    t = m.Table(name="orders", schema="public", storage_connection="c", grain=["id"],
                description="o", columns=[
                    m.Column(name="id", type="integer", primary_key=True),
                    m.Column(name="amount", type="decimal", aggregation="additive", unit="USD"),
                    m.Column(name="is_rush", type="boolean"),
                    m.Column(name="is_gift", type="boolean")])
    assert len(build.suggest_metrics(
        t, D.get_dialect("postgresql"), max_per_table=cap)) == expected


