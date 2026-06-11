"""
Regression tests for plugins/agami/scripts/render_examples_validation.py.

The renderer reads shared/examples-validation-template.html, substitutes
placeholders, and writes a self-contained HTML file. Used by agami-connect
Phase 5 to render every seed example as a validation card.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from render_examples_validation import render  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")


def _example_unreviewed() -> dict:
    return {
        "n": 1,
        "question": "How many customers do we have?",
        "sql": "SELECT COUNT(*) AS customer_count FROM customers",
        "state": "unreviewed",
        "row_count": 1,
        "row_headers": ["customer_count"],
        "row_preview": [["5"]],
        "validated_by": None,
        "validated_at": None,
        "error": None,
    }


def _example_validated() -> dict:
    return {
        "n": 2,
        "question": "Top 5 customers by spend",
        "sql": "SELECT c.name, SUM(amount) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name ORDER BY spend DESC LIMIT 5",
        "state": "validated",
        "row_count": 5,
        "row_headers": ["name", "spend"],
        "row_preview": [
            ["Carol Chen", "148.95"],
            ["Dave Davis", "93.96"],
        ],
        "validated_by": "reviewer@example.com",
        "validated_at": "2026-05-10T14:30:00Z",
        "error": None,
    }


def _example_errored() -> dict:
    return {
        "n": 3,
        "question": "Active customers in last 30 days",
        "sql": "SELECT * FROM customers WHERE last_seen_at > DATE('now','-30 days')",
        "state": "unreviewed",
        "row_count": 0,
        "row_headers": [],
        "row_preview": [],
        "error": "no such column: last_seen_at",
    }


# --- Happy path -----------------------------------------------------------

def test_render_three_examples_no_unsubstituted_placeholders():
    html = render(
        title="Seed examples · default",
        profile="default",
        items=[_example_unreviewed(), _example_validated(), _example_errored()],
    )
    # The dashboard template has zero literal {{...}} markers (unlike the chart
    # template which has them in its doc-comment block), so any leftover is a
    # substitution miss.
    placeholders = PLACEHOLDER_RE.findall(html)
    assert placeholders == [], f"unsubstituted: {placeholders}"


def test_render_substitutes_profile_and_title():
    html = render(
        title="Seed examples · production",
        profile="production",
        items=[_example_unreviewed()],
    )
    assert "Seed examples · production" in html
    assert 'profile = "production"' in html


def test_render_includes_question_and_sql_payloads():
    html = render(
        title="x", profile="p",
        items=[_example_unreviewed(), _example_validated()],
    )
    assert "How many customers do we have?" in html
    assert "Top 5 customers by spend" in html
    # SQL fragments survive JSON encoding into the JS const.
    assert "SELECT COUNT(*)" in html
    assert "ORDER BY spend DESC" in html


def test_render_validated_state_carries_signoff():
    html = render(title="x", profile="p", items=[_example_validated()])
    assert "reviewer@example.com" in html
    assert '"state": "validated"' in html


def test_render_errored_example_includes_error_text():
    html = render(title="x", profile="p", items=[_example_errored()])
    assert "no such column: last_seen_at" in html


def test_render_empty_items_renders_fallback_card():
    html = render(title="x", profile="p", items=[])
    assert "const items = []" in html
    # The template's empty-state JS still renders a fallback card with guidance.
    assert 'id="items"' in html


# --- Validation guards ----------------------------------------------------

def test_render_rejects_invalid_state():
    bad = _example_unreviewed()
    bad["state"] = "in_progress"  # not in enum
    with pytest.raises(ValueError, match="state"):
        render(title="x", profile="p", items=[bad])


def test_render_rejects_missing_required_question():
    bad = {"n": 1, "sql": "SELECT 1"}  # no question
    with pytest.raises(ValueError, match="question"):
        render(title="x", profile="p", items=[bad])


def test_render_rejects_n_not_positive():
    bad = _example_unreviewed()
    bad["n"] = 0
    with pytest.raises(ValueError, match="n"):
        render(title="x", profile="p", items=[bad])


def test_render_rejects_row_preview_not_list_of_lists():
    bad = _example_unreviewed()
    bad["row_preview"] = ["not a list of lists"]
    with pytest.raises(ValueError, match="row_preview"):
        render(title="x", profile="p", items=[bad])


# --- Bulk operations smoke ------------------------------------------------

def test_render_handles_realistic_count():
    """Most introspects produce 10–15 seed examples; ensure we render them all."""
    items = []
    for i in range(15):
        e = _example_unreviewed()
        e["n"] = i + 1
        e["question"] = f"Question {i + 1}"
        items.append(e)
    html = render(title="x", profile="p", items=items)
    for i in range(1, 16):
        assert f"Question {i}" in html
