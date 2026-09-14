"""An organisation's own row cap and statement deadline (#329).

Core stores no such setting. A consumer registers a provider, `(org_id) -> {"max_rows", "timeout_s"}`,
and core resolves it ONCE per call, holds the answer for the whole call, and writes the same numbers
into a forked child's environment — so the ordered bound family (watchdog < native < outer <
supervisor) stays one budget on both sides of the fork. Anything missing or unusable falls back to
the deployment's `AGAMI_SQL_MAX_ROWS` / `AGAMI_SQL_TIMEOUT_S`, and there is no ceiling.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

import execute_sql  # noqa: E402
import tools  # noqa: E402

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"

_LIMITS = {
    "acme": {"max_rows": 5000, "timeout_s": 90},
    "globex": {"max_rows": 20, "timeout_s": 4},
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AGAMI_SQL_MAX_ROWS", raising=False)
    monkeypatch.delenv("AGAMI_SQL_TIMEOUT_S", raising=False)
    tools.set_statement_limits_provider(None)
    yield
    tools.set_statement_limits_provider(None)


def _in_org(monkeypatch, org_id: str) -> None:
    # Patched rather than set on `_current_org_ctx`, so monkeypatch undoes it and no later test starts
    # inside this organisation.
    monkeypatch.setattr(tools, "_current_org_id", lambda: org_id)


def _by_org(org_id: str):
    return _LIMITS.get(org_id)


# ----------------------------------------------------------------------------------------------
# The pin: in-process resolvers see the organisation's numbers, and only for the call
# ----------------------------------------------------------------------------------------------


def test_the_override_reaches_both_in_process_resolvers():
    tools.set_statement_limits_provider(_by_org)

    with tools.pinned_statement_limits("acme") as pinned:
        assert pinned == (5000, 90)
        assert execute_sql._resolve_row_cap() == 5000
        assert execute_sql._resolve_timeout_s() == 90

    assert execute_sql._resolve_row_cap() == execute_sql._DEFAULT_MAX_ROWS
    assert execute_sql._resolve_timeout_s() == execute_sql._DEFAULT_TIMEOUT_S


def test_two_organisations_in_sequence_do_not_leak_into_each_other():
    """One worker thread serves one organisation after another; a pin that outlived its call would
    hand the next organisation these limits."""
    tools.set_statement_limits_provider(_by_org)

    with tools.pinned_statement_limits("acme"):
        assert execute_sql._resolve_timeout_s() == 90
    assert execute_sql._statement_limits.get() is None

    with tools.pinned_statement_limits("globex"):
        assert (execute_sql._resolve_row_cap(), execute_sql._resolve_timeout_s()) == (20, 4)
    assert execute_sql._statement_limits.get() is None

    with tools.pinned_statement_limits("initech"):  # no row for this one
        assert execute_sql._resolve_timeout_s() == execute_sql._DEFAULT_TIMEOUT_S


def test_the_pin_is_released_when_the_call_raises():
    tools.set_statement_limits_provider(_by_org)

    with pytest.raises(RuntimeError), tools.pinned_statement_limits("acme"):
        raise RuntimeError("boom")

    assert execute_sql._statement_limits.get() is None


def test_tool_execute_sql_holds_the_callers_limits_for_the_whole_call(monkeypatch):
    tools.set_statement_limits_provider(_by_org)
    _in_org(monkeypatch, "globex")
    seen: dict = {}

    def _body(args):
        seen["limits"] = (execute_sql._resolve_row_cap(), execute_sql._resolve_timeout_s())
        return "{}"

    monkeypatch.setattr(tools, "_tool_execute_sql", _body)
    tools.tool_execute_sql({"sql": "SELECT 1"})

    assert seen["limits"] == (20, 4)
    assert execute_sql._statement_limits.get() is None


def test_the_provider_is_asked_once_per_call(monkeypatch):
    """Once, not once per reader: a provider whose answer changed mid-call must not be able to give
    the supervisor bound and the child environment different numbers."""
    calls: list[str] = []

    def _counting(org_id):
        calls.append(org_id)
        return {"max_rows": 5000 + len(calls), "timeout_s": 90 + len(calls)}

    tools.set_statement_limits_provider(_counting)
    _in_org(monkeypatch, "acme")
    seen: dict = {}

    def _body(args):
        first = tools._pass_child_env()
        seen["child"] = (first["AGAMI_SQL_MAX_ROWS"], first["AGAMI_SQL_TIMEOUT_S"])
        seen["supervisor"] = execute_sql._resolve_timeout_s() + execute_sql._SUPERVISOR_SKEW_S
        seen["reported"] = tools.statement_limits()
        return "{}"

    monkeypatch.setattr(tools, "_tool_execute_sql", _body)
    tools.tool_execute_sql({"sql": "SELECT 1"})

    assert calls == ["acme"]
    assert seen["child"] == ("5001", "91")
    assert seen["supervisor"] == 91 + execute_sql._SUPERVISOR_SKEW_S
    assert seen["reported"] == {"max_rows": 5001, "timeout_s": 91}


# ----------------------------------------------------------------------------------------------
# The fork: the child is handed, and really resolves, the same budget
# ----------------------------------------------------------------------------------------------


def test_the_fork_path_hands_the_child_the_organisations_budget(monkeypatch):
    tools.set_statement_limits_provider(_by_org)
    _in_org(monkeypatch, "acme")
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "30")
    monkeypatch.setattr(tools, "_INJECTED_EXECUTOR", None)
    captured: dict = {}

    class _Stop(Exception):
        pass

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        raise _Stop  # the budget is decided by now; nothing after the fork is under test

    monkeypatch.setattr(tools.subprocess, "run", _fake_run)
    monkeypatch.setattr(tools, "_resolve_call_datasource", lambda args: "demo")

    with pytest.raises(_Stop):
        tools.tool_execute_sql({"sql": "SELECT 1"})

    assert captured["env"]["AGAMI_SQL_MAX_ROWS"] == "5000"
    assert captured["env"]["AGAMI_SQL_TIMEOUT_S"] == "90"
    assert captured["timeout"] == 90 + execute_sql._SUPERVISOR_SKEW_S


def test_a_real_child_resolves_the_budget_the_parent_bounded():
    """Driven across the actual process boundary. The child has no provider and no pin — only the
    environment `_pass_child_env` built — and must reach the parent's number, or the supervisor
    stops waiting inside the budget the child is still enforcing."""
    tools.set_statement_limits_provider(_by_org)

    with tools.pinned_statement_limits("acme"):
        env = tools._pass_child_env()
        parent_bound = execute_sql._resolve_timeout_s() + execute_sql._SUPERVISOR_SKEW_S

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import execute_sql; print(execute_sql._resolve_row_cap(), execute_sql._resolve_timeout_s())",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={**env, "PYTHONPATH": str(PKG_SRC)},
    )
    assert child.returncode == 0, child.stderr
    child_rows, child_timeout = (int(x) for x in child.stdout.split())

    assert (child_rows, child_timeout) == (5000, 90)
    assert parent_bound > child_timeout


def test_without_a_provider_the_child_env_spells_the_deployment_values(monkeypatch):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "250")

    with tools.pinned_statement_limits("acme"):
        env = tools._pass_child_env()

    assert env["AGAMI_SQL_MAX_ROWS"] == "250"
    assert env["AGAMI_SQL_TIMEOUT_S"] == str(execute_sql._DEFAULT_TIMEOUT_S)


# ----------------------------------------------------------------------------------------------
# Fallbacks: never raise, fall back to the environment
# ----------------------------------------------------------------------------------------------


def test_no_provider_means_the_deployment_values(monkeypatch):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "300")
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "12")

    with tools.pinned_statement_limits("acme") as pinned:
        assert pinned == (300, 12)


@pytest.mark.parametrize(
    "answer",
    [None, {}, {"max_rows": None, "timeout_s": None}],
)
def test_an_organisation_without_its_own_limits_gets_the_deployment_values(monkeypatch, answer):
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "12")
    tools.set_statement_limits_provider(lambda org_id: answer)

    with tools.pinned_statement_limits("acme") as pinned:
        assert pinned == (execute_sql._DEFAULT_MAX_ROWS, 12)


def test_one_key_may_be_overridden_alone():
    tools.set_statement_limits_provider(lambda org_id: {"timeout_s": 600})

    assert tools.statement_limits("acme") == {
        "max_rows": execute_sql._DEFAULT_MAX_ROWS,
        "timeout_s": 600,
    }


@pytest.mark.parametrize("bad", [0, -1, "50", 2.5, True, [10]])
def test_an_unusable_value_falls_back_with_a_warning(monkeypatch, caplog, bad):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "300")
    tools.set_statement_limits_provider(lambda org_id: {"max_rows": bad, "timeout_s": 45})

    with caplog.at_level(logging.WARNING, logger=tools._LOG.name):
        limits = tools.statement_limits("acme")

    assert limits == {"max_rows": 300, "timeout_s": 45}  # the good key still applies
    assert any("max_rows" in r.getMessage() for r in caplog.records)


def test_there_is_no_ceiling():
    """The owner's decision: an administrator may set any positive number. Stated as a test so a
    clamp added later is a deliberate change rather than a quiet one."""
    tools.set_statement_limits_provider(
        lambda org_id: {"max_rows": 10_000_000, "timeout_s": 86_400}
    )

    assert tools.statement_limits("acme") == {"max_rows": 10_000_000, "timeout_s": 86_400}


def test_a_provider_that_raises_falls_back_and_does_not_fail_the_call(caplog):
    def _broken(org_id):
        raise ConnectionError("settings store unavailable")

    tools.set_statement_limits_provider(_broken)

    with caplog.at_level(logging.WARNING, logger=tools._LOG.name):
        with tools.pinned_statement_limits("acme") as pinned:
            assert pinned == (execute_sql._DEFAULT_MAX_ROWS, execute_sql._DEFAULT_TIMEOUT_S)

    assert any("failed" in r.getMessage() for r in caplog.records)


def test_a_provider_that_returns_a_non_mapping_falls_back(caplog):
    tools.set_statement_limits_provider(lambda org_id: (5000, 90))

    with caplog.at_level(logging.WARNING, logger=tools._LOG.name):
        assert tools.statement_limits("acme")["timeout_s"] == execute_sql._DEFAULT_TIMEOUT_S

    assert any("not a mapping" in r.getMessage() for r in caplog.records)


def test_a_non_callable_provider_is_refused_at_registration():
    with pytest.raises(TypeError):
        tools.set_statement_limits_provider({"max_rows": 10})  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------------------
# What is reported: effective limits, deployment defaults, recommended values
# ----------------------------------------------------------------------------------------------


def test_statement_limits_reports_the_current_organisations_limits(monkeypatch):
    tools.set_statement_limits_provider(_by_org)
    _in_org(monkeypatch, "acme")

    assert tools.statement_limits() == {"max_rows": 5000, "timeout_s": 90}
    assert tools.statement_limits("globex") == {"max_rows": 20, "timeout_s": 4}


def test_defaults_report_the_deployment_and_the_recommendation_separately(monkeypatch):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "2500")
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "45")
    tools.set_statement_limits_provider(_by_org)

    assert tools.statement_limit_defaults() == {
        "deployment": {"max_rows": 2500, "timeout_s": 45},
        "recommended": {"max_rows": 1000, "timeout_s": 30},
    }


# ----------------------------------------------------------------------------------------------
# What the client is told matches what is enforced for its organisation
# ----------------------------------------------------------------------------------------------


def test_the_description_states_the_callers_numbers(monkeypatch):
    tools.set_statement_limits_provider(_by_org)
    _in_org(monkeypatch, "acme")

    described = tools.tool_description("execute_sql", tools.TOOLS["execute_sql"]["description"])

    assert "5,000 rows" in described and "90s" in described
    assert tools._execute_sql_limits_sentence() in described
    assert "max_rows" not in described  # ACE-087: the removed argument is never advertised


def test_only_our_execute_sql_description_is_rewritten(monkeypatch):
    tools.set_statement_limits_provider(_by_org)
    _in_org(monkeypatch, "acme")
    other = tools.TOOLS["list_datasources"]["description"]

    assert tools.tool_description("list_datasources", other) == other
    assert tools.tool_description("execute_sql", "a consumer's own tool") == "a consumer's own tool"


def test_the_http_server_lists_the_callers_numbers(monkeypatch):
    pytest.importorskip("mcp")
    import mcp.types as mt
    import mcp_http

    tools.set_statement_limits_provider(_by_org)
    server = mcp_http.build_server()
    handler = server.request_handlers[mt.ListToolsRequest]

    async def _list_as(org_id: str) -> str:
        token = tools._current_org_ctx.set(org_id)
        try:
            result = await handler(mt.ListToolsRequest(method="tools/list"))
        finally:
            tools._current_org_ctx.reset(token)
        listed = {t.name: t.description for t in result.root.tools}
        return listed["execute_sql"]

    assert "5,000 rows" in asyncio.run(_list_as("acme"))
    assert "20 rows" in asyncio.run(_list_as("globex"))


def test_the_refusal_names_the_organisations_row_cap():
    """An existing session keeps the description it listed; the refusal re-resolves per call, so it
    is where a changed limit is met first."""
    tools.set_statement_limits_provider(_by_org)

    with tools.pinned_statement_limits("globex"):
        refusal = execute_sql._resource_limit_refusal(None)

    assert "20-row limit" in refusal.detail


def test_create_app_registers_the_adapters_provider(monkeypatch):
    pytest.importorskip("mcp")
    import dataclasses

    import mcp_http

    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
    adapters = dataclasses.replace(mcp_http.default_adapters(), statement_limits=_by_org)

    mcp_http.create_app(adapters=adapters)
    try:
        assert tools._STATEMENT_LIMITS_PROVIDER is _by_org
    finally:
        tools.set_injected_executor(None)

    mcp_http.create_app()
    assert tools._STATEMENT_LIMITS_PROVIDER is None
    assert os.environ.get("AGAMI_SQL_MAX_ROWS") is None  # registration touches no environment
