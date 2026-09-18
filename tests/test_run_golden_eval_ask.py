"""`run_golden_eval.py --ask` answers one question the way a golden-run item is answered: the same
context assembly, the same client generator, the same fixed sentences; the reconcile skill's door to
agami's answer without its own context in play."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import run_golden_eval as rge  # noqa: E402
from semantic_model import golden_run as gr  # noqa: E402


class _Generator:
    """A stand-in for the client: records what it was given, answers what the test set."""

    made: list = []

    def __init__(self, schema, *, timeout_s):
        self.schema, self.timeout_s = schema, timeout_s
        _Generator.made.append(self)

    def generate(self, question, org, datasource):
        self.asked = (question, org, datasource, self.schema(question))
        return _Generator.answer


def _wire(monkeypatch, tmp_path):
    monkeypatch.setattr(rge, "GENERATOR", _Generator)
    monkeypatch.setattr(rge, "_fetch_context", lambda root, top_k, profile: {"root": root, "top_k": top_k, "profile": profile, "areas": ["store"], "org_context": "ctx"})
    monkeypatch.setattr(rge, "_model_context", lambda cached, question: f"schema for {question} with {cached['top_k']} examples")
    monkeypatch.setattr(rge.agami_paths, "profile_dir", lambda profile: tmp_path / profile)
    org = lambda: "local"  # noqa: E731
    org.cache_clear = lambda: None  # the session fixture clears the real resolver's cache at teardown
    monkeypatch.setattr(rge.tools, "resolved_org_id", org)
    _Generator.made.clear()


def test_ask_answers_one_question_with_the_golden_runs_generator_and_context(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    _Generator.answer = gr.GeneratedSql(sql="SELECT COUNT(*) AS n FROM orders", error=None)
    out = tmp_path / "rows" / "1" / "agami-answer.json"
    assert rge.main(["--profile", "demo", "--ask", "How many orders?", "--top-k", "3", "--timeout-s", "45", "--out", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    # `mode` says which surface answered. The two measure different things, so a reader deciding
    # whether to trust a number has to be told which one produced it.
    assert printed == {"question": "How many orders?", "sql": "SELECT COUNT(*) AS n FROM orders",
                       "statements": ["SELECT COUNT(*) AS n FROM orders"], "error": None, "mode": "context"}
    assert json.loads(out.read_text()) == printed
    gen = _Generator.made[0]
    assert gen.timeout_s == 45.0 and gen.asked == ("How many orders?", "local", "demo", "schema for How many orders? with 3 examples")


def test_ask_with_no_statement_exits_3_with_the_fixed_sentence_and_never_a_statement(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    _Generator.answer = gr.GeneratedSql(sql="", error=gr._GENERATION_UNAVAILABLE)
    assert rge.main(["--profile", "demo", "--ask", "How many orders?"]) == rge._NO_STATEMENT == 3
    printed = json.loads(capsys.readouterr().out)
    assert printed["sql"] is None and printed["error"] == "the generator command could not be started on this machine"


def test_ask_refuses_a_dataset_beside_it_and_an_empty_question(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    assert rge.main(["--profile", "demo", "--ask", "q", "--dataset", "d"]) == rge._CANNOT_START
    assert rge.main(["--profile", "demo", "--ask", "   "]) == rge._CANNOT_START


def test_the_ask_door_and_the_golden_run_share_one_generator():
    assert rge.GENERATOR is gr.ClaudeCliGenerator


def test_ask_file_fetches_the_context_once_and_spawns_per_question_in_parallel(monkeypatch, tmp_path, capsys):
    import threading
    _wire(monkeypatch, tmp_path)
    fetched = []
    monkeypatch.setattr(rge, "_fetch_context", lambda root, top_k, profile: fetched.append(1) or {"root": root, "top_k": top_k, "profile": profile, "areas": ["store"], "org_context": "ctx"})
    seen_threads = set()

    class _Slow(_Generator):
        def generate(self, question, org, datasource):
            seen_threads.add(threading.get_ident())
            if question == "broken":
                return gr.GeneratedSql(sql="", error=gr._GENERATION_EXITED)
            return gr.GeneratedSql(sql=f"SELECT '{question}'", error=None)
    monkeypatch.setattr(rge, "GENERATOR", _Slow)
    chunk = tmp_path / "chunk.json"
    chunk.write_text(json.dumps({"chunk": [{"row": 7, "question": "How many orders?"}, {"row": 8, "question": "broken"}, {"row": 9, "question": "What was revenue?"}, {"row": 10}]}))
    out_dir = tmp_path / "run" / "rows"
    rc = rge.main(["--profile", "demo", "--ask-file", str(chunk), "--out-dir", str(out_dir), "--parallel", "3"])
    printed = json.loads(capsys.readouterr().out)
    assert rc == rge._NO_STATEMENT and printed["asked"] == 4 and printed["answered"] == 2 and printed["parallel"] == 3
    assert [a["row"] for a in printed["answers"]] == [7, 8, 9, 10]  # the file's order, whatever finished first
    assert printed["answers"][1]["error"] == "the generator exited without answering" and printed["answers"][3]["error"] == "the row carries no question"
    assert json.loads((out_dir / "7" / "agami-answer.json").read_text())["sql"] == "SELECT 'How many orders?'"
    assert len(fetched) == 1 and len(_Generator.made) == 1  # one context, one generator, for the whole batch
    # a bare list works too, and a file that is not a list of rows is refused with one line
    (tmp_path / "list.json").write_text(json.dumps([{"row": 1, "question": "q"}]))
    assert rge.main(["--profile", "demo", "--ask-file", str(tmp_path / "list.json")]) == 0
    (tmp_path / "bad.json").write_text(json.dumps({"rows": "nope"}))
    assert rge.main(["--profile", "demo", "--ask-file", str(tmp_path / "bad.json")]) == rge._CANNOT_START
    assert rge.main(["--profile", "demo", "--ask", "q", "--ask-file", str(chunk)]) == rge._CANNOT_START



def test_ask_writes_every_statement_the_client_wrote_and_answers_with_the_last(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    _Generator.answer = gr.GeneratedSql(sql="SELECT COUNT(*) AS n FROM orders", error=None,
                                        statements=("SELECT status FROM orders LIMIT 5", "SELECT COUNT(*) AS n FROM orders"))
    assert rge.main(["--profile", "demo", "--ask", "How many orders?"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["sql"] == "SELECT COUNT(*) AS n FROM orders"
    assert printed["statements"] == ["SELECT status FROM orders LIMIT 5", "SELECT COUNT(*) AS n FROM orders"]


# --- agami's result under --via mcp, written in code through the guard ---------------------------

from types import SimpleNamespace  # noqa: E402

ANSWER_SQL = "SELECT region, COUNT(*) AS n FROM orders GROUP BY region"


def _answer(status="ok", detail=None, area=None):
    call = {"tool": "execute_sql", "args": {"datasource": "sales", "sql": ANSWER_SQL, **({"area": area} if area else {})},
            "status": status}
    if detail:
        call["detail"] = detail
    return {"sql": ANSWER_SQL, "error": detail, "mode": "mcp", "probes": [
        {"tool": "get_datasource_schema", "args": {"datasource": "sales"}, "status": None}, call]}


def _guard(monkeypatch, envelope):
    calls = []

    def fake(sql, profile, area, *, executor, org_id=None, no_safety=False):
        calls.append({"sql": sql, "profile": profile, "area": area, "executor": executor, "no_safety": no_safety})
        return envelope

    monkeypatch.setattr(rge.execute_sql, "execute_guarded", fake)
    return calls


def test_a_statement_that_ran_is_run_once_more_through_the_guard_for_its_result(monkeypatch, tmp_path):
    calls = _guard(monkeypatch, SimpleNamespace(status="ok", data=SimpleNamespace(columns=["region", "n"], rows=[("east", 3), ("west", 5)])))

    rge._write_agami_result(tmp_path, _answer(area="store"), "sales")

    assert calls == [{"sql": ANSWER_SQL, "profile": "sales", "area": "store", "executor": rge.execute_sql.BUILTIN_EXECUTOR, "no_safety": False}]
    assert (tmp_path / "actual.csv").read_text().splitlines() == ["region,n", "east,3", "west,5"]
    assert json.loads((tmp_path / "agami-run.json").read_text())["status"] == "ok"


def test_a_statement_the_server_did_not_run_is_never_run_again(monkeypatch, tmp_path):
    """The outcome comes from the trace. Running it again, by any road, is how a statement the server
    blocked could reach the database."""
    calls = _guard(monkeypatch, SimpleNamespace(status="ok", data=SimpleNamespace(columns=["n"], rows=[(1,)])))
    (tmp_path / "actual.csv").write_text("n\n1\n")  # left by an earlier run of this row
    refusal = "query references column(s) not in the semantic model: orders.units_sold — only columns declared on the model's tables may be queried."

    rge._write_agami_result(tmp_path, _answer(status="refused", detail=refusal), "sales")

    assert calls == []
    assert not (tmp_path / "actual.csv").exists()
    run = json.loads((tmp_path / "agami-run.json").read_text())
    assert run["status"] == "refused" and run["detail"] == refusal and run["source"] == "trace"


def test_the_guard_refusing_the_rerun_is_recorded_with_its_rule_and_no_result(monkeypatch, tmp_path):
    _guard(monkeypatch, SimpleNamespace(status="refused", refusal=SimpleNamespace(rule="column_scope", detail="query references column(s) not in the semantic model: orders.x")))

    rge._write_agami_result(tmp_path, _answer(), "sales")

    run = json.loads((tmp_path / "agami-run.json").read_text())
    assert (run["status"], run["rule"]) == ("refused", "column_scope")
    assert not (tmp_path / "actual.csv").exists()


def test_a_database_failure_on_the_rerun_is_recorded_with_its_kind(monkeypatch, tmp_path):
    _guard(monkeypatch, SimpleNamespace(status="failed", failure=SimpleNamespace(kind="network", message="The database was unreachable.")))

    rge._write_agami_result(tmp_path, _answer(), "sales")

    run = json.loads((tmp_path / "agami-run.json").read_text())
    assert (run["status"], run["kind"], run["detail"]) == ("failed", "network", "The database was unreachable.")


def test_an_answer_with_no_statement_or_no_trace_writes_nothing(monkeypatch, tmp_path):
    calls = _guard(monkeypatch, SimpleNamespace(status="ok", data=SimpleNamespace(columns=["n"], rows=[(1,)])))

    rge._write_agami_result(tmp_path, {"sql": None, "probes": []}, "sales")
    rge._write_agami_result(tmp_path, {"sql": ANSWER_SQL}, "sales")

    assert calls == [] and not (tmp_path / "agami-run.json").exists()


def test_asking_a_row_again_with_no_statement_clears_the_old_result(monkeypatch, tmp_path):
    """An old `actual.csv` beside an answer that has none would be read as this answer's result."""
    _guard(monkeypatch, SimpleNamespace(status="ok", data=SimpleNamespace(columns=["n"], rows=[(1,)])))
    (tmp_path / "actual.csv").write_text("n\n1\n")
    (tmp_path / "agami-run.json").write_text('{"status": "ok"}')

    rge._write_agami_result(tmp_path, {"sql": None, "error": "the generator exited without answering"}, "sales")

    assert not (tmp_path / "actual.csv").exists() and not (tmp_path / "agami-run.json").exists()


