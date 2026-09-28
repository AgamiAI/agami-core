"""The activity log records what each tool call really did.

Four facts decide how often the AI gets a call wrong, and each was missing or false on `tool_calls`:
the real failure kind of an `execute_sql` (it was the literal `failed`), the names that missed and
what the server offered instead, every parameter the call was sent, and the size of the reply.

The failure-kind and miss criteria are driven over the real HTTP transport (`mcp_http.create_app`
under a `TestClient`) and read back from the row, not by calling `record_tool_call` directly. The
typed outcome and the per-call context exist only on that path, and the last time a recorded field
was tested only at the function, the column was NULL on every production row while the tests passed.
The one exception is a wrong-shape argument, which the MCP SDK rejects over HTTP before the handler
runs; see `_as_the_transport_records`.

Synthetic throughout: agami-core is public.
"""

from __future__ import annotations

import contextvars
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import tools  # noqa: E402
from store import Store  # noqa: E402

from test_schema_name_hints import _write_model  # noqa: E402

PROFILE = "acme"
ACTOR = "you@example.com"


@pytest.fixture(autouse=True)
def _isolate():
    """`_INJECTED_EXECUTOR` is a process global that `create_app` sets; never leak it."""
    tools.set_injected_executor(None)
    yield
    tools.set_injected_executor(None)


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A served install: a migrated app database to record into and a two-area model on disk
    (`sales`: orders + order_items, metric order_count; `people`: users, metric headcount)."""
    app_db = "sqlite://" + str(tmp_path / "app.db")
    s = Store.connect(app_db)
    s.run_migrations()
    s.close()
    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)
    monkeypatch.setenv("AGAMI_DB_URL", app_db)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("DATASOURCE_URL__ACME", "postgresql://demo@your-cluster.example.com/demo")
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://your-host.example.com")
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", "x" * 40)
    # Served, the model is read from the database, so it is seeded there from the tree on disk.
    import model_store
    from semantic_model import loader

    tools.resolved_org_id.cache_clear()
    s = Store.connect(app_db)
    model_store.write_datasource(
        s, PROFILE, loader.load_datasource(artifacts / PROFILE), org_id=tools._current_org_id()
    )
    s.commit()
    s.close()
    return app_db


def _http(calls, *, executor=None, extra_tools=None) -> list[str]:
    """Each `(tool, arguments)` over the authenticated HTTP transport; the text the client received.

    `executor=None` leaves `create_app`'s own executor in place; `"fork"` removes it, so
    `execute_sql` takes the forked-child path with its supervisor; anything else is injected.
    """
    import mcp_http
    from oauth_server import issue_jwt
    from starlette.testclient import TestClient

    headers = {
        "Authorization": f"Bearer {issue_jwt(ACTOR)}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    texts = []
    with TestClient(mcp_http.create_app(extra_tools=extra_tools)) as client:
        if executor == "fork":
            tools.set_injected_executor(None)
        elif executor is not None:
            tools.set_injected_executor(executor)
        init = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
        session = init.headers.get("mcp-session-id")
        headers2 = {**headers, **({"mcp-session-id": session} if session else {})}
        client.post(
            "/mcp", headers=headers2, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        for i, (name, arguments) in enumerate(calls):
            resp = client.post(
                "/mcp",
                headers=headers2,
                json={
                    "jsonrpc": "2.0",
                    "id": 10 + i,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            assert resp.status_code == 200, resp.text
            texts.append(resp.json()["result"]["content"][0]["text"])
    return texts


def _rows(url: str) -> list[dict]:
    s = Store.connect(url)
    try:
        # `ts` has one-second resolution, so calls in the same second fall back to insert order.
        return [dict(r) for r in s.query("SELECT * FROM tool_calls ORDER BY ts, rowid")]
    finally:
        s.close()


def _one(url: str) -> dict:
    (row,) = _rows(url)
    return row


def _missed(row: dict) -> list[dict]:
    return json.loads(row["missed"])["entries"]


# --- SC1: the real failure kind ------------------------------------------------------------------


class _Raises:
    def __init__(self, code: int, text: str):
        self.code, self.text = code, text

    def execute(self, vetted_sql, creds, *, profile):
        raise execute_sql.ExecutorError(self.text, code=self.code)


# Each reachable kind, raised by an injected executor with a code and the words its own driver would
# use. `timeout` is the one kind no executor raises: it is the fork supervisor's, driven below.
_KINDS = [
    ("syntax", 'syntax error at or near "FROM"'),
    ("column_not_found", "no such column: nope"),
    ("table_not_found", "no such table: nope"),
    ("permission", "permission denied for table orders"),
    ("auth", "password authentication failed for user demo"),
    ("network", "connection refused by the server"),
    ("dsn", "could not translate host name to address"),
    ("driver_missing", "no module named demo_driver"),
    ("other", "something nobody anticipated"),
    ("sign_in_required", "no stored credential for this person"),
]


@pytest.mark.parametrize("kind, text", _KINDS, ids=[k for k, _ in _KINDS])
def test_a_failed_query_records_its_kind(served, kind, text):
    executor = _Raises(execute_sql.FAILURE_KIND_TO_EXIT[kind], text)
    (reply,) = _http(
        [("execute_sql", {"sql": "SELECT id FROM orders", "datasource": PROFILE})],
        executor=executor,
    )

    body = json.loads(reply)
    assert (body["status"], body["failure"]["kind"]) == ("failed", kind)
    row = _one(served)
    assert (row["success"], row["error_kind"]) == (0, kind)


def test_a_supervisor_timeout_on_the_fork_path_records_timeout(served, monkeypatch):
    def _timed_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(tools.subprocess, "run", _timed_out)
    (reply,) = _http(
        [("execute_sql", {"sql": "SELECT id FROM orders", "datasource": PROFILE})],
        executor="fork",
    )

    assert json.loads(reply)["failure"]["kind"] == "timeout"
    assert _one(served)["error_kind"] == "timeout"


def test_a_refusal_still_records_its_rule(served):
    """The rule outranks the kind, so a refusal is unchanged — and `audit_unavailable`'s skip, which
    reads the rule off `error_kind`, keeps working."""
    _http([("execute_sql", {"sql": "DELETE FROM orders", "datasource": PROFILE})])

    assert _one(served)["error_kind"] == "read_only"


# --- SC2: a partial table miss -------------------------------------------------------------------


def test_one_real_and_one_unknown_table_is_a_success_with_one_miss(served):
    (reply,) = _http(
        [("get_datasource_schema", {"datasource": PROFILE, "dataset_names": ["orders", "ordrs"]})]
    )

    offered = json.JSONDecoder().raw_decode(reply)[0]["tables"]["ordrs"]["did_you_mean"]
    row = _one(served)
    assert (row["success"], row["error_kind"], row["miss_count"]) == (1, None, 1)
    assert _missed(row) == [{"kind": "table", "name": "ordrs", "did_you_mean": offered}]
    assert "orders" in offered


def test_a_partial_miss_past_the_suggestion_cap_is_still_counted(served):
    unknown = [f"zz_unknown_{i:02d}" for i in range(tools._SUGGESTED_MISSES + 2)]
    _http(
        [("get_datasource_schema", {"datasource": PROFILE, "dataset_names": ["orders", *unknown]})]
    )

    row = _one(served)
    assert row["success"] == 1 and row["miss_count"] == len(unknown)
    assert [m["name"] for m in _missed(row)] == unknown
    # Searched up to the cap, so each has a list, empty when nothing was close; never searched past it.
    cap = tools._SUGGESTED_MISSES
    assert all("did_you_mean" in m for m in _missed(row)[:cap])
    assert not any("did_you_mean" in m for m in _missed(row)[cap:])


def test_a_defined_but_unresolvable_table_is_not_a_miss(served, monkeypatch):
    """A name the model defines is not the caller's mistake (#258), so it is not recorded as one."""
    real = tools._table_contexts

    def _one_unresolved(org, names, L, index=None):
        ctx = real(org, names, L, index=index)
        ctx["tables"]["order_items"] = {"error": "not found in scope"}
        return ctx

    monkeypatch.setattr(tools, "_table_contexts", _one_unresolved)
    _http(
        [
            (
                "get_datasource_schema",
                {"datasource": PROFILE, "dataset_names": ["orders", "order_items"]},
            )
        ]
    )

    row = _one(served)
    assert (row["success"], row["miss_count"], row["missed"]) == (1, None, None)


