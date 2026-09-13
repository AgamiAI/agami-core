"""A window written against the clock (`>= DATE_TRUNC('year', CURRENT_DATE) + INTERVAL '7' MONTH`)
resolves to words, compares against another relative window, and never against a literal date."""

from __future__ import annotations

from semantic_model import golden_claims as gc


def _window(where: str, dialect: str = "postgres"):
    return gc.read_claims(f"SELECT COUNT(*) FROM orders o WHERE {where}", dialect=dialect).date_window


def test_a_relative_window_resolves_to_words_not_a_date():
    w = _window("o.order_date >= DATE_TRUNC('year', CURRENT_DATE) + INTERVAL '7' MONTH")
    assert w is not None and w.start == "start of this year + 7 month" and w.start_inclusive is True and w.end is None
    w = _window("o.order_date >= CURRENT_DATE - INTERVAL '30 days'")
    assert w is not None and w.start == "today - 30 day"
    w = _window("o.order_date < DATE_TRUNC('month', CURRENT_DATE)")
    assert w is not None and w.end == "start of this month" and w.end_inclusive is False


def test_two_statements_with_the_same_relative_window_agree_and_different_ones_differ():
    a = "SELECT COUNT(*) FROM orders o WHERE o.order_date >= DATE_TRUNC('year', CURRENT_DATE) + INTERVAL '7' MONTH"
    b = "SELECT COUNT(*) FROM orders WHERE order_date >= DATE_TRUNC('year', CURRENT_DATE) + INTERVAL '7 months'"
    c = "SELECT COUNT(*) FROM orders WHERE order_date >= DATE_TRUNC('year', CURRENT_DATE) + INTERVAL '5' MONTH"
    by = lambda d: {c.name: c.status for c in d.claims}  # noqa: E731
    assert by(gc.compare_statements(a, b, dialect="postgres"))["date_window"] == gc.AGREES
    assert by(gc.compare_statements(a, c, dialect="postgres"))["date_window"] == gc.DIFFERS


def test_a_relative_window_against_a_literal_date_reads_unknown_never_differs():
    a = "SELECT COUNT(*) FROM orders WHERE order_date >= DATE_TRUNC('year', CURRENT_DATE) + INTERVAL '5' MONTH"
    b = "SELECT COUNT(*) FROM orders WHERE order_date >= '2025-06-01'"
    diff = gc.compare_statements(a, b, dialect="postgres")
    assert {c.name: c.status for c in diff.claims}["date_window"] == gc.UNKNOWN
    assert not diff.gated


def test_a_shape_the_resolver_still_does_not_model_reads_none():
    assert _window("o.order_date >= NOW() - o.grace_period") is None
    assert _window("DATE_TRUNC('quarter', o.order_date) = '2025-04-01'") is None
