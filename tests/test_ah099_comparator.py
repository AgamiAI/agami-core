"""A result-set cell is compared through a canonical key, never as the driver handed it over.

Two result sets that say the same thing arrive as different Python objects: one driver returns
``Decimal('5.00')`` where another returns ``5``, one attaches UTC where another does not, and an
answer key read out of YAML is text where the live run is a ``datetime``. Comparing those raw is
not merely imprecise, it is wrong in ways that silently pass:

* ``True == 1 == 1.0 == Decimal(1)`` and all four share a hash, so a ``Counter`` collapses a
  boolean column onto an integer column of zeros and ones and reports them identical.
* ``float('nan') != float('nan')``, but dict's identity fast-path makes the *same* NaN object
  collapse anyway — so a row count would depend on whether the driver reused an object.
* ``Decimal('0.1') != 0.1``, so the same number read through two drivers disagrees.

These tests pin the canonical keys that close all three, and the deliberate choices made where
more than one answer was defensible. Synthetic values only — nothing here names real data.
"""

from __future__ import annotations

import dataclasses
import pickle
import random
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from execute_sql import ExecResult  # noqa: E402
from semantic_model import comparator as c  # noqa: E402
from semantic_model.golden import GoldenBounds  # noqa: E402

# --- numbers -------------------------------------------------------------------------------


def test_int_and_padded_decimal_share_a_key():
    # `unit_price` comes back as REAL from SQLite and as NUMERIC from Postgres; the answer key
    # would never match the run if the trailing zeros counted.
    assert c.canonical_cell(5) == c.canonical_cell(Decimal("5.00"))
    assert c.canonical_cell(5) == c.canonical_cell(5.0)


def test_decimal_and_float_of_the_same_number_share_a_key():
    # Decimal(0.1) is 0.1000000000000000055…; only Decimal(repr(0.1)) lands on Decimal('0.1').
    assert c.canonical_cell(Decimal("0.1")) == c.canonical_cell(0.1)


def test_normalize_survives_the_exponent_form():
    # Decimal('100').normalize() is Decimal('1E+2'), which is a different repr but the same value
    # and the same hash — so the key still compares equal to a plain 100.
    assert c.canonical_cell(100) == c.canonical_cell(Decimal("100.000"))
    assert hash(c.canonical_cell(100)) == hash(c.canonical_cell(Decimal("100.000")))


def test_a_number_is_tagged_num():
    assert c.canonical_cell(7)[0] == "num"
    assert c.canonical_cell(Decimal("-2.5"))[0] == "num"


def test_negative_zero_matches_zero():
    assert c.canonical_cell(-0.0) == c.canonical_cell(0)


def test_number_keys_are_immune_to_the_ambient_decimal_context():
    # The decimal context is process-global and any caller can lower its precision; a key that
    # moved with it would compare two identical runs as different.
    import decimal

    saved = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 3
        narrowed = c.canonical_cell(Decimal("1.23456789012345"))
    finally:
        decimal.getcontext().prec = saved
    assert narrowed == c.canonical_cell(Decimal("1.23456789012345"))


# --- non-finite numbers --------------------------------------------------------------------


def test_two_independent_nans_share_one_bucket():
    first, second = float("nan"), float("nan")
    assert first is not second and first != second
    assert c.canonical_cell(first) == c.canonical_cell(second)
    counted = Counter([c.canonical_cell(first), c.canonical_cell(second)])
    assert list(counted.values()) == [2]


def test_decimal_nan_matches_float_nan():
    assert c.canonical_cell(Decimal("NaN")) == c.canonical_cell(float("nan"))
    assert c.canonical_cell(float("nan"))[0] == "nan"


def test_infinities_keep_their_sign_and_stay_numbers():
    assert c.canonical_cell(float("inf"))[0] == "num"
    assert c.canonical_cell(float("inf")) != c.canonical_cell(float("-inf"))
    assert c.canonical_cell(float("inf")) == c.canonical_cell(Decimal("Infinity"))
    assert c.canonical_cell(float("-inf")) == c.canonical_cell(Decimal("-Infinity"))


def test_nan_is_not_infinity():
    assert c.canonical_cell(float("nan")) != c.canonical_cell(float("inf"))


# --- quantize ------------------------------------------------------------------------------


def test_quantize_buckets_a_twelfth_digit_difference_together():
    left = c.canonical_cell(Decimal("1.00000000001"), quantize=True)
    right = c.canonical_cell(Decimal("1.0"), quantize=True)
    assert left == right


def test_quantize_keeps_a_fifth_digit_difference_apart():
    left = c.canonical_cell(Decimal("1.0001"), quantize=True)
    right = c.canonical_cell(Decimal("1.0002"), quantize=True)
    assert left != right


def test_quantize_leaves_non_numbers_alone():
    for value in ("2025-01-01", "shipped", True, None, float("nan"), float("inf")):
        assert c.canonical_cell(value, quantize=True) == c.canonical_cell(value)


def test_quantize_is_off_by_default():
    assert c.canonical_cell(Decimal("1.00000000001")) != c.canonical_cell(Decimal("1.0"))


# --- booleans ------------------------------------------------------------------------------


def test_bool_and_int_do_not_collide():
    # The one that silently breaks a Counter: `is_active` as a real boolean against `is_active`
    # as the 0/1 integer SQLite stores must not read as the same column.
    assert c.canonical_cell(True) != c.canonical_cell(1)
    assert c.canonical_cell(False) != c.canonical_cell(0)
    assert Counter([c.canonical_cell(True), c.canonical_cell(1)]).total() == 2
    assert len(Counter([c.canonical_cell(True), c.canonical_cell(1)])) == 2


@pytest.mark.parametrize(
    "spelling, expected",
    [
        ("t", True),
        ("T", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("Yes", True),
        ("f", False),
        ("false", False),
        ("FALSE", False),
        ("no", False),
        ("NO", False),
    ],
)
def test_boolean_spellings_become_bools(spelling, expected):
    assert c.canonical_cell(spelling) == ("bool", expected)


def test_a_boolean_spelling_agrees_with_the_bool():
    assert c.canonical_cell("t") == c.canonical_cell(True)
    assert c.canonical_cell("no") == c.canonical_cell(False)
    assert c.canonical_cell("t") != c.canonical_cell("f")


def test_a_word_that_merely_starts_like_one_is_text():
    assert c.canonical_cell("nope") == ("text", "nope")
    assert c.canonical_cell("truest") == ("text", "truest")


# --- dates ---------------------------------------------------------------------------------


def test_date_datetime_and_iso_text_share_a_key():
    assert c.canonical_cell(date(2025, 1, 1)) == c.canonical_cell(datetime(2025, 1, 1))
    assert c.canonical_cell(date(2025, 1, 1)) == c.canonical_cell("2025-01-01")
    assert c.canonical_cell(date(2025, 1, 1))[0] == "date"


def test_a_datetime_with_a_time_is_not_the_bare_date():
    assert c.canonical_cell(datetime(2025, 1, 1, 9, 30)) != c.canonical_cell(date(2025, 1, 1))


def test_a_trailing_z_parses():
    # Python 3.10's fromisoformat raises on a trailing `Z`, and this repo still supports 3.10.
    assert c.canonical_cell("2025-01-01T00:00:00Z") == c.canonical_cell(date(2025, 1, 1))


def test_a_space_separated_timestamp_parses():
    # What SQLite stores in `placed_at`.
    assert c.canonical_cell("2025-03-04 09:30:00") == c.canonical_cell(datetime(2025, 3, 4, 9, 30))


def test_fractional_seconds_parse():
    assert c.canonical_cell("2025-03-04T09:30:00.500000") == c.canonical_cell(
        datetime(2025, 3, 4, 9, 30, 0, 500000)
    )


def test_an_aware_datetime_is_read_as_an_instant_in_utc():
    # The deliberate choice: an aware value is converted to UTC and its offset dropped, so a naive
    # datetime is read as a UTC wall clock. One driver attaches tzinfo to a timestamp column where
    # another does not, and an answer key authored as text carries no offset at all — splitting
    # those apart would fail every case that crosses a driver.
    aware = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert c.canonical_cell(aware) == c.canonical_cell(datetime(2025, 1, 1, 12, 0))
    shifted = datetime(2025, 1, 1, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert c.canonical_cell(shifted) == c.canonical_cell(datetime(2025, 1, 1, 12, 0))


def test_an_aware_midnight_utc_matches_the_date():
    aware = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert c.canonical_cell(aware) == c.canonical_cell(date(2025, 1, 1))


@pytest.mark.parametrize(
    "not_a_date",
    [
        "00000000000042",
        "2025",
        "20250101",
        "2025-1-1",
        "x2025-01-01",
        "2025-01-01x",
        "2025-01-01T00:00:00Z ",
        "the 2025-01-01 order",
    ],
)
def test_a_string_that_is_not_strictly_date_shaped_stays_text(not_a_date):
    # A zero-padded identifier is the case that matters: coerced to a date it would compare equal
    # to some other identifier that happens to round to the same day.
    assert c.canonical_cell(not_a_date) == ("text", not_a_date)


def test_a_date_shaped_string_that_is_not_a_real_day_stays_text():
    assert c.canonical_cell("2025-13-45") == ("text", "2025-13-45")


# --- text and nulls ------------------------------------------------------------------------


def test_null_is_not_the_empty_string():
    assert c.canonical_cell(None) == ("null", None)
    assert c.canonical_cell(None) != c.canonical_cell("")
    assert c.canonical_cell("") == ("text", "")


def test_text_is_neither_stripped_nor_case_folded():
    # A comparator that folded these would hide a real difference between two result sets.
    assert c.canonical_cell(" a ") != c.canonical_cell("a")
    assert c.canonical_cell("A") != c.canonical_cell("a")
    assert c.canonical_cell(" a ") == ("text", " a ")


def test_an_unrecognised_object_becomes_its_text():
    assert c.canonical_cell(b"paid") == ("text", str(b"paid"))
    assert c.canonical_cell(["web", "store"]) == ("text", str(["web", "store"]))


def test_a_numeric_string_is_not_coerced_to_a_number():
    # Coercion here would make a text column of order ids compare equal to a numeric one.
    assert c.canonical_cell("5") == ("text", "5")
    assert c.canonical_cell("5") != c.canonical_cell(5)


# --- rows ----------------------------------------------------------------------------------


def test_canonical_row_canonicalises_every_cell():
    row = c.canonical_row(("web", 5, None, date(2025, 1, 1)))
    assert row == (("text", "web"), ("num", Decimal("5")), ("null", None), ("date", "2025-01-01"))


def test_canonical_row_forwards_quantize():
    left = c.canonical_row((Decimal("1.00000000001"),), quantize=True)
    right = c.canonical_row((Decimal("1.0"),), quantize=True)
    assert left == right


def test_two_rows_that_agree_land_in_one_counter_bucket():
    counted = Counter([c.canonical_row(("web", 5)), c.canonical_row(("web", Decimal("5.00")))])
    assert list(counted.values()) == [2]


# --- the coarse type lattice ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        (True, "bool"),
        ("t", "bool"),
        (5, "number"),
        (Decimal("1.5"), "number"),
        (float("nan"), "number"),
        (float("inf"), "number"),
        (date(2025, 1, 1), "date"),
        ("2025-01-01", "date"),
        (datetime(2025, 1, 1, 9, 30), "date"),
        ("shipped", "text"),
        ("", "text"),
        (b"paid", "text"),
    ],
)
def test_cell_type_over_every_tag(value, expected):
    assert c.cell_type(c.canonical_cell(value)) == expected