# --- SC3: whole-call misses ----------------------------------------------------------------------


def _error(reply: str) -> dict:
    return json.JSONDecoder().raw_decode(reply)[0]["error"]


def test_an_unknown_area_is_recorded_with_its_suggestions(served):
    (reply,) = _http([("get_datasource_schema", {"datasource": PROFILE, "area": "sale"})])

    row = _one(served)
    assert (row["success"], row["error_kind"]) == (0, "not_found")
    assert _missed(row) == [
        {"kind": "area", "name": "sale", "did_you_mean": _error(reply)["did_you_mean"]}
    ]
    assert row["miss_count"] == 1


def test_an_unknown_datasource_is_recorded_with_its_suggestions(served, monkeypatch):
    monkeypatch.setattr(tools, "_known_datasources", lambda: [PROFILE, "billing"])
    (reply,) = _http([("get_datasource_schema", {"datasource": "acmee"})])

    row = _one(served)
    assert (row["success"], row["error_kind"]) == (0, "not_found")
    assert _missed(row) == [
        {"kind": "datasource", "name": "acmee", "did_you_mean": _error(reply)["did_you_mean"]}
    ]
    assert _error(reply)["did_you_mean"] == [PROFILE]


def test_a_known_datasource_with_a_broken_model_is_not_a_miss(served, monkeypatch):
    monkeypatch.setattr(tools, "_known_datasources", lambda: [PROFILE, "billing"])
    _http([("get_datasource_schema", {"datasource": "billing"})])

    row = _one(served)
    assert (row["success"], row["error_kind"], row["missed"]) == (0, "not_found", None)


