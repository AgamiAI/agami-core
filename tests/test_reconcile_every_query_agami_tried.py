"""Every query agami tried for a row is recorded, with what happened to it and whether it was right.

A query that did not run is where there is most to learn, and the server ends agami's session at the
first one, so the query agami wrote next never ran either. The run executes that one afterwards,
through the same guard, and the record grades it: that says whether agami would have recovered on its
own. None of this changes the row's verdict: the session still ended at the failure, and the failure is
still the finding.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402

BLOCKED_SQL = "SELECT region, units_sold FROM orders"
NEXT_SQL = "SELECT region, COUNT(*) AS n FROM orders GROUP BY region"
REFUSAL = ("query references column(s) not in the semantic model: orders.units_sold — only columns declared on "
           "the model's tables may be queried.")
LEDGER_OK = {"rows": [{"part": "runs", "verdict": "confirmed", "kind": None, "depends_on": [], "evidence": {}, "note": ""}],
             "verdict": "confirmed", "counts": {"confirmed": 1}}
SAME = {"status": "scored", "accuracy": 1.0, "column_pairs": [["region", "region"], ["n", "n"]], "paired_row_share": 1.0,
        "unmatched_golden_columns": [], "unmatched_generated_columns": []}
BLOCKED_WHY = ("agami's safety check blocked it before it reached the database: it names a column the semantic model "
               "does not have (orders.units_sold).")


def _run(tmp_path: Path, **row: Any) -> Path:
    run = tmp_path / "20260916-160000"
    (run / "rows" / "1").mkdir(parents=True)
    base = {"row": 1, "label": "By region", "question": "Orders by region?",
            "statement": "SELECT region, COUNT(*) AS n FROM orders GROUP BY region", "expected": None,
            "provenance": {"shape": "b", "source": "the sheet", "file": "plan.csv", "line": 2}}
    base.update(row)
    (run / "intake.json").write_text(json.dumps({"rows": [base]}))
    return run


def _write(run: Path, **files: Any) -> Path:
    row_dir = run / "rows" / "1"
    for name, body in files.items():
        path = row_dir / name.replace("__", ".").replace("_", "-")
        if body is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(body if isinstance(body, str) else json.dumps(body))
    return row_dir


def _call(sql: str, status: str | None = "ok", **extra: Any) -> dict:
    return {"tool": "execute_sql", "args": {"datasource": "sales", "sql": sql}, "status": status, **extra}


def _stopped(sql: str, reason: str = "query_failed") -> dict:
    return {"tool": "execute_sql", "args": {"datasource": "sales", "sql": sql}, "stopped": reason}


def _blocked_answer(*extra_calls: dict) -> dict:
    return {"row": 1, "question": "Orders by region?", "sql": BLOCKED_SQL, "statements": [BLOCKED_SQL],
            "error": REFUSAL, "mode": "mcp", "probes": [
                {"tool": "get_datasource_schema", "args": {"datasource": "sales"}, "status": None},
                _call(BLOCKED_SQL, "refused", detail=REFUSAL), _stopped(NEXT_SQL), *extra_calls]}


def _blocked_row(run: Path, **files: Any) -> Path:
    defaults = {"agami_answer__json": _blocked_answer(), "agami_run__json": {"status": "refused", "detail": REFUSAL, "source": "trace"},
                "statement__csv": "region,n\neast,3\nwest,5\n", "ledger__json": LEDGER_OK, "next_query__sql": NEXT_SQL}
    return _write(run, **{**defaults, **files})


# --- the query agami tried next ------------------------------------------------------------------


def test_a_blocked_query_and_the_right_next_query_are_both_recorded_and_graded(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _blocked_row(run, next_query_run__json={"status": "ok"}, next_query_comparison__json=SAME)

    rec = reconcile.record(run, 1)

    blocked, tried_next = rec["attempts"]
    assert (blocked["happened"], blocked["run_by"], blocked["grade"]) == ("blocked", "agami", "no_answer")
    assert blocked["why"] == BLOCKED_WHY and blocked["sql"] == BLOCKED_SQL
    assert blocked["grade_words"] == "No answer: it did not run."
    assert (tried_next["happened"], tried_next["run_by"], tried_next["grade"]) == ("ran", "reconcile", "right")
    assert tried_next["grade_words"] == "Right: the same result as your query."
    # The verdict is unchanged: the session ended at the failure, and that is the finding.
    assert rec["status"] == "error" and rec["sql"] == BLOCKED_SQL


@pytest.mark.parametrize(("comparison", "grade", "words"), [
    ({"status": "scored", "accuracy": 0.0, "column_pairs": [], "paired_row_share": 0.0, "unmatched_golden_columns": ["n"]},
     "wrong", "Wrong: a different result from your query."),
    ({"status": "scored", "accuracy": 0.0, "column_pairs": [["region", "region"]], "paired_row_share": 1.0, "unmatched_golden_columns": ["n"]},
     "partly", "Partly right: the same rows as your query, with different columns."),
    ({"status": "unscored"}, "not_graded", "Not graded: the two results could not be compared."),
    (None, "not_graded", "Not graded: its result has not been compared with yours."),
    ("", "not_graded", "Not graded: the comparison of its result with yours could not be read."),
])
def test_the_next_query_is_graded_by_its_comparison(tmp_path: Path, comparison: Any, grade: str, words: str) -> None:
    run = _run(tmp_path)
    _blocked_row(run, next_query_run__json={"status": "ok"}, next_query_comparison__json=comparison)

    tried_next = reconcile.record(run, 1)["attempts"][1]

    assert (tried_next["grade"], tried_next["grade_words"]) == (grade, words)


def test_an_unreadable_comparison_falls_back_to_a_number_diff(tmp_path: Path) -> None:
    run = _run(tmp_path, statement=None, expected=8)
    _blocked_row(run, next_query_run__json={"status": "ok"}, next_query_comparison__json="", next_query_diff__json={"match": True, "delta": 0})

    tried_next = reconcile.record(run, 1)["attempts"][1]

    assert (tried_next["grade"], tried_next["grade_words"]) == ("right", "Right: the same result as your number.")


@pytest.mark.parametrize(("run_file", "happened", "run_by", "grade", "why"), [
    ({"status": "refused", "rule": "select_star", "detail": "query uses SELECT * — every column must be named so it can be checked against the semantic model."},
     "blocked", "reconcile", "no_answer",
     "agami's safety check blocked it before it reached the database: it uses SELECT * instead of naming its columns."),
    ({"status": "failed", "kind": "syntax", "detail": "The generated SQL was not valid for this database. Re-run the query."},
     "failed", "reconcile", "no_answer",
     "The database returned an error: The generated SQL was not valid for this database. Re-run the query."),
    # Reconcile failing to reach the database says nothing about agami's statement.
    ({"status": "failed", "kind": "network", "detail": "The database was unreachable."}, "failed", "reconcile", "not_graded",
     "Reconcile could not run it: The database was unreachable."),
    # The run can stop between writing the statement and its run file. Nobody ran it.
    (None, "not_run", "agami", "not_graded", "Reconcile has no readable record of running it."),
    ("", "not_run", "agami", "not_graded", "Reconcile has no readable record of running it."),
    ({"status": "not_run", "reason": "trimmed"}, "not_run", "agami", "not_graded",
     "Its text was cut short in the run's record, so reconcile did not run it."),
])
def test_what_happened_to_the_next_query_is_read_from_its_run_file(
    tmp_path: Path, run_file: Any, happened: str, run_by: str, grade: str, why: str
) -> None:
    run = _run(tmp_path)
    _blocked_row(run, next_query_run__json=run_file)

    tried_next = reconcile.record(run, 1)["attempts"][1]

    assert (tried_next["happened"], tried_next["run_by"], tried_next["grade"], tried_next["why"]) == (happened, run_by, grade, why)


# --- queries that never ran ----------------------------------------------------------------------


def test_a_later_query_names_the_one_that_ended_the_session_and_the_rest_are_counted(tmp_path: Path) -> None:
    run = _run(tmp_path)
    answer = _blocked_answer(_stopped("SELECT 2"), _stopped("SELECT 3"), _stopped("SELECT 4"))
    _write(run, agami_answer__json=answer, agami_run__json={"status": "refused", "detail": REFUSAL},
           statement__csv="region,n\neast,3\n", ledger__json=LEDGER_OK, next_query__sql=NEXT_SQL,
           next_query_run__json={"status": "ok"}, next_query_comparison__json=SAME)

    attempts = reconcile.record(run, 1)["attempts"]

    # The next query is always listed; one more that never ran is listed besides it, and a line counts the rest.
    assert [a["query"] for a in attempts] == [1, 2, 3, None]
    assert attempts[2]["why"] == "It never ran: agami's session ended when query 1 did not run."
    assert attempts[3]["why"] == "2 more queries never ran: agami's session ended when query 1 did not run."
    assert attempts[3]["more"] == 2


def test_a_query_stopped_by_the_runs_ceiling_does_not_blame_a_query_that_worked(tmp_path: Path) -> None:
    run = _run(tmp_path)
    answer = {"sql": NEXT_SQL, "mode": "mcp", "probes": [_call(NEXT_SQL), _stopped("SELECT 2", "ceiling")]}
    _write(run, agami_answer__json=answer, agami_run__json={"status": "ok"}, actual__csv="region,n\neast,3\n",
           statement__csv="region,n\neast,3\n", ledger__json=LEDGER_OK, comparison__json=SAME)

    stopped = reconcile.record(run, 1)["attempts"][1]

    assert (stopped["happened"], stopped["why"]) == ("not_run", "It never ran: agami had already run as many queries as a run allows.")


def test_the_count_of_queries_stopped_by_the_ceiling_says_why_they_never_ran(tmp_path: Path) -> None:
    run = _run(tmp_path)
    answer = {"sql": NEXT_SQL, "mode": "mcp", "probes": [_call(NEXT_SQL), *(_stopped(f"SELECT {n}", "ceiling") for n in (2, 3, 4))]}
    _write(run, agami_answer__json=answer, agami_run__json={"status": "ok"}, actual__csv="region,n\neast,3\n",
           statement__csv="region,n\neast,3\n", ledger__json=LEDGER_OK, comparison__json=SAME)

    summary = reconcile.record(run, 1)["attempts"][-1]

    assert summary["why"] == "2 more queries never ran: agami had already run as many queries as a run allows."


# --- the queries that ran ------------------------------------------------------------------------


def test_the_answering_query_is_found_as_the_trace_check_finds_it(tmp_path: Path) -> None:
    """The client reports its statement without the trailing semicolon it sent, and in its own case."""
    run = _run(tmp_path)
    look = "SELECT DISTINCT region FROM orders"
    answer = {"sql": NEXT_SQL.lower(), "mode": "mcp", "probes": [
        _call(NEXT_SQL + ";"), _call(look), _call(NEXT_SQL + ";")]}
    _write(run, agami_answer__json=answer, agami_run__json={"status": "ok"}, actual__csv="region,n\neast,3\n",
           statement__csv="region,n\neast,3\n", ledger__json=LEDGER_OK, comparison__json=SAME)

    first, looked, answered = reconcile.record(run, 1)["attempts"]

    # The same statement twice: only the last is the answer.
    assert "answered" not in first and (first["grade"], first["why"]) == ("not_graded", "agami ran it to look at the data.")
    assert looked["grade_words"] == "Not graded: it was a look at the data, not the answer."
    assert answered["answered"] is True and (answered["grade"], answered["why"]) == ("right", "agami answered from it.")


def test_every_query_says_what_happened_who_ran_it_why_and_its_grade(tmp_path: Path) -> None:
    """Two looks at the data, then a query that was blocked. None of the three answered, and each
    still says what became of it."""
    run = _run(tmp_path)
    answer = {"sql": BLOCKED_SQL, "error": REFUSAL, "mode": "mcp", "probes": [
        _call("SELECT DISTINCT region FROM orders"), _call(BLOCKED_SQL.replace("units_sold", "region")),
        _call(BLOCKED_SQL, "refused", detail=REFUSAL)]}
    _write(run, agami_answer__json=answer, agami_run__json={"status": "refused", "detail": REFUSAL, "source": "trace"},
           statement__csv="region,n\neast,3\n", ledger__json=LEDGER_OK)

    attempts = reconcile.record(run, 1)["attempts"]

    assert all(a["happened"] and a["run_by"] and a["why"] and a["grade"] and a["grade_words"] for a in attempts)
    assert [a["why"] for a in attempts[:2]] == ["agami ran it.", "agami ran it."]
    assert not any(a.get("answered") for a in attempts)


def test_a_row_that_ended_at_a_failure_has_no_answering_query(tmp_path: Path) -> None:
    """The failed statement is the row's `sql`. An earlier query with the same text that ran is still
    not an answer: the session did not end on it."""
    run = _run(tmp_path)
    answer = {"sql": BLOCKED_SQL, "error": "no", "mode": "mcp", "probes": [
        _call(BLOCKED_SQL), _call(BLOCKED_SQL, "failed", detail="The statement referenced a column this database does not have.")]}
    _write(run, agami_answer__json=answer, agami_run__json={"status": "failed", "detail": "no", "source": "trace"},
           statement__csv="region,n\neast,3\n", ledger__json=LEDGER_OK)

    first, _failed = reconcile.record(run, 1)["attempts"]

    assert "answered" not in first and first["grade_words"] == "Not graded: agami gave no answer to compare it with."


@pytest.mark.parametrize(("match", "grade", "words"), [
    (True, "right", "Right: the same result as your number."),
    (False, "wrong", "Wrong: a different result from your number."),
])
def test_a_number_row_grades_the_answer_against_the_number(tmp_path: Path, match: bool, grade: str, words: str) -> None:
    run = _run(tmp_path, statement=None, expected=3)
    _write(run, agami_answer__json={"sql": "SELECT COUNT(*) AS n FROM orders", "mode": "mcp",
                                     "probes": [_call("SELECT COUNT(*) AS n FROM orders")]},
           agami_run__json={"status": "ok"}, actual__csv="n\n3\n", diff__json={"match": match, "delta": 0 if match else 2, "delta_pct": 0.0})

    (only,) = reconcile.record(run, 1)["attempts"]

    assert (only["grade"], only["grade_words"]) == (grade, words)


def test_a_question_with_nothing_of_yours_is_not_graded(tmp_path: Path) -> None:
    run = _run(tmp_path, statement=None)
    _write(run, agami_answer__json={"sql": NEXT_SQL, "mode": "mcp", "probes": [_call(NEXT_SQL)]},
           agami_run__json={"status": "ok"}, actual__csv="region,n\neast,3\n")

    (only,) = reconcile.record(run, 1)["attempts"]

    assert (only["grade"], only["grade_words"]) == ("not_graded", "Not graded: there is no query or number of yours to compare it with.")


def test_a_query_that_ran_on_a_row_with_no_answer_is_not_called_a_look(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _write(run, agami_answer__json={"sql": None, "error": "the client exited without answering", "mode": "mcp",
                                     "probes": [_call(NEXT_SQL)]}, statement__csv="region,n\neast,3\n", ledger__json=LEDGER_OK)

    (only,) = reconcile.record(run, 1)["attempts"]

    assert (only["grade"], only["grade_words"]) == ("not_graded", "Not graded: agami gave no answer to compare it with.")


def test_a_tool_that_crashed_is_not_described_as_the_database(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _write(run, agami_answer__json={"sql": NEXT_SQL, "error": "TimeoutError", "mode": "mcp",
                                     "probes": [_call(NEXT_SQL, "raised", detail="TimeoutError")]},
           statement__csv="region,n\neast,3\n", ledger__json=LEDGER_OK)

    (only,) = reconcile.record(run, 1)["attempts"]

    assert (only["happened"], only["why"]) == ("crashed", "agami's own tool failed (TimeoutError), so no result came back.")
    assert (only["grade"], only["grade_words"]) == ("no_answer", "No answer: it did not run.")


def test_a_run_without_a_trace_records_no_attempts(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _write(run, agami_answer__json={"sql": NEXT_SQL, "statements": [NEXT_SQL]}, agami_run__json={"status": "ok"},
           actual__csv="region,n\neast,3\n", statement__csv="region,n\neast,3\n", ledger__json=LEDGER_OK)

    assert "attempts" not in reconcile.record(run, 1)


# --- the card ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("files", "line"), [
    ({"next_query_run__json": {"status": "ok"}, "next_query_comparison__json": SAME},
     "agami's next query, which reconcile ran after agami's session ended, gave the same result as your query."),
    ({"next_query_run__json": {"status": "ok"}, "next_query_comparison__json": {"status": "scored", "accuracy": 0.0, "column_pairs": [], "paired_row_share": 0.0, "unmatched_golden_columns": ["n"]}},
     "agami's next query, which reconcile ran after agami's session ended, gave a different result from your query."),
    ({"next_query_run__json": {"status": "ok"}, "next_query_comparison__json": {"status": "scored", "accuracy": 0.0, "column_pairs": [["region", "region"]], "paired_row_share": 1.0, "unmatched_golden_columns": ["n"]}},
     "agami's next query, which reconcile ran after agami's session ended, returned the same rows as your query, with different columns."),
    ({"next_query_run__json": {"status": "ok"}},
     "Reconcile ran agami's next query after agami's session ended, but it was not graded: its result has not been compared with yours."),
    ({"next_query_run__json": {"status": "failed", "detail": "relation does not exist"}},
     "agami's next query did not run either. The database returned an error: relation does not exist."),
    ({"next_query_run__json": {"status": "failed", "kind": "auth", "detail": "The database rejected the connection's credentials."}}, None),
    ({"next_query_run__json": None}, None),
])
def test_the_card_says_what_became_of_the_next_query(tmp_path: Path, files: dict, line: str | None) -> None:
    run = _run(tmp_path)
    _blocked_row(run, **files)
    reconcile.record(run, 1)

    (item,) = reconcile.report_items(run)

    assert item["fix"] == "semantic_model"
    assert item["change"][0] == "agami's query did not run. " + BLOCKED_WHY
    assert (item["change"][2] if len(item["change"]) > 2 and item["change"][2].startswith(("agami's next", "Reconcile ran")) else None) == line


def test_the_card_says_there_is_nothing_to_compare_when_the_row_carries_nothing_of_yours(tmp_path: Path) -> None:
    run = _run(tmp_path, statement=None)
    _write(run, agami_answer__json=_blocked_answer(), agami_run__json={"status": "refused", "detail": REFUSAL},
           next_query__sql=NEXT_SQL, next_query_run__json={"status": "ok"})
    reconcile.record(run, 1)

    words = reconcile._next_query_words(json.loads((run / "rows.jsonl").read_text().splitlines()[0]))

    assert words == ("Reconcile ran agami's next query after agami's session ended, but it was not graded: "
                     "there is no query or number of yours to compare it with.")


def test_the_rendered_page_carries_the_attempts_and_nothing_but_their_words(tmp_path: Path) -> None:
    """The renderer projects each item onto the fields a card may carry, so `attempts` must be listed
    there, and nothing smuggled onto an attempt may reach the page."""
    import render_reconcile_report as rr

    run = _run(tmp_path)
    _blocked_row(run, next_query_run__json={"status": "ok"}, next_query_comparison__json=SAME)
    reconcile.record(run, 1)
    items = reconcile.report_items(run)
    items[0]["attempts"][0]["rows"] = [["east", 3]]

    page = rr.render(title="Reconcile · demo", profile="demo", run=run.name, items=items)

    line = next(ln for ln in page.splitlines() if ln.strip().startswith("const DATA = {"))
    carried = json.loads(line.split("items: ", 1)[1].rsplit(" };", 1)[0])[0]["attempts"]
    assert [a["grade"] for a in carried] == ["no_answer", "right"]
    assert all("rows" not in a for a in carried)


def test_the_page_lists_the_queries_collapsed_inside_the_sql_section() -> None:
    tpl = (REPO_ROOT / "plugins" / "agami" / "shared" / "reconcile-report-template.html").read_text(encoding="utf-8")
    assert "'<details class=\"attempts\"><summary>Every query agami tried ('" in tpl
    assert '<details class="attempts" open' not in tpl
    # Declared before the function that reads it, so a render during load cannot reach it uninitialised.
    assert tpl.index("const HAPPENED = {") < tpl.index("function sqlBlock(item)")