def test_a_null_contributes_no_type():
    assert c.cell_type(c.canonical_cell(None)) is None


# --- hashability ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        "t",
        5,
        5.0,
        Decimal("5.00"),
        float("nan"),
        float("inf"),
        float("-inf"),
        date(2025, 1, 1),
        datetime(2025, 1, 1, 9, 30),
        "2025-01-01",
        "",
        " a ",
        b"paid",
        ["web", "store"],
        {"channel": "web"},
    ],
)
def test_every_canonical_cell_is_hashable(value):
    key = c.canonical_cell(value)
    assert isinstance(key, tuple) and len(key) == 2
    hash(key)
    assert Counter([key])[key] == 1


# --- is the result ordered? ------------------------------------------------------------------


def test_a_top_level_order_by_is_ordered():
    assert c.has_top_level_order_by("SELECT a FROM t ORDER BY a") == (True, None)


def test_a_subquery_order_by_is_not_ordered():
    # The inner ORDER BY orders the input to the outer SELECT, which is then free to return its
    # rows in any order. `find(exp.Order)` would read this as ordered; `args["order"]` does not.
    ordered, note = c.has_top_level_order_by("SELECT a FROM (SELECT a FROM t ORDER BY a) x")
    assert (ordered, note) == (False, None)


def test_a_window_order_by_is_not_ordered():
    sql = "SELECT a, ROW_NUMBER() OVER (ORDER BY a) FROM t"
    assert c.has_top_level_order_by(sql) == (False, None)


def test_a_cte_order_by_is_not_ordered():
    sql = "WITH c AS (SELECT a FROM t ORDER BY a) SELECT a FROM c"
    assert c.has_top_level_order_by(sql) == (False, None)


def test_an_aggregate_order_by_is_not_ordered():
    # `array_agg(a ORDER BY a)` orders what goes INTO one cell, not the rows that come out.
    assert c.has_top_level_order_by("SELECT array_agg(a ORDER BY a) FROM t") == (False, None)


def test_order_by_with_a_limit_is_ordered():
    assert c.has_top_level_order_by("SELECT a FROM t ORDER BY a LIMIT 10") == (True, None)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t ORDER BY a;",
        "-- the answer key\nSELECT a FROM t ORDER BY a",
        "   \n\tSELECT a FROM t ORDER BY a",
        "select a from t order by a",
    ],
)
def test_incidental_spelling_does_not_hide_the_ordering(sql):
    assert c.has_top_level_order_by(sql) == (True, None)


def test_a_union_ordered_as_a_whole_is_ordered():
    # The ORDER BY hangs off the Union node here, which `args["order"]` reads because it is asked
    # of whatever the top-level node turned out to be rather than of a Select.
    sql = "SELECT a FROM t UNION SELECT b FROM u ORDER BY 1"
    assert c.has_top_level_order_by(sql) == (True, None)


def test_a_union_arm_ordered_alone_is_not_ordered():
    # Ordering one arm is not a total order of the result the caller receives.
    sql = "SELECT a FROM t ORDER BY a UNION SELECT b FROM u"
    assert c.has_top_level_order_by(sql) == (False, None)


@pytest.mark.parametrize("sql", ["not sql at all", "SELECT FROM", "SELECT 'unterminated"])
def test_an_unreadable_statement_is_assumed_ordered(sql):
    # Deliberate: assuming UNORDERED would silently stop checking an ordering the author asked
    # for, and a visible false failure is recoverable where a silent weakening is not.
    ordered, note = c.has_top_level_order_by(sql)
    assert ordered is True
    assert note


@pytest.mark.parametrize("sql", [None, "", "   \n\t "])
def test_a_missing_statement_is_assumed_ordered(sql):
    # None is guarded separately from the parse: sqlglot raises TypeError on it, not a
    # SqlglotError, so it would escape the except clause.
    ordered, note = c.has_top_level_order_by(sql)
    assert ordered is True
    assert note


def test_a_backtick_statement_needs_its_dialect():
    # The dialect earns its place here: read with the generic grammar the same statement does not
    # parse at all, and the case would silently fall to the assumed-ordered note.
    sql = "SELECT `a` FROM `t` ORDER BY `a`"
    generic_ordered, generic_note = c.has_top_level_order_by(sql)
    assert generic_ordered is True
    assert generic_note
    assert c.has_top_level_order_by(sql, dialect="mysql") == (True, None)


def test_a_dialect_parse_still_reads_an_unordered_statement():
    # ...and the dialect path is not just returning True for everything.
    sql = "SELECT `a` FROM `t`"
    assert c.has_top_level_order_by(sql, dialect="mysql") == (False, None)


# --- matching columns by their values --------------------------------------------------------


def test_columns_in_a_different_order_match():
    pairing, unmatched = c.match_columns(
        ["channel", "orders"],
        [("web", 1), ("store", 2)],
        ["orders", "channel"],
        [(1, "web"), (2, "store")],
        ordered=True,
    )
    assert pairing == {0: 1, 1: 0}
    assert unmatched == ()


def test_columns_with_different_names_match():
    # A generated statement is free to alias the total; it still answered the question.
    pairing, unmatched = c.match_columns(
        ["orders"], [(1,), (2,)], ["order_count"], [(1,), (2,)], ordered=True
    )
    assert pairing == {0: 0}
    assert unmatched == ()


def test_a_golden_column_with_no_partner_is_named():
    pairing, unmatched = c.match_columns(
        ["channel", "revenue"],
        [("web", 10), ("store", 20)],
        ["channel"],
        [("web",), ("store",)],
        ordered=True,
    )
    assert pairing == {0: 0}
    assert unmatched == ("revenue",)


def test_identical_value_vectors_do_not_collapse_onto_one_partner():
    # The case a careless pairing gets wrong: two golden columns carrying the same values must
    # take two DIFFERENT partners, and the complete matching has to be found even though the
    # duplicate generated columns are not the first candidates encountered.
    golden_rows = [(1, "web", 1), (2, "store", 2)]
    generated_rows = [("web", 1, 1), ("store", 2, 2)]
    pairing, unmatched = c.match_columns(
        ["first", "channel", "second"],
        golden_rows,
        ["channel", "left", "right"],
        generated_rows,
        ordered=True,
    )
    assert unmatched == ()
    assert len(pairing) == 3
    # ...and no generated column was handed to two golden columns.
    assert len(set(pairing.values())) == 3
    assert pairing[1] == 0


def test_a_duplicated_golden_column_leaves_one_unmatched():
    # Two golden columns of the same values against one generated column: exactly one pairs, and
    # the other is named rather than quietly sharing the partner.
    pairing, unmatched = c.match_columns(
        ["a", "b"], [(1, 1), (2, 2)], ["only"], [(1,), (2,)], ordered=True
    )
    assert len(pairing) == 1
    assert len(unmatched) == 1
    assert unmatched[0] in ("a", "b")


def test_a_bool_column_pairs_with_the_int_column_of_its_name_and_agrees_on_no_row():
    # Slice 1's tags exist for this: `is_active` as a real boolean against the 0/1 SQLite stores
    # is a different answer, and raw equality would have called it a match. The two columns share
    # a name, so they are paired as the same column; the agreement says every row differs, and the
    # score then reads "0 of 2 rows matched" rather than "no generated column carries is_active".
    paired = c.pair_columns(
        ["is_active"], [(True,), (False,)], ["is_active"], [(1,), (0,)], ordered=True, quantize=False
    )
    assert paired.pairing == {0: 0} and paired.unmatched == ()
    assert paired.agreement == {0: 0.0}


def test_column_values_in_a_different_row_order_match_when_unordered():
    pairing, unmatched = c.match_columns(
        ["channel"], [("web",), ("store",)], ["channel"], [("store",), ("web",)], ordered=False
    )
    assert pairing == {0: 0}
    assert unmatched == ()


def test_column_values_in_a_different_row_order_pair_by_name_and_disagree_row_for_row_when_ordered():
    # The column is there under its own name, so it pairs; the rows then disagree position by
    # position, which is what an ordered comparison is for. The score says so in rows, not columns.
    pairing, unmatched = c.match_columns(
        ["channel"], [("web",), ("store",)], ["channel"], [("store",), ("web",)], ordered=True
    )
    assert pairing == {0: 0} and unmatched == ()
    golden = c.ExecResult(columns=["channel"], rows=[("web",), ("store",)])
    generated = c.ExecResult(columns=["channel"], rows=[("store",), ("web",)])
    score = c.compare_result_sets(golden, generated, golden_sql="SELECT channel FROM t ORDER BY channel")
    assert score.accuracy == 0.0 and score.reason.startswith("0 of the answer key's 2 rows matched")
    assert score.column_pairs == (("channel", "channel"),) and score.column_agreement == (0.0,)


def test_a_mixed_type_column_sorts_without_raising_when_unordered():
    # Sorting the raw cells here raises: a naive datetime does not compare with an aware one, and
    # a Decimal does not compare with a string. The sort key is derived from the canonical form.
    left = [(datetime(2025, 1, 1, 9, 30),), (None,), ("web",), (5,)]
    right = [(5,), ("web",), (datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc),), (None,)]
    pairing, unmatched = c.match_columns(["mixed"], left, ["mixed"], right, ordered=False)
    assert pairing == {0: 0}
    assert unmatched == ()