def test_a_scope_where_no_named_table_exists_records_every_name(served):
    (reply,) = _http(
        [("get_datasource_schema", {"datasource": PROFILE, "dataset_names": ["ordrs", "sales"]})]
    )

    row = _one(served)
    assert (row["success"], row["error_kind"], row["miss_count"]) == (0, "not_found", 2)
    offered = _error(reply)["did_you_mean"]
    # `sales` is an area: it gets a hint, not a guess, and is recorded with nothing offered.
    assert _missed(row) == [
        {"kind": "table", "name": "ordrs", "did_you_mean": offered["ordrs"]},
        {"kind": "table", "name": "sales", "did_you_mean": []},
    ]


def test_tables_outside_the_declared_area_are_misplaced_or_unknown(served):
    _http(
        [
            (
                "get_datasource_schema",
                {"datasource": PROFILE, "area": "sales", "dataset_names": ["users", "usrs"]},
            )
        ]
    )

    row = _one(served)
    assert (row["success"], row["miss_count"]) == (0, 2)
    by_name = {m["name"]: m for m in _missed(row)}
    assert by_name["users"] == {"kind": "misplaced_table", "name": "users", "did_you_mean": []}
    assert by_name["usrs"]["kind"] == "table" and "users" in by_name["usrs"]["did_you_mean"]


def test_misplaced_tables_all_in_no_area_are_unknown_tables(served):
    _http(
        [
            (
                "get_datasource_schema",
                {"datasource": PROFILE, "area": "sales", "dataset_names": ["usrs"]},
            )
        ]
    )

    (miss,) = _missed(_one(served))
    assert miss["kind"] == "table" and miss["name"] == "usrs"


def _as_the_transport_records(name: str, handler, arguments: dict) -> str:
    """The handler run and recorded exactly as `mcp_http._call_tool` composes it: inside a Context
    this frame owns, reset first, with the overrides read back out of it.

    For the wrong-shape arguments only. Over HTTP the MCP SDK validates arguments against the tool's
    `inputSchema` before the handler runs, so a `mode` outside its enum or an `area` sent as a list
    never reaches the handler there, and no `tool_calls` row is written for it at all. The handler's
    own refusal is what a transport without that validation (stdio, an embedder's dispatch) returns.
    """
    ctx = contextvars.copy_context()
    ctx.run(tools.reset_typed_outcome)
    reply = ctx.run(handler, arguments)
    tools.record_tool_call(
        name=name,
        arguments=arguments,
        result_text=reply,
        execution_ms=1,
        actor=ACTOR,
        **tools.typed_outcome_overrides(ctx),
    )
    return reply


def test_an_invalid_argument_records_the_key_and_the_value_sent(served):
    reply = _as_the_transport_records(
        "get_datasource_schema",
        tools.tool_get_datasource_schema,
        {"datasource": PROFILE, "mode": "sumary"},
    )

    row = _one(served)
    assert (row["success"], row["error_kind"]) == (0, "invalid_argument")
    assert _missed(row) == [
        {
            "kind": "argument",
            "name": "mode",
            "value": "sumary",
            "did_you_mean": _error(reply)["did_you_mean"],
        }
    ]


