"""A table that matched only because a different column holds the same repeated value is not a match.

The comparator pairs columns by their values, so a renamed column is the same column. A different
column that happens to hold the same values pairs too: the person's `is_gift` is "N" on every row,
agami returned `is_express`, also "N" on every row, and the table scored accuracy 1.0. Values alone
cannot tell those two apart, so the comparator is not changed. The ledger has the two names and the
person's result, and grades the pair `unresolved`, which the weakest-part rule turns into
`match_unverified`. What is pinned here is that the rule catches that case, and only that case: a
renamed column whose values tell stays a match, a same-named pair stays a match, a scalar row is
untouched, and the card says why in plain words.

Each comparison below is scored by the real comparator, the way `sm compare-results --match values
--unordered` scores it. Synthetic throughout: a `demo` shop's orders.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import parse_reconcile_report  # noqa: E402
import reconcile  # noqa: E402
import render_reconcile_report  # noqa: E402

comparator = pytest.importorskip("semantic_model.comparator")

SENTENCE = ("The two answers match only because your is_gift and agami's is_express hold the same values, "
            "mostly one value repeated. Check that agami returned the column you meant.")


def _csv(columns: list[str], rows: list[list[str]]) -> str:
    return "\n".join([",".join(columns)] + [",".join(row) for row in rows]) + "\n"


def _write(row_dir: Path, name: str, payload) -> None:
    row_dir.mkdir(parents=True, exist_ok=True)
    (row_dir / name).write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def _checked(row_dir: Path) -> None:
    """Every file a successful statement run leaves behind, each saying there was nothing to grade."""
    _write(row_dir, "run.json", {"status": "ok", "rule": None, "kind": None, "detail": None})
    _write(row_dir, "statement-prepare.json", {"aggregates": [], "findings": [], "unchecked": None})
    _write(row_dir, "statement-receipt.json", {"tables": {"items": []}, "joins": {"items": []}, "columns": {"items": []}})
    _write(row_dir, "join-probes.json", {"joins": [], "joins_written": 0, "dropped": 0, "cardinality": {},
                                         "unique_by_model": {}, "unreadable": None})
    _write(row_dir, "filter-values.judge.json", {"literals": [], "unreadable": None})
    _write(row_dir, "question_fit.json", {"fit": "plausible", "reason": None})
    _write(row_dir, "claims.json", {"claims": [{"name": "tables", "status": "agrees",
                                                "generated": ["orders"], "golden": ["orders"]}]})


def _compare(row_dir: Path) -> dict:
    """`sm compare-results --match values --unordered`, as the skill runs it, into `comparison.json`."""
    score = comparator.compare_result_sets(
        comparator.result_from_csv(row_dir / "statement.csv"), comparator.result_from_csv(row_dir / "actual.csv"),
        match="values", ordered=False)
    payload = dataclasses.asdict(score)
    _write(row_dir, "comparison.json", payload)
    return payload


def _table_row(row_dir: Path, yours: tuple[list[str], list[list[str]]], agamis: tuple[list[str], list[list[str]]]) -> dict:
    _checked(row_dir)
    _write(row_dir, "statement.csv", _csv(*yours))
    _write(row_dir, "actual.csv", _csv(*agamis))
    return _compare(row_dir)


REGIONS = ["north", "south", "east", "west", "centre", "coast"]


def _run(tmp_path: Path, rows: dict[int, tuple[tuple, tuple]]) -> Path:
    """A run whose rows are tables: intake, agami's answer, both results, the comparison, the ledger and
    the record, each through the verb the skill uses."""
    run = tmp_path / "20260916-120000"
    intake = []
    for n, (yours, agamis) in rows.items():
        row_dir = run / "rows" / str(n)
        statement = f"SELECT {', '.join(yours[0])} FROM orders"
        intake.append({"row": n, "label": f"Orders {n}", "question": "Which orders are gifts, by region?",
                       "statement": statement, "expected": None,
                       "provenance": {"shape": "b", "source": "the sheet", "file": "plan.csv", "line": n + 1}})
        _table_row(row_dir, yours, agamis)
        sql = f"SELECT {', '.join(agamis[0])} FROM orders"
        _write(row_dir, "agami-answer.json", {"sql": sql, "statements": [sql], "error": None})
        assert reconcile.main(["ledger", "--row-dir", str(row_dir), "--with-claims"]) == 0
    _write(run, "intake.json", {"rows": intake})
    for n in rows:
        reconcile.record(run, n)
    return run


def _records(run: Path) -> dict[int, dict]:
    return {rec["row"]: rec for rec in (json.loads(line) for line in (run / "rows.jsonl").read_text().splitlines() if line.strip())}


def test_a_different_column_holding_the_same_repeated_value_is_not_a_match(tmp_path, capsys):
    yours = (["region", "is_gift"], [[r, "N"] for r in REGIONS])
    agamis = (["region", "is_express"], [[r, "N"] for r in REGIONS])
    run = _run(tmp_path, {1: (yours, agamis)})
    capsys.readouterr()

    # The comparator, unchanged, scores the swap a full match: this is the false match being fixed.
    score = json.loads((run / "rows" / "1" / "comparison.json").read_text())
    assert score["accuracy"] == 1.0 and ["is_gift", "is_express"] in score["column_pairs"]

    ledger = json.loads((run / "rows" / "1" / "ledger.json").read_text())
    part = next(p for p in ledger["rows"] if p["part"] == "value_pair:is_gift")
    assert part["verdict"] == "unresolved" and ledger["verdict"] == "unresolved"
    assert part["evidence"] == {"yours": "is_gift", "agami": "is_express", "rows": 6}
    assert "N" not in json.dumps(part["evidence"])  # rows.jsonl carries the ledger, and no result value
    assert not any(p["part"].startswith("value_pair:region") for p in ledger["rows"])  # a same-named pair

    rec = _records(run)[1]
    assert rec["match"] is True and rec["status"] == "match_unverified"

    item = reconcile.report_items(run)[0]
    assert item["sentence"] == SENTENCE
    assert item["status_words"] == "same answer, but part of your query could not be checked"
    assert item["keep_allowed"] is False and item["owner"] != "keep"
    assert 1 not in parse_reconcile_report.keepable_rows(run)
    row = next(r for r in item["diff"] if r.get("family") == "value_pair")
    assert row["key"] == "agami's column for your is_gift" and row["section"] == "data" and row["state"] == "open"
    assert (row["yours"], row["agami"]) == ("is_gift", "is_express")
    assert item["summaries"]["data"].endswith("Your is_gift and agami's is_express match only through one repeated value.")
    # Every check ran, so none is counted as one that could not, and the change does not say otherwise.
    assert item["result"]["unchecked"] == 0 and "couldn't be run" not in item["summaries"]["checks"]
    assert item["fix"] == "none" and item["change"][0].startswith("Open Data to see the two columns side by side.")
    assert item["todo"] == ["Check the column agami returned."]


def test_the_report_page_carries_the_sentence(tmp_path, capsys):
    yours = (["region", "is_gift"], [[r, "N"] for r in REGIONS])
    agamis = (["region", "is_express"], [[r, "N"] for r in REGIONS])
    run = _run(tmp_path, {1: (yours, agamis)})
    out = run / "report.html"
    assert render_reconcile_report.main(["--title", "Reconcile · demo", "--profile", "demo",
                                         "--run-dir", str(run), "--out", str(out)]) == 0
    capsys.readouterr()
    page = out.read_text(encoding="utf-8")
    assert SENTENCE in page and "agami's column for your is_gift" in page
    assert reconcile.check_run(run)["ok"] is True


def test_a_renamed_column_whose_values_tell_stays_a_match(tmp_path):
    yours = (["region", "orders"], [[r, str(10 + i)] for i, r in enumerate(REGIONS)])
    agamis = (["region_name", "order_count"], [[r, str(10 + i)] for i, r in enumerate(REGIONS)])
    run = _run(tmp_path, {1: (yours, agamis)})
    assert json.loads((run / "rows" / "1" / "comparison.json").read_text())["accuracy"] == 1.0
    ledger = json.loads((run / "rows" / "1" / "ledger.json").read_text())
    assert not any(p["part"].startswith("value_pair:") for p in ledger["rows"]) and ledger["verdict"] == "confirmed"
    assert _records(run)[1]["status"] == "match"
    item = reconcile.report_items(run)[0]
    assert item["sentence"].startswith("The two answers match row for row")


def test_a_same_named_pair_stays_a_match_after_the_comparator_s_folding(tmp_path):
    # A qualifier and a different case are the same name to the comparator, so they are here too,
    # however little the column's values tell.
    yours = (["region", "o.IS_GIFT"], [[r, "N"] for r in REGIONS])
    agamis = (["region", "is_gift"], [[r, "N"] for r in REGIONS])
    run = _run(tmp_path, {1: (yours, agamis)})
    score = json.loads((run / "rows" / "1" / "comparison.json").read_text())
    assert ["o.IS_GIFT", "is_gift"] in score["column_pairs"]
    assert _records(run)[1]["status"] == "match"


@pytest.mark.parametrize("fills, flagged", [(6, True), (4, True), (3, False)])
def test_one_value_must_fill_more_than_half_the_rows(tmp_path, fills, flagged):
    flags = ["N"] * fills + ["Y"] * (len(REGIONS) - fills)
    yours = (["region", "is_gift"], [[r, f] for r, f in zip(REGIONS, flags)])
    agamis = (["region", "is_express"], [[r, f] for r, f in zip(REGIONS, flags)])
    run = _run(tmp_path, {1: (yours, agamis)})
    assert _records(run)[1]["status"] == ("match_unverified" if flagged else "match")


def test_a_one_row_table_repeats_nothing_and_stays_a_match(tmp_path):
    # With one row every column is "one value on every row", and a legitimate rename of a one-row
    # result is the common case. A single row repeats nothing, so it is not counted.
    run = _run(tmp_path, {1: ((["revenue", "orders"], [["4200", "17"]]), (["total_revenue", "order_count"], [["4200", "17"]]))})
    assert _records(run)[1]["status"] == "match"


def test_a_scalar_row_is_untouched(tmp_path):
    row_dir = tmp_path / "rows" / "1"
    _checked(row_dir)
    _write(row_dir, "statement.csv", "revenue\n4200\n")
    _write(row_dir, "actual.csv", "total\n4200\n")
    _write(row_dir, "diff.json", reconcile.diff(4200.0, 4200.0))
    ledger = reconcile.ledger(row_dir, with_claims=True)
    assert ledger["verdict"] == "confirmed" and not any(p["part"].startswith("value_pair:") for p in ledger["rows"])
    # Even when a comparison was scored for it, one cell is one row and is never flagged.
    _compare(row_dir)
    assert not any(p["part"].startswith("value_pair:") for p in reconcile.ledger(row_dir, with_claims=True)["rows"])


def test_only_a_full_match_between_two_statements_is_read(tmp_path):
    row_dir = tmp_path / "rows" / "1"
    _table_row(row_dir, (["region", "is_gift"], [[r, "N"] for r in REGIONS]),
               (["region", "is_express"], [[r, "N"] for r in REGIONS]))
    assert any(p["part"] == "value_pair:is_gift" for p in reconcile.ledger(row_dir, with_claims=True)["rows"])
    # Without a second statement there is nothing to compare, and the comparison is not read.
    assert not any(p["part"].startswith("value_pair:") for p in reconcile.ledger(row_dir)["rows"])
    # A table that did not match is already not a match; the part adds nothing to it.
    score = json.loads((row_dir / "comparison.json").read_text())
    for changed in ({"accuracy": 0.5}, {"status": "unscored", "accuracy": None}):
        _write(row_dir, "comparison.json", dict(score, **changed))
        assert not any(p["part"].startswith("value_pair:") for p in reconcile.ledger(row_dir, with_claims=True)["rows"])
    # A result that is gone is skipped.
    _write(row_dir, "comparison.json", score)
    (row_dir / "statement.csv").write_text("")
    assert not any(p["part"].startswith("value_pair:") for p in reconcile.ledger(row_dir, with_claims=True)["rows"])


ALTERNATING = ["Y", "N"] * 3


@pytest.mark.parametrize("first, second, flagged", [
    # Your second is_gift is "N" on every row and paired with agami's is_express: the false match. Read
    # by name, the pair read your first is_gift, whose values vary, and the row stayed a match.
    (ALTERNATING, ["N"] * 6, True),
    # Your first is_gift is the constant one, and it paired with agami's is_gift. Read by name, the
    # is_express pair read that first column too, and the row was flagged from the wrong values.
    (["N"] * 6, ALTERNATING, False),
])
def test_a_repeated_column_name_is_read_by_position(tmp_path, first, second, flagged):
    rows = [[r, a, b] for r, a, b in zip(REGIONS, first, second)]
    run = _run(tmp_path, {1: ((["region", "is_gift", "is_gift"], rows), (["region", "is_gift", "is_express"], rows))})
    score = json.loads((run / "rows" / "1" / "comparison.json").read_text())
    assert score["accuracy"] == 1.0
    assert score["column_pairs"] == [["region", "region"], ["is_gift", "is_gift"], ["is_gift", "is_express"]]
    ledger = json.loads((run / "rows" / "1" / "ledger.json").read_text())
    evidence = [p["evidence"] for p in ledger["rows"] if p["part"].startswith("value_pair:")]
    assert evidence == ([{"yours": "is_gift", "agami": "is_express", "rows": 6}] if flagged else [])
    assert _records(run)[1]["status"] == ("match_unverified" if flagged else "match")


def test_several_pairs_are_named_in_one_sentence(tmp_path):
    yours = (["region", "is_gift", "is_rush"], [[r, "N", "0"] for r in REGIONS])
    agamis = (["region", "is_express", "is_sample"], [[r, "N", "0"] for r in REGIONS])
    run = _run(tmp_path, {1: (yours, agamis)})
    item = reconcile.report_items(run)[0]
    assert item["sentence"] == ("The two answers match only because two pairs of columns hold the same values, mostly one "
                                "value repeated: your is_gift and agami's is_express; your is_rush and agami's is_sample. "
                                "Check that agami returned the columns you meant.")
    assert item["summaries"]["data"].endswith("Two of your columns match agami's only through one repeated value.")


def test_other_checks_that_did_not_pass_are_still_named_and_a_different_selection_asks_for_an_example(tmp_path, capsys):
    yours = (["region", "is_gift"], [[r, "N"] for r in REGIONS])
    agamis = (["region", "is_express"], [[r, "N"] for r in REGIONS])
    run = _run(tmp_path, {1: (yours, agamis)})
    row_dir = run / "rows" / "1"
    _write(row_dir, "statement-prepare.json", {"aggregates": [], "findings": [], "unchecked": "the pre-flight could not read it"})
    _write(row_dir, "claims.json", {"claims": [
        {"name": "tables", "status": "agrees", "generated": ["orders"], "golden": ["orders"]},
        {"name": "outputs", "status": "differs", "generated": ["orders.region", "orders.is_express"],
         "golden": ["orders.region", "orders.is_gift"]}]})
    assert reconcile.main(["ledger", "--row-dir", str(row_dir), "--with-claims"]) == 0
    capsys.readouterr()
    assert reconcile.record(run, 1)["status"] == "match_unverified"
    item = reconcile.report_items(run)[0]
    assert item["sentence"] == (SENTENCE + " These checks did not pass either: fan-out."
                                " The two queries differ in: selects; the match may not hold on other data.")
    # agami selected another column, so the fix is the one a different query with nothing wrong behind it gets.
    assert item["fix"] == "examples" and item["keep_allowed"] is False
    assert item["result"]["unchecked"] == 1  # the pre-flight that could not run, and not the repeated value



def test_a_check_that_could_not_run_is_still_said_beside_the_repeated_value(tmp_path, capsys):
    yours = (["region", "is_gift"], [[r, "N"] for r in REGIONS])
    agamis = (["region", "is_express"], [[r, "N"] for r in REGIONS])
    run = _run(tmp_path, {1: (yours, agamis)})
    row_dir = run / "rows" / "1"
    _write(row_dir, "statement-prepare.json", {"aggregates": [], "findings": [], "unchecked": "the pre-flight could not read it"})
    assert reconcile.main(["ledger", "--row-dir", str(row_dir), "--with-claims"]) == 0
    capsys.readouterr()
    assert reconcile.record(run, 1)["status"] == "match_unverified"
    item = reconcile.report_items(run)[0]
    # The same query with nothing to fix. It still says a check could not run, as the checks summary
    # does, and adds the look at the two columns — but never opens with "Nothing to change", which
    # would contradict the instruction beside it.
    assert item["fix"] == "none" and item["result"]["unchecked"] == 1
    assert "couldn't be run" in item["summaries"]["checks"]
    assert item["change"] == ["Some checks could not run against the database, so this row is not offered as an example.",
                              "Open Data to see the two columns side by side. If agami returned the wrong column, "
                              "add your query as a prompt example for this question through /agami-save-correction."]
    assert not any("Nothing to change" in line for line in item["change"])
    assert item["todo"] == ["Check the column agami returned."]