def test_matching_forwards_quantize():
    golden_rows = [(Decimal("1.00000000001"),)]
    generated_rows = [(Decimal("1.0"),)]
    # Under another name the two pair only when quantize makes the cells equal...
    assert c.match_columns(["v"], golden_rows, ["w"], generated_rows, ordered=True)[0] == {}
    assert c.match_columns(["v"], golden_rows, ["w"], generated_rows, ordered=True, quantize=True)[0] == {0: 0}
    # ...and under the same name they pair either way, quantize deciding whether the one row agrees.
    strict = c.pair_columns(["v"], golden_rows, ["v"], generated_rows, ordered=True, quantize=False)
    loose = c.pair_columns(["v"], golden_rows, ["v"], generated_rows, ordered=True, quantize=True)
    assert strict.agreement == {0: 0.0} and loose.agreement == {0: 1.0}


def test_matching_a_ragged_row_is_surfaced_as_this_module_s_error():
    with pytest.raises(c.RaggedRow):
        c.match_columns(["a", "b"], [(1,)], ["a", "b"], [(1, 2)], ordered=True)


# --- comparing the rows ----------------------------------------------------------------------


def test_rows_are_projected_onto_the_pairing():
    # The generated side carries an extra column and the matched one sits elsewhere; only the
    # paired columns are compared, in golden column order.
    golden_rows = [("web", 1), ("store", 2)]
    generated_rows = [(99, 1, "web"), (99, 2, "store")]
    assert c.compare_rows(golden_rows, generated_rows, {0: 2, 1: 1}, ordered=True) == (2, 2, 2)


def test_row_order_is_irrelevant_when_unordered():
    golden_rows = [("web", 1), ("store", 2)]
    generated_rows = [("store", 2), ("web", 1)]
    pairing = {0: 0, 1: 1}
    assert c.compare_rows(golden_rows, generated_rows, pairing, ordered=False) == (2, 2, 2)


def test_row_order_is_decisive_when_ordered():
    golden_rows = [("web", 1), ("store", 2)]
    generated_rows = [("store", 2), ("web", 1)]
    pairing = {0: 0, 1: 1}
    assert c.compare_rows(golden_rows, generated_rows, pairing, ordered=True) == (0, 2, 2)


def test_duplicate_rows_count_as_a_multiset_not_a_set():
    # A set would say these two agree once; they agree twice, and the duplicate is signal.
    pairing = {0: 0}
    assert c.compare_rows([("web",), ("web",)], [("web",), ("web",)], pairing, ordered=False) == (
        2,
        2,
        2,
    )


def test_a_duplicate_on_one_side_only_reduces_the_overlap():
    pairing = {0: 0}
    assert c.compare_rows([("web",), ("web",)], [("web",)], pairing, ordered=False) == (1, 2, 1)
    assert c.compare_rows([("web",)], [("web",), ("web",)], pairing, ordered=False) == (1, 1, 2)


def test_partial_overlap_returns_both_counts():
    golden_rows = [("web",), ("store",), ("kiosk",)]
    generated_rows = [("kiosk",), ("web",), ("phone",)]
    assert c.compare_rows(golden_rows, generated_rows, {0: 0}, ordered=False) == (2, 3, 3)


def test_a_shorter_generated_side_matches_only_where_it_reaches_when_ordered():
    golden_rows = [("web",), ("store",), ("kiosk",)]
    assert c.compare_rows(golden_rows, [("web",), ("store",)], {0: 0}, ordered=True) == (2, 3, 2)


def test_nan_rows_count_deterministically():
    # Two independently-produced NaNs are unequal and would never meet in a raw comparison; the
    # canonical bucket makes the count depend on the values rather than on object identity.
    golden_rows = [(float("nan"),), (float("nan"),)]
    generated_rows = [(float("nan"),), (float("nan"),)]
    assert c.compare_rows(golden_rows, generated_rows, {0: 0}, ordered=False) == (2, 2, 2)
    assert c.compare_rows(golden_rows, [(float("nan"),)], {0: 0}, ordered=False) == (1, 2, 1)


def test_comparing_forwards_quantize():
    golden_rows = [(Decimal("1.00000000001"),)]
    generated_rows = [(Decimal("1.0"),)]
    assert c.compare_rows(golden_rows, generated_rows, {0: 0}, ordered=False) == (0, 1, 1)
    assert c.compare_rows(golden_rows, generated_rows, {0: 0}, ordered=False, quantize=True) == (
        1,
        1,
        1,
    )


def test_comparing_a_ragged_row_is_surfaced_as_this_module_s_error():
    # `ExecResult` does not validate that a row is as wide as its column list, so a short row can
    # reach here; it must arrive as this module's own error rather than an IndexError from a
    # projection, which would say nothing about which side was malformed.
    with pytest.raises(c.RaggedRow):
        c.compare_rows([("web", 1)], [("web",)], {0: 0, 1: 1}, ordered=False)


# --- the public entry point ------------------------------------------------------------------
#
# One call decides a whole item: whether the answer key ordered its rows, which generated column
# answers which golden one, and how far the rows agree — reported as an `ItemScore` that carries
# verdicts and counts and never a cell.

# An answer key that does not order its rows, so a comparison here is a multiset comparison.
_UNORDERED = "SELECT channel, orders FROM orders_by_channel"
# ...and the same statement with the ordering the author asked for.
_ORDERED = "SELECT channel, orders FROM orders_by_channel ORDER BY orders"


def _res(columns, rows) -> ExecResult:
    """An ExecResult from literals, so a test reads as the table it is describing."""
    return ExecResult(columns=list(columns), rows=[tuple(row) for row in rows])


def test_columns_in_a_different_order_still_score_a_full_match():
    golden = _res(["channel", "orders"], [("web", 1), ("store", 2)])
    generated = _res(["orders", "channel"], [(1, "web"), (2, "store")])
    score = c.compare_result_sets(golden, generated, golden_sql=_UNORDERED)
    assert score.status == "scored"
    assert score.accuracy == 1.0
    assert score.unmatched_golden_columns == ()


def test_columns_with_different_names_still_score_a_full_match():
    golden = _res(["orders"], [(1,), (2,)])
    generated = _res(["order_count"], [(1,), (2,)])
    assert c.compare_result_sets(golden, generated, golden_sql=_UNORDERED).accuracy == 1.0


def test_columns_renamed_and_reordered_together_still_score_a_full_match():
    # The case a name-based or position-based comparison gets wrong in both directions at once.
    golden = _res(["channel", "orders"], [("web", 1), ("store", 2)])
    generated = _res(["order_count", "sales_channel"], [(1, "web"), (2, "store")])
    assert c.compare_result_sets(golden, generated, golden_sql=_UNORDERED).accuracy == 1.0


def test_an_unmatched_golden_column_fails_and_is_named():
    # Every row the generated statement did return is right, and it is still the wrong answer: a
    # column the question asked for is missing, so the score is a real 0.0 rather than the 1.0 the
    # row overlap alone would have reported.
    golden = _res(["channel", "revenue"], [("web", 10), ("store", 20)])
    generated = _res(["channel"], [("web",), ("store",)])
    score = c.compare_result_sets(golden, generated, golden_sql=_UNORDERED)
    assert score.status == "scored"
    assert score.accuracy == 0.0
    assert score.unmatched_golden_columns == ("revenue",)
    assert "revenue" in score.reason


def test_an_extra_generated_column_passes_values_and_fails_exact():
    golden = _res(["channel"], [("web",), ("store",)])
    generated = _res(["channel", "orders"], [("web", 1), ("store", 2)])
    loose = c.compare_result_sets(golden, generated, match="values", golden_sql=_UNORDERED)
    assert loose.accuracy == 1.0
    strict = c.compare_result_sets(golden, generated, match="exact", golden_sql=_UNORDERED)
    assert strict.status == "scored"
    assert strict.accuracy == 0.0


def test_two_empty_result_sets_are_unscored():
    # Deliberately not a pass. Two empty results agree about nothing: the comparison checked no
    # value, and reporting that as a full match is how a broken statement scores like a right one.
    score = c.compare_result_sets(
        _res(["channel"], []), _res(["channel"], []), golden_sql=_UNORDERED
    )
    assert score.status == "unscored"
    assert score.accuracy is None
    assert score.reason


def test_row_order_is_irrelevant_without_an_order_by_and_decisive_with_one():
    golden = _res(["channel"], [("web",), ("store",)])
    generated = _res(["channel"], [("store",), ("web",)])
    loose = c.compare_result_sets(golden, generated, golden_sql=_UNORDERED)
    assert (loose.order_sensitive, loose.accuracy) == (False, 1.0)
    strict = c.compare_result_sets(golden, generated, golden_sql=_ORDERED)
    assert (strict.order_sensitive, strict.accuracy) == (True, 0.0)


def test_the_ordering_is_read_from_the_answer_key_alone():
    # The generated statement dropped the ORDER BY and returned the same rows shuffled. This
    # function never sees that statement — which is the point: ordering is what the AUTHOR asked
    # for, so the case is still judged order-sensitively and still fails.
    golden = _res(["orders"], [(1,), (2,), (3,)])
    generated = _res(["orders"], [(3,), (1,), (2,)])
    score = c.compare_result_sets(golden, generated, golden_sql=_ORDERED)
    assert score.order_sensitive is True
    assert score.accuracy == 0.0


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT channel FROM (SELECT channel FROM t ORDER BY channel) x",
        "SELECT channel, ROW_NUMBER() OVER (ORDER BY channel) FROM t",
    ],
)
def test_an_order_by_below_the_top_level_leaves_the_result_unordered(sql):
    golden = _res(["channel"], [("web",), ("store",)])
    generated = _res(["channel"], [("store",), ("web",)])
    score = c.compare_result_sets(golden, generated, golden_sql=sql)
    assert score.order_sensitive is False
    assert score.accuracy == 1.0


def test_a_row_duplicated_on_one_side_only_fails():
    # A fan-out returns the web row twice. The duplicate changes the column's values, so the
    # column finds no partner at all and the item scores zero — both row counts are on the score
    # for whoever reads it.
    golden = _res(["channel"], [("web",), ("store",)])
    generated = _res(["channel"], [("web",), ("web",), ("store",)])
    score = c.compare_result_sets(golden, generated, golden_sql=_UNORDERED)
    assert score.accuracy == 0.0
    assert (score.golden_row_count, score.generated_row_count) == (2, 3)


def test_partial_overlap_scores_between_zero_and_one_and_reports_both_counts():
    # Both columns pair, and the ROWS still disagree: the generated statement attached the wrong
    # order count to two of the three channels.
    golden = _res(["channel", "orders"], [("web", 1), ("store", 2), ("kiosk", 3)])
    generated = _res(["channel", "orders"], [("web", 1), ("store", 3), ("kiosk", 2)])
    score = c.compare_result_sets(golden, generated, golden_sql=_UNORDERED)
    assert 0.0 < score.accuracy < 1.0
    assert score.accuracy == pytest.approx(1 / 3)
    assert (score.golden_row_count, score.generated_row_count) == (3, 3)