@pytest.mark.parametrize(
    "arguments, key",
    [
        ({"area": ["sales", "people"]}, "area"),
        ({"area": 7}, "area"),
        ({"dataset_names": 7}, "dataset_names"),
    ],
)
def test_an_invalid_argument_that_is_not_a_string_records_only_the_key(served, arguments, key):
    _as_the_transport_records(
        "get_datasource_schema",
        tools.tool_get_datasource_schema,
        {"datasource": PROFILE, **arguments},
    )

    assert _missed(_one(served)) == [{"kind": "argument", "name": key, "did_you_mean": []}]


def test_a_refused_table_scope_past_the_cap_records_unsearched_names_bare(served):
    unknown = [f"zz_unknown_{i:02d}" for i in range(tools._SUGGESTED_MISSES + 2)]
    _http([("get_datasource_schema", {"datasource": PROFILE, "dataset_names": unknown})])

    missed = _missed(_one(served))
    assert [m["name"] for m in missed] == unknown
    assert all(m["did_you_mean"] == [] for m in missed[: tools._SUGGESTED_MISSES])
    assert not any("did_you_mean" in m for m in missed[tools._SUGGESTED_MISSES :])


def test_misplaced_names_the_refusal_does_not_show_are_recorded_bare(served):
    # Sorted, `users` falls past the cap, behind eleven unknown names.
    unknown = [f"aa_{i:02d}" for i in range(tools._SUGGESTED_MISSES + 1)]
    _http(
        [
            (
                "get_datasource_schema",
                {"datasource": PROFILE, "area": "sales", "dataset_names": [*unknown, "users"]},
            )
        ]
    )

    by_name = {m["name"]: m for m in _missed(_one(served))}
    assert all("did_you_mean" in by_name[n] for n in unknown[: tools._SUGGESTED_MISSES])
    assert by_name[unknown[-1]] == {"kind": "table", "name": unknown[-1]}
    assert by_name["users"] == {"kind": "misplaced_table", "name": "users"}


# --- SC4: metric misses --------------------------------------------------------------------------


def test_every_reported_unknown_metric_is_a_recorded_miss(served):
    names = ["order_cnt", "headcount"] + [
        f"zz_metric_{i:02d}" for i in range(tools._SUGGESTED_MISSES)
    ]
    (reply,) = _http(
        [("get_datasource_schema", {"datasource": PROFILE, "area": "sales", "metric_names": names})]
    )

    reported = json.JSONDecoder().raw_decode(reply)[0]["unknown_metric_names"]
    row = _one(served)
    assert row["success"] == 1 and row["miss_count"] == len(reported) == len(names)
    # Past the suggestion cap a name was never searched, so it is recorded with no `did_you_mean`
    # at all: an empty list would say "searched, nothing close".
    assert _missed(row) == [
        {"kind": "metric", "name": r["name"], "did_you_mean": r.get("did_you_mean", [])[:3]}
        if i < tools._SUGGESTED_MISSES
        else {"kind": "metric", "name": r["name"]}
        for i, r in enumerate(reported)
    ]
    # `headcount` exists outside the `sales` scope: reported with a hint, recorded as a miss.
    assert {"kind": "metric", "name": "headcount", "did_you_mean": []} in _missed(row)


def test_a_table_refusal_records_no_metric_misses(served):
    """The refusal carries no `unknown_metric_names`, so the row must not claim metric misses."""
    (reply,) = _http(
        [
            (
                "get_datasource_schema",
                {"datasource": PROFILE, "dataset_names": ["zzzq"], "metric_names": ["order_cnt"]},
            )
        ]
    )

    assert "unknown_metric_names" not in json.JSONDecoder().raw_decode(reply)[0]
    row = _one(served)
    assert row["miss_count"] == 1
    assert [(m["kind"], m["name"]) for m in _missed(row)] == [("table", "zzzq")]


# --- SC5: the examples tool ----------------------------------------------------------------------


def test_an_examples_call_naming_an_unknown_area_records_the_miss(served):
    _http([("get_prompt_examples", {"datasource": PROFILE, "area": "peple"})])

    row = _one(served)
    (miss,) = _missed(row)
    assert (miss["kind"], miss["name"]) == ("area", "peple") and "people" in miss["did_you_mean"]


