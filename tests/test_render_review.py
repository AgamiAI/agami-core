"""
Regression tests for plugins/agami/scripts/render_review.py.

The renderer reads shared/review-dashboard-template.html, substitutes
placeholders, and writes a self-contained HTML file. These tests exercise
the substitution pipeline against synthetic items + summary fixtures.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from render_review import render  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")


def _items_join_card() -> dict:
    return {
        "n": 1,
        "entity_type": "join",
        "rule_1": False,
        "id": "relationships.orders_to_customers",
        "yaml_path": "public/orders.yaml",
        "title": "public.orders → public.customers",
        "confidence": 0.62,
        "review_state": "unreviewed",
        "origin": "introspect_heuristic",
        "signals": [
            {"ok": True, "text": "Both columns named `customer_id`"},
            {"ok": True, "text": "`customers.customer_id` has a unique index"},
            {"ok": False, "text": "No FK declared in DB metadata"},
        ],
        "inferred": "orders.customer_id = customers.customer_id",
        "reply_hint": "approve 1",
    }


def _items_metric_card() -> dict:
    return {
        "n": 2,
        "entity_type": "metric",
        "rule_1": True,
        "id": "metrics.revenue",
        "yaml_path": "public/_schema.yaml",
        "title": "metric: revenue",
        "subtitle": "metric · sign-off required",
        "confidence": 0.85,
        "review_state": "unreviewed",
        "origin": "llm_suggested",
        "signals": [
            {"ok": True, "text": "Source column: orders.amount_usd"},
            {"ok": True, "text": "Numeric type: numeric(12,2)"},
        ],
        "inferred": "SUM(orders.amount_usd)",
        "extra_lines": [
            {"label": "Definition", "text": "Net revenue, gross of refunds, in USD."},
        ],
        "reply_hint": "approve 2 by you@example.com role=cfo",
    }


def _summary_full() -> dict:
    return {
        "auto_approved": {
            "datasets": 4,
            "fields": 27,
            "fk_relationships": 3,
            "field_descriptions_from_comments": 0,
        },
        "needs_review": {
            "inferred_relationships": 0,
            "low_confidence_field_descriptions": 24,
            "metric_proposals": 1,
            "named_filter_proposals": 0,
            "stale": 0,
        },
    }


# --- happy-path render -----------------------------------------------------

def test_render_full_dashboard_no_unsubstituted_placeholders():
    html = render(
        title="Review queue · default · threshold 0.7",
        threshold=0.7,
        model_version="abc123def456",
        items=[_items_join_card(), _items_metric_card()],
        summary=_summary_full(),
    )
    placeholders = PLACEHOLDER_RE.findall(html)
    # Doc-comment placeholders inside <!-- ... --> are explicit literals; the
    # only real check is that all live placeholders were substituted. The
    # template's HTML comment block is the only source of literal {{...}}
    # text, so we count those expected occurrences.
    # Currently the template's HTML comment has 0 in dashboard mode (no
    # {{...}} markers in the comment for review-dashboard-template).
    assert placeholders == [], f"unsubstituted: {placeholders}"


def test_render_substitutes_threshold_and_model_version():
    html = render(
        title="Review",
        threshold=0.5,
        model_version="def789",
        items=[_items_join_card()],
        summary=_summary_full(),
    )
    assert ">0.5<" in html or "threshold = \"0.5\"" in html
    assert "def789" in html


def test_render_includes_items_json_payload():
    html = render(
        title="Review",
        threshold=0.7,
        model_version="x",
        items=[_items_join_card(), _items_metric_card()],
        summary=_summary_full(),
    )
    # The items array gets serialized into the JS — both entry titles must appear.
    assert "orders_to_customers" in html
    assert "revenue" in html


def test_render_empty_queue_still_renders():
    html = render(
        title="Review",
        threshold=0.7,
        model_version="x",
        items=[],
        summary={"auto_approved": {}, "needs_review": {}},
    )
    assert "const items = []" in html
    # Template's "no items" fallback renders an explanatory card via JS — we
    # confirm the items div exists for it to write into.
    assert 'id="items"' in html


# --- validation guards ----------------------------------------------------

def test_render_accepts_all_four_review_states():
    """The 4-tab dashboard accepts every review_state — `approved` and
    `rejected` entries now appear in their own tabs (Approved Automatically /
    Manually Approved / Rejected) rather than being filtered out."""
    for state in ("unreviewed", "approved", "rejected", "stale"):
        item = _items_join_card()
        item["review_state"] = state
        # No exception — the renderer accepts all four.
        render(title="x", threshold=0.7, model_version="v", items=[item], summary=None)


def test_render_rejects_unknown_review_state():
    """A made-up state value is still rejected — only the documented enum is valid."""
    item = _items_join_card()
    item["review_state"] = "in_progress"  # not in enum
    with pytest.raises(ValueError, match="review_state"):
        render(title="x", threshold=0.7, model_version="v", items=[item], summary=None)


def test_render_rejects_unknown_tab():
    """The new `tab` field is validated against {review, auto, manual, rejected}."""
    item = _items_join_card()
    item["tab"] = "elsewhere"
    with pytest.raises(ValueError, match="tab"):
        render(title="x", threshold=0.7, model_version="v", items=[item], summary=None)


def test_render_rejects_invalid_entity_type():
    item = _items_join_card()
    item["entity_type"] = "bogus"
    with pytest.raises(ValueError, match="entity_type"):
        render(title="x", threshold=0.7, model_version="v", items=[item], summary=None)


def test_render_rejects_confidence_out_of_range():
    item = _items_join_card()
    item["confidence"] = 1.5
    with pytest.raises(ValueError, match="confidence"):
        render(title="x", threshold=0.7, model_version="v", items=[item], summary=None)


def test_render_rejects_missing_required_keys():
    item = {"entity_type": "join", "n": 1}  # missing 'title'
    with pytest.raises(ValueError, match="title"):
        render(title="x", threshold=0.7, model_version="v", items=[item], summary=None)


# --- numbering / ordering --------------------------------------------------

def test_items_numbering_is_caller_responsibility():
    """The renderer trusts the caller's numbering — n=99 with one item is fine.
    The skill assigns numbers; the renderer just displays them."""
    item = _items_join_card()
    item["n"] = 99
    html = render(title="x", threshold=0.7, model_version="v", items=[item], summary=None)
    assert '"n": 99' in html


# --- Pillar D: primary/secondary partition in the For Review tab ----------

def test_template_carries_priority_partition_helpers():
    """The renderer always ships the template prelude; the partition logic
    lives in the template (client-side JS) so any rendered file has the
    helpers compiled in. Smoke-check the classes / function names are present
    in the output."""
    html = render(
        title="x", threshold=0.7, model_version="abc", items=[],
        summary={"auto_approved": {}, "needs_review": {}},
    )
    assert "partitionByPriority" in html, "primary/secondary partition helper missing"
    assert "priority-hint" in html, "priority-hint CSS class missing"
    assert "secondary-wrap" in html, "secondary-wrap CSS class missing"
    assert "PRIMARY_TYPES" in html, "PRIMARY_TYPES const missing"


def test_template_summary_messaging_pillar_d():
    """The Pillar D copy on the For Review tab tells users that low-confidence
    items can wait and self-approve as they query."""
    html = render(
        title="x", threshold=0.7, model_version="abc", items=[],
        summary={},
    )
    # The "Optional —" framing for the secondary section.
    assert "Optional" in html
    # The "self-approve as you query" promise.
    assert "self-approve" in html or "self approve" in html