def test_a_near_miss_over_a_wide_result_cannot_round_up_to_a_pass():
    # An item passes at exactly 1.0, and 4002 of 4004 rows is 0.99950… — a result that disagreed
    # about a row has to stay below the gate however wide it is. The score is the raw share, so
    # there is nothing left to round it up; three-decimal presentation is the report's business.
    size = 4004
    rows = [(index, index) for index in range(size)]
    swapped = list(rows)
    swapped[0], swapped[1] = (0, 1), (1, 0)
    score = c.compare_result_sets(
        _res(["id", "n"], rows), _res(["id", "n"], swapped), golden_sql=_UNORDERED
    )
    assert score.accuracy < 1.0
    assert score.accuracy == pytest.approx(4002 / size)


def test_the_canonical_keys_reach_the_public_comparison():
    # Slice 1's three cases end to end: a padded decimal, a date written as text, and a
    # zero-padded identifier that must stay text rather than being read as a day.
    golden = _res(["total", "day", "account"], [(5, date(2025, 1, 1), "00000000000042")])
    generated = _res(
        ["total", "day", "account"], [(Decimal("5.00"), "2025-01-01", "00000000000042")]
    )
    score = c.compare_result_sets(golden, generated, golden_sql=_UNORDERED)
    assert score.accuracy == 1.0
    assert score.unmatched_golden_columns == ()


def test_a_null_does_not_match_the_empty_string_end_to_end():
    golden = _res(["note"], [(None,)])
    generated = _res(["note"], [("",)])
    score = c.compare_result_sets(golden, generated, golden_sql=_UNORDERED)
    assert score.accuracy == 0.0
    # The column pairs by its name and the one row disagrees: a null is not an empty string.
    assert score.column_pairs == (("note", "note"),) and score.column_agreement == (0.0,)
    assert score.unmatched_golden_columns == ()


def test_a_boolean_agrees_with_its_text_spelling_but_never_with_an_int():
    golden = _res(["is_active"], [(True,), (False,)])
    spelled = c.compare_result_sets(
        golden, _res(["is_active"], [("t",), ("f",)]), golden_sql=_UNORDERED
    )
    assert spelled.accuracy == 1.0
    stored = c.compare_result_sets(golden, _res(["is_active"], [(1,), (0,)]), golden_sql=_UNORDERED)
    assert stored.accuracy == 0.0
    # Paired by name, agreeing on no row: the difference is in every value, not in a missing column.
    assert stored.column_pairs == (("is_active", "is_active"),) and stored.column_agreement == (0.0,)
    assert stored.unmatched_golden_columns == ()


# --- the five levels -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level, golden, generated, bounds, expected",
    [
        ("exact", _res(["orders"], [(1,), (2,)]), _res(["orders"], [(1,), (2,)]), None, 1.0),
        ("exact", _res(["orders"], [(1,), (2,)]), _res(["orders"], [(1,), (9,)]), None, 0.5),  # same name, one of two rows agrees: the share, not a missing column
        # A twelfth-digit difference is inside `values`' tolerance and outside `exact`'s.
        (
            "values",
            _res(["total"], [(Decimal("1.00000000001"),)]),
            _res(["total"], [(Decimal("1.0"),)]),
            None,
            1.0,
        ),
        ("values", _res(["total"], [(2,)]), _res(["total"], [(3,)]), None, 0.0),
        # `shape` never looks at a value: not one of these cells agrees, and the counts and the
        # coarse types all do.
        (
            "shape",
            _res(["channel", "orders"], [("web", 1), ("store", 2)]),
            _res(["c", "n"], [("kiosk", 77), ("phone", 88)]),
            None,
            1.0,
        ),
        ("shape", _res(["orders"], [(1,), (2,)]), _res(["orders"], [(1,)]), None, 0.0),
        (
            "bounded",
            _res(["orders"], [(1,), (2,)]),
            _res(["orders"], [(1,), (2,)]),
            GoldenBounds(min_rows=1, max_rows=3),
            1.0,
        ),
        (
            "bounded",
            _res(["orders"], [(1,)]),
            _res(["orders"], [(1,), (2,), (3,), (4,), (5,)]),
            GoldenBounds(min_rows=1, max_rows=3),
            0.0,
        ),
        ("nonempty", _res(["orders"], [(1,)]), _res(["orders"], [(9,)]), None, 1.0),
        # The golden side is empty because a `nonempty` item has no answer key to fill it.
        ("nonempty", _res(["orders"], []), _res(["orders"], []), None, 0.0),
    ],
)
def test_each_level_over_a_passing_and_a_failing_pair(level, golden, generated, bounds, expected):
    score = c.compare_result_sets(
        golden, generated, match=level, golden_sql=_UNORDERED, bounds=bounds
    )
    assert score.status == "scored"
    assert score.accuracy == expected


def test_shape_passes_a_pair_whose_values_disagree_entirely():
    # The reason `shape` exists: an item whose answer moves with the data still gates on the
    # answer having the right form.
    golden = _res(
        ["channel", "orders", "day"], [("web", 1, date(2025, 1, 1)), ("store", 2, date(2025, 1, 2))]
    )
    generated = _res(
        ["c", "n", "d"], [("kiosk", 900, date(2030, 6, 6)), ("phone", 901, date(2030, 6, 7))]
    )
    assert (
        c.compare_result_sets(golden, generated, match="shape", golden_sql=_UNORDERED).accuracy
        == 1.0
    )


def test_shape_fails_when_a_column_type_disagrees():
    golden = _res(["orders"], [(1,), (2,)])
    generated = _res(["orders"], [("one",), ("two",)])
    score = c.compare_result_sets(golden, generated, match="shape", golden_sql=_UNORDERED)
    assert score.accuracy == 0.0
    assert "orders" in score.reason


def test_shape_fails_when_the_column_count_disagrees():
    golden = _res(["channel", "orders"], [("web", 1)])
    generated = _res(["channel"], [("web",)])
    assert (
        c.compare_result_sets(golden, generated, match="shape", golden_sql=_UNORDERED).accuracy
        == 0.0
    )


def test_shape_lets_an_all_null_column_agree_with_anything():
    # A NULL is the absence of a value, not a value of some type, so an all-NULL column asserts
    # nothing about the type of its partner.
    golden = _res(["note"], [(None,), (None,)])
    generated = _res(["note"], [("shipped",), ("held",)])
    assert (
        c.compare_result_sets(golden, generated, match="shape", golden_sql=_UNORDERED).accuracy
        == 1.0
    )


# --- the bounded band ------------------------------------------------------------------------


def test_bounded_without_a_band_is_an_error():
    # The reader refuses to author this pair, but the function can be called directly and has to
    # say so rather than passing an item it compared nothing about.
    score = c.compare_result_sets(
        _res(["orders"], [(1,)]), _res(["orders"], [(1,)]), match="bounded", golden_sql=_UNORDERED
    )
    assert score.status == "error"
    assert score.accuracy is None
    assert score.reason


def test_a_value_band_on_a_multi_cell_result_is_an_error():
    score = c.compare_result_sets(
        _res(["orders"], [(1,)]),
        _res(["channel", "orders"], [("web", 1), ("store", 2)]),
        match="bounded",
        golden_sql=_UNORDERED,
        bounds=GoldenBounds(min_value=0, max_value=10),
    )
    assert score.status == "error"
    assert score.reason


@pytest.mark.parametrize(
    "value, expected",
    # The two edges are in here because the band is INCLUSIVE at both ends: an author writing
    # `max_value: 10` means ten is an acceptable answer, and without these a `<` quietly becoming
    # `<=` — or the reverse — changes every band in the suite and fails nothing.
    [(5, 1.0), (0, 1.0), (10, 1.0), (50, 0.0), (-1, 0.0)],
)
def test_a_value_band_judges_the_single_generated_cell(value, expected):
    score = c.compare_result_sets(
        _res(["total"], [(7,)]),
        _res(["total"], [(value,)]),
        match="bounded",
        golden_sql=_UNORDERED,
        bounds=GoldenBounds(min_value=0, max_value=10),
    )
    assert score.status == "scored"
    assert score.accuracy == expected


def test_a_row_band_judges_the_generated_row_count():
    golden = _res(["orders"], [(1,)])
    in_band = c.compare_result_sets(
        golden,
        _res(["orders"], [(1,), (2,)]),
        match="bounded",
        golden_sql=_UNORDERED,
        bounds=GoldenBounds(min_rows=2),
    )
    assert in_band.accuracy == 1.0
    out_of_band = c.compare_result_sets(
        golden,
        _res(["orders"], [(1,)]),
        match="bounded",
        golden_sql=_UNORDERED,
        bounds=GoldenBounds(min_rows=2),
    )
    assert out_of_band.accuracy == 0.0


def test_a_value_band_on_a_non_numeric_cell_is_an_error():
    score = c.compare_result_sets(
        _res(["total"], [(5,)]),
        _res(["total"], [("shipped",)]),
        match="bounded",
        golden_sql=_UNORDERED,
        bounds=GoldenBounds(max_value=10),
    )
    assert score.status == "error"
    assert score.reason


# --- totality, status, and what a diagnostic may carry ----------------------------------------


def test_status_is_three_way_and_distinguishable():
    scored = c.compare_result_sets(
        _res(["orders"], [(1,)]), _res(["orders"], [(1,)]), golden_sql=_UNORDERED
    )
    unscored = c.compare_result_sets(
        _res(["orders"], []), _res(["orders"], []), golden_sql=_UNORDERED
    )
    errored = c.compare_result_sets(
        _res(["orders"], [(1,)]), _res(["orders"], [(1,)]), match="bounded", golden_sql=_UNORDERED
    )
    assert {scored.status, unscored.status, errored.status} == {"scored", "unscored", "error"}
    assert scored.accuracy == 1.0
    assert unscored.accuracy is None and errored.accuracy is None


def test_a_ragged_row_is_reported_as_an_error_rather_than_raised():
    # `ExecResult` does not validate that a row is as wide as its column list, so a short row can
    # reach here. The scoring step is total: a malformed item costs that item, not the run.
    golden = _res(["channel", "orders"], [("zzsentinelcell", 1)])
    generated = _res(["channel", "orders"], [("web",)])
    score = c.compare_result_sets(golden, generated, golden_sql=_UNORDERED)
    assert score.status == "error"
    assert score.accuracy is None
    assert score.reason
    assert "zzsentinelcell" not in score.reason