def test_an_examples_call_omitting_the_area_records_no_miss(served):
    _http([("get_prompt_examples", {"datasource": PROFILE, "query": "orders"})])

    row = _one(served)
    assert (row["missed"], row["miss_count"]) == (None, None)


def test_misses_do_not_leak_into_the_next_tool(served):
    _http(
        [
            ("get_datasource_schema", {"datasource": PROFILE, "area": "sale"}),
            ("list_datasources", {}),
        ]
    )

    first, second = _rows(served)
    assert first["miss_count"] == 1
    assert (second["missed"], second["miss_count"]) == (None, None)


def test_resetting_a_context_clears_misses_it_inherited():
    """`copy_context()` copies what is current, so a miss published before the reset would be read
    back as the next tool's. Set in a throwaway outer context so nothing reaches another test."""

    def _inherit_then_reset():
        tools._call_misses.set([{"kind": "area", "name": "sale", "did_you_mean": []}])
        ctx = contextvars.copy_context()
        ctx.run(tools.reset_typed_outcome)
        return tools.typed_outcome_overrides(ctx)

    assert "missed" not in contextvars.copy_context().run(_inherit_then_reset)


@pytest.mark.parametrize(
    "handler, missing, valid",
    [
        (tools.tool_get_datasource_schema, {"area": "sale"}, {}),
        (tools.tool_get_prompt_examples, {"area": "peple"}, {"query": "orders"}),
    ],
)
def test_each_handler_clears_the_last_calls_misses_on_entry(served, handler, missing, valid):
    """The stdio server runs every call in one context and never resets it, so the handler must."""
    ctx = contextvars.copy_context()
    ctx.run(handler, {"datasource": PROFILE, **missing})
    assert ctx.get(tools._call_misses)

    ctx.run(handler, {"datasource": PROFILE, **valid})
    assert not ctx.get(tools._call_misses)


# --- SC6: every parameter the call was sent ------------------------------------------------------


def test_arguments_hold_what_was_sent_apart_from_own_column_keys(served):
    sent = {
        "datasource": PROFILE,
        "area": "sales",
        "mode": "summary",
        "query": "revenue by month",
        "metric_names": ["order_count"],
        "thread_id": "t1",
        "correlation_id": "c1",
        "user_question": "how are sales?",
        "client_model": "demo-model",
    }
    _http([("get_datasource_schema", sent)])

    row = _one(served)
    assert json.loads(row["arguments"]) == {
        "values": {
            "area": "sales",
            "mode": "summary",
            "query": "revenue by month",
            "metric_names": ["order_count"],
        },
        "truncated": False,
    }
    assert (row["thread_id"], row["user_question"]) == ("t1", "how are sales?")


def test_arguments_on_the_examples_tool_hold_query_and_top_k(served):
    _http([("get_prompt_examples", {"datasource": PROFILE, "query": "orders", "top_k": 3})])

    assert json.loads(_one(served)["arguments"])["values"] == {"query": "orders", "top_k": 3}


def test_a_call_sent_only_own_column_keys_records_null(served):
    _http(
        [
            ("list_datasources", {}),
            ("execute_sql", {"sql": "DELETE FROM orders", "datasource": PROFILE, "raw_query": "x"}),
        ]
    )

    assert [r["arguments"] for r in _rows(served)] == [None, None]


def _record(name: str, arguments, **kw):
    tools.record_tool_call(
        name=name,
        arguments=arguments,
        result_text=kw.pop("result_text", "{}"),
        execution_ms=1,
        actor=ACTOR,
        **kw,
    )


def test_example_is_its_own_column_only_on_execute_sql(served):
    example = {"id": "abc123", "use": "followed"}
    _record("execute_sql", {"sql": "SELECT 1", "example": example})
    _record("acme_tool", {"example": example})

    on_sql, on_other = _rows(served)
    assert on_sql["arguments"] is None and on_sql["example_id"] == "abc123"
    assert json.loads(on_other["arguments"])["values"] == {"example": example}
    assert on_other["example_id"] is None


# --- SC7: the reply size -------------------------------------------------------------------------


def test_the_reply_size_is_the_text_the_client_received(served):
    replies = _http(
        [
            ("list_datasources", {}),
            ("get_datasource_schema", {"datasource": PROFILE}),
            ("execute_sql", {"sql": "DELETE FROM orders", "datasource": PROFILE}),
        ]
    )

    for reply, row in zip(replies, _rows(served), strict=True):
        assert row["result_chars"] == len(reply)
        assert row["result_tokens_est"] == math.ceil(len(reply) / 4)


