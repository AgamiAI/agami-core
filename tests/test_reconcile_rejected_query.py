"""A row whose query did not run is a semantic-model finding, not a row to ask again.

The first live run of `--via mcp` failed two rows the same way: the semantic model named a column and
a table the database did not have. The report told the reader to "ask agami again", the one action
that cannot work, while the evidence in the same row named the actual fix. The card now says in plain
words what stopped the query and why.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402

REJECTION = ("The statement referenced a column this database does not have. If the model was built "
             "against an older schema, re-introspect the datasource.")
COLUMN_REFUSAL = ("query references column(s) not in the semantic model: orders.units_sold — only columns "
                  "declared on the model's tables may be queried.")
# The three resource limits, in the executor's own words. Each arrives as a `refused` like a scope
# block does, and each fires once the statement is away: the first drops a result that came back.
ROW_CAP = "The result exceeded the 1000-row limit, so it was not returned."
TIMEOUT = "The statement ran longer than the 30s limit and was cancelled."
ABANDONED = "The executor did not return within the 35s limit and the query was abandoned."
FAILED_SQL = "SELECT COUNT(DISTINCT order_id) FROM sales.orders WHERE region_code = '41'"


def _row(**over) -> dict:
    rec = {
        "row": 1, "label": "Orders in the west region",
        "question": "How many orders were placed in the west region?",
        "expected": 234.0, "actual": None, "delta_pct": None, "match": False, "status": "error",
        "sql": FAILED_SQL, "recorded": None, "error": REJECTION,
        "provenance": {"shape": "d", "source": "the sales dashboard", "file": "tiles.csv", "line": 2},
    }
    rec.update(over)
    return rec


def _trace(status: str = "failed", detail: str = REJECTION) -> list[dict]:
    return [
        {"tool": "get_datasource_schema", "args": {"datasource": "sales"}, "status": None},
        {"tool": "get_prompt_examples", "args": {"query": "orders"}, "status": None},
        {"tool": "execute_sql", "args": {"sql": FAILED_SQL}, "status": status, "detail": detail},
        {"tool": "execute_sql", "args": {"sql": "SELECT 1"}, "stopped": "query_failed"},
    ]


def _fit_ledger(verdict: str, fit: str) -> dict:
    return {"rows": [{"part": "question_fit", "verdict": verdict, "kind": None, "depends_on": [],
                      "evidence": {"fit": fit}, "note": "the statement may not answer the question"}]}


def _items(tmp_path: Path, records: list[dict]) -> list[dict]:
    run = tmp_path / "20260916-114500"
    (run / "rows").mkdir(parents=True)
    (run / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return reconcile.report_items(run)


def test_a_query_the_database_rejected_points_at_the_semantic_model(tmp_path: Path) -> None:
    item = _items(tmp_path, [_row(probes=_trace())])[0]

    assert item["fix"] == "semantic_model"
    assert item["fix_words"] == "fix the semantic model"
    assert item["owner"] == "model"


def test_a_safety_check_block_counts_the_same_as_a_database_rejection(tmp_path: Path) -> None:
    """An out-of-scope column is the semantic model being wrong about the database."""
    item = _items(tmp_path, [_row(probes=_trace(status="refused", detail=COLUMN_REFUSAL))])[0]

    assert item["fix"] == "semantic_model" and item["owner"] == "model"


def test_a_tool_that_crashed_is_a_row_to_ask_again(tmp_path: Path) -> None:
    """`raised` is agami's own tool throwing. It says nothing about the semantic model, so sending the
    reader to change a definition would be the wrong fix."""
    item = _items(tmp_path, [_row(probes=_trace(status="raised", detail="KeyError"), error="KeyError")])[0]

    assert reconcile._rejected_query({"probes": _trace(status="raised", detail="KeyError")}) is None
    assert item["fix"] == "ask_again" and item["owner"] == "agami"


def test_the_row_says_in_plain_words_what_stopped_the_query_and_what_to_do(tmp_path: Path) -> None:
    change = _items(tmp_path, [_row(probes=_trace(status="refused", detail=COLUMN_REFUSAL))])[0]["change"]

    assert change[0] == ("agami's query did not run. agami's safety check blocked it before it reached the "
                         "database: it names a column the semantic model does not have (orders.units_sold).")
    assert "Asking again would hit the same problem" in change[1]
    assert "/agami-connect" in change[1] and "/agami-save-correction" in change[1]


def test_a_database_rejection_is_relayed_as_one(tmp_path: Path) -> None:
    change = _items(tmp_path, [_row(probes=_trace())])[0]["change"]

    assert change[0] == "agami's query did not run. The database returned an error: " + REJECTION


@pytest.mark.parametrize("status, detail, said", [
    ("refused", ROW_CAP,
     "agami's query returned no result. agami stopped it: " + ROW_CAP),
    ("refused", "query uses SELECT * — every column must be named so it can be checked against the semantic model.",
     "agami's query did not run. agami's safety check blocked it before it reached the database: "
     "it uses SELECT * instead of naming its columns."),
    ("failed", "The database rejected the connection's credentials.",
     "agami's query did not run. The database returned an error: The database rejected the connection's credentials."),
], ids=["a result too large", "SELECT *", "a credential"])
def test_a_query_stopped_for_a_reason_that_is_not_the_model_is_a_row_to_ask_again(
    tmp_path: Path, status: str, detail: str, said: str
) -> None:
    """Only a name one side has and the other lacks is rejected again on every attempt. A limit, a
    SELECT * or a credential says nothing about the semantic model, and sending the reader there
    would be the wrong fix. The row still says, word for word, what stopped the query — and the row
    cap fires AFTER the statement ran, so neither sentence may say it did not run or that anything
    was stopped before the database saw it."""
    item = _items(tmp_path, [_row(probes=_trace(status=status, detail=detail), error=detail)])[0]

    assert item["fix"] == "ask_again" and item["owner"] == "agami"
    assert item["change"][0] == said
    assert item["change"][1] == "That does not point at the semantic model, so ask the question again."


@pytest.mark.parametrize("detail", [ROW_CAP, TIMEOUT, ABANDONED],
                         ids=["the row cap", "the statement timeout", "the executor bound"])
def test_a_limit_that_fires_after_the_statement_ran_is_never_called_a_block(tmp_path: Path, detail: str) -> None:
    """All three resource limits arrive as `refused`, and all three fire once the statement is away:
    the row cap drops a result the executor had already returned, and the two time bounds stop or
    abandon a statement the database was busy with. A card that says the query never reached the
    database sends the reader looking for a scope problem instead of narrowing their query, so no
    line on it may say that — and every line must agree with the others."""
    item = _items(tmp_path, [_row(probes=_trace(status="refused", detail=detail), error=detail)])[0]
    lines = [item["sentence"], item["summaries"]["sql"], *item["change"]]

    assert item["change"][0] == "agami's query returned no result. agami stopped it: " + detail
    assert item["sentence"] == "This row could not be compared: agami's query returned no result."
    assert item["summaries"]["sql"] == "agami's query returned no result, so the two were never compared."
    assert not any("did not run" in line or "before it reached the database" in line for line in lines)


def test_one_missing_name_among_several_reasons_still_points_at_the_model(tmp_path: Path) -> None:
    detail = ("query references table(s) not in the semantic model: returns — only tables declared in the model may "
              "be queried. query references table(s) whose name matches tables in more than one schema, so which "
              "one is meant cannot be decided: orders.")
    item = _items(tmp_path, [_row(probes=_trace(status="refused", detail=detail))])[0]

    assert item["fix"] == "semantic_model"


def test_a_client_that_never_answered_is_still_a_row_to_ask_again(tmp_path: Path) -> None:
    """The other half of the split. No trace, or a trace with no query that did not run, means nothing
    was blocked or rejected, so the old advice is still the right advice."""
    item = _items(tmp_path, [_row(error="the generator exited without answering", sql=None)])[0]

    assert item["fix"] == "ask_again" and item["owner"] == "agami"


def test_the_sql_line_never_claims_two_queries_agreed_when_neither_was_compared(tmp_path: Path) -> None:
    """`differs` is empty both when the queries agreed on everything and when nothing was compared.
    On a row whose query did not run, reading that silence as agreement had the card assert agreement
    it had no evidence for, on the one row a person opened to find out what went wrong."""
    item = _items(tmp_path, [_row(probes=_trace())])[0]

    assert item["summaries"]["sql"] == "agami's query did not run, so the two were never compared."


def test_a_graded_question_fit_does_not_hide_that_nothing_was_compared(tmp_path: Path) -> None:
    """The fit check lives in the SQL section, but it reads the person's query rather than comparing
    two. Counting it as a comparison made a row whose query never ran say the two queries "ask for the
    same things", whenever the fit had been graded."""
    doubtful = _items(tmp_path / "a", [_row(probes=_trace(), ledger=_fit_ledger("unresolved", "doubtful"))])[0]
    plausible = _items(tmp_path / "b", [_row(probes=_trace(), ledger=_fit_ledger("confirmed", "plausible"))])[0]

    assert doubtful["summaries"]["sql"] == ("Your query might not answer the question. "
                                            "agami's query did not run, so the two were never compared.")
    assert plausible["summaries"]["sql"] == "agami's query did not run, so the two were never compared."


