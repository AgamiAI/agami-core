"""What a client sees of the tool surface is unchanged by the SDK 2 upgrade (ACE-152).

The fixtures under `tests/fixtures/ace152/` were captured over HTTP on MCP SDK 1.x, before the pin
moved, and this file only ever reads them. Two claims ride on them:

- **The tool list** (criterion 10). For every registered tool, `name`, `description` and the
  serialized `inputSchema` equal the pre-upgrade capture, in three shapes of the list: the defaults,
  `AGAMI_REQUIRE_THREAD_ID` on, and a per-organisation statement-limits provider. Compared byte for
  byte on 2026-07-28. On 2025-06-18 SDK 2 re-serializes the result through the 2025 `InputSchema`
  model, which moves `type` after `properties`/`required` without changing a value, so there the
  comparison is of parsed JSON, where key order means nothing.
- **An `execute_sql` envelope and its audit row** (criterion 7), for an `ok`, a `refused` and a
  `failed` call. The envelope inside `content[0].text` is compared byte for byte; the outer result
  as parsed JSON, because SDK 2 orders `content[0]`'s keys differently and adds `resultType` on the
  modern path.

Volatile values (ids, timestamps, timings) are replaced by a placeholder on both sides before the
comparison, so the fixtures pin shape and content, not a particular run.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("jwt")
pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

import mcp_http  # noqa: E402
import tools  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

from mcp_eras import ERAS, MODERN, envelope, rpc  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ace152"
PUBLIC_BASE_URL = "https://demo.example.com"
PROFILE = "acme"
SECRET = "x" * 40

VARIANTS = ("defaults", "require_thread_id", "per_org_limits")

_VOLATILE = "<volatile>"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Pin every input a tool description or an envelope reads, so the capture is reproducible."""
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    for name in (
        "AGAMI_ORG_ID",
        "AGAMI_SIGNING_SECRET",
        "AGAMI_DB_URL",
        "APP_DATABASE_URL",
        "AGAMI_SQL_MAX_ROWS",
        "AGAMI_SQL_TIMEOUT_S",
        "AGAMI_REQUIRE_THREAD_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    # `create_app` installs both process-wide; a later test must not inherit this file's.
    tools.set_statement_limits_provider(None)
    tools.set_injected_executor(None)


# --- the tool list -------------------------------------------------------------------------------


def _limits(org_id: str) -> dict | None:
    return {"max_rows": 5000, "timeout_s": 90} if org_id == PROFILE else None


def _variant_app(variant: str, monkeypatch) -> object:
    # A named organisation, so the limits provider has one to answer for.
    monkeypatch.setenv("AGAMI_ORG_ID", PROFILE)
    adapters = None
    if variant == "require_thread_id":
        monkeypatch.setenv("AGAMI_REQUIRE_THREAD_ID", "1")
    elif variant == "per_org_limits":
        adapters = replace(mcp_http.default_adapters(), statement_limits=_limits)
    return mcp_http.create_app(adapters=adapters)


def served_tools(variant: str, era: str, monkeypatch) -> list[dict[str, str]]:
    """`tools/list` as a client receives it, one entry per tool.

    `inputSchema` is kept as its compact serialization in the order the wire carried it, which is
    what makes a key-order change visible to the byte comparison.
    """
    with TestClient(_variant_app(variant, monkeypatch), base_url=PUBLIC_BASE_URL) as client:
        response = rpc(client, era, "tools/list")
    assert response.status_code == 200, response.text
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": json.dumps(tool["inputSchema"], separators=(",", ":"), ensure_ascii=False),
        }
        for tool in envelope(response)["result"]["tools"]
    ]


@pytest.mark.parametrize("era", ERAS)
@pytest.mark.parametrize("variant", VARIANTS)
def test_tools_list_matches_pre_upgrade(variant, era, monkeypatch):
    expected = json.loads((FIXTURES / "tools_list_pre_sdk2.json").read_text())[variant]
    served = served_tools(variant, era, monkeypatch)
    if era == MODERN:
        assert served == expected
    else:
        assert [{**t, "inputSchema": json.loads(t["inputSchema"])} for t in served] == [
            {**t, "inputSchema": json.loads(t["inputSchema"])} for t in expected
        ]


# --- execute_sql envelopes and their audit rows --------------------------------------------------

# A statement per outcome. `returns` is declared in the model and absent from the warehouse, so it
# passes every gate and fails in the driver — a failure no gate can pre-empt.
CALLS = {
    "ok": "SELECT id FROM orders",
    "refused": "DELETE FROM orders",
    "failed": "SELECT id FROM returns",
}