def test_a_raising_handler_records_no_size(served):
    def _boom(args):
        raise RuntimeError("boom")

    probe = {
        "handler": _boom,
        "description": "probe",
        "inputSchema": {"type": "object", "properties": {}},
    }
    _http([("probe", {})], extra_tools={"probe": probe})

    row = _one(served)
    assert (row["success"], row["error_kind"]) == (0, "exception")
    assert (row["result_chars"], row["result_tokens_est"]) == (None, None)


def test_a_raised_call_records_no_size_even_with_text(served):
    _record("acme_tool", {}, result_text="partial", raised=True)

    row = _one(served)
    assert (row["result_chars"], row["result_tokens_est"]) == (None, None)


def test_an_empty_reply_records_a_size_of_zero(served):
    _record("acme_tool", {}, result_text="")

    row = _one(served)
    assert (row["result_chars"], row["result_tokens_est"]) == (0, 0)


def test_no_reply_text_records_no_size(served):
    _record("acme_tool", {}, result_text=None)

    row = _one(served)
    assert (row["result_chars"], row["result_tokens_est"]) == (None, None)


# --- SC8: bounded against a hostile caller -------------------------------------------------------


def _assert_clean(stored: str, cap: int) -> dict:
    assert len(stored) <= cap
    assert not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in stored)
    doc = json.loads(stored)
    assert doc["truncated"] is True
    return doc


def test_ten_thousand_names_stay_inside_both_bounds(served):
    names = [f"zz_{i:05d}\n" for i in range(10_000)]
    (reply,) = _http([("get_datasource_schema", {"datasource": PROFILE, "dataset_names": names})])

    assert _error(reply)["kind"] == "not_found"
    row = _one(served)
    _assert_clean(row["arguments"], tools.AUDIT_ARGUMENTS_MAX_CHARS)
    doc = _assert_clean(row["missed"], tools.AUDIT_MISSED_MAX_CHARS)
    assert doc["entries"] and row["miss_count"] == 10_000  # the true total, not what fitted
    assert all("\n" not in m["name"] for m in doc["entries"])


def test_one_huge_value_is_cut_and_says_so(served):
    _record("acme_tool", {"query": "x" * 100_000, "top_k": 3})

    doc = _assert_clean(_one(served)["arguments"], tools.AUDIT_ARGUMENTS_MAX_CHARS)
    assert len(doc["values"]["query"]) == tools.AUDIT_ARG_VALUE_MAX_CHARS
    assert doc["values"]["top_k"] == 3


def test_control_characters_are_replaced_everywhere(served):
    _record("acme_tool", {"query": "a\x00b\x1bc\x7f", "k\ney": ["x\ty"], "nested": {"n": "r\rs"}})

    stored = _one(served)["arguments"]
    assert json.loads(stored) == {
        "values": {"query": "a b c ", "k ey": ["x y"], "nested": {"n": "r s"}},
        "truncated": False,
    }


def test_a_parameter_named_truncated_cannot_forge_the_flag(served):
    _record("acme_tool", {"truncated": False, "query": "x" * 100_000})

    doc = json.loads(_one(served)["arguments"])
    assert doc["truncated"] is True and doc["values"]["truncated"] is False


def test_wide_and_deep_values_are_cut(served):
    deep: dict = {"leaf": 1}
    for _ in range(6):
        deep = {"d": deep}
    _record(
        "acme_tool",
        {"many": list(range(500)), "wide": {str(i): i for i in range(500)}, "deep": deep},
    )

    doc = json.loads(_one(served)["arguments"])
    assert doc["truncated"] is True
    assert len(doc["values"]["many"]) == tools.AUDIT_ARG_LIST_MAX_ITEMS
    assert len(doc["values"]["wide"]) == tools.AUDIT_ARG_LIST_MAX_ITEMS
    assert doc["values"]["deep"]["d"]["d"] == {"d": None}  # the top-level parameters are depth 0


def test_top_level_keys_past_the_cap_are_dropped(served):
    _record("acme_tool", {f"k{i:03d}": i for i in range(200)})

    doc = json.loads(_one(served)["arguments"])
    assert doc["truncated"] is True and len(doc["values"]) == tools.AUDIT_ARG_LIST_MAX_ITEMS


