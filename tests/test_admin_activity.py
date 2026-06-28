"""The admin activity views — Tool calls (audit-grade flat) + Sessions (best-effort grouping).

Covers the read helpers' grouping/degradation and the two rendered tabs (actor shown, SQL/question
escaped, no secret leak, admin-gated).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("argon2")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import mcp_http  # noqa: E402
import model_store  # noqa: E402
import user_store  # noqa: E402
from contracts import ToolCallRecord  # noqa: E402
from model_store import DbActivitySink  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

BASE = "https://your-host.example.com"
SECRET = "x" * 40
ADMIN_USER = "admin@example.com"
ADMIN_PW = "admin-password-localtest"


def _call(s, **kw):
    rec = {"ts": kw.pop("ts"), "tool_name": kw.pop("tool_name", "execute_sql"), "source": "mcp_server"}
    rec.update(kw)
    DbActivitySink(s).record_tool_call(ToolCallRecord(**rec))


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = "sqlite://" + str(tmp_path / "activity.db")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", ADMIN_PW)
    for v in ("AGAMI_OIDC_GOOGLE_CLIENT_ID", "AGAMI_OIDC_GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)
    s = Store.connect(url)
    s.run_migrations()
    user_store.seed_admin_from_env(s)
    s.close()
    return url


# --- read helpers ------------------------------------------------------------


def test_list_sessions_groups_by_thread_and_degrades(env):
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:39:00Z", actor="jordan@example.com", sql="A", success=True,
          thread_id="t1", user_question="q1", execution_ms=10)
    _call(s, ts="2026-06-27T10:42:00Z", actor="jordan@example.com", sql="B", success=False,
          thread_id="t1", execution_ms=20)
    _call(s, ts="2026-06-27T10:41:00Z", actor="sam@example.com", sql="C", success=True,
          thread_id="t2", execution_ms=30)
    _call(s, ts="2026-06-27T10:30:00Z", actor="morgan@example.com", sql="D", success=True,
          thread_id=None, execution_ms=40)  # no thread → its own singleton session
    _call(s, ts="2026-06-27T10:50:00Z", actor="pat@example.com", sql="E", success=True,
          thread_id="t3")  # no latency recorded → avg_ms must be None
    sessions = model_store.list_sessions(s)
    s.close()
    by_thread = {x["thread_id"]: x for x in sessions if x["thread_id"]}
    assert by_thread["t1"]["query_count"] == 2 and by_thread["t1"]["error_count"] == 1
    assert by_thread["t1"]["started"] == "2026-06-27T10:39:00Z"
    assert by_thread["t1"]["last_activity"] == "2026-06-27T10:42:00Z"
    assert by_thread["t1"]["avg_ms"] == 15
    assert by_thread["t3"]["avg_ms"] is None  # all-null latency degrades cleanly
    assert by_thread["t2"]["query_count"] == 1
    singletons = [x for x in sessions if x["thread_id"] is None]
    assert len(singletons) == 1 and singletons[0]["query_count"] == 1  # degraded → ungrouped


def test_list_sessions_does_not_blend_different_actors_on_a_colliding_thread(env):
    # thread_id is self-reported (untrusted); a collision across users must not merge/misattribute them.
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:00:00Z", actor="jordan@example.com", sql="A", success=True, thread_id="shared")
    _call(s, ts="2026-06-27T10:01:00Z", actor="sam@example.com", sql="B", success=True, thread_id="shared")
    sessions = model_store.list_sessions(s)
    s.close()
    assert len(sessions) == 2
    assert {x["actor"] for x in sessions} == {"jordan@example.com", "sam@example.com"}
    assert all(x["query_count"] == 1 for x in sessions)


def test_list_tool_calls_is_newest_first(env):
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:00:00Z", tool_name="list_datasources", actor="a", success=True)
    _call(s, ts="2026-06-27T11:00:00Z", tool_name="execute_sql", actor="b", sql="X", success=True)
    rows = model_store.list_tool_calls(s)
    s.close()
    assert [r["tool_name"] for r in rows] == ["execute_sql", "list_datasources"]


# --- the rendered tabs -------------------------------------------------------


@pytest.fixture
def client(env):
    return TestClient(mcp_http.build_app(), base_url=BASE)


def _login(c):
    c.post("/admin/login", data={"username": ADMIN_USER, "password": ADMIN_PW})


def test_calls_tab_shows_actor_and_detail(client, env):
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:42:00Z", actor="jordan@example.com", datasource="SALES_DATA",
          sql="SELECT region FROM orders", row_count=5, execution_ms=84, success=True,
          user_question="revenue by region?")
    s.close()
    _login(client)
    html = client.get("/admin?tab=calls").text
    assert "jordan@example.com" in html and "execute_sql" in html and "SALES_DATA" in html
    assert "SELECT region FROM orders" in html and "revenue by region?" in html  # in the drawer


def test_activity_views_escape_sql_and_question(client, env):
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:42:00Z", actor="x", sql="SELECT '<script>alert(1)</script>'",
          user_question="<img src=x onerror=alert(1)>", success=True, thread_id="t1")
    s.close()
    _login(client)
    for tab in ("calls", "sessions"):
        html = client.get(f"/admin?tab={tab}").text
        assert "<script>alert(1)" not in html and "<img src=x" not in html
        assert "&lt;script&gt;" in html


def test_sessions_tab_groups_and_handles_missing_question(client, env):
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:42:00Z", actor="jordan@example.com", sql="A", success=True,
          thread_id="t1", user_question="what is the revenue by region")
    _call(s, ts="2026-06-27T10:30:00Z", actor="sam@example.com", sql="B", success=True, thread_id=None)
    s.close()
    _login(client)
    html = client.get("/admin?tab=sessions").text
    assert "what is the revenue by region" in html  # the self-reported question
    assert "no question reported" in html  # graceful when Claude didn't supply one


def test_activity_requires_a_session(client):
    assert client.get("/admin?tab=calls", follow_redirects=False).status_code == 302
    assert client.get("/admin?tab=sessions", follow_redirects=False).status_code == 302


def test_activity_does_not_leak_the_password_hash(client, env):
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:42:00Z", actor=ADMIN_USER, sql="SELECT 1", success=True)
    s.close()
    _login(client)
    assert "password_hash" not in client.get("/admin?tab=calls").text


def test_activity_tabs_drop_the_redundant_helper_text(client, env):
    # The column headers + tab name already say what these are; the helper sentences were noise.
    _login(client)
    assert "Every tool call, newest first" not in client.get("/admin?tab=calls").text
    assert "Queries grouped into conversations" not in client.get("/admin?tab=sessions").text