def test_the_lead_sentence_does_not_end_in_two_full_stops(tmp_path: Path) -> None:
    """The relayed message is a sentence already, so appending regardless put two stops on the line
    a person reads first. The row has no trace, because only then is the error appended at all."""
    sentence = _items(tmp_path, [_row()])[0]["sentence"]

    assert sentence == "This row could not be compared: agami's query failed: " + REJECTION.rstrip(".") + "."


def test_a_stopped_call_is_not_mistaken_for_the_rejection(tmp_path: Path) -> None:
    """A stopped call carries no status. It is a consequence of the rejection, not a second one, and
    reading it as the rejection would quote an empty detail at the reader."""
    trace = [{"tool": "execute_sql", "args": {"sql": "SELECT 1"}, "stopped": "query_failed"}]
    assert reconcile._rejected_query({"probes": trace}) is None
    assert reconcile._rejected_query({"probes": _trace()})["detail"] == REJECTION


def test_why_a_query_did_not_run_is_said_plainly_for_each_refusal() -> None:
    why = reconcile._why_it_did_not_run
    blocked = "agami's safety check blocked it before it reached the database: "
    assert why("refused", "query references table(s) not in the semantic model: returns — only tables declared in "
                          "the model may be queried.") == blocked + "it reads a table the semantic model does not have (returns)."
    # One rule, two reasons: the table IS in the semantic model, twice, and the fix is to qualify it.
    assert why("refused", "query references table(s) whose name matches tables in more than one schema, so which one "
                          "is meant cannot be decided: orders.") == (
        blocked + "a table name it uses exists in more than one schema, so which one is meant cannot be decided (orders).")
    assert why("refused", "query references table(s) not in the semantic model: returns — only tables declared in the "
                          "model may be queried. query references table(s) whose name matches tables in more than one "
                          "schema, so which one is meant cannot be decided: orders.") == (
        blocked + "it reads a table the semantic model does not have (returns); a table name it uses exists in more "
                  "than one schema, so which one is meant cannot be decided (orders).")
    assert why("refused", "query uses SELECT * — every column must be named so it can be checked against the semantic "
                          "model.") == blocked + "it uses SELECT * instead of naming its columns."
    # A refusal none of the gates explains. It may have fired before the statement was sent or after
    # it ran, and the card cannot tell which, so it claims only the part it can see.
    assert why("refused", "a rule nobody has named yet.") == "agami stopped it: a rule nobody has named yet."
    assert why("refused", ROW_CAP) == "agami stopped it: " + ROW_CAP
    assert why("refused", TIMEOUT) == "agami stopped it: " + TIMEOUT
    assert why("refused", ABANDONED) == "agami stopped it: " + ABANDONED
    # One reason the gates know and one they do not is still one we cannot place, so neither is it.
    assert why("refused", "query uses SELECT * — every column must be named. query hit a rule nobody has named yet.") == (
        "agami stopped it: query uses SELECT * — every column must be named. query hit a rule nobody has named yet.")
    assert why("failed", "relation does not exist") == "The database returned an error: relation does not exist."
    assert why("raised", "TimeoutError") == "It did not run: TimeoutError."