def test_asking_a_row_again_clears_the_comparison_of_the_answer_before_it(monkeypatch, tmp_path):
    """`record` reads `comparison.json` for the row's verdict. Left beside a new result, the previous
    answer's comparison reports a match the new result does not support."""
    _guard(monkeypatch, SimpleNamespace(status="ok", data=SimpleNamespace(columns=["n"], rows=[(999,)])))
    (tmp_path / "comparison.json").write_text('{"status": "scored", "accuracy": 1.0}')
    (tmp_path / "diff.json").write_text('{"match": true, "delta": 0}')

    rge._write_agami_result(tmp_path, _answer(area="store"), "sales")

    assert not (tmp_path / "comparison.json").exists() and not (tmp_path / "diff.json").exists()
    assert (tmp_path / "actual.csv").read_text().splitlines() == ["n", "999"]


def test_only_the_servers_own_statement_is_run_again(monkeypatch, tmp_path):
    """A statement the trace does not hold character for character is not run. The trace is the
    only record of what the server let through, so anything else would be a statement nobody guarded
    in the session."""
    calls = _guard(monkeypatch, SimpleNamespace(status="ok", data=SimpleNamespace(columns=["n"], rows=[(1,)])))
    answer = _answer(area="store")
    answer["sql"] = ANSWER_SQL.lower()

    rge._write_agami_result(tmp_path, answer, "sales")

    assert calls == [] and not (tmp_path / "actual.csv").exists() and not (tmp_path / "agami-run.json").exists()


