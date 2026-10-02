"""Every tool reply is compact JSON (#418).

The reader of a tool reply is a model, which gets nothing from indentation and pays for every
character of it — pretty-printing was ~25% of a schema reply's JSON and ~22% of an `execute_sql`
result. Nothing failed when it was there, so nothing would notice it coming back: these tests are
what does.

Each reply family is checked once, including the HTTP server's re-serialisation that stamps
`caller_identity` — that step parses every hosted reply and dumps it again, so it would undo a fix
made only in `tools`.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import execute_sql  # noqa: E402
import tools  # noqa: E402
from semantic_model import build  # noqa: E402
from semantic_model.models import Datasource, StorageConnection, SubjectArea  # noqa: E402


def assert_compact(reply: str) -> dict:
    """The JSON at the head of `reply` is exactly its compact serialisation.

    Stronger than "no newline": it fails on any separator other than `,` and `:`. Whatever follows
    the JSON (the schema reply's domain-context prose) is not JSON and is not checked here.
    """
    head, end = json.JSONDecoder().raw_decode(reply)
    assert reply[:end] == json.dumps(head, separators=(",", ":")), reply[:200]
    return head


@pytest.fixture
def local_model(tmp_path, monkeypatch):
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE", "AGAMI_ORG_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    tools.resolved_org_id.cache_clear()
    (tmp_path / "local").mkdir(parents=True, exist_ok=True)
    (tmp_path / "local" / "credentials").write_text("[crm]\nurl = postgresql://u:p@h/db\n")
    org = Datasource(
        datasource="crm",
        storage_connections=[StorageConnection(name="warehouse", storage_type="PostgreSQL")],
        subject_areas=[SubjectArea(name="Sales", description="Sales area")],
    )
    build.write_tree(org, tmp_path / "crm")
    tools.bootstrap_paths()


def test_list_datasources(local_model):
    assert assert_compact(tools.tool_list_datasources({}))["datasources"]


@pytest.mark.parametrize(
    "args",
    [
        {},  # the overview
        {"dataset_names": ["x"]},  # a table-scoped call
        {"area": "NoSuchArea"},  # an error reply
    ],
)
def test_get_datasource_schema(local_model, args):
    assert_compact(tools.tool_get_datasource_schema({"datasource": "crm", **args}))


def test_get_prompt_examples_error_reply(local_model):
    """Examples come back as Markdown when there are any; the JSON replies are the empty and error
    cases, and an unknown area is one of them."""
    assert_compact(tools.tool_get_prompt_examples({"datasource": "crm", "area": "NoSuchArea"}))


class _Rows:
    def execute(self, vetted_sql, creds, *, profile):
        return execute_sql.ExecResult(
            columns=["n", "label"], rows=[(1, "a"), (2, "b")], truncated=False
        )


class _Boom:
    def execute(self, vetted_sql, creds, *, profile):
        raise execute_sql.ExecutorError("SQLite execution error: no such column", code=5)


@pytest.fixture
def sql_edge(monkeypatch):
    tools.set_injected_executor(None)
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    yield
    tools.set_injected_executor(None)


@pytest.mark.parametrize(
    "executor,sql,status",
    [
        (_Rows(), "SELECT n, label FROM t", "ok"),
        (_Boom(), "SELECT nope FROM t", "failed"),
        (None, "DELETE FROM t", "refused"),
    ],
)
def test_execute_sql_every_status(sql_edge, executor, sql, status):
    if executor is not None:
        tools.set_injected_executor(executor)
    head = assert_compact(tools.tool_execute_sql({"sql": sql, "datasource": "acme"}))
    assert head["status"] == status


def test_no_reply_builder_pretty_prints():
    """The behavioural tests above cover each reply family once; this covers every call site.

    Most of the 19 sites are error branches — a missing driver, a table named under the wrong area,
    an ambiguous datasource — that no test reaches cheaply, and a new branch would be added the same
    way: by copying a neighbour. Every `json.dumps` in these two modules builds a reply, so any
    `indent=` in them is a reply that would go out pretty-printed.
    """
    from pathlib import Path

    for module in (tools, pytest.importorskip("mcp_http")):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "indent=" not in source, f"{Path(module.__file__).name} pretty-prints a reply (#418)"


def test_the_hosted_caller_identity_stamp_keeps_it_compact():
    mcp_http = pytest.importorskip("mcp_http")
    reply = (
        json.dumps({"status": "ok", "rows": [[1, "a"]]}, separators=(",", ":")) + "\n\nprose tail"
    )

    stamped = mcp_http._with_caller_identity(reply, "jordan@example.com")

    head = assert_compact(stamped)
    assert head["caller_identity"] == "jordan@example.com"
    assert stamped.endswith("\n\nprose tail")  # the non-JSON tail survives untouched