def test_a_non_json_value_is_stored_as_text(served):
    _record("acme_tool", {"when": object.__new__(_Opaque), "pair": (1, 2)})

    assert json.loads(_one(served)["arguments"])["values"] == {"when": "opaque", "pair": [1, 2]}


class _Opaque:
    def __str__(self) -> str:
        return "opaque"


def test_a_long_missed_name_and_its_suggestions_are_bounded(served):
    _record(
        "acme_tool",
        {},
        missed=[
            {
                "kind": "table",
                "name": "  " + "n" * 500 + "\n",
                "did_you_mean": ["a", "b", "c", "d"],
            },
        ],
    )

    row = _one(served)
    doc = json.loads(row["missed"])
    (entry,) = doc["entries"]
    assert entry["name"] == "n" * tools.AUDIT_MISS_NAME_MAX_CHARS
    assert entry["did_you_mean"] == ["a", "b", "c"]
    assert doc["truncated"] is True and row["miss_count"] == 1


def test_scalars_are_kept_and_a_non_finite_float_stays_valid_json(served):
    _record("acme_tool", {"v": float("inf"), "b": True, "n": None, "f": 1.5})

    stored = _one(served)["arguments"]
    assert "Infinity" not in stored.replace('"inf"', "")
    assert json.loads(stored)["values"] == {"v": "inf", "b": True, "n": None, "f": 1.5}


def test_one_oversized_parameter_does_not_evict_the_rest():
    stored = tools._bounded_arguments("acme_tool", {"a": ["x" * 1000] * 50, "b": "keep"})

    assert len(stored) <= tools.AUDIT_ARGUMENTS_MAX_CHARS
    doc = json.loads(stored)
    assert doc["truncated"] is True and doc["values"] == {"b": "keep"}


def test_an_oversized_scope_keeps_the_query_on_the_row(served):
    names = [f"{i:02d}" + "n" * 998 for i in range(50)]
    _http(
        [
            (
                "get_datasource_schema",
                {"datasource": PROFILE, "dataset_names": names, "query": "revenue"},
            )
        ]
    )

    doc = _assert_clean(_one(served)["arguments"], tools.AUDIT_ARGUMENTS_MAX_CHARS)
    assert doc["values"] == {"query": "revenue"}


def test_keys_that_collapse_together_say_so(served):
    _record("acme_tool", {"k\ney": 1, "k ey": 2})

    doc = json.loads(_one(served)["arguments"])
    assert doc["truncated"] is True and list(doc["values"]) == ["k ey"]


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("no text for this one")


def test_a_value_that_cannot_become_text_is_a_placeholder():
    doc = json.loads(tools._bounded_arguments("acme_tool", {"v": _Unprintable(), "top_k": 3}))

    assert doc["truncated"] is True
    assert isinstance(doc["values"]["v"], str) and doc["values"]["top_k"] == 3


def test_an_int_past_the_digit_limit_is_a_placeholder():
    doc = json.loads(tools._bounded_arguments("acme_tool", {"n": 10**5000, "top_k": 3}))

    assert doc["truncated"] is True
    assert isinstance(doc["values"]["n"], str) and doc["values"]["top_k"] == 3


# C1 controls, the line and paragraph separators, the bidi controls, and one lone surrogate.
_UNICODE_CONTROLS = "\x85\x9b\u2028\u2029\u200e\u200f\u202a\u202e\u2066\u2069\ud800\udfff"


def _has_unicode_control(value) -> bool:
    return any(ch in _UNICODE_CONTROLS for ch in json.dumps(value, ensure_ascii=False))


def test_unicode_controls_are_replaced_everywhere(served):
    dirty = "a" + _UNICODE_CONTROLS + "b"
    _record(
        "acme_tool",
        {"query": dirty, dirty: [dirty]},
        missed=[{"kind": dirty, "name": dirty, "did_you_mean": [dirty], "value": dirty}],
    )

    row = _one(served)
    arguments, missed = json.loads(row["arguments"]), json.loads(row["missed"])
    assert not _has_unicode_control(arguments) and not _has_unicode_control(missed)
    (entry,) = missed["entries"]
    assert entry["kind"] == entry["name"] == "a" + " " * len(_UNICODE_CONTROLS) + "b"