def test_a_tool_that_raised_is_recorded_as_a_statement_that_did_not_run(monkeypatch, tmp_path):
    """`raised` is the trace's word for agami's own tool throwing. The run file's vocabulary has no
    such status, and nothing came back, so the row records the statement as not run."""
    calls = _guard(monkeypatch, SimpleNamespace(status="ok", data=SimpleNamespace(columns=["n"], rows=[(1,)])))
    answer = _answer(status="raised", detail="KeyError")

    rge._write_agami_result(tmp_path, answer, "sales")

    run = json.loads((tmp_path / "agami-run.json").read_text())
    assert (run["status"], run["detail"], run["source"]) == ("not_run", "KeyError", "trace")
    assert calls == [] and not (tmp_path / "actual.csv").exists()


def test_the_area_comes_from_the_query_that_is_run_again(monkeypatch, tmp_path):
    calls = _guard(monkeypatch, SimpleNamespace(status="ok", data=SimpleNamespace(columns=["n"], rows=[(1,)])))
    answer = _answer(area="store")
    answer["probes"].append({"tool": "execute_sql", "args": {"sql": "SELECT 1", "area": "finance"}, "status": "ok"})

    rge._write_agami_result(tmp_path, answer, "sales")

    assert [c["area"] for c in calls] == ["store"]