def test_the_plain_words_are_keyed_to_the_safety_checks_own_refusal_text() -> None:
    """The prefixes are copied from the refusals the safety check writes. If one is reworded there, the
    card would quietly fall back to the raw text, so each must still appear in that source."""
    source = (REPO_ROOT / "packages" / "agami-core" / "src" / "semantic_model" / "runtime.py").read_text(encoding="utf-8")
    flat = re.sub(r'"\s*\n\s*"', "", source)  # join the implicitly concatenated string literals
    for prefix, _plain in reconcile._BLOCKED_BECAUSE:
        assert prefix in flat, prefix
    for prefix in reconcile._NAMES_SOMETHING_MISSING["refused"]:
        assert prefix in flat, prefix


def test_the_database_reasons_are_keyed_to_the_guards_own_failure_text() -> None:
    source = (REPO_ROOT / "packages" / "agami-core" / "src" / "execute_sql.py").read_text(encoding="utf-8")
    flat = re.sub(r'"\s*\n\s*"', "", source)
    for prefix in reconcile._NAMES_SOMETHING_MISSING["failed"]:
        assert prefix in flat, prefix


def test_the_resource_limits_are_quoted_in_the_executors_own_words() -> None:
    """The three limit sentences this file tests with are copied from `_resource_limit_refusal`. If
    one is reworded there, the cases above would stop standing for anything the product says, so each
    half either side of the configured number must still appear in that source."""
    source = (REPO_ROOT / "packages" / "agami-core" / "src" / "execute_sql.py").read_text(encoding="utf-8")
    flat = re.sub(r'"\s*\n\s*"', "", source)
    for fragment in ("The result exceeded the ", "-row limit, so it was not returned.",
                     "The statement ran longer than the ", "s limit and was cancelled.",
                     "The executor did not return within the ", "s limit and the query was abandoned."):
        assert fragment in flat, fragment


