"""Two facts on every card, read by code: the result (does the data match, are the two queries the same)
and the fix (your query, the semantic model, the examples, the question, agami again, nothing)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402

from test_reconcile_report_items import (  # noqa: E402
    DEFECT,
    ERROR,
    GAPS_AND_GRAIN,
    SCALAR_MATCH,
    TABLE_DIFF,
    _part,
    _run,
)


def _claims(*cs):
    return {"claims": [{"name": n, "status": st, "golden": y, "generated": g} for n, st, y, g in cs]}


def _items(tmp_path, records):
    return {i["row"]: i for i in reconcile.report_items(_run(tmp_path, records))}


def test_every_result_label_and_its_fix(tmp_path):
    same = SCALAR_MATCH  # data matches, every claim agrees
    diff_query = dict(SCALAR_MATCH, row=2, claims=_claims(("tables", "agrees", ["orders"], ["orders"]),
                                                          ("filter_predicates", "differs", ["eq(orders.status, 'paid')"], ["eq(orders.state, 'paid')"])))
    cosmetic = dict(SCALAR_MATCH, row=3, claims=_claims(("tables", "agrees", ["orders"], ["orders"]), ("ordering", "differs", [["a", "asc"]], [])))
    partly = dict(TABLE_DIFF, row=4)  # same rows, extra columns on your side
    same_query_diff_answer = dict(SCALAR_MATCH, row=5, status="mismatch", match=False, actual=1.0, delta_pct=-0.99,
                                  recorded={"columns": ["revenue"], "rows": [[1.0]]})
    diff_both = dict(SCALAR_MATCH, row=6, status="mismatch", match=False, actual=1.0, delta_pct=-0.99, recorded={"columns": ["revenue"], "rows": [[1.0]]},
                     claims=_claims(("tables", "differs", ["orders"], ["orders", "refunds"])))
    number_only = {"row": 7, "label": "Orders", "question": "How many?", "expected": 10.0, "actual": 9.0, "delta_pct": -0.1, "match": False, "status": "mismatch",
                   "recorded": {"columns": ["n"], "rows": [[9.0]]}, "provenance": {"shape": "c"}}
    items = _items(tmp_path, [same, diff_query, cosmetic, partly, same_query_diff_answer, diff_both, number_only, dict(ERROR, row=8), dict(DEFECT, row=9), dict(GAPS_AND_GRAIN, row=10)])
    got = {r: (i["result"]["label"], i["result"]["data"], i["result"]["query"], i["fix"]) for r, i in items.items()}
    assert got == {
        1: ("match", "matches", "same", "none"),
        2: ("same answer, different query", "matches", "different", "examples"),
        3: ("same answer, different query", "matches", "different", "none"),
        4: ("same rows, different columns", "partly", "different", "query"),
        5: ("same query, different answer", "differs", "same", "ask_again"),
        6: ("different answer", "differs", "different", "examples"),
        7: ("different answer", "differs", "not_comparable", "question"),
        8: ("could not compare", "could_not_compare", "not_comparable", "ask_again"),
        9: ("different answer", "differs", "not_comparable", "query"),
        10: ("different answer", "differs", "different", "semantic_model"),
    }
    assert items[2]["sentence"].endswith("The two queries differ in: filters; the match may not hold on other data.")
    assert items[3]["sentence"].endswith("The two queries differ in: ordered by; a cosmetic difference.")
    assert items[2]["owner"] == "keep" and items[2]["keep_allowed"] is True and items[2]["fix_words"] == "add an example"  # the gate's word, whatever the fix
    assert items[2]["change"][0].startswith("Add your query as a prompt example")
    assert items[1]["owner"] == "keep" and items[1]["keep_allowed"] is True and items[1]["fix_words"] == "nothing to fix"
    assert items[3]["keep_allowed"] is True  # a cosmetic difference does not block the keep offer
    assert items[5]["change"][0].startswith("Ask the question again; the same query gave a different answer")
    assert items[8]["change"][0].startswith("Ask the question again in other words; agami's query failed")
    assert items[4]["result"]["unchecked"] == 1  # the date window: one check, counted once
    assert items[4]["result"]["differs_in"] == ["columns"]


def test_a_different_query_is_a_noted_fact_and_the_row_still_matches(tmp_path):
    row_dir = tmp_path / "r"; row_dir.mkdir()
    (row_dir / "run.json").write_text(json.dumps({"status": "ok", "rule": None, "kind": None, "detail": None}))
    (row_dir / "claims.json").write_text(json.dumps({"unreadable": {"sql_file": None, "against_sql_file": None}, "temporal_predicates": {"sql_file": 0, "against_sql_file": 0},
        "claims": [{"name": "filter_predicates", "status": "differs", "generated": ["a"], "golden": ["b"]},
                   {"name": "date_window", "status": "unknown", "generated": None, "golden": None}]}))
    ledger = reconcile.ledger(row_dir, with_claims=True)
    parts = {r["part"]: r for r in ledger["rows"]}
    assert parts["predicates"]["verdict"] == "noted" and parts["date_window"]["verdict"] == "confirmed"
    # noted never raises the row's verdict: with every measured part confirmed, a matching number is a match
    assert reconcile._VERDICT_RANK[reconcile.NOTED] < reconcile._VERDICT_RANK[reconcile.CONFIRMED]
    assert reconcile.row_status(True, reconcile.CONFIRMED) == "match"


def test_a_doubtful_fit_is_the_questions_fix_even_when_the_answer_matches(tmp_path):
    doubtful = json.loads(json.dumps(SCALAR_MATCH)); doubtful["row"] = 1; doubtful["status"] = "match_unverified"
    doubtful["ledger"]["rows"] = [_part("runs", "confirmed"), _part("question_fit", "unresolved", {"fit": "doubtful"}, note="the grain differs")]
    doubtful["claims"] = _claims(("tables", "differs", ["orders", "payments"], ["orders"]))
    item = reconcile.report_items(_run(tmp_path, [doubtful]))[0]
    assert item["result"]["label"] == "same answer, different query" and item["fix"] == "question" and item["keep_allowed"] is False
    assert item["change"][0].startswith("Reword the question, or change your query, so they ask the same thing.")


def test_columns_are_compared_by_data_and_a_renamed_column_is_the_same_column(tmp_path):
    """The comparator pairs columns by values. Yours with three extra columns, all others paired under
    other names, is "same rows, different columns", and the pairs are named; a table whose every
    column paired is a match even when every name differs."""
    partly = dict(TABLE_DIFF, row=1, recorded={"columns": ["o.number", "o.status", "o.region", "o.region_code", "o.placed_at"], "rows": []},
                  comparison={"result_set": {"accuracy": 0.0, "reason": "no generated column carries the values of: planned_ship_date, delivered_at, channel",
                                             "unmatched_golden_columns": ["planned_ship_date", "delivered_at", "channel"],
                                             "column_pairs": [["number", "o.number"], ["status", "o.status"], ["placed_at", "o.placed_at"], ["region", "o.region"], ["region_code", "o.region_code"]],
                                             "unmatched_generated_columns": [], "golden_row_count": 21, "generated_row_count": 21}})
    renamed = dict(TABLE_DIFF, row=2, status="match", statement_recorded={"columns": ["number", "status"], "row_count": 21}, recorded={"columns": ["o.number", "o.status"], "rows": []},
                   comparison={"result_set": {"accuracy": 1.0, "reason": "", "unmatched_golden_columns": [], "column_pairs": [["number", "o.number"], ["status", "o.status"]],
                                              "unmatched_generated_columns": [], "golden_row_count": 21, "generated_row_count": 21}},
                   ledger={"rows": [_part("runs", "confirmed"), _part("question_fit", "confirmed", {"fit": "plausible"})], "verdict": "confirmed", "counts": {}}, claims=None)
    items = _items(tmp_path, [partly, renamed])
    rows1 = {r["key"]: r for r in items[1]["diff"]}
    assert items[1]["result"]["label"] == "same rows, different columns" and items[1]["fix"] == "query"
    assert rows1["columns"]["state"] == "defect" and rows1["columns"]["yours_hi"] == ["planned_ship_date", "delivered_at", "channel"] and rows1["columns"].get("agami_hi") in (None, [])
    assert rows1["columns"]["note"] is None and ["number", "o.number"] in rows1["columns"]["renamed"]
    assert rows1["values"]["state"] == "held" and rows1["values"]["yours"] == "identical on the 5 paired columns"
    assert items[1]["prefill"]["fix"] == "remove planned_ship_date, delivered_at, channel"
    rows2 = {r["key"]: r for r in items[2]["diff"]}
    assert items[2]["result"]["label"] == "match" and rows2["columns"]["state"] == "held" and rows2["columns"]["renamed"] == [["number", "o.number"], ["status", "o.status"]]


def test_the_change_text_the_prefill_and_the_fix_come_from_one_source(tmp_path):
    items = _items(tmp_path, [dict(GAPS_AND_GRAIN, row=1), dict(DEFECT, row=2)])
    gaps = items[1]
    assert gaps["fix"] == "semantic_model" and gaps["change"][0].startswith("The semantic model is missing: value list for")
    assert gaps["prefill"]["change"].startswith("add value list for") and gaps["prefill"]["reword"] == GAPS_AND_GRAIN["question"]
    assert gaps["change"][1].startswith("Also:")  # the doubtful fit rides along as a second line
    defect = items[2]
    assert defect["fix"] == "query" and defect["prefill"]["fix"] == "value orders.status='Delivered'"


def test_identical_column_names_with_different_row_counts_are_one_column_set(tmp_path):
    """Values cannot pair when the row counts differ, so the columns fall back to names: identical
    names read as the same columns, and only the rows check carries the difference."""
    rec = dict(TABLE_DIFF, row=1, statement_recorded={"columns": ["department", "pending_items"], "row_count": 13},
               recorded={"columns": ["department", "pending_items"], "rows": []},
               comparison={"result_set": {"accuracy": 0.0, "reason": "the answer key has 13 rows and the generated result has 759",
                                          "unmatched_golden_columns": [], "column_pairs": [], "unmatched_generated_columns": [],
                                          "golden_row_count": 13, "generated_row_count": 759}})
    item = reconcile.report_items(_run(tmp_path, [rec]))[0]
    rows = {r["key"]: r for r in item["diff"]}
    assert rows["rows"]["state"] == "defect" and rows["rows"]["yours"] == "13 rows" and rows["rows"]["agami"] == "759 rows"
    assert rows["columns"]["state"] == "held" and not rows["columns"].get("yours_hi") and not rows["columns"].get("agami_hi")
    assert item["result"]["data"] == "differs" and "columns" not in item["result"]["differs_in"]



def test_a_different_projection_alone_is_a_different_query(tmp_path):
    """The eighth claim: two statements that select different expressions are not the same query,
    however the other seven agree."""
    only_outputs = dict(SCALAR_MATCH, row=2, claims=_claims(("tables", "agrees", ["orders"], ["orders"]),
                                                            ("outputs", "differs", ["sum(orders.amount)"], ["avg(orders.amount)"])))
    items = _items(tmp_path, [only_outputs])
    assert items[2]["result"]["query"] == "different" and items[2]["result"]["label"] == "same answer, different query"
    assert items[2]["result"]["differs_in"] == ["selects"] and items[2]["fix"] == "examples"
