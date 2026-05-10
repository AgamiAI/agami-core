"""
Regression tests for the trust-receipt panel in render_chart.py.

These exercise the new {{RECEIPT_JSON}} placeholder and the warning banner
behavior. The legacy (no-receipt) path must still render — backward
compatibility is critical until every caller is migrated.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from render_chart import render  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")


def _section() -> dict:
    return {
        "title": "Top customers",
        "insights": "Carol Chen leads.",
        "chart_type": "bar",
        "labels": ["Carol Chen", "Dave Davis"],
        "datasets": [{"label": "Spend", "data": [148.95, 93.96]}],
        "table_headers": ["Customer", "Spend"],
        "table_rows": [["Carol Chen", "$148.95"], ["Dave Davis", "$93.96"]],
        "sql": "SELECT name, SUM(amount) FROM orders GROUP BY name",
    }


def _receipt_clean() -> dict:
    return {
        "model_version": "abc123def456",
        "tables_used": [
            {"qname": "public.orders", "rows": 12000,
             "freshness": "2026-05-09T23:00:00Z (nightly batch)"},
        ],
        "relationships": [
            {"name": "orders_to_customers", "from_to": "orders → customers",
             "confidence": 1.0, "review_state": "approved", "origin": "fk"},
        ],
        "metrics": [
            {"name": "total_spend", "definition_prose": "Sum of completed-order amounts in USD.",
             "signed_off_by": "jane.smith@example.com", "signed_off_role": "cfo",
             "signed_off_at": "2026-03-15T10:00:00Z"},
        ],
        "named_filters": [],
        "warnings": [],
    }


def _receipt_with_warning() -> dict:
    return {
        "model_version": "abc123",
        "tables_used": [{"qname": "public.orders", "rows": 1000}],
        "relationships": [
            {"name": "orders_to_customers", "from_to": "orders → customers",
             "confidence": 0.62, "review_state": "unreviewed", "origin": "introspect_heuristic"},
        ],
        "warnings": ["Used 1 unreviewed join (orders → customers, conf 0.62). Review now?"],
    }


# --- happy paths ----------------------------------------------------------

def test_render_with_receipt_substitutes_payload():
    html = render(
        title="Test report",
        summary="A test.",
        sections=[_section()],
        receipt=_receipt_clean(),
    )
    # The receipt JSON ends up inline in the JS. Check key pieces.
    assert "model_version" in html
    assert "abc123def456" in html
    assert "jane.smith@example.com" in html


def test_render_with_warnings_includes_warning_text():
    html = render(
        title="Test",
        summary="",
        sections=[_section()],
        receipt=_receipt_with_warning(),
    )
    # Warning text must be present in the page so the JS can render the banner.
    assert "Used 1 unreviewed join" in html
    # The CSS class is in the template — confirm the banner styling is wired.
    assert "trust-warning-banner" in html


def test_render_legacy_no_receipt_still_works():
    """Backward compat: callers without a receipt still get a clean report."""
    html = render(
        title="Test",
        summary="No receipt path",
        sections=[_section()],
        receipt=None,
    )
    # Receipt is null in the JS — the template's `if (receipt)` guard skips
    # the receipt panel entirely.
    assert "const receipt = null" in html


def test_render_no_unsubstituted_placeholders():
    html = render(
        title="Test",
        summary="x",
        sections=[_section()],
        receipt=_receipt_clean(),
    )
    placeholders = PLACEHOLDER_RE.findall(html)
    # The template's HTML doc-comment block contains literal {{...}}
    # placeholders as documentation. Filter those out by looking at lines
    # outside HTML comments.
    in_comment = False
    live_placeholders = []
    for line in html.splitlines():
        if "<!--" in line:
            in_comment = True
        if not in_comment:
            live_placeholders.extend(PLACEHOLDER_RE.findall(line))
        if "-->" in line:
            in_comment = False
    assert live_placeholders == [], f"unsubstituted: {live_placeholders}"


# --- validation guards ----------------------------------------------------

def test_render_rejects_non_dict_receipt():
    with pytest.raises(ValueError, match="receipt"):
        render(title="x", summary="", sections=[_section()], receipt="not a dict")


def test_render_rejects_bad_receipt_field_types():
    bad = _receipt_clean()
    bad["tables_used"] = "should be a list"
    with pytest.raises(ValueError, match="tables_used"):
        render(title="x", summary="", sections=[_section()], receipt=bad)


def test_render_rejects_bad_model_version_type():
    bad = _receipt_clean()
    bad["model_version"] = 12345  # int, not str
    with pytest.raises(ValueError, match="model_version"):
        render(title="x", summary="", sections=[_section()], receipt=bad)


# --- empty receipt is allowed ---------------------------------------------

def test_render_with_empty_receipt():
    """An empty receipt object (no fields populated) still renders — the
    template's per-section conditionals just skip every empty subsection."""
    html = render(title="x", summary="", sections=[_section()], receipt={})
    # JS receipt object is present, just minimal.
    assert "const receipt = {}" in html