def test_the_record_says_in_plain_words_why_a_blocked_query_did_not_run(tmp_path: Path) -> None:
    run = tmp_path / "20260916-114500"
    row_dir = run / "rows" / "1"
    row_dir.mkdir(parents=True)
    (run / "intake.json").write_text(json.dumps({"rows": [
        {"row": 1, "label": "Orders", "question": "How many orders?", "statement": None, "expected": 42.0,
         "provenance": {"shape": "c", "source": "the sheet", "file": "tiles.csv", "line": 2}}]}))
    probes = _trace(status="refused", detail=COLUMN_REFUSAL)
    (row_dir / "agami-answer.json").write_text(json.dumps(
        {"sql": FAILED_SQL, "error": COLUMN_REFUSAL, "mode": "mcp", "probe_count": 0, "probes": probes}))
    (row_dir / "agami-run.json").write_text(json.dumps({"status": "refused", "detail": COLUMN_REFUSAL, "source": "trace"}))

    rec = reconcile.record(run, 1)

    assert rec["status"] == "error" and rec["error"] == (
        "agami's query did not run. agami's safety check blocked it before it reached the database: it names a "
        "column the semantic model does not have (orders.units_sold).")
    # The trace reaches the card only through the record, so the routing is checked from here too.
    (run / "rows.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    assert reconcile.report_items(run)[0]["fix"] == "semantic_model"


def _record_with_run(tmp_path: Path, run_file: dict) -> dict:
    run = tmp_path / "20260916-114500"
    row_dir = run / "rows" / "1"
    row_dir.mkdir(parents=True)
    (run / "intake.json").write_text(json.dumps({"rows": [
        {"row": 1, "label": "Orders", "question": "How many orders?", "statement": None, "expected": 42.0,
         "provenance": {"shape": "c", "source": "the sheet", "file": "tiles.csv", "line": 2}}]}))
    (row_dir / "agami-answer.json").write_text(json.dumps({"sql": FAILED_SQL, "error": None}))
    (row_dir / "agami-run.json").write_text(json.dumps(run_file))
    return reconcile.record(run, 1)


def test_a_database_rejection_from_the_session_is_said_the_same_plain_way(tmp_path: Path) -> None:
    rec = _record_with_run(tmp_path, {"status": "failed", "detail": REJECTION, "source": "trace"})

    assert rec["error"] == "agami's query did not run. The database returned an error: " + REJECTION


def test_a_refusal_when_run_again_for_its_result_is_not_said_to_have_stopped_the_session(tmp_path: Path) -> None:
    """The statement ran in agami's session and was refused only when run again for its result. It
    did run, so "agami's query did not run" would be false."""
    rec = _record_with_run(tmp_path, {"status": "refused", "rule": "column_scope", "detail": COLUMN_REFUSAL})

    assert rec["error"] == "agami's statement did not run: " + COLUMN_REFUSAL
