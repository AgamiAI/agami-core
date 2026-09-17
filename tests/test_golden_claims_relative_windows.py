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


def _status(a: str, b: str, dialect: str = "postgres") -> str:
    return {c.name: c.status for c in gc.compare_statements(a, b, dialect=dialect).claims}["date_window"]


def test_a_negative_count_folds_into_the_operator_so_two_spellings_agree_and_never_gate():
    q = "SELECT COUNT(*) FROM orders WHERE order_date >= {}"
    assert _status(q.format("CURRENT_DATE + INTERVAL '-30 days'"), q.format("CURRENT_DATE - INTERVAL '30 days'")) == gc.AGREES
    assert _status(q.format("DATE_ADD(CURDATE(), INTERVAL -30 DAY)"), q.format("CURDATE() - INTERVAL 30 DAY"), "mysql") == gc.AGREES
    assert _status(q.format("DATE_SUB(CURRENT_DATE(), INTERVAL -7 DAY)"), q.format("DATE_ADD(CURRENT_DATE(), INTERVAL 7 DAY)"), "bigquery") == gc.AGREES
    assert _status(q.format("DATEADD(month, -7, CURRENT_DATE)"), q.format("CURRENT_DATE - INTERVAL '7 months'"), "snowflake") == gc.AGREES
    assert _status(q.format("DATEADD(month, 7, CURRENT_DATE)"), q.format("CURRENT_DATE + INTERVAL '7 months'"), "redshift") == gc.AGREES
    assert _status(q.format("DATE_ADD(CURRENT_DATE, INTERVAL -30 DAY)"), q.format("CURRENT_DATE - INTERVAL 30 DAY"), "duckdb") == gc.AGREES
    for a, b in ((q.format("CURRENT_DATE + INTERVAL '-30 days'"), q.format("CURRENT_DATE - INTERVAL '30 days'")),):
        assert not gc.compare_statements(a, b, dialect="postgres").gated


def test_exactly_convertible_units_compare_alike_and_inexact_ones_do_not():
    q = "SELECT COUNT(*) FROM orders WHERE order_date >= CURRENT_DATE - INTERVAL {}"
    assert _status(q.format("'4 weeks'"), q.format("'28 days'")) == gc.AGREES
    assert _status(q.format("'1 year'"), q.format("'12 months'")) == gc.AGREES
    assert _status(q.format("'1 quarter'"), q.format("'3 months'")) == gc.AGREES
    assert _status(q.format("'1 month'"), q.format("'30 days'")) == gc.DIFFERS


def test_now_against_today_reads_unknown_except_under_a_truncation():
    q = "SELECT COUNT(*) FROM orders WHERE order_date >= {}"
    assert _status(q.format("NOW() - INTERVAL '30 days'"), q.format("CURRENT_DATE - INTERVAL '30 days'")) == gc.UNKNOWN
    assert _status(q.format("DATE_TRUNC('month', NOW())"), q.format("DATE_TRUNC('month', CURRENT_DATE)")) == gc.AGREES
    assert _status(q.format("NOW() - INTERVAL 7 MONTH"), q.format("CURRENT_TIMESTAMP - INTERVAL 7 MONTH"), "mysql") == gc.AGREES
    assert _window("o.order_date >= NOW() - INTERVAL '30 days'").start == "now - 30 day"


def test_between_reads_relative_bounds_and_a_two_digit_year_stays_a_date():
    a = "SELECT COUNT(*) FROM orders WHERE order_date BETWEEN DATE_TRUNC('year', CURRENT_DATE) AND CURRENT_DATE"
    b = "SELECT COUNT(*) FROM orders WHERE order_date >= DATE_TRUNC('year', CURRENT_DATE) AND order_date <= CURRENT_DATE"
    assert _status(a, b) == gc.AGREES
    w = gc.read_claims("SELECT COUNT(*) FROM orders WHERE EXTRACT(YEAR FROM order_date) = 25", dialect="postgres").date_window
    assert w is not None and w.start == "0025-01-01" and not gc._symbolic(w.start)


def test_a_pathological_relative_bound_reads_none_and_never_raises():
    deep = "SELECT x FROM t WHERE d >= CURRENT_DATE" + " + INTERVAL '1' DAY" * 3000
    assert gc.read_claims(deep, dialect="postgres").date_window is None
    nested = "SELECT x FROM t WHERE d >= " + "CAST(" * 3000 + "CURRENT_DATE" + " AS DATE)" * 3000
    claims = gc.read_claims(nested, dialect="postgres")
    assert claims.date_window is None
    # a window a handful of steps deep still folds
    assert _window("o.order_date >= (CURRENT_DATE - INTERVAL '1' DAY) - INTERVAL '2' DAY").start == "today - 1 day - 2 day"