def test_every_query_that_ran_on_a_row_with_an_error_was_a_probe():
    trace = (
        {"tool": "execute_sql", "args": {"sql": "SELECT DISTINCT region FROM orders"}, "status": "ok"},
        {"tool": "execute_sql", "args": {"sql": "SELECT 1 FROM returns"}, "status": "refused", "detail": "no"},
    )
    failed = gr.GeneratedSql(sql="SELECT 1 FROM returns", error="no", trace=trace)
    answered = gr.GeneratedSql(sql="SELECT DISTINCT region FROM orders", error=None, trace=trace[:1])

    assert rge._answer_payload(1, "q", failed, "mcp")["probe_count"] == 1
    assert rge._answer_payload(1, "q", answered, "mcp")["probe_count"] == 0


def test_ask_file_via_mcp_writes_each_rows_result_beside_its_answer(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    calls = _guard(monkeypatch, SimpleNamespace(status="ok", data=SimpleNamespace(columns=["n"], rows=[(7,)])))
    trace = tuple(_answer()["probes"])

    class _McpGenerator:
        def generate(self, question, org, datasource):
            return gr.GeneratedSql(sql=ANSWER_SQL, error=None, statements=(ANSWER_SQL,), trace=trace)

    monkeypatch.setattr(rge, "_generator_for", lambda args: (_McpGenerator(), None))

    def no_command_line_tier(*args, **kwargs):
        raise AssertionError("agami's statement must never be run through a command-line tier")

    monkeypatch.setattr(rge.subprocess, "run", no_command_line_tier)
    questions = tmp_path / "chunk.json"
    questions.write_text(json.dumps({"chunk": [{"row": 4, "question": "Orders by region?"}]}))

    assert rge.main(["--profile", "sales", "--via", "mcp", "--ask-file", str(questions), "--out-dir", str(tmp_path / "rows")]) == 0

    assert len(calls) == 1
    assert (tmp_path / "rows" / "4" / "actual.csv").read_text().splitlines() == ["n", "7"]
    assert json.loads((tmp_path / "rows" / "4" / "agami-run.json").read_text())["status"] == "ok"


def test_every_answer_is_on_disk_before_any_result_is_run(monkeypatch, tmp_path, capsys):
    """The clients are already paid for. A run that dies on the first row's guarded re-run must not
    take the other rows' answers with it, so every answer file is written before any result runs."""
    _wire(monkeypatch, tmp_path)
    trace = tuple(_answer()["probes"])

    def die(sql, profile, area, *, executor, org_id=None, no_safety=False):
        raise SystemExit(2)   # what `execute_guarded` lets through, rather than catching

    monkeypatch.setattr(rge.execute_sql, "execute_guarded", die)

    class _McpGenerator:
        def generate(self, question, org, datasource):
            return gr.GeneratedSql(sql=ANSWER_SQL, error=None, statements=(ANSWER_SQL,), trace=trace)

    monkeypatch.setattr(rge, "_generator_for", lambda args: (_McpGenerator(), None))
    questions = tmp_path / "chunk.json"
    questions.write_text(json.dumps({"chunk": [{"row": 1, "question": "Orders by region?"},
                                               {"row": 2, "question": "Orders by month?"}]}))

    with pytest.raises(SystemExit):
        rge.main(["--profile", "sales", "--via", "mcp", "--ask-file", str(questions), "--out-dir", str(tmp_path / "rows")])

    for row in (1, 2):
        assert json.loads((tmp_path / "rows" / str(row) / "agami-answer.json").read_text())["sql"] == ANSWER_SQL


def test_ask_via_mcp_writes_the_result_beside_its_answer_file(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    calls = _guard(monkeypatch, SimpleNamespace(status="ok", data=SimpleNamespace(columns=["n"], rows=[(9,)])))
    trace = tuple(_answer()["probes"])

    class _McpGenerator:
        def generate(self, question, org, datasource):
            return gr.GeneratedSql(sql=ANSWER_SQL, error=None, statements=(ANSWER_SQL,), trace=trace)

    monkeypatch.setattr(rge, "_generator_for", lambda args: (_McpGenerator(), None))
    out = tmp_path / "rows" / "2" / "agami-answer.json"

    assert rge.main(["--profile", "sales", "--via", "mcp", "--ask", "Orders by region?", "--out", str(out)]) == 0

    assert len(calls) == 1
    assert (out.parent / "actual.csv").read_text().splitlines() == ["n", "9"]
