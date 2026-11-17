"""What the AI client reads says WHETHER a definition is approved, never WHO approved it.

The model keeps its sign-off trail — `signed_off_by` / `signed_off_role` / `signed_off_at` stay in
the model files, in curation, and on the model explorer page the model owner reads. None of it
rides in anything sent to the client: the `execute_sql` receipt (and the copy of it the audit trail
stores, which is the same receipt), the schema reply, the prompt examples, or the `sm context` /
`sm bundle` payload a skill reads. `review_state` stays everywhere it was, because "approved" is the
fact an answer needs; the person behind it is not.

Every assertion here is a scan of the whole payload, by key and by value, rather than a lookup of
the three keys where they used to be: a signer that resurfaced under a new key, or inside a nested
object, is the regression this file exists to catch.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

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
from semantic_model import loader as L  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

SIGNER = "signer-probe@example.com"
ROLE = "probe_role_owner"
SIGNED_AT = "2026-01-02T03:04:05Z"
SIGNED_OFF = {
    "review_state": "approved",
    "signed_off_by": SIGNER,
    "signed_off_role": ROLE,
    "signed_off_at": SIGNED_AT,
}
SIGNER_KEYS = {"signed_off_by", "signed_off_role", "signed_off_at"}

# Matches the approved metric's binding AND writes the approved, declared join.
STATEMENT = (
    "SELECT c.id, SUM(o.amount) FROM orders o JOIN customers c "
    "ON o.customer_id = c.id GROUP BY c.id"
)


@pytest.fixture(autouse=True)
def _isolate():
    tools.set_injected_executor(None)
    yield
    tools.set_injected_executor(None)


def _write_model(root: Path) -> None:
    """One area: `orders` -> `customers`, an approved join, an approved metric, an approved entity —
    every object the model signs off, each signed by the same probe identity."""
    yaml = __import__("yaml")
    area = root / "subject_areas" / "s"
    for sub in ("tables", "metrics", "entities"):
        (area / sub).mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "p",
                "version": 1,
                "storage_connections": [{"name": "c", "storage_type": "SQLite"}],
                "subject_areas": ["subject_areas/s"],
            }
        )
    )
    (area / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "s",
                "tables": [
                    {"storage_connection": "c", "schema": "main", "table": t}
                    for t in ("orders", "customers")
                ],
            }
        )
    )
    for name, cols in (
        (
            "orders",
            [
                {"name": "id", "type": "integer", "primary_key": True},
                {"name": "customer_id", "type": "integer"},
                {"name": "amount", "type": "decimal"},
            ],
        ),
        ("customers", [{"name": "id", "type": "integer", "primary_key": True}]),
    ):
        (area / "tables" / f"{name}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": name,
                    "schema": "main",
                    "storage_connection": "c",
                    "grain": ["id"],
                    "description": name,
                    "columns": cols,
                }
            )
        )
    (area / "relationships.yaml").write_text(
        yaml.safe_dump(
            {
                "relationships": [
                    {
                        "from_table": "orders",
                        "from_column": "customer_id",
                        "to_table": "customers",
                        "to_column": "id",
                        "from_schema": "main",
                        "to_schema": "main",
                        "relationship": "many_to_one",
                        "confidence": "confirmed",
                        **SIGNED_OFF,
                    }
                ]
            }
        )
    )
    (area / "metrics" / "revenue.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "revenue",
                "description": "Total order amount.",
                "calculation": "Sum of order amounts.",
                "bindings": {"SQLite": "SUM(amount)"},
                "source_tables": ["orders"],
                "confidence": "confirmed",
                **SIGNED_OFF,
            }
        )
    )
    (area / "entities" / "customer.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "customer",
                "description": "A customer.",
                "maps_to": [{"table": "customers", "column": "id", "primary": True}],
                "primary_table": "customers",
                "confidence": "confirmed",
                **SIGNED_OFF,
            }
        )
    )


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    (artifacts / "p").mkdir(parents=True)
    _write_model(artifacts / "p")
    db = tmp_path / "w.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER, amount REAL)")
    con.execute("CREATE TABLE customers (id INTEGER)")
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(1, 1, 5.0), (2, 1, 7.0)])
    con.execute("INSERT INTO customers VALUES (1)")
    con.commit()
    con.close()
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("DATASOURCE_URL__P", f"sqlite:///{db}")
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_ORG_ID", "AGAMI_SQL_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(tools, "QUERY_LOG", artifacts / "query_log.jsonl")
    return artifacts


def _signer_traces(payload: Any, path: str = "$") -> list[str]:
    """Every place in `payload` that names a signer: a signer key, or the probe identity as a value
    or inside a string."""
    found: list[str] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k in SIGNER_KEYS:
                found.append(f"{path}.{k}")
            found += _signer_traces(v, f"{path}.{k}")
    elif isinstance(payload, (list, tuple)):
        for i, v in enumerate(payload):
            found += _signer_traces(v, f"{path}[{i}]")
    elif isinstance(payload, str) and (SIGNER in payload or ROLE in payload):
        found.append(path)
    return found


def _assert_no_signer(raw: str) -> None:
    assert SIGNER not in raw and ROLE not in raw and SIGNED_AT not in raw, raw
    assert "signed_off" not in raw, raw
    assert _signer_traces(json.loads(raw)) == []


def test_the_receipt_says_approved_and_never_who(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    _write_model(root)
    receipt = rt.assemble_receipt(L.load_datasource(root), STATEMENT)

    (join,) = receipt["joins"]["items"]
    assert join["status"] == rt.DECLARED
    assert join["review_state"] == "approved"
    metrics = [i for i in receipt["columns"]["items"] if i.get("status") == "matched"]
    assert [m["name"] for m in metrics] == ["revenue"]
    assert metrics[0]["review_state"] == "approved"
    _assert_no_signer(json.dumps(receipt, default=str))


def test_execute_sql_body_and_the_stored_receipt_carry_no_signer(warehouse):
    """The client's body and the audit row hold the same receipt, so neither names a signer."""
    tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    raw = tools.tool_execute_sql({"sql": STATEMENT, "datasource": "p"})
    body = json.loads(raw)
    assert body["status"] == "ok", body
    assert body["receipt"]["joins"]["items"][0]["review_state"] == "approved"
    _assert_no_signer(raw)

    log = (warehouse / "query_log.jsonl").read_text()
    assert SIGNER not in log and "signed_off" not in log


