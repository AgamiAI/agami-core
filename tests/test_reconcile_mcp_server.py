"""The server a reconcile run serves to its cold client: probe freely, but the first query that does
not answer ends the row, and every call is on the record.

These drive `RunBudget` directly rather than over a pipe. The rules are the thing under test, and a
subprocess would test the JSON-RPC loop `mcp_harness` already owns.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile_mcp_server as rms  # noqa: E402

OK = json.dumps({"status": "ok", "data": {"rows": [[1]]}})
FAILED = json.dumps({"status": "failed", "failure": {"kind": "column_not_found", "message": "no column o.amt"}})
REFUSED = json.dumps({"status": "refused", "refusal": {"rule": "table_scope", "detail": "orders is not declared"}})
SCHEMA = json.dumps({"datasets": [{"name": "orders"}]})


def _budget(tmp_path: Path, ceiling: int = 10) -> rms.RunBudget:
    return rms.RunBudget(trace_path=tmp_path / "tool_calls.jsonl", ceiling=ceiling)


def _trace(budget: rms.RunBudget) -> list[dict]:
    if not budget.trace_path.exists():
        return []
    return [json.loads(line) for line in budget.trace_path.read_text().splitlines() if line.strip()]


def _stopped_reason(text: str) -> str | None:
    payload = json.loads(text)
    stopped = payload.get("run_harness_stopped")
    return stopped["reason"] if stopped else None


# --- rule 1: a query that did not answer ends the row -----------------------


def test_probing_the_data_does_not_cost_the_row_its_answer(tmp_path: Path) -> None:
    """The regression a flat cap of one query would have caused.

    A client that reads the values of a column before it can filter on one is a person doing the
    same, and the row must still reach an answer. This is the case the whole design turns on: a cap
    would end the row here and reconcile would report a defect against a semantic model that works.
    """
    budget = _budget(tmp_path)
    run = budget.wrap("execute_sql", lambda args: OK)

    for _ in range(5):
        assert _stopped_reason(run({"sql": "SELECT DISTINCT status FROM orders"})) is None

    assert budget.queries_ok == 5
    assert budget.stopped_reason is None


@pytest.mark.parametrize("outcome", [FAILED, REFUSED], ids=["warehouse rejected it", "the guardrail refused it"])
def test_the_query_after_one_that_did_not_answer_is_stopped(tmp_path: Path, outcome: str) -> None:
    """A refusal counts the same as a failure: an out-of-scope table is the semantic model being
    wrong about the warehouse, which is the finding, not an obstacle to route around."""
    budget = _budget(tmp_path)
    run = budget.wrap("execute_sql", lambda args, answers=iter([outcome, OK]): next(answers))

    assert _stopped_reason(run({"sql": "SELECT SUM(o.amt) FROM orders o"})) is None
    assert _stopped_reason(run({"sql": "SELECT SUM(o.amount) FROM orders o"})) == "query_failed"
    # What the row says about the failure is the product's own sentence, carried in the trace.
    said = json.loads(outcome).get("refusal", {}).get("detail") or json.loads(outcome)["failure"]["message"]
    assert _trace(budget)[0]["detail"] == said


def test_a_probe_that_answered_does_not_excuse_a_later_failure(tmp_path: Path) -> None:
    """The latch is about the last outcome, not about whether anything ever worked."""
    budget = _budget(tmp_path)
    answers = iter([OK, OK, FAILED, OK])
    run = budget.wrap("execute_sql", lambda args: next(answers))

    assert _stopped_reason(run({"sql": "SELECT 1"})) is None
    assert _stopped_reason(run({"sql": "SELECT 2"})) is None
    assert _stopped_reason(run({"sql": "SELECT 3"})) is None
    assert _stopped_reason(run({"sql": "SELECT 4"})) == "query_failed"
    assert budget.queries_ok == 2


def test_a_handler_that_raised_ends_the_row_too(tmp_path: Path) -> None:
    """The server turns a raise into an isError result, so without this the next query would run as
    though nothing had gone wrong."""
    budget = _budget(tmp_path)

    def boom(_args: dict) -> str:
        raise RuntimeError("driver exploded")

    with pytest.raises(RuntimeError):
        budget.wrap("execute_sql", boom)({"sql": "SELECT 1"})

    assert budget.stopped_reason == "query_failed"
    assert _stopped_reason(budget.wrap("execute_sql", lambda a: OK)({"sql": "SELECT 2"})) == "query_failed"


# --- the other three tools are never budgeted -------------------------------


@pytest.mark.parametrize("tool", ["get_datasource_schema", "get_prompt_examples", "list_datasources"])
def test_reading_the_semantic_model_is_never_stopped(tmp_path: Path, tool: str) -> None:
    """These are local and cheap, and the more a client calls them the more the trace has to say."""
    budget = _budget(tmp_path, ceiling=2)
    read = budget.wrap(tool, lambda args: SCHEMA)

    for _ in range(20):
        assert _stopped_reason(read({"datasource": "sales"})) is None
    assert budget.queries_ok == 0


def test_a_failed_query_does_not_stop_the_client_reading_the_model(tmp_path: Path) -> None:
    """The client still has to explain itself, and it needs the schema to do that."""
    budget = _budget(tmp_path)
    budget.wrap("execute_sql", lambda a: FAILED)({"sql": "SELECT 1"})

    assert _stopped_reason(budget.wrap("get_datasource_schema", lambda a: SCHEMA)({})) is None


# --- the runaway ceiling ----------------------------------------------------


def test_the_ceiling_stops_a_client_that_has_stopped_making_progress(tmp_path: Path) -> None:
    budget = _budget(tmp_path, ceiling=3)
    run = budget.wrap("execute_sql", lambda args: OK)

    for _ in range(3):
        assert _stopped_reason(run({"sql": "SELECT 1"})) is None
    assert _stopped_reason(run({"sql": "SELECT 1"})) == "ceiling"


# --- the trace --------------------------------------------------------------


def test_every_call_lands_in_the_trace_in_order(tmp_path: Path) -> None:
    budget = _budget(tmp_path)
    budget.wrap("get_prompt_examples", lambda a: SCHEMA)({"query": "how many orders"})
    budget.wrap("get_datasource_schema", lambda a: SCHEMA)({"area": "sales"})
    budget.wrap("execute_sql", lambda a: OK)({"sql": "SELECT COUNT(*) FROM orders"})

    trace = _trace(budget)
    assert [entry["tool"] for entry in trace] == ["get_prompt_examples", "get_datasource_schema", "execute_sql"]
    assert trace[1]["args"]["area"] == "sales"
    assert trace[2]["args"]["sql"] == "SELECT COUNT(*) FROM orders"
    assert trace[2]["status"] == "ok"


def test_a_stopped_call_is_on_the_record_as_stopped(tmp_path: Path) -> None:
    """Otherwise a run could not tell a client that gave up from one that was stopped."""
    budget = _budget(tmp_path)
    run = budget.wrap("execute_sql", lambda args, answers=iter([FAILED, OK]): next(answers))
    run({"sql": "SELECT 1"})
    run({"sql": "SELECT 2"})

    assert [entry.get("stopped") for entry in _trace(budget)] == [None, "query_failed"]


def test_no_trace_is_written_when_the_run_asked_for_none(tmp_path: Path) -> None:
    budget = rms.RunBudget(trace_path=None)
    budget.wrap("execute_sql", lambda a: OK)({"sql": "SELECT 1"})
    assert list(tmp_path.iterdir()) == []


# --- what the client reads --------------------------------------------------


def test_the_client_is_told_the_query_did_not_run_rather_than_that_it_returned_nothing(tmp_path: Path) -> None:
    """The rule is about a query that did not run. A query that ran and returned no rows answered,
    and a client told otherwise would go looking for the wrong thing."""
    assert "did not run" in rms.STOPPED_AFTER_FAILURE
    assert "did not return rows" not in rms.STOPPED_AFTER_FAILURE
    budget = _budget(tmp_path)
    run = budget.wrap("execute_sql", lambda args, answers=iter([FAILED, OK]): next(answers))
    run({"sql": "SELECT SUM(o.amt) FROM orders o"})

    assert "did not run" in run({"sql": "SELECT 1"})


def test_a_stopped_call_never_pretends_agami_refused(tmp_path: Path) -> None:
    """The harness stopped this, not the product. A message shaped like agami's own refusal would
    put words in its mouth on a page someone reads to decide whether to trust it."""
    budget = _budget(tmp_path)
    run = budget.wrap("execute_sql", lambda args, answers=iter([FAILED, OK]): next(answers))
    run({"sql": "SELECT 1"})
    payload = json.loads(run({"sql": "SELECT 2"}))

    assert "run_harness_stopped" in payload
    assert "status" not in payload and "refusal" not in payload
    assert "do not rewrite" in payload["run_harness_stopped"]["message"].lower()


# --- installing over the real registry --------------------------------------


def test_install_wraps_every_tool_and_keeps_the_one_registry(tmp_path: Path) -> None:
    """`mcp_harness.TOOLS is tools.TOOLS` is pinned elsewhere, and it is what makes both servers
    advertise one registry. Installing must mutate that object, never rebind it."""
    import mcp_harness
    import tools

    before = dict(tools.TOOLS)
    registry = tools.TOOLS
    try:
        rms.install(_budget(tmp_path))
        assert tools.TOOLS is registry
        assert mcp_harness.TOOLS is tools.TOOLS
        assert set(tools.TOOLS) == set(before)
        for name, meta in tools.TOOLS.items():
            assert meta["handler"] is not before[name]["handler"], name
            assert meta["inputSchema"] is before[name]["inputSchema"], name
    finally:
        tools.TOOLS.clear()
        tools.TOOLS.update(before)


def test_the_server_takes_its_trace_path_from_the_environment_and_the_default_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(rms.TRACE_ENV, str(tmp_path / "t.jsonl"))
    budget = rms.budget_from_env()
    # The ceiling is pinned to its number, not to the constant: comparing the default with itself
    # asserts nothing, and the server's docstring and the PR both tell a reader it is ten.
    assert budget.trace_path == tmp_path / "t.jsonl" and budget.ceiling == 10
    assert rms.DEFAULT_MAX_QUERIES == 10
