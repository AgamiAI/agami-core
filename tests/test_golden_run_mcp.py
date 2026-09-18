"""The tool-driven generator: the cold client gets the agami tools and nothing else, and what it
says afterwards is judged against what the server saw it do.

`ClaudeCliGenerator`'s isolation tests live in `test_golden_run.py` and still hold there. What is
pinned here is the part that CHANGED — the child now has four tools, so the flags that kept it from
reading the answer key have to still be doing that with the tools open — and the checks that turn a
client contradicting its own trace into an error row.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "agami-core" / "src"))

from semantic_model import golden_run as gr  # noqa: E402

QUESTION = "How many orders were placed in Q3?"
ORG = "Northwind"
DATASOURCE = "sales"
ANSWERED = "SELECT COUNT(*) AS n FROM orders"
ANSWER = json.dumps({"sql": ANSWERED, "value": "41822"})


def _envelope(result: str = ANSWER) -> str:
    """What `--output-format json` writes: the answer inside the client's own envelope."""
    return json.dumps({"type": "result", "result": result, "num_turns": 5, "is_error": False})


def _server() -> gr.McpServer:
    return gr.McpServer(
        command="/usr/bin/python3",
        args=("-m", "reconcile_mcp_server"),
        env={"AGAMI_PROFILE": "sales", "AGAMI_ARTIFACTS_DIR": "/artifacts"},
    )


