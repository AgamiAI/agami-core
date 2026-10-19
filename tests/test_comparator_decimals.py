"""A golden item can say how many decimal places its answer is good to.

An answer key that rounds in its own SQL returns ``21582.83``. A generated statement that computes
the same thing was never told to round, and returns ``21582.8325``. Under ``values`` each number is
keyed at nine significant digits, where those two differ, so a right answer scored nothing.

``decimals`` on the item is the author's statement of the precision the answer has. These tests pin
what it forgives, and — the larger half — what it must go on refusing: a value that differs at the
stated precision, a whole number, and every level other than ``values``. Synthetic values only —
nothing here names real data.
"""

from __future__ import annotations

import sys
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
from pydantic import ValidationError  # noqa: E402
from semantic_model import comparator as c  # noqa: E402
from semantic_model.golden import GoldenItem  # noqa: E402

ROUNDED_SQL = "SELECT name, ROUND(score, 2) AS score FROM scores ORDER BY score DESC"


def _result(*rows: tuple, columns: tuple[str, ...] = ("name", "score")) -> ExecResult:
    return ExecResult(columns=list(columns), rows=[tuple(row) for row in rows], truncated=False)


def _key(value, decimals: int | None, quantize: bool = True) -> tuple:
    return c.canonical_cell(value, quantize=quantize, decimals=decimals)


# --- the key ---------------------------------------------------------------------------------


def test_a_rounded_and_an_unrounded_spelling_share_a_key_at_the_stated_precision():
    assert _key(Decimal("21582.83"), 2) == _key(Decimal("21582.8325064"), 2)
    # A float from one driver and a numeric from another, as everywhere else in this module.
    assert _key(8.4046762589928058, 2) == _key(Decimal("8.40"), 2)


def test_the_same_two_numbers_stay_apart_without_decimals():
    """The failure this exists for, pinned so the default cannot quietly loosen to cover it."""
    assert _key(Decimal("21582.83"), None) != _key(Decimal("21582.8325064"), None)


def test_a_difference_at_the_stated_precision_keeps_two_numbers_apart():
    assert _key(Decimal("21582.83"), 2) != _key(Decimal("21582.84"), 2)
    assert _key(Decimal("21582.83"), 2) != _key(Decimal("21582.8351"), 2)


@pytest.mark.parametrize(
    ("unrounded", "rounded"),
    [("2.345", "2.35"), ("-2.345", "-2.35"), ("2.344999", "2.34"), ("0.005", "0.01")],
)
def test_a_tie_rounds_half_up_the_way_a_database_rounds_a_numeric(unrounded, rounded):
    """Half-even would put 2.345 on 2.34 and fail it against a key whose own SQL wrote 2.35."""
    assert _key(Decimal(unrounded), 2) == _key(Decimal(rounded), 2)


def test_a_whole_number_is_never_rounded_whatever_decimals_says():
    """An id or a count has no tail to forgive, at this precision or any other."""
    assert _key(1000000007, 0) != _key(1000000008, 0)
    assert _key(Decimal("1000000007"), 2) == _key(1000000007, 2)


def test_zero_decimals_rounds_a_fraction_to_the_whole_number():
    assert _key(Decimal("3.4"), 0) == _key(3, 0)
    assert _key(Decimal("3.6"), 0) != _key(3, 0)


def test_decimals_without_quantize_does_nothing():
    """It sizes the bucket `values` asks for. `exact` asks for none, so it must change nothing."""
    assert _key(Decimal("21582.8325"), 2, quantize=False) == c.canonical_cell(Decimal("21582.8325"))
    assert _key(Decimal("21582.8325"), 2, quantize=False) != _key(
        Decimal("21582.83"), 2, quantize=False
    )


def test_nan_and_infinity_are_untouched_by_decimals():
    assert _key(float("nan"), 2) == c.canonical_cell(float("nan"))
    assert _key(Decimal("Infinity"), 2) == c.canonical_cell(Decimal("Infinity"))


# --- the score -------------------------------------------------------------------------------