def test_a_malformed_missed_entry_is_stored_rather_than_raised(served):
    _record(
        "acme_tool",
        {},
        missed=[
            {},
            {"kind": "table", "name": "a", "did_you_mean": None},
            {"kind": "table", "name": "b", "did_you_mean": "orders"},
            {"kind": "table", "name": "c", "did_you_mean": 7},
            {"kind": "table", "name": _Unprintable()},
            "orders",
        ],
    )

    row = _one(served)
    empty, none, text, number, unprintable, bare = _missed(row)
    assert empty == {"kind": "", "name": ""}
    assert none == {"kind": "table", "name": "a"}
    assert text["did_you_mean"] == ["orders"] and number["did_you_mean"] == ["7"]
    assert isinstance(unprintable["name"], str) and bare == {"kind": "", "name": "orders"}
    assert row["miss_count"] == 6


def test_building_missed_entries_stops_once_the_budget_is_spent(monkeypatch):
    built = []
    real = tools._audit_text

    def _counting(raw, cap):
        built.append(raw)
        return real(raw, cap)

    monkeypatch.setattr(tools, "_audit_text", _counting)
    tools._bounded_missed([{"kind": "table", "name": f"t{i}"} for i in range(10_000)])

    # Two texts per entry (kind and name); far fewer entries than were sent can ever fit.
    assert len(built) < 2 * tools.AUDIT_MISSED_MAX_CHARS // 10


# --- SC9: the migration ---------------------------------------------------------------------------


_NEW_COLUMNS = ("arguments", "missed", "miss_count", "result_chars", "result_tokens_est")


def test_rows_written_before_the_migration_read_null(tmp_path):
    url = "sqlite://" + str(tmp_path / "existing.db")
    s = Store.connect(url)
    s.run_migrations()
    s.execute(
        "INSERT INTO tool_calls (id, ts, org_id, tool_name, source, success) VALUES (?,?,?,?,?,?)",
        ("row-1", "2026-09-27T00:00:00Z", "local", "execute_sql", "mcp", 1),
    )
    s.commit()
    s.run_migrations()
    (row,) = s.query(f"SELECT {', '.join(_NEW_COLUMNS)} FROM tool_calls WHERE id = 'row-1'")
    s.close()
    assert all(dict(row)[c] is None for c in _NEW_COLUMNS)


def test_an_older_record_shape_still_writes(served):
    from types import SimpleNamespace

    import model_store

    older = SimpleNamespace(
        ts="2026-09-27T00:00:00Z",
        org_id="local",
        actor=ACTOR,
        tool_name="execute_sql",
        datasource=PROFILE,
        sql="SELECT 1",
        row_count=1,
        execution_ms=1,
        success=True,
        error_kind=None,
        source="mcp",
        user_question=None,
        agent_query=None,
        thread_id=None,
        correlation_id=None,
    )
    s = Store.connect(served)
    model_store.DbActivitySink(s).record_tool_call(older)
    s.close()

    row = _one(served)
    assert all(row[c] is None for c in _NEW_COLUMNS)


# --- SC10: no response changes -------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler, arguments",
    [
        (tools.tool_get_datasource_schema, {"dataset_names": ["orders", "ordrs"]}),
        (tools.tool_get_datasource_schema, {"area": "sale"}),
        (tools.tool_get_datasource_schema, {"dataset_names": ["ordrs"]}),
        (tools.tool_get_datasource_schema, {"area": "sales", "dataset_names": ["users"]}),
        (tools.tool_get_datasource_schema, {"area": "sales", "metric_names": ["order_cnt"]}),
        (tools.tool_get_datasource_schema, {"mode": "sumary"}),
        (tools.tool_get_prompt_examples, {"area": "peple"}),
    ],
)
def test_noting_a_miss_changes_no_reply(served, monkeypatch, handler, arguments):
    """Byte-compare each miss path's reply with the noting on and off."""
    args = {"datasource": PROFILE, **arguments}
    ctx = contextvars.copy_context()
    ctx.run(tools.reset_typed_outcome)
    with_noting = ctx.run(handler, dict(args))
    assert tools.typed_outcome_overrides(ctx)["missed"]

    monkeypatch.setattr(tools, "_note_miss", lambda *a, **k: None)
    tools._ORG_CACHE.clear()
    assert tools.typed_outcome_overrides(contextvars.copy_context()).get("missed") is None
    assert handler(dict(args)) == with_noting