@pytest.mark.parametrize(
    "args",
    [
        {"datasource": "p"},
        {"datasource": "p", "area": "s"},
        {"datasource": "p", "dataset_names": ["orders", "customers"]},
        {"datasource": "p", "dataset_names": ["orders"]},
        {"datasource": "p", "query": "revenue"},
    ],
    ids=["default", "area", "both-tables", "one-table", "query"],
)
def test_the_schema_reply_carries_no_signer(warehouse, args):
    raw = tools.tool_get_datasource_schema(args)
    assert "error" not in json.loads(raw), raw
    _assert_no_signer(raw)


def test_the_full_schema_still_says_the_join_is_approved(warehouse):
    body = json.loads(
        tools.tool_get_datasource_schema(
            {"datasource": "p", "dataset_names": ["orders", "customers"]}
        )
    )
    rels = body.get("relationships") or [
        r
        for t in body.get("tables", {}).values()
        if isinstance(t, dict)
        for r in t.get("relationships", [])
    ]
    assert any(r.get("review_state") == "approved" for r in rels), body


def test_prompt_examples_carry_no_signer(warehouse):
    _assert_no_signer(tools.tool_get_prompt_examples({"datasource": "p", "query": "revenue"}))


def test_the_skill_context_and_bundle_carry_no_signer(tmp_path):
    """`sm context` / `sm bundle` are what the local skills read in place of the schema tool."""
    root = tmp_path / "p"
    root.mkdir()
    _write_model(root)
    org = L.load_datasource(root)
    ctx = L.get_table_context(
        org, ["orders", "customers"], area="s", include=list(L.DEFAULT_BUNDLE_INCLUDE)
    )
    assert ctx["relationships"][0]["review_state"] == "approved"
    assert ctx["metrics"][0]["review_state"] == "approved"
    _assert_no_signer(json.dumps(ctx, default=str))
    bundle = L.get_subject_area_bundle(org, "s")
    assert bundle["entities"][0]["review_state"] == "approved"
    _assert_no_signer(json.dumps(bundle, default=str))


def test_the_model_itself_keeps_the_signer(tmp_path):
    """The trail is hidden from the client, not deleted: the loaded model still has it."""
    root = tmp_path / "p"
    root.mkdir()
    _write_model(root)
    org = L.load_datasource(root)
    sa = org.subject_area("s")
    assert sa.relationships[0].signed_off_by == SIGNER
    assert sa.metrics[0].signed_off_by == SIGNER
    assert sa.entities[0].signed_off_by == SIGNER