def test_a_malformed_result_is_an_error_rather_than_an_escaping_exception():
    # Totality has to hold for the inputs nobody anticipated as well as the one that was:
    # `ExecResult` is an unvalidated dataclass, so a row can be anything at all. The reason names
    # the failure without quoting whatever it choked on.
    golden = _res(["channel"], [("zzsentinelcell",)])
    generated = ExecResult(columns=["channel"], rows=[None])
    score = c.compare_result_sets(golden, generated, golden_sql=_UNORDERED)
    assert score.status == "error"
    assert score.accuracy is None
    assert score.reason and "zzsentinelcell" not in score.reason


def test_a_diagnostic_carries_neither_the_answer_key_nor_a_cell():
    # The one rule the whole module is under: the payload here IS result data, and a score
    # travels. Column names and counts may go; SQL and cells may not.
    sql = "SELECT zzsentinelsql FROM"  # deliberately unparseable, so a note is produced too
    golden = _res(["channel"], [("zzsentinelcell",), ("store",)])
    generated = _res(["channel"], [("other",), ("store",)])
    score = c.compare_result_sets(golden, generated, golden_sql=sql)
    written = " ".join((score.reason, *score.notes, *score.unmatched_golden_columns))
    assert score.notes
    assert "zzsentinelsql" not in written
    assert "zzsentinelcell" not in written


@pytest.mark.parametrize("sql", [None, "", "not sql at all"])
def test_an_unreadable_answer_key_is_scored_order_sensitively_and_says_so(sql):
    golden = _res(["channel"], [("web",), ("store",)])
    generated = _res(["channel"], [("store",), ("web",)])
    score = c.compare_result_sets(golden, generated, golden_sql=sql)
    assert score.order_sensitive is True
    assert any("ordering was assumed" in note for note in score.notes)
    assert score.accuracy == 0.0


def test_a_readable_answer_key_leaves_the_notes_empty():
    score = c.compare_result_sets(
        _res(["orders"], [(1,)]), _res(["orders"], [(1,)]), golden_sql=_UNORDERED
    )
    assert score.notes == ()


def test_an_item_score_is_a_frozen_dataclass():
    # A returned value, never an authored file — so a dataclass like `ExecResult` and `Envelope`,
    # not a pydantic model, and frozen so a caller cannot edit a verdict in place.
    score = c.compare_result_sets(
        _res(["orders"], [(1,)]), _res(["orders"], [(1,)]), golden_sql=_UNORDERED
    )
    assert dataclasses.is_dataclass(score)
    with pytest.raises(dataclasses.FrozenInstanceError):
        score.accuracy = 0.0


def test_an_unknown_match_level_is_an_error_rather_than_a_crash():
    # `MatchLevel` is a Literal, which a type checker enforces and a direct caller can ignore.
    score = c.compare_result_sets(
        _res(["orders"], [(1,)]), _res(["orders"], [(1,)]), match="whatever", golden_sql=_UNORDERED
    )
    assert score.status == "error"
    assert score.accuracy is None


# --- a root node that cannot carry an ordering -------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # A trailing comment, or a second statement, makes sqlglot wrap the whole thing in a node
        # that carries no `order` argument of its own.
        "SELECT n FROM t ORDER BY n;\n-- a trailing comment",
        "SET search_path TO acme; SELECT n FROM t ORDER BY n",
        # sqlglot has no grammar for EXPLAIN and falls back to a Command node — and it WARNS
        # rather than raising, so the unparsed net never fires for it.
        "EXPLAIN SELECT a FROM t ORDER BY a",
    ],
)
def test_a_root_that_cannot_carry_an_order_is_assumed_ordered(sql):
    # Asking `args['order']` of a node that has no such argument always answers None, which reads
    # an ordered answer key as unordered and says nothing about having done so — the silent
    # weakening this module refuses everywhere else.
    ordered, note = c.has_top_level_order_by(sql)
    assert ordered is True
    assert note


def test_a_parenthesised_statement_is_unwrapped_before_its_order_is_read():
    # The order sits on the node inside the parentheses, or on the wrapper when the ORDER BY is
    # written outside them; both are a total order of the rows the caller receives.
    assert c.has_top_level_order_by("(SELECT a FROM t ORDER BY a)") == (True, None)
    assert c.has_top_level_order_by("(SELECT a FROM t) ORDER BY a") == (True, None)
    assert c.has_top_level_order_by("(SELECT a FROM t)") == (False, None)


def test_an_unreadable_root_is_scored_order_sensitively_end_to_end():
    # What the gap cost: the rows are reversed and the item scored a full 1.0 with no note.
    golden = _res(["n"], [(1,), (2,)])
    generated = _res(["n"], [(2,), (1,)])
    score = c.compare_result_sets(
        golden, generated, golden_sql="SELECT n FROM t ORDER BY n;\n-- a trailing comment"
    )
    assert score.order_sensitive is True
    assert score.notes
    assert score.accuracy == 0.0


# --- the parser can fail in ways that are not a SqlglotError -----------------------------------


@pytest.mark.parametrize(
    "sql, dialect",
    [
        ("SELECT a FROM t ORDER BY a", "zzsentineldialect"),
        ("SELECT " + "(" * 200 + "1" + ")" * 200, None),
    ],
)
def test_a_statement_that_breaks_the_parser_outright_is_still_a_score(sql, dialect):
    # An unknown dialect raises ValueError and deep nesting raises RecursionError; neither is a
    # SqlglotError, and this read happens outside the scoring call's own totality net.
    ordered, note = c.has_top_level_order_by(sql, dialect=dialect)
    assert ordered is True
    assert note
    score = c.compare_result_sets(
        _res(["a"], [(1,)]), _res(["a"], [(1,)]), golden_sql=sql, dialect=dialect
    )
    assert score.notes
    assert "zzsentineldialect" not in " ".join((score.reason, *score.notes))


# --- an empty answer key is the normal shape for the keyless levels ----------------------------


def test_nonempty_scores_zero_when_both_sides_are_empty():
    # A `nonempty` item has no answer key by design, so its golden side is legitimately empty.
    # Dropping the pair as unscored is exactly how an agent that returned NO ROWS — the one
    # failure the level exists to catch — escapes the score entirely.
    score = c.compare_result_sets(
        _res(["orders"], []), _res(["orders"], []), match="nonempty", golden_sql=_UNORDERED
    )
    assert score.status == "scored"
    assert score.accuracy == 0.0


def test_bounded_scores_zero_when_both_sides_are_empty():
    score = c.compare_result_sets(
        _res(["orders"], []),
        _res(["orders"], []),
        match="bounded",
        golden_sql=_UNORDERED,
        bounds=GoldenBounds(min_rows=1),
    )
    assert score.status == "scored"
    assert score.accuracy == 0.0


# --- the quantize bucket is a bucket, and says so ----------------------------------------------


def test_quantize_does_not_bucket_two_different_integer_ids():
    # Relatively 3.8e-9 apart, which nine significant digits rounds onto one key: every id or
    # count above ~1e9 would compare equal to its neighbours under `values`.
    left = c.canonical_cell(1100170109835, quantize=True)
    right = c.canonical_cell(1100170114000, quantize=True)
    assert left != right


def test_quantize_leaves_a_whole_number_alone_however_it_was_spelled():
    # The exemption is by VALUE and not by Python type: the same id arrives as an int from one
    # driver, a Decimal from another and a float from a third, and all three have to keep keying
    # alike or `values` would fail two identical numbers.
    keys = {
        c.canonical_cell(value, quantize=True)
        for value in (1100170109835, Decimal("1100170109835"), 1100170109835.0)
    }
    assert len(keys) == 1


def test_two_values_straddling_a_bucket_edge_do_not_pair():
    # Pinned deliberately, because the name invites the wrong reading: this is a rounding BUCKET
    # at nine significant digits, not a tolerance. These two are 8e-14 apart — four orders of
    # magnitude inside what a 1e-9 tolerance would forgive — and they still key apart, because a
    # bucket edge falls between them. A tolerance is not an equivalence relation and a Counter
    # needs one, so the bucket stays; nobody should "tighten" it believing otherwise.
    left = c.canonical_cell(Decimal("0.1234567895"), quantize=True)
    right = c.canonical_cell(Decimal("0.12345678949999"), quantize=True)
    assert left != right


# --- the NaN bucket is a value, not an identity ------------------------------------------------


def test_the_nan_bucket_survives_leaving_the_process():
    # The sentinel is a plain string rather than a NaN, which no same-object comparison can show:
    # a tuple holding one NaN object compares equal to ITSELF through the identity fast-path, so
    # a `float('nan')` sentinel passes every in-process check and still opens a fresh bucket the
    # moment the two keys are built from different objects.
    key = c.canonical_cell(float("nan"))
    travelled = pickle.loads(pickle.dumps(key))
    assert travelled == key
    counted = Counter([key, travelled])
    assert counted.total() == 2 and len(counted) == 1


# --- rows and columns are different complaints -------------------------------------------------


def test_a_row_count_difference_is_reported_as_rows_and_a_missing_column_as_a_column():
    # A column vector's length IS the row count, so when the counts differ no column can pair —
    # and every such case used to be laundered through the missing-column branch, which is the
    # string a person reads in the report for the most common regression there is.
    counts = c.compare_result_sets(
        _res(["id"], [(n,) for n in range(100)]),
        _res(["id"], [(n,) for n in range(99)]),
        golden_sql=_UNORDERED,
    )
    assert counts.accuracy == 0.0
    assert counts.unmatched_golden_columns == ()
    assert "column" not in counts.reason
    assert (counts.golden_row_count, counts.generated_row_count) == (100, 99)
    absent = c.compare_result_sets(
        _res(["channel", "revenue"], [("web", 10), ("store", 20)]),
        _res(["channel"], [("web",), ("store",)]),
        golden_sql=_UNORDERED,
    )
    assert absent.unmatched_golden_columns == ("revenue",)
    assert "revenue" in absent.reason


# --- a band with two halves ---------------------------------------------------------------------