# Row columns whose value differs on every run.
_VOLATILE_COLUMNS = {"id", "ts", "execution_ms", "audit_id", "correlation_id", "conversation_id"}
# Envelope keys whose value differs on every run, matched on the serialized text so the bytes
# between them are still compared.
_VOLATILE_KEYS = ("audit_id", "execution_ms", "elapsed_ms", "executed_at", "ts")


def _write_model(root: Path) -> None:
    import yaml

    area = root / "subject_areas" / "sales"
    (area / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "Shop",
                "version": 1,
                "storage_connections": [{"name": "c", "storage_type": "SQLite"}],
                "subject_areas": ["subject_areas/sales"],
            }
        )
    )
    (area / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "sales",
                "tables": [
                    {"storage_connection": "c", "schema": "public", "table": "orders"},
                    {"storage_connection": "c", "schema": "public", "table": "returns"},
                ],
            }
        )
    )
    for table in ("orders", "returns"):
        (area / "tables" / f"{table}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": table,
                    "schema": "public",
                    "storage_connection": "c",
                    "grain": ["id"],
                    "description": table,
                    "columns": [{"name": "id", "type": "integer", "primary_key": True}],
                }
            )
        )


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A served install: an audit store, a one-area model, and a warehouse holding `orders` only."""
    app_db = "sqlite://" + str(tmp_path / "app.db")
    store = Store.connect(app_db)
    store.run_migrations()
    store.close()
    _write_model(tmp_path / "artifacts" / PROFILE)
    warehouse = tmp_path / "warehouse.db"
    con = sqlite3.connect(warehouse)
    con.execute("CREATE TABLE orders (id INTEGER)")
    con.executemany("INSERT INTO orders (id) VALUES (?)", [(1,), (2,)])
    con.commit()
    con.close()
    monkeypatch.setenv("AGAMI_DB_URL", app_db)
    monkeypatch.setenv("DATASOURCE_URL__ACME", f"sqlite:///{warehouse}")
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    return app_db


def _scrub_text(text: str) -> str:
    for key in _VOLATILE_KEYS:
        text = re.sub(rf'("{key}": )("[^"]*"|-?\d+(\.\d+)?)', rf'\1"{_VOLATILE}"', text)
    return text


def execute_sql_outcomes(app_db: str, era: str) -> dict[str, dict]:
    """For each outcome: the result a client receives and the `tool_calls` row it wrote."""
    from oauth_server import issue_jwt

    bearer = issue_jwt("jordan@example.com")
    outcomes: dict[str, dict] = {}
    with TestClient(mcp_http.create_app(), base_url=PUBLIC_BASE_URL) as client:
        for rid, (outcome, sql) in enumerate(CALLS.items(), start=2):
            response = rpc(
                client,
                era,
                "tools/call",
                {"name": "execute_sql", "arguments": {"sql": sql, "datasource": PROFILE}},
                rid=rid,
                bearer=bearer,
            )
            assert response.status_code == 200, response.text
            result = envelope(response)["result"]
            result["content"][0]["text"] = _scrub_text(result["content"][0]["text"])
            outcomes[outcome] = {"result": result}
    store = Store.connect(app_db)
    try:
        rows = store.query("SELECT * FROM tool_calls WHERE tool_name = 'execute_sql' ORDER BY ts")
    finally:
        store.close()
    by_sql = {row["sql"]: row for row in rows}
    for outcome, sql in CALLS.items():
        row = dict(by_sql[sql])
        outcomes[outcome]["row"] = {
            k: (_VOLATILE if k in _VOLATILE_COLUMNS and v is not None else v) for k, v in row.items()
        }
    return outcomes


@pytest.mark.parametrize("era", ERAS)
def test_execute_sql_envelopes_match_pre_upgrade(served, era):
    expected = json.loads((FIXTURES / "execute_sql_envelopes_pre_sdk2.json").read_text())
    actual = execute_sql_outcomes(served, era)
    for outcome in CALLS:
        want, got = expected[outcome], actual[outcome]
        # The envelope a model reads, byte for byte.
        assert got["result"]["content"][0]["text"] == want["result"]["content"][0]["text"], outcome
        # The rest of the result as parsed JSON, less the one field SDK 2 adds on 2026-07-28.
        got_result = {k: v for k, v in got["result"].items() if k != "resultType"}
        assert got_result == want["result"], outcome
        # Only the columns the capture had: a column added since (ACE-152's own `error_detail`) is
        # additive, and its value is asserted where it is introduced.
        assert {k: got["row"][k] for k in want["row"]} == want["row"], outcome