def _flag_value(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


class _RecordedSpawn:
    """Stands in for the client, and for the server it would have talked to.

    The trace is normally written by that server at the transport boundary. Nothing runs here, so
    this writes it instead — into the path it finds in the `--mcp-config` the generator built, which
    is also how it checks the generator put a usable one there.
    """

    def __init__(self, stdout: str | None = None, calls: list[dict] | None = None, returncode: int = 0) -> None:
        self.invocations: list[tuple[list[str], dict]] = []
        self.system_prompts: list[str] = []
        self._stdout = _envelope() if stdout is None else stdout
        self._calls = calls or [{"tool": "execute_sql", "args": {"sql": ANSWERED}, "status": "ok"}]
        self._returncode = returncode

    def __call__(self, args, **kwargs):
        args = list(args)
        self.invocations.append((args, kwargs))
        prompt_path = _flag_value(args, "--system-prompt-file")
        self.system_prompts.append(Path(prompt_path).read_text(encoding="utf-8") if prompt_path else "")
        config = json.loads(_flag_value(args, "--mcp-config") or "{}")
        trace = config["mcpServers"]["agami"]["env"]["AGAMI_RECONCILE_TRACE"]
        Path(trace).write_text("".join(json.dumps(c) + "\n" for c in self._calls), encoding="utf-8")
        return subprocess.CompletedProcess(args, self._returncode, self._stdout, "")

    def everything_given(self) -> str:
        parts: list[str] = []
        for (args, kwargs), prompt in zip(self.invocations, self.system_prompts):
            parts += args
            parts.append(kwargs.get("input") or "")
            parts.append(prompt)
            parts += [f"{k}={v}" for k, v in (kwargs.get("env") or {}).items()]
        return "\n".join(parts)


@pytest.fixture()
def spawn(monkeypatch):
    recorder = _RecordedSpawn()
    monkeypatch.setattr(gr.subprocess, "run", recorder)
    return recorder


def _generate(server: gr.McpServer | None = None) -> gr.GeneratedSql:
    return gr.ClaudeMcpGenerator(server or _server(), timeout_s=300.0).generate(QUESTION, ORG, DATASOURCE)


# --- the argument list ------------------------------------------------------


def test_the_child_gets_the_agami_tools_and_no_other_source_of_anything(spawn):
    """Three flags are unchanged from the one-shot generator and one is added, and which ones stayed
    is the security argument. `--tools ""` still names the BUILT-IN set, so the child still has no
    Read, no Bash and no Glob — which is what keeps `HOME` inert now that it has tools at all."""
    _generate()
    args, kwargs = spawn.invocations[0]

    assert Path(args[0]).name == "claude" and args[1] == "-p"
    assert _flag_value(args, "--tools") == ""  # still no built-in tools: no Read, no Bash, no Glob
    assert "--strict-mcp-config" in args  # only the server --mcp-config names
    assert _flag_value(args, "--setting-sources") == ""  # no CLAUDE.md, no settings, no .mcp.json
    assert _flag_value(args, "--output-format") == "json"
    assert _flag_value(args, "--permission-mode") == "dontAsk"
    assert kwargs["timeout"] == 300.0


def test_only_the_four_agami_tools_are_allowed(spawn):
    _generate()
    allowed = (_flag_value(spawn.invocations[0][0], "--allowedTools") or "").split(",")

    assert sorted(allowed) == sorted(
        [
            "mcp__agami__list_datasources",
            "mcp__agami__get_datasource_schema",
            "mcp__agami__get_prompt_examples",
            "mcp__agami__execute_sql",
        ]
    )
    assert all(name.startswith("mcp__agami__") for name in allowed)


def test_the_mcp_config_names_one_server_and_carries_this_calls_trace(spawn):
    _generate()
    config = json.loads(_flag_value(spawn.invocations[0][0], "--mcp-config") or "{}")

    assert list(config["mcpServers"]) == ["agami"]
    entry = config["mcpServers"]["agami"]
    assert entry["command"] == "/usr/bin/python3" and entry["args"] == ["-m", "reconcile_mcp_server"]
    assert entry["env"]["AGAMI_PROFILE"] == "sales"
    assert entry["env"]["AGAMI_RECONCILE_TRACE"].endswith("tool_calls.jsonl")


def test_two_questions_never_share_a_trace(spawn):
    """A generator is shared across a thread pool, so a path fixed at construction would have every
    question in a chunk appending to one file and nothing could say which row wrote what."""
    generator = gr.ClaudeMcpGenerator(_server(), timeout_s=60.0)
    generator.generate(QUESTION, ORG, DATASOURCE)
    generator.generate("A different question", ORG, DATASOURCE)

    traces = [
        json.loads(_flag_value(args, "--mcp-config"))["mcpServers"]["agami"]["env"]["AGAMI_RECONCILE_TRACE"]
        for args, _ in spawn.invocations
    ]
    assert traces[0] != traces[1]


def test_the_trace_is_not_left_behind_after_the_generation(spawn):
    _generate()
    trace = json.loads(_flag_value(spawn.invocations[0][0], "--mcp-config"))["mcpServers"]["agami"]["env"][
        "AGAMI_RECONCILE_TRACE"
    ]
    assert not Path(trace).exists()


# --- the isolation the tools did not buy back -------------------------------


def test_the_childs_own_environment_stays_the_allowlist(spawn, monkeypatch):
    """The artifacts directory reaches the SERVER through the mcp config, and the client never.

    That division is the whole reason the tools can be opened at all: `_CHILD_ENV_KEYS` still
    withholds every path from the client, and the client still has no tool that could open one.
    """
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", "/secret/artifacts-root")
    monkeypatch.setenv("DATASOURCE_URL", "postgres://user:pw@host/db")
    monkeypatch.setenv("DATASOURCE_URL__SALES", "postgres://user:pw@host/sales")
    _generate()

    passed = spawn.invocations[0][1]["env"]
    assert set(passed) <= set(gr._CHILD_ENV_KEYS)
    assert "AGAMI_ARTIFACTS_DIR" not in passed
    assert not any(key.startswith("DATASOURCE_URL") for key in passed)
    assert "/secret/artifacts-root" not in spawn.everything_given()
    assert "user:pw@host" not in spawn.everything_given()


def test_the_prompt_does_not_tell_the_client_how_many_queries_to_run(spawn):
    """The rule is real and the server enforces it. Asking for it here instead would mean it could
    be partly obeyed, and then nobody knows what the run measured."""
    _generate()
    prompt = spawn.system_prompts[0].casefold()

    for constraint in ("one query", "only one", "exactly one", "single query", "do not probe", "without probing"):
        assert constraint not in prompt, constraint
    assert "agami server's own instructions" in prompt  # what it is told to follow instead


# --- reading the client's answer --------------------------------------------


def test_the_value_comes_back_with_the_statement(spawn):
    generated = _generate()

    assert generated.sql == ANSWERED
    assert generated.value == "41822"
    assert generated.error is None


def test_a_client_envelope_that_is_not_json_is_still_searched_for_an_answer():
    """A client that wrote the object plainly is read the same way. The envelope is a convenience,
    never the only way to find an answer."""
    assert ANSWERED in gr._client_envelope(ANSWER)


# --- judging the answer against the trace -----------------------------------


def test_a_query_that_did_not_answer_beats_whatever_the_client_said(monkeypatch):
    """Rule 1, and the order matters: the failure IS the finding, so a client's tidy summary of
    having worked around it must not bury the message that says what broke."""
    monkeypatch.setattr(
        gr.subprocess,
        "run",
        _RecordedSpawn(
            calls=[
                {"tool": "get_datasource_schema", "args": {"area": "sales"}, "status": None},
                {
                    "tool": "execute_sql",
                    "args": {"sql": "SELECT SUM(o.amt) FROM orders o"},
                    "status": "failed",
                    "detail": "no column o.amt",
                },
            ]
        ),
    )
    generated = _generate()

    assert generated.error == "no column o.amt"
    assert generated.sql == "SELECT SUM(o.amt) FROM orders o"  # kept, so what failed can be read
    assert len(generated.trace) == 2


def test_a_refused_query_is_a_finding_exactly_like_a_failed_one(monkeypatch):
    """An out-of-scope table is the semantic model being wrong about the warehouse."""
    monkeypatch.setattr(
        gr.subprocess,
        "run",
        _RecordedSpawn(
            calls=[
                {
                    "tool": "execute_sql",
                    "args": {"sql": "SELECT * FROM ledger"},
                    "status": "refused",
                    "detail": "ledger is not declared by this datasource",
                }
            ]
        ),
    )
    assert _generate().error == "ledger is not declared by this datasource"


def test_a_number_with_no_query_behind_it_is_an_error_row(monkeypatch):
    monkeypatch.setattr(gr.subprocess, "run", _RecordedSpawn(calls=[
        {"tool": "get_datasource_schema", "args": {}, "status": None},
    ]))
    generated = _generate()

    assert generated.error == gr._GENERATION_UNQUERIED
    assert generated.sql == ""


def test_a_statement_the_client_did_not_run_is_an_error_row(monkeypatch):
    monkeypatch.setattr(gr.subprocess, "run", _RecordedSpawn(calls=[
        {"tool": "execute_sql", "args": {"sql": "SELECT COUNT(*) FROM customers"}, "status": "ok"},
    ]))
    generated = _generate()

    assert generated.error == gr._GENERATION_UNRUN
    assert generated.sql == ""


def test_a_statement_the_client_reformatted_still_counts_as_run(monkeypatch):
    """Calling a reformatted statement "one it did not run" would turn a good row into a false
    defect, which is the most expensive kind of error this report can make."""
    ran = "select  count(*)\n  as n\nfrom orders ;"
    monkeypatch.setattr(gr.subprocess, "run", _RecordedSpawn(calls=[
        {"tool": "execute_sql", "args": {"sql": ran}, "status": "ok"},
    ]))
    generated = _generate()

    assert generated.error is None
    # The row carries the server's spelling, never the client's: that is the statement that ran.
    assert generated.sql == ran.strip() and generated.statements == (ran.strip(),)


def test_the_row_carries_what_the_server_ran_when_the_client_quoted_it_loosely(monkeypatch):
    """The loose match cannot tell a comment from code or `'Shipped'` from `'shipped'`. So the
    client's copy only finds the statement, and anything that runs it again must get the server's
    text. Here the client's copy would have run a second query that ends the first one's comment."""
    ran = "SELECT COUNT(*) AS n FROM orders -- UNION ALL SELECT note FROM orders"
    reported = "SELECT COUNT(*) AS n FROM orders --\nUNION ALL SELECT note FROM orders"
    monkeypatch.setattr(gr.subprocess, "run", _RecordedSpawn(
        stdout=_envelope(json.dumps({"sql": reported, "value": "3"})),
        calls=[{"tool": "execute_sql", "args": {"sql": ran}, "status": "ok"}],
    ))
    generated = _generate()

    assert generated.error is None
    assert generated.sql == ran


def test_an_exact_copy_wins_over_a_near_one(monkeypatch):
    """Two statements that differ only in the case of a literal both ran. The client quoted one of
    them exactly, and that is the one the row keeps, even though the other ran later."""
    quoted = "SELECT COUNT(*) FROM orders WHERE status = 'Shipped'"
    other = "SELECT COUNT(*) FROM orders WHERE status = 'shipped'"
    monkeypatch.setattr(gr.subprocess, "run", _RecordedSpawn(
        stdout=_envelope(json.dumps({"sql": quoted, "value": "7"})),
        calls=[
            {"tool": "execute_sql", "args": {"sql": quoted}, "status": "ok"},
            {"tool": "execute_sql", "args": {"sql": other}, "status": "ok"},
        ],
    ))
    assert _generate().sql == quoted


def test_the_latest_near_copy_wins_when_no_copy_is_exact(monkeypatch):
    """A client that reformatted the same statement twice reported neither spelling exactly. The row
    takes the LAST one the server ran, because that is the statement whose result the client read."""
    first = "SELECT COUNT(*) AS n FROM orders"
    last = "SELECT  COUNT(*)  AS  n  FROM  orders"
    monkeypatch.setattr(gr.subprocess, "run", _RecordedSpawn(
        stdout=_envelope(json.dumps({"sql": "select count(*) as n from orders", "value": "3"})),
        calls=[
            {"tool": "execute_sql", "args": {"sql": first}, "status": "ok"},
            {"tool": "execute_sql", "args": {"sql": last}, "status": "ok"},
        ],
    ))
    assert _generate().sql == last


def test_a_tool_that_raised_ends_the_row_like_any_query_that_did_not_run(monkeypatch):
    monkeypatch.setattr(gr.subprocess, "run", _RecordedSpawn(calls=[
        {"tool": "execute_sql", "args": {"sql": ANSWERED}, "status": "raised", "detail": "TimeoutError"},
    ]))
    generated = _generate()

    assert generated.error == "TimeoutError"
    assert generated.sql == ANSWERED


def test_probing_first_and_answering_last_is_an_ordinary_row(monkeypatch):
    """Four probes then the answer. The probes are the measurement, not a problem."""
    monkeypatch.setattr(gr.subprocess, "run", _RecordedSpawn(calls=[
        {"tool": "get_prompt_examples", "args": {"query": QUESTION}, "status": None},
        {"tool": "get_datasource_schema", "args": {"area": "sales"}, "status": None},
        {"tool": "execute_sql", "args": {"sql": "SELECT DISTINCT status FROM orders"}, "status": "ok"},
        {"tool": "execute_sql", "args": {"sql": ANSWERED}, "status": "ok"},
    ]))
    generated = _generate()

    assert generated.error is None and generated.sql == ANSWERED
    assert len([e for e in generated.trace if e["tool"] == "execute_sql"]) == 2


def test_the_trace_is_kept_even_when_the_client_never_answered(monkeypatch):
    """A row that produced nothing is the row whose trace a person most wants to read."""
    monkeypatch.setattr(gr.subprocess, "run", _RecordedSpawn(stdout=_envelope("no idea, sorry"), calls=[
        {"tool": "get_datasource_schema", "args": {"area": "sales"}, "status": None},
        {"tool": "execute_sql", "args": {"sql": ANSWERED}, "status": "ok"},
    ]))
    generated = _generate()

    assert generated.error == gr._GENERATION_UNREADABLE
    assert len(generated.trace) == 2


def test_an_effort_level_is_checked_before_a_run_rather_than_by_the_client():
    with pytest.raises(ValueError):
        gr.ClaudeMcpGenerator(_server(), timeout_s=60.0, effort="thorough")


def test_a_stopped_query_is_never_the_last_one_that_worked(monkeypatch):
    """Taken from the first real run against a live profile, which is where the bug showed.

    A stopped call carries no `status` at all, so reading a missing status as success named the
    STOPPED statement as the last successful one — on exactly the rows a person opens to find out
    what broke. The failed statement is the row's, and nothing after it worked.
    """
    monkeypatch.setattr(gr.subprocess, "run", _RecordedSpawn(calls=[
        {"tool": "get_datasource_schema", "args": {}, "status": None},
        {"tool": "get_prompt_examples", "args": {"query": QUESTION}, "status": None},
        {
            "tool": "execute_sql",
            "args": {"sql": "SELECT COUNT(*) FROM orders WHERE order_status = 'open'"},
            "status": "failed",
            "detail": "The statement referenced a column this database does not have.",
        },
        {"tool": "get_datasource_schema", "args": {"dataset_names": ["orders"]}, "status": None},
        {"tool": "execute_sql", "args": {"sql": "SELECT COUNT(*) FROM orders WHERE is_open"},
         "stopped": "query_failed"},
    ]))
    generated = _generate()

    assert generated.error == "The statement referenced a column this database does not have."
    assert "order_status" in generated.sql  # the statement that failed is the row's
    assert not any(gr._answered(entry) for entry in generated.trace if entry["tool"] == "execute_sql")