def test_a_row_band_still_judges_a_result_the_value_band_cannot_reach():
    # Both halves are authored and the result is not a single cell. Erroring here leaves the item
    # permanently unjudgeable and reports a run fault, where the author wrote a perfectly good row
    # band to judge it with.
    bounds = GoldenBounds(min_rows=1, max_rows=5, max_value=100)
    golden = _res(["orders"], [(1,)])
    inside = c.compare_result_sets(
        golden,
        _res(["channel", "orders"], [("web", 1), ("store", 2)]),
        match="bounded",
        golden_sql=_UNORDERED,
        bounds=bounds,
    )
    assert (inside.status, inside.accuracy) == ("scored", 1.0)
    outside = c.compare_result_sets(
        golden,
        _res(["channel", "orders"], [("web", n) for n in range(6)]),
        match="bounded",
        golden_sql=_UNORDERED,
        bounds=bounds,
    )
    assert (outside.status, outside.accuracy) == ("scored", 0.0)
    assert "rows" in outside.reason


def test_an_empty_result_under_a_value_band_scores_zero_rather_than_erroring():
    # "The agent returned nothing" is a wrong answer and not an unjudgeable case — catching it is
    # half of why a band is authored at all.
    score = c.compare_result_sets(
        _res(["total"], [(5,)]),
        _res(["total"], []),
        match="bounded",
        golden_sql=_UNORDERED,
        bounds=GoldenBounds(min_value=0, max_value=10),
    )
    assert score.status == "scored"
    assert score.accuracy == 0.0


# --- the module's public surface -----------------------------------------------------------------


def test_the_module_exports_only_its_public_surface():
    # The scoring call and the value it hands back. Everything else is an internal these tests
    # reach as a module attribute, and `MatchLevel`/`GoldenBounds` belong to `golden`.
    assert set(c.__all__) == {"compare_result_sets", "ItemScore"}


# --- pairing by agreement --------------------------------------------------------------------------

_TEN = [(f"c{i}", i * 10) for i in range(10)]


def test_a_column_agreeing_on_most_rows_pairs_and_the_score_counts_the_rows_that_match():
    golden = c.ExecResult(columns=["customer", "total"], rows=_TEN)
    rows = list(_TEN)
    rows[3] = ("c3", 31)
    generated = c.ExecResult(columns=["customer", "amount"], rows=rows)
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.column_pairs == (("customer", "customer"), ("total", "amount"))
    assert score.column_agreement == (1.0, 0.9)
    assert score.accuracy == 0.9 and score.paired_row_share == 0.9
    assert score.reason.startswith("9 of the answer key's 10 rows matched")
    assert score.unmatched_golden_columns == () and score.unmatched_generated_columns == ()


def test_a_differently_named_column_agreeing_on_a_minority_of_rows_stays_unmatched():
    golden = c.ExecResult(columns=["customer", "total"], rows=_TEN)
    rows = [(name, value if i < 3 else value + 1) for i, (name, value) in enumerate(_TEN)]
    generated = c.ExecResult(columns=["customer", "amount"], rows=rows)
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.unmatched_golden_columns == ("total",) and score.unmatched_generated_columns == ("amount",)
    assert score.accuracy == 0.0 and score.reason == "no generated column carries the values of: total"
    # The customer column paired and agrees on every row: the share over the paired columns says so.
    assert score.column_pairs == (("customer", "customer"),) and score.paired_row_share == 1.0


def test_a_same_named_column_pairs_at_any_agreement():
    golden = c.ExecResult(columns=["customer", "total"], rows=_TEN)
    generated = c.ExecResult(columns=["customer", "o.total"], rows=[(n, v + 1) for n, v in _TEN])
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.column_pairs == (("customer", "customer"), ("total", "o.total"))
    assert score.column_agreement == (1.0, 0.0)
    assert score.accuracy == 0.0 and score.reason.startswith("0 of the answer key's 10 rows matched")


def test_the_caller_can_ask_for_the_rows_as_a_set_whatever_the_statement_ordered():
    golden = c.ExecResult(columns=["channel"], rows=[("web",), ("store",)])
    generated = c.ExecResult(columns=["channel"], rows=[("store",), ("web",)])
    ordered_sql = "SELECT channel FROM t ORDER BY channel"
    score = c.compare_result_sets(golden, generated, golden_sql=ordered_sql, ordered=False)
    assert score.accuracy == 1.0 and score.order_sensitive is False
    assert score.notes == ("row order was not compared",)
    # The statement still decides when the caller says nothing.
    assert c.compare_result_sets(golden, generated, golden_sql=ordered_sql).accuracy == 0.0


def test_nothing_pairs_when_the_row_counts_differ_and_the_share_is_absent():
    golden = c.ExecResult(columns=["customer"], rows=_TEN)
    generated = c.ExecResult(columns=["customer"], rows=_TEN[:9])
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.column_pairs == () and score.column_agreement == () and score.paired_row_share is None


# --- pairing columns whose values are equal as multisets --------------------------------------------
#
# When the order does not count, a column's values are sorted before they are compared, so two
# different flags that are each true on two rows of four are equal vectors. Which golden flag takes
# which generated flag then decides whether the rows line up at all.

# Two flags with equal counts on different rows, and a region that tells the rows apart. `is_staff`
# is a third flag with the same counts, for a generated statement that selects a column too many.
_FLAG_COLUMNS = ["region", "is_active", "is_verified"]
_FLAG_ROWS = [
    ("north", True, False),
    ("south", True, True),
    ("east", False, True),
    ("west", False, False),
]
_IS_STAFF = {"north": True, "south": False, "east": True, "west": False}
_REORDERED = ["region", "is_verified", "is_active"]


def _flags_selected_as(columns, *, labels=None, shuffle=True):
    """The flag rows as a statement selecting `columns` returns them: under `labels` when given, and
    with the rows in another order unless `shuffle` is off."""
    rows = list(reversed(_FLAG_ROWS)) if shuffle else _FLAG_ROWS
    selected = []
    for row in rows:
        cells = dict(zip(_FLAG_COLUMNS, row), is_staff=_IS_STAFF[row[0]])
        selected.append(tuple(cells[name] for name in columns))
    return c.ExecResult(columns=list(labels or columns), rows=selected)


_FLAGS_BY_NAME = (("region", "region"), ("is_active", "is_active"), ("is_verified", "is_verified"))


def test_same_named_flags_selected_in_the_other_order_pair_by_name_when_unordered():
    golden = c.ExecResult(columns=_FLAG_COLUMNS, rows=_FLAG_ROWS)
    score = c.compare_result_sets(
        golden, _flags_selected_as(_REORDERED), match="values", ordered=False
    )
    assert score.accuracy == 1.0 and score.column_pairs == _FLAGS_BY_NAME


def _counting_compare_rows(monkeypatch):
    """Count how many times stage one reads the rows to measure a pairing."""
    real, pairings = c.compare_rows, []

    def counted(golden_rows, generated_rows, pairing, **kept):
        pairings.append(pairing)
        return real(golden_rows, generated_rows, pairing, **kept)

    monkeypatch.setattr(c, "compare_rows", counted)
    return pairings


def test_the_in_order_pairing_is_not_measured_when_the_names_already_line_up_every_row(monkeypatch):
    # The two pairings differ, so stage one would read the rows once for each to see which lines up
    # more. But both pair the same golden columns, so nothing can line up more rows than the shorter
    # result holds, and the names have already lined up that many. The second read can only tie, a
    # tie goes to the names, so it is skipped. On six flags it saved 0.41s to 0.33s at 20,000 rows
    # and 5.38s to 4.50s at 200,000.
    measured = _counting_compare_rows(monkeypatch)
    golden = c.ExecResult(columns=_FLAG_COLUMNS, rows=_FLAG_ROWS)
    pairing = c.pair_columns(
        _FLAG_COLUMNS,
        _FLAG_ROWS,
        _REORDERED,
        _flags_selected_as(_REORDERED).rows,
        ordered=False,
    )
    assert len(measured) == 1
    named = tuple((golden.columns[g], _REORDERED[p]) for g, p in sorted(pairing.pairing.items()))
    assert named == _FLAGS_BY_NAME


def test_the_in_order_pairing_is_still_measured_when_the_names_leave_a_row_out(monkeypatch):
    # The labels are on the other column, so pairing by name misaligns every row. Stage one has to
    # read the rows for both pairings to find that out, and the in-order one replaces the names.
    measured = _counting_compare_rows(monkeypatch)
    rows = [(1, 2), (2, 3), (3, 1)]
    pairing = c.pair_columns(["x", "y"], rows, ["y", "x"], rows, ordered=False)
    assert len(measured) == 2
    assert pairing.pairing == {0: 0, 1: 1}


def test_an_extra_flag_with_equal_counts_listed_first_does_not_take_a_named_partner():
    golden = c.ExecResult(columns=_FLAG_COLUMNS, rows=_FLAG_ROWS)
    generated = _flags_selected_as(["region", "is_staff", "is_verified", "is_active"])
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0 and score.column_pairs == _FLAGS_BY_NAME
    assert score.unmatched_generated_columns == ("is_staff",)


def test_a_tie_between_the_two_pairings_goes_to_the_names():
    # Without the region, swapping two flags with equal counts leaves the same rows as a multiset,
    # so both pairings line up every row. The names break the tie.
    golden = _flags_selected_as(["is_active", "is_verified"], shuffle=False)
    generated = _flags_selected_as(["is_verified", "is_active"])
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0
    assert score.column_pairs == (("is_active", "is_active"), ("is_verified", "is_verified"))


def test_swapped_labels_still_pair_by_values_when_the_names_would_misalign_the_rows():
    # The generated statement put each label on the other column. Pairing by name would misalign
    # every row; pairing in order lines them all up, so that pairing is kept.
    rows = [(1, 2), (2, 3), (3, 1)]
    golden = c.ExecResult(columns=["x", "y"], rows=rows)
    generated = c.ExecResult(columns=["y", "x"], rows=rows)
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0
    assert score.column_pairs == (("x", "y"), ("y", "x"))


def test_a_repeated_golden_label_with_one_column_aliased_still_scores_a_full_match():
    rows = [("a", "b"), ("b", "c"), ("c", "a")]
    golden = c.ExecResult(columns=["name", "name"], rows=rows)
    generated = c.ExecResult(columns=["from_name", "name"], rows=rows)
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0
    assert score.column_pairs == (("name", "from_name"), ("name", "name"))


@pytest.mark.parametrize("golden_columns", [_FLAG_COLUMNS, _REORDERED])
def test_a_flag_without_a_namesake_does_not_take_a_later_flag_s_namesake(golden_columns):
    # `is_active` is selected as `active`, so it has no namesake. Checked in both golden column
    # orders: it must not take `is_verified`'s partner whether it comes before that column or after.
    golden = _flags_selected_as(golden_columns, shuffle=False)
    generated = _flags_selected_as(_REORDERED, labels=["region", "is_verified", "active"])
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0
    assert dict(score.column_pairs) == {
        "region": "region", "is_active": "active", "is_verified": "is_verified"
    }


