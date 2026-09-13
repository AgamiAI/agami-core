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
        8: ("could not run", "could_not_compare", "not_comparable", "ask_again"),
        9: ("different answer", "differs", "not_comparable", "query"),
        10: ("different answer", "differs", "different", "semantic_model"),
    }
    assert items[2]["sentence"].endswith("The two queries differ in: filters; the match may not hold on other data.")
    assert items[3]["sentence"].endswith("The two queries differ in: ordered by; a cosmetic difference.")
    assert items[2]["owner"] == "agami" and items[2]["fix_words"] == "fix the examples"
    assert items[2]["change"][0].startswith("Add your query as a prompt example")
    assert items[1]["owner"] == "keep" and items[1]["keep_allowed"] is True and items[1]["fix_words"] == "nothing to fix"
    assert items[3]["keep_allowed"] is True  # a cosmetic difference does not block the keep offer
    assert items[5]["change"][0].startswith("Ask the question again; the same query gave a different answer")
    assert items[8]["change"][0].startswith("Ask the question again in other words; agami's query failed")
    assert items[4]["result"]["unchecked"] == 2  # the open date window part, and the unknown claim
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