GOLDEN = _result(("ada", Decimal("21582.83")), ("bo", Decimal("8.40")), ("cy", Decimal("3.75")))
GENERATED = _result(
    ("ada", Decimal("21582.83250641583")),
    ("bo", Decimal("8.4046762589928058")),
    ("cy", Decimal("3.7450980392156863")),
    columns=("name", "full_score"),
)


def test_values_alone_fails_a_right_answer_that_was_not_rounded():
    score = c.compare_result_sets(GOLDEN, GENERATED, match="values", golden_sql=ROUNDED_SQL)
    assert score.accuracy == 0.0


def test_values_with_decimals_passes_it_and_says_what_precision_was_used():
    score = c.compare_result_sets(
        GOLDEN, GENERATED, match="values", golden_sql=ROUNDED_SQL, decimals=2
    )
    assert (score.status, score.accuracy, score.reason) == ("scored", 1.0, "")
    assert "numbers were compared at 2 decimal place(s)" in score.notes
    # The facts beside the score describe the same comparison the score ran.
    assert score.column_pairs == (("name", "name"), ("score", "full_score"))
    assert score.paired_row_share == 1.0


def test_decimals_still_fails_a_value_that_differs_at_that_precision():
    wrong = _result(
        ("ada", Decimal("21582.83250641583")),
        ("bo", Decimal("8.4146762589928058")),
        ("cy", Decimal("3.7450980392156863")),
        columns=("name", "score"),
    )
    score = c.compare_result_sets(GOLDEN, wrong, match="values", golden_sql=ROUNDED_SQL, decimals=2)
    assert score.accuracy == pytest.approx(2 / 3)


def test_decimals_still_fails_a_wrong_whole_number():
    golden = _result(("ada", 12), ("bo", 7), columns=("name", "orders"))
    wrong = _result(("ada", 12), ("bo", 8), columns=("name", "orders"))
    score = c.compare_result_sets(golden, wrong, match="values", decimals=0, ordered=True)
    assert score.accuracy == 0.5


def test_exact_ignores_decimals_and_claims_no_rounding():
    """A caller that passes it under `exact` gets `exact`. The dataset reader refuses the pair, and
    this is the second line behind it."""
    score = c.compare_result_sets(
        GOLDEN, GENERATED, match="exact", golden_sql=ROUNDED_SQL, decimals=2
    )
    assert score.accuracy == 0.0
    assert not any("decimal place" in note for note in score.notes)


def test_no_decimals_adds_no_note():
    score = c.compare_result_sets(GOLDEN, GOLDEN, match="values", golden_sql=ROUNDED_SQL)
    assert score.accuracy == 1.0
    assert score.notes == ()


# --- the item --------------------------------------------------------------------------------


def _item(**fields) -> GoldenItem:
    return GoldenItem.model_validate(
        {"id": "top-scores", "query": "Who scored highest?", "expected": {"sql_confirmed": False},
         **fields}
    )


def test_an_item_says_nothing_about_decimals_by_default():
    assert _item().decimals is None
    assert _item(match="values").decimals is None


@pytest.mark.parametrize("decimals", [0, 2, 8])
def test_decimals_is_accepted_under_values(decimals):
    assert _item(match="values", decimals=decimals).decimals == decimals


@pytest.mark.parametrize("match", ["exact", "shape", "nonempty"])
def test_decimals_is_refused_at_a_level_that_would_not_read_it(match):
    with pytest.raises(ValidationError, match="decimals is set but match is not values"):
        _item(match=match, decimals=2)


def test_decimals_is_refused_under_the_default_level():
    """An item that says nothing is `exact`, so a bare `decimals` is the same refused pair."""
    with pytest.raises(ValidationError, match="decimals is set but match is not values"):
        _item(decimals=2)


@pytest.mark.parametrize("decimals", [-1, 1.5, "two"])
def test_decimals_must_be_a_whole_number_of_zero_or_more(decimals):
    with pytest.raises(ValidationError):
        _item(match="values", decimals=decimals)