def test_flags_selected_in_the_other_order_score_through_the_answer_key_s_ordering():
    golden = c.ExecResult(columns=_FLAG_COLUMNS, rows=_FLAG_ROWS)
    unordered_sql = "SELECT region, is_active, is_verified FROM accounts"
    score = c.compare_result_sets(golden, _flags_selected_as(_REORDERED), golden_sql=unordered_sql)
    assert score.order_sensitive is False and score.accuracy == 1.0
    # With an ORDER BY the rows are compared in place, so the same statement passes only when its
    # rows come back in the answer key's order. Neither verdict changed with this pairing.
    ordered_sql = unordered_sql + " ORDER BY region"
    in_place = _flags_selected_as(_REORDERED, shuffle=False)
    assert c.compare_result_sets(golden, in_place, golden_sql=ordered_sql).accuracy == 1.0
    moved = _flags_selected_as(_REORDERED)
    assert c.compare_result_sets(golden, moved, golden_sql=ordered_sql).accuracy < 1.0


# --- searching the assignments inside a class of equal columns --------------------------------------
#
# When neither the names nor the column order line the rows up, the comparator tries the ways to pair
# a class of equal columns and keeps the one that lines up the most rows. Most cases run on two
# fixtures, because rows line up differently in each: in the flag rows `region` names each row once,
# and in the trio rows `tier` repeats, so no column names a row.

_FLAG_TRIO = ["region", "is_active", "is_verified", "is_staff"]
_TRIO = ["tier", "is_active", "is_verified", "is_staff"]
# Three flags, each true on four rows of eight. Only the right assignment lines up all eight rows.
_TRIO_ROWS = [
    ("gold", True, True, False),
    ("gold", True, False, False),
    ("gold", False, False, True),
    ("silver", True, False, True),
    ("silver", False, True, False),
    ("silver", False, True, True),
    ("bronze", True, True, True),
    ("bronze", False, False, False),
]


def _trio_selected_as(columns, labels):
    """The trio rows as a statement selecting `columns` returns them under `labels`, rows reversed."""
    picked = [_TRIO.index(name) for name in columns]
    rows = [tuple(row[i] for i in picked) for row in reversed(_TRIO_ROWS)]
    return c.ExecResult(columns=list(labels), rows=rows)


def _both_row_kinds(columns, labels):
    """The answer key and a statement selecting `columns` under `labels`, once for each fixture. The
    first column is the one that names the rows, or does not."""
    flag_labels = ["region", *labels[1:]] if labels[0] == "tier" else labels
    return [
        pytest.param(
            _flags_selected_as(_FLAG_TRIO, shuffle=False),
            _flags_selected_as(["region", *columns[1:]], labels=flag_labels),
            id="a-column-names-each-row",
        ),
        pytest.param(
            c.ExecResult(columns=_TRIO, rows=_TRIO_ROWS),
            _trio_selected_as(["tier", *columns[1:]], labels),
            id="no-column-names-a-row",
        ),
    ]


def test_every_column_renamed_with_two_equal_count_flags_scores_a_full_match():
    # Renamed, no column has a name to pair by, and in order each flag takes the other's partner.
    golden = c.ExecResult(columns=_FLAG_COLUMNS, rows=_FLAG_ROWS)
    generated = _flags_selected_as(_REORDERED, labels=["area", "flag_a", "flag_b"])
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0
    assert score.column_pairs == (
        ("region", "area"), ("is_active", "flag_b"), ("is_verified", "flag_a")
    )


def test_every_column_renamed_scores_a_full_match_when_no_column_names_a_row():
    golden = c.ExecResult(columns=_TRIO, rows=_TRIO_ROWS)
    generated = _trio_selected_as(
        ["is_staff", "tier", "is_verified", "is_active"], ["s", "t", "v", "a"]
    )
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0
    assert dict(score.column_pairs) == {
        "tier": "t", "is_active": "a", "is_verified": "v", "is_staff": "s"
    }


@pytest.mark.parametrize(
    "golden, generated",
    _both_row_kinds(["tier", "is_staff", "is_active", "is_verified"], _TRIO),
)
def test_labels_swapped_inside_a_class_of_three_flags_still_score_a_full_match(golden, generated):
    # Each label sits on another flag's values, and the columns are selected in another order, so
    # neither the names nor the order line the rows up.
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0
    assert dict(score.column_pairs[1:]) == {
        "is_active": "is_verified", "is_verified": "is_staff", "is_staff": "is_active"
    }


@pytest.mark.parametrize(
    "golden, generated",
    _both_row_kinds(
        ["tier", "is_verified", "is_staff", "is_active"], ["tier", "flag", "is_staff", "from_flag"]
    ),
)
def test_a_repeated_label_with_one_aliased_inside_a_class_of_three_scores_a_full_match(
    golden, generated
):
    golden = c.ExecResult(columns=[golden.columns[0], "flag", "flag", "is_staff"], rows=golden.rows)
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0
    assert score.column_pairs[1:] == (
        ("flag", "from_flag"), ("flag", "flag"), ("is_staff", "is_staff")
    )


def test_a_tie_between_assignments_goes_to_the_one_pairing_more_names():
    # `a` and `b` hold the same flag, so pairing them either way lines up every row. One way pairs
    # `b` with its namesake, and that one is kept.
    rows = [(True, True, False), (True, True, True), (False, False, True), (False, False, False)]
    golden = c.ExecResult(columns=["a", "b", "c"], rows=rows)
    generated = c.ExecResult(columns=["a", "b", "x"], rows=[row[::-1] for row in reversed(rows)])
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0
    assert score.column_pairs == (("a", "x"), ("b", "b"), ("c", "a"))


def test_a_dropped_flag_is_reported_missing_rather_than_its_equal_count_twin():
    # Two golden flags and one generated flag with the same counts: the generated flag pairs with the
    # golden flag whose rows it lines up, so the report names the flag that is really missing.
    golden = c.ExecResult(columns=_FLAG_COLUMNS, rows=_FLAG_ROWS)
    generated = _flags_selected_as(["region", "is_verified"], labels=["region", "verified"])
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.unmatched_golden_columns == ("is_active",)
    assert score.column_pairs == (("region", "region"), ("is_verified", "verified"))


_SEVEN_FLAGS = [f"flag_{k}" for k in range(7)]
# Seven flags, each true on four rows of eight and each on different rows, beside a row name.
_SEVEN_ROWS = [(f"r{row}", *((row + k) % 8 < 4 for k in range(7))) for row in range(8)]


def _seven_flags_renamed_and_rotated():
    """The seven flags renamed, each selected in the next one's place."""
    golden = c.ExecResult(columns=["name", *_SEVEN_FLAGS], rows=_SEVEN_ROWS)
    rotated = [0, *range(2, 8), 1]
    rows = [tuple(row[i] for i in rotated) for row in reversed(_SEVEN_ROWS)]
    return golden, c.ExecResult(columns=["n", *(f"f{k}" for k in range(7))], rows=rows)


def test_a_class_too_large_to_search_keeps_the_rule_s_pairing_and_completes():
    # Seven equal columns can pair in 5,040 ways, past the cap, so the class keeps the in-order
    # pairing. Every flag then takes the next one's partner and the score stays below 1.0: the limit.
    golden, generated = _seven_flags_renamed_and_rotated()
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.status == "scored" and score.accuracy < 1.0
    assert score.column_pairs == (("name", "n"), *((f"flag_{k}", f"f{k}") for k in range(7)))


def test_the_same_class_lines_up_when_the_cap_allows_the_search(monkeypatch):
    monkeypatch.setattr(c, "_ASSIGNMENT_CAP", 5040)
    golden, generated = _seven_flags_renamed_and_rotated()
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0


def _seven_flags_relabelled():
    """The seven flags with their labels rotated: pairing by names misaligns every row, and the
    plain in-order pairing lines them all up."""
    golden = c.ExecResult(columns=["name", *_SEVEN_FLAGS], rows=_SEVEN_ROWS)
    relabelled = ["name", *_SEVEN_FLAGS[1:], _SEVEN_FLAGS[0]]
    return golden, c.ExecResult(columns=relabelled, rows=list(reversed(_SEVEN_ROWS)))


def test_a_class_too_large_to_search_still_falls_back_from_names_to_column_order():
    # Seven equal columns are past the assignment cap, so the class is not searched and the fallback
    # is all that is left: the names pair `flag_0` with the column labelled `flag_0`, which holds
    # `flag_6`'s values, and misalign every row. The plain in-order pairing lines them all up, so it
    # replaces the names. Nothing else on this branch exercises that fallback, because a class the
    # search takes reaches the same pairing on its own.
    golden, generated = _seven_flags_relabelled()
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0
    assert score.column_pairs == (
        ("name", "name"), *((f"flag_{k}", f"flag_{(k + 1) % 7}") for k in range(7))
    )


def _one_flag_against_many(width):
    """One golden flag against `width` generated flags that hold the same values as a set, only one
    of them on the same rows. Nothing but a search can tell which."""
    rows = [(f"r{row}", row % 2 == 0) for row in range(8)]
    golden = c.ExecResult(columns=["key", "is_active"], rows=rows)
    # `c0` is true on the first four rows; only the last column is true on the golden column's rows.
    generated_rows = [
        (row[0], *[(position < 4) if k < width - 1 else row[1] for k in range(width)])
        for position, row in enumerate(reversed(rows))
    ]
    columns = ["key", *(f"c{k}" for k in range(width))]
    return golden, c.ExecResult(columns=columns, rows=generated_rows)


def test_a_wide_shallow_class_is_searched_and_lines_the_rows_up():
    # The assignment cap counts assignments, and 40 options at one slot is only 40 of them. So the
    # class is searched, one column deep and forty wide, and the descent reads the result once per
    # option to find the one that lines the rows up. That is what the search costs for a wide class,
    # and it is bounded by the generated columns, since an option is one of them.
    golden, generated = _one_flag_against_many(40)
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0
    assert score.column_pairs == (("key", "key"), ("is_active", "c39"))


def _rotated_result(rows):
    """A right answer of six columns holding one pool of values, every column renamed and selected
    in the next one's place. Nothing outside the class holds the rows together, so every option at
    the first slot lines up every row: the descent chooses on column order alone, which is wrong
    here by construction, and only backtracking reaches the answer."""
    numbers = random.Random(3)
    columns = [numbers.sample(range(rows), rows) for _ in range(6)]
    golden_rows = [tuple(column[row] for column in columns) for row in range(rows)]
    rotated = [*range(1, 6), 0]
    return (
        c.ExecResult(columns=[f"p{k}" for k in range(6)], rows=golden_rows),
        c.ExecResult(
            columns=[f"q{k}" for k in range(6)],
            rows=[tuple(row[k] for k in rotated) for row in reversed(golden_rows)],
        ),
    )


