"""An unqualified column in a single-table statement reads as that table's column, so `ORDER BY opened`
and `ORDER BY r.opened` are one claim; with two tables in scope it stays bare."""

from __future__ import annotations

from semantic_model import golden_claims as gc


def _by_name(a: str, b: str, dialect: str = "postgres") -> dict:
    return {c.name: c.status for c in gc.compare_statements(a, b, dialect=dialect).claims}


def test_bare_and_qualified_spellings_agree_on_one_table():
    a = "SELECT number FROM requests r WHERE r.state = 'open' ORDER BY r.opened"
    b = "SELECT number FROM requests WHERE state = 'open' ORDER BY opened"
    by = _by_name(a, b)
    assert by["filter_predicates"] == gc.AGREES and by["ordering"] == gc.AGREES
    assert gc.read_claims(b, dialect="postgres").filter_predicates == frozenset({"eq(requests.state, 'open')"})


def test_with_two_tables_a_bare_column_stays_bare():
    sql = "SELECT r.number FROM requests r JOIN items i ON i.request_id = r.id WHERE state = 'open'"
    assert "eq(state, 'open')" in gc.read_claims(sql, dialect="postgres").filter_predicates  # bare: two tables could own it
