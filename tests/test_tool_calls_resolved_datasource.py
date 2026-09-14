"""The activity log records the datasource a call ran against, not the argument it was sent (#328).

A call that omitted `datasource` ran against the datasource the server resolved and was logged with
an empty one, so any per-datasource reading of the log silently lost those rows. The row now carries
the resolved datasource and `datasource_source` says whether the client named it.
"""

from __future__ import annotations

import contextvars

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import tools  # noqa: E402
from semantic_model import build  # noqa: E402
from semantic_model.models import Datasource, SubjectArea  # noqa: E402
from store import Store  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = "sqlite://" + str(tmp_path / "calls.db")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    s = Store.connect(url)
    s.run_migrations()
    s.close()
    return url


def _rows(url):
    s = Store.connect(url)
    rows = s.query("SELECT * FROM tool_calls ORDER BY ts")
    s.close()
    return rows


def _record(arguments, **overrides):
    tools.record_tool_call(
        name="get_datasource_schema",
        arguments=arguments,
        result_text="{}",
        execution_ms=1,
        actor="you@example.com",
        **overrides,
    )


def test_a_resolved_datasource_is_recorded_as_resolved(db):
    _record({}, datasource="crm")

    (row,) = _rows(db)
    assert row["datasource"] == "crm"
    assert row["datasource_source"] == "resolved"


def test_a_named_datasource_is_recorded_as_explicit(db):
    _record({"datasource": "crm"}, datasource="crm")

    (row,) = _rows(db)
    assert (row["datasource"], row["datasource_source"]) == ("crm", "explicit")


def test_a_named_datasource_with_no_override_is_still_explicit(db):
    """An embedder that dispatches handlers itself and states nothing keeps today's behaviour."""
    _record({"datasource": "crm"})

    (row,) = _rows(db)
    assert (row["datasource"], row["datasource_source"]) == ("crm", "explicit")


def test_both_values_reach_the_activity_reader(db):
    """`_TOOL_CALL_COLS` is narrower than the INSERT, so a column missing from it is written on every
    row and read by nobody — the Activity view reads through `list_sessions`, not `SELECT *`."""
    import model_store

    _record({}, datasource="crm")
    _record({"datasource": "billing"}, datasource="billing")
    s = Store.connect(db)
    # Rows are stamped with the store's own tenant, not the reader's `local` default.
    (org_id,) = {r["org_id"] for r in s.query("SELECT org_id FROM tool_calls")}
    sessions = model_store.list_sessions(s, org_id=org_id)
    s.close()

    calls = [c for session in sessions for turn in session["turns"] for c in turn["calls"]]
    assert sorted((c["datasource"], c["datasource_source"]) for c in calls) == [
        ("billing", "explicit"),
        ("crm", "resolved"),
    ]


def test_a_tool_about_no_datasource_records_neither(db):
    tools.record_tool_call(
        name="list_datasources", arguments={}, result_text="{}", execution_ms=1, actor="a"
    )

    (row,) = _rows(db)
    assert not row["datasource"]
    assert row["datasource_source"] is None


@pytest.fixture
def local_model(tmp_path, monkeypatch):
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_ORG_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    # The fallback chain picks this when the call names nothing.
    monkeypatch.setenv("AGAMI_PROFILE", "crm")
    tools.resolved_org_id.cache_clear()
    build.write_tree(
        Datasource(datasource="crm", subject_areas=[SubjectArea(name="Sales", description="s")]),
        tmp_path / "crm",
    )
    tools.bootstrap_paths()


@pytest.mark.parametrize(
    "handler, arguments",
    [
        (tools.tool_get_datasource_schema, {}),
        (tools.tool_get_prompt_examples, {"query": "deals"}),
        (tools.tool_execute_sql, {"sql": "DELETE FROM t"}),  # refused before any database
    ],
)
def test_each_handler_publishes_what_it_resolved(local_model, handler, arguments):
    """Read back the way the served transport reads it: from a Context the caller owns, because a
    ContextVar set inside the worker's copy is invisible outside it."""
    ctx = contextvars.copy_context()
    ctx.run(tools.reset_typed_outcome)
    ctx.run(handler, arguments)

    assert tools.typed_outcome_overrides(ctx)["datasource"] == "crm"


def test_a_published_datasource_does_not_leak_into_the_next_tool(local_model):
    ctx = contextvars.copy_context()
    ctx.run(tools.tool_get_datasource_schema, {})
    ctx.run(tools.reset_typed_outcome)

    assert "datasource" not in tools.typed_outcome_overrides(ctx)