@pytest.mark.parametrize("rows", [50, 500])
def test_the_budget_lets_the_search_reach_a_right_answer_at_either_size(monkeypatch, rows):
    # With no floor the budget is the rows alone, and a right answer is reached at both sizes.
    monkeypatch.setattr(c, "_SEARCH_ROW_FLOOR", 0)
    golden, generated = _rotated_result(rows)
    assert c.compare_result_sets(golden, generated, match="values", ordered=False).accuracy == 1.0


def test_a_budget_that_does_not_grow_with_the_rows_leaves_a_larger_result_short(monkeypatch):
    # The bug this pins: the budget was a flat count, so it bought a hundred reads of a small result
    # and a handful of a large one, and the search stopped before it could recover from a descent
    # that went the wrong way. Here the flat budget is exactly what 50 rows earn. It is enough at 50
    # rows and not at 500, which is why the budget counts reads of the result and not rows.
    monkeypatch.setattr(c, "_SEARCH_ROW_FACTOR", 0)
    monkeypatch.setattr(c, "_SEARCH_ROW_FLOOR", 100 * 50)
    small = c.compare_result_sets(*_rotated_result(50), match="values", ordered=False)
    large = c.compare_result_sets(*_rotated_result(500), match="values", ordered=False)
    assert small.accuracy == 1.0
    assert large.status == "scored" and large.accuracy < 1.0


def _nothing_to_read(monkeypatch):
    """Leave the search no rows to read once its first descent has ended."""
    monkeypatch.setattr(c, "_SEARCH_ROW_FACTOR", 0)
    monkeypatch.setattr(c, "_SEARCH_ROW_FLOOR", 0)


@pytest.mark.parametrize(
    "golden, generated",
    _both_row_kinds(["tier", "is_staff", "is_active", "is_verified"], _TRIO),
)
def test_a_search_out_of_rows_to_read_still_finishes_its_first_descent(
    monkeypatch, golden, generated
):
    # With nothing to spend, the search still pairs every column once, taking the branch that lines
    # up the most rows each time, and here that is the right assignment. A large result used to run
    # out of rows before it paired every column, so a right answer kept the rule's pairing and
    # scored near 0.
    _nothing_to_read(monkeypatch)
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0
    assert dict(score.column_pairs[1:]) == {
        "is_active": "is_verified", "is_verified": "is_staff", "is_staff": "is_active"
    }


def test_a_search_out_of_rows_after_its_first_descent_keeps_the_best_it_has_found(monkeypatch):
    # Every flag is True on one row and False on the other, so each first pairing lines up both
    # rows, and the tie goes to column order. That branch pairs `is_active` with `f0` and lines up
    # no row once every column is paired. With rows to read, the search goes on to the right
    # assignment. With none, it stops there and keeps the rule's pairing, which completes.
    golden = c.ExecResult(
        columns=["tier", "is_active", "is_verified", "is_staff"],
        rows=[("gold", False, True, True), ("gold", True, False, False)],
    )
    generated = c.ExecResult(
        columns=["t", "f0", "f1", "f2"],
        rows=[("gold", True, True, False), ("gold", False, False, True)],
    )
    assert c.compare_result_sets(golden, generated, match="values", ordered=False).accuracy == 1.0
    _nothing_to_read(monkeypatch)
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.status == "scored" and score.accuracy < 1.0
    assert score.column_pairs == (
        ("tier", "t"), ("is_active", "f0"), ("is_verified", "f1"), ("is_staff", "f2")
    )


def test_a_repeated_row_lines_up_only_as_often_as_both_results_hold_it():
    # No column names a row, and `gold` with True then False appears twice in the answer key. Either
    # first pairing lines up all four rows, so the tie goes to column order and the wrong assignment
    # completes first. Under it the generated rows hold that row once, and `gold` with False then True
    # twice where the answer key holds it once. Counting each such row by the result that holds it
    # more often would call all four rows lined up, and the right assignment would never be reached.
    golden = c.ExecResult(
        columns=["tier", "is_active", "is_verified"],
        rows=[
            ("gold", True, False),
            ("gold", True, False),
            ("gold", False, True),
            ("silver", False, True),
        ],
    )
    generated = c.ExecResult(
        columns=["t", "f0", "f1"],
        rows=[
            ("silver", True, False),
            ("gold", True, False),
            ("gold", False, True),
            ("gold", False, True),
        ],
    )
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.accuracy == 1.0
    assert score.column_pairs == (("tier", "t"), ("is_active", "f1"), ("is_verified", "f0"))


def test_an_ordered_comparison_never_searches(monkeypatch):
    # Equal vectors are equal row for row when the order counts, so any choice lines up the same
    # rows. The pairing is by names first and then in order, exactly as before the search.
    def no_search(*_args, **_kwargs):
        raise AssertionError("an ordered comparison searched")

    monkeypatch.setattr(c, "_line_up_equal_columns", no_search)
    rows = [(True, True, False), (True, True, True), (False, False, True), (False, False, False)]
    golden = c.ExecResult(columns=["a", "b", "c"], rows=rows)
    # `b` and `x` hold the flag `a` and `b` share, and `a` holds `c`'s. `b` takes its namesake, then
    # `a` takes the first equal column left, and `c` pairs with the column labelled `a`.
    generated = c.ExecResult(columns=["b", "x", "a"], rows=rows)
    score = c.compare_result_sets(golden, generated, match="values", ordered=True)
    assert score.accuracy == 1.0
    assert score.column_pairs == (("a", "x"), ("b", "b"), ("c", "a"))


# --- a column that could agree with most columns by chance ------------------------------------------

_CUSTOMERS = [f"c{i}" for i in range(10)]


def _two_columns(names, second):
    return c.ExecResult(columns=list(names), rows=list(zip(_CUSTOMERS, second)))


@pytest.mark.parametrize("ordered", [False, True])
def test_a_flag_that_is_n_on_every_row_does_not_pair_with_another_mostly_n_column(ordered):
    golden = _two_columns(["customer", "is_closed"], ["N"] * 10)
    generated = _two_columns(["customer", "is_hidden"], ["N"] * 9 + ["Y"])
    score = c.compare_result_sets(golden, generated, match="values", ordered=ordered)
    assert score.accuracy == 0.0
    assert score.reason == "no generated column carries the values of: is_closed"
    assert score.unmatched_golden_columns == ("is_closed",)
    assert score.unmatched_generated_columns == ("is_hidden",)
    # Every column that did pair agrees on every row, and the report says so.
    assert score.column_pairs == (("customer", "customer"),) and score.paired_row_share == 1.0


@pytest.mark.parametrize("ordered", [False, True])
def test_a_same_named_flag_pairs_at_any_agreement_however_little_its_values_vary(ordered):
    golden = _two_columns(["customer", "is_closed"], ["N"] * 10)
    generated = _two_columns(["customer", "o.is_closed"], ["N"] * 9 + ["Y"])
    score = c.compare_result_sets(golden, generated, match="values", ordered=ordered)
    assert score.column_pairs == (("customer", "customer"), ("is_closed", "o.is_closed"))
    assert score.column_agreement == (1.0, 0.9)
    assert score.accuracy == 0.9


@pytest.mark.parametrize("ordered", [False, True])
def test_a_renamed_skewed_column_wrong_on_one_row_is_reported_missing(ordered):
    # The accepted cost of the rule above. The column is there under another name and wrong on one
    # row, but most of its rows hold one value, so agreeing on nine rows of ten is no evidence that
    # it is the same column. It is reported missing rather than paired.
    statuses = ["open"] * 8 + ["closed"] * 2
    golden = _two_columns(["customer", "status"], statuses)
    generated = _two_columns(["customer", "state"], ["closed"] + statuses[1:])
    score = c.compare_result_sets(golden, generated, match="values", ordered=ordered)
    assert score.unmatched_golden_columns == ("status",)
    assert score.reason == "no generated column carries the values of: status"


def test_a_balanced_flag_does_not_pair_with_an_unrelated_flag_when_unordered():
    # As multisets, five true of ten and six true of ten overlap on nine rows, whatever rows the
    # values sit on. So under an unordered comparison this flag's values are not telling.
    golden = _two_columns(["customer", "is_admin"], [True] * 5 + [False] * 5)
    generated = _two_columns(["customer", "is_staff"], [False] + [True] * 6 + [False] * 3)
    score = c.compare_result_sets(golden, generated, match="values", ordered=False)
    assert score.unmatched_golden_columns == ("is_admin",) and score.accuracy == 0.0


def test_a_balanced_flag_renamed_and_wrong_on_one_row_still_pairs_when_ordered():
    # Row by row, agreeing on nine rows of ten is evidence for a flag that is true on half of them.
    flags = [True] * 5 + [False] * 5
    golden = _two_columns(["customer", "is_admin"], flags)
    generated = _two_columns(["customer", "admin"], [False] + flags[1:])
    score = c.compare_result_sets(golden, generated, match="values", ordered=True)
    assert score.column_pairs == (("customer", "customer"), ("is_admin", "admin"))
    assert score.column_agreement == (1.0, 0.9) and score.accuracy == 0.9


def _vector(*values):
    return tuple(c.canonical_cell(value) for value in values)


def test_telling_when_ordered_allows_one_value_on_exactly_half_the_rows():
    assert c._telling(_vector("Y", "Y", "N", "N"), ordered=True) is True
    assert c._telling(_vector("Y", "Y", "Y", "N"), ordered=True) is False


@pytest.mark.parametrize("ordered", [False, True])
def test_telling_is_false_for_a_mostly_null_a_single_row_and_an_empty_column(ordered):
    assert c._telling(_vector(None, None, None, 7), ordered=ordered) is False
    assert c._telling(_vector(42), ordered=ordered) is False
    assert c._telling((), ordered=ordered) is False


def test_telling_when_unordered_needs_more_than_half_the_values_distinct():
    assert c._telling(_vector(1, 2, 3, 4, 5, 1, 2, 3, 4, 5), ordered=False) is False
    assert c._telling(_vector(1, 2, 3, 4, 5, 6, 2, 3, 4, 5), ordered=False) is True
    # A flag true on half its rows is telling in place, but not as a multiset.
    assert c._telling(_vector("Y", "Y", "N", "N"), ordered=False) is False
