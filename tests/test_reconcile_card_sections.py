"""The card is a verdict, one thing to do, and three closed sections.

Sandeep ran five statements against a real warehouse and the card buried the answer. What is pinned
here is what the redesign owes him: a row knows which section it belongs to, a line nobody can act on
is gone, a summary names the exception when there is one, and the five-row sample is bounded at the
surface that publishes it rather than trusted from whatever built it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402
import render_reconcile_report as rr  # noqa: E402


def _row(key, state, section, family=None, **kw):
    row = {"key": key, "state": state, "section": section, "yours": kw.pop("yours", None),
           "agami": None, "note": None}
    if family:
        row["family"] = family
    row.update(kw)
    return row


# --- every row knows its section -----------------------------------------------------------------


def test_a_diff_row_carries_the_section_it_belongs_to():
    rec = {"status": "mismatch", "statement": "SELECT 1", "sql": "SELECT 1",
           "recorded": {"columns": ["n"], "row_count": 3}, "statement_recorded": {"columns": ["n"], "row_count": 3},
           "claims": {"claims": [{"name": "tables", "status": "agrees", "generated": ["t"], "golden": ["t"]}]},
           "ledger": {"rows": [{"part": "join:a-b", "verdict": "confirmed", "kind": None, "depends_on": [],
                                "evidence": {}, "note": ""},
                               {"part": "question_fit", "verdict": "confirmed", "kind": None, "depends_on": [],
                                "evidence": {"fit": "plausible"}, "note": ""}],
                      "verdict": "confirmed", "counts": {}}}
    rows, _ = reconcile._diff_rows(rec, None)
    by_key = {r["key"]: r for r in rows}
    assert by_key["answer"]["section"] == "data"
    assert by_key["tables read"]["section"] == "sql"
    assert by_key["join a to b"]["section"] == "model"
    # the fit is a judgment about the statement against its question, so it sits with the queries
    assert by_key["answers the question"]["section"] == "sql"


# --- what a reader cannot act on is gone ---------------------------------------------------------


def test_a_wide_column_having_no_declared_value_list_earns_no_line():
    # The probe reads the whole column and ignores the statement's own filters, so a query pinned to
    # one city still reported "more than 25 distinct values". True, and no use to anybody.
    rows = [_row("value list for schools.city", "noted", "model", "values_declared",
                 yours="too many values to list")]
    assert reconcile._condense(rows) == []


def test_a_value_list_that_is_actually_missing_is_kept():
    rows = [_row("value list for orders.state", "gap", "model", "values_declared", yours="no value list")]
    assert len(reconcile._condense(rows)) == 1


def test_a_join_that_dropped_nothing_earns_no_line_and_one_that_dropped_rows_does():
    nothing = [_row("rows dropped by join a to b", "noted", "model", "dropped_rows", yours="0 of 4,000 a rows")]
    some = [_row("rows dropped by join a to b", "noted", "model", "dropped_rows", yours="63,931 of 269,158 a rows")]
    assert reconcile._condense(nothing) == []
    assert len(reconcile._condense(some)) == 1


def test_twelve_value_checks_that_all_passed_become_one_counted_line():
    rows = [_row(f"value t.c={n}", "held", "model", "literal", yours="exists") for n in range(12)]
    out = reconcile._condense(rows)
    assert len(out) == 1 and out[0]["key"] == "12 values checked, all exist"
    assert out[0]["rolled"] == 12 and out[0]["state"] == "held"


def test_a_value_check_that_failed_keeps_every_member_visible():
    rows = [_row("value t.c=a", "held", "model", "literal", yours="exists"),
            _row("value t.c=b", "defect", "model", "literal", yours="matches no rows")]
    assert len(reconcile._condense(rows)) == 2


# --- the summaries -------------------------------------------------------------------------------


def test_a_summary_names_the_exception_and_counts_when_there_is_none():
    rows = [_row("values", "held", "data", yours="9 of 10 rows match"),
            _row("columns", "defect", "data", yours=["a", "b"], yours_hi=["b"]),
            _row("tables read", "differs", "sql"), _row("limit", "held", "sql"),
            _row("12 values checked, all exist", "held", "model", "literal", rolled=12),
            _row("default filter on orders", "gap", "model", "default_filter")]
    s = reconcile._summaries(rows, {"label": "different answer", "data": "differs"})
    assert s["data"] == "9 of 10 rows match · 1 column only yours"
    assert s["sql"] == "1 of 2 differ: tables read"
    # the roll-up stands for the checks it replaced, so the count is of checks, not of lines
    assert s["model"] == "12 passed · 1 did not: default filter on orders"


def test_a_doubtful_fit_leads_the_sql_summary_because_it_is_the_thing_worth_opening():
    rows = [_row("answers the question", "open", "sql", "question_fit", yours="doubtful"),
            _row("limit", "differs", "sql")]
    s = reconcile._summaries(rows, {"label": "different answer", "data": "differs"})
    assert s["sql"].startswith("answers the question: doubtful · ")


def test_a_row_that_could_not_be_compared_reads_its_label():
    rows = [_row("answer", "open", "data", yours="failed")]
    s = reconcile._summaries(rows, {"label": "different query, answer not compared", "data": "could_not_compare"})
    assert s["data"] == "different query, answer not compared"


# --- the sample ----------------------------------------------------------------------------------


def _csvs(tmp_path, yours, agami):
    d = tmp_path / "rows" / "1"
    d.mkdir(parents=True)
    (d / "statement.csv").write_text(yours, encoding="utf-8")
    (d / "actual.csv").write_text(agami, encoding="utf-8")
    return d


def test_the_sample_puts_the_rows_that_disagree_first(tmp_path):
    d = _csvs(tmp_path,
              "name,n\na,1\nb,2\nc,3\nd,4\ne,5\nf,6\n",
              "name,n\na,1\nb,99\nc,3\nd,4\ne,5\nf,6\n")
    sample = reconcile._sample(d, {"column_pairs": [["name", "name"], ["n", "n"]]})
    assert sample["rows"][0]["yours"] == ["b", "2"] and sample["rows"][0]["same"] is False
    assert sample["shown"] == 5 and sample["total"] == 6 and sample["aligned"] is True


def test_the_sample_falls_back_to_names_when_the_comparator_reported_no_pairs(tmp_path):
    # Differing row counts make the comparator short-circuit before pairing anything, which is
    # exactly the row a reader wants to look at.
    d = _csvs(tmp_path, "name,n\na,1\nb,2\n", "name,n\na,1\n")
    sample = reconcile._sample(d, {"column_pairs": []})
    assert sample["by_name"] is True and sample["aligned"] is False
    assert sample["pairs"] == [["name", "name"], ["n", "n"]]
    assert all(r["agami"] is None for r in sample["rows"])


def test_no_sample_when_a_side_never_ran(tmp_path):
    d = _csvs(tmp_path, "name\na\n", "")
    assert reconcile._sample(d, {"column_pairs": [["name", "name"]]}) is None


# --- the page bounds the sample itself ------------------------------------------------------------

_ITEM = {"row": 1, "question": "q", "status": "mismatch", "diff": []}


def test_the_renderer_refuses_more_than_five_rows_whatever_built_them():
    six = {"pairs": [["a", "a"]], "rows": [{"yours": ["x"], "agami": ["x"], "same": True} for _ in range(6)]}
    with pytest.raises(ValueError, match="at most 5 rows"):
        rr._validate_item(dict(_ITEM, sample=six), 0)


def test_the_renderer_refuses_a_sample_that_is_not_the_shape_it_expects():
    with pytest.raises(ValueError, match="two-name pairs"):
        rr._validate_item(dict(_ITEM, sample={"pairs": ["a"], "rows": []}), 0)
    with pytest.raises(ValueError, match="'yours' list"):
        rr._validate_item(dict(_ITEM, sample={"pairs": [["a", "a"]], "rows": [{"agami": ["x"]}]}), 0)


def test_a_five_row_sample_reaches_the_page(tmp_path):
    sample = {"pairs": [["name", "name"]], "rows": [{"yours": ["a"], "agami": ["a"], "same": True}],
              "shown": 1, "total": 1, "aligned": True, "by_name": False, "only_yours": [], "only_agami": []}
    html = rr.render(title="t", profile="p", run="r", items=[dict(_ITEM, sample=sample)])
    assert '"sample"' in html and "function grid(sample)" in html
    # and the page still refuses result rows everywhere else
    assert "result rows are never rendered" in Path(rr.__file__).read_text(encoding="utf-8")


# --- the page is in the person's order, not the checkpoint's ---------------------------------------


def test_cards_render_in_row_order_however_the_checkpoint_was_written(tmp_path):
    # `record` replaces a row by appending a new line, so a row re-run after a fix ends up last in
    # rows.jsonl. That is right for an append log and wrong for a page.
    run = tmp_path / "20260915-000000"
    (run / "rows").mkdir(parents=True)
    written = [1, 3, 4, 5, 2]
    (run / "rows.jsonl").write_text("".join(
        json.dumps({"row": n, "label": f"r{n}", "question": f"q{n}", "status": "match", "match": True,
                    "recorded": {"columns": ["n"], "rows": [[1]]},
                    "ledger": {"rows": [], "verdict": "confirmed", "counts": {}}, "ledger_verdict": "confirmed"}) + "\n"
        for n in written), encoding="utf-8")
    assert [i["row"] for i in reconcile.report_items(run)] == [1, 2, 3, 4, 5]


# --- the decision block ---------------------------------------------------------------------------


def test_the_decisions_are_a_named_group_not_five_anonymous_radios():
    # Five radios sharing a name are a group; a group with no legend has no accessible name, so a
    # reader is told five options and never what they decide.
    html = rr.render(title="t", profile="p", run="r", items=[dict(_ITEM, fix="query", owner="you")])
    assert '<fieldset class="grade opts"><legend>What to do</legend>' in html


def test_the_suggestion_is_selected_before_anyone_clicks_and_the_block_carries_it():
    html = rr.render(title="t", profile="p", run="r", items=[dict(_ITEM, fix="query", owner="you")])
    # one map, read by the radios and by the seeding, so the page and the pasted-back block agree
    assert "function suggestionFor(item)" in html
    assert "function seedDecisions()" in html and "seedDecisions();\n    renderItems();" in html
    # and it is still labelled, so a reader sees the tool chose it
    assert "' <span class=\"tag suggested\">suggested</span>'" in html
