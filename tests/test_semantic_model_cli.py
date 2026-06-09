"""Tests for the model CLI `prepare` command (tier-independent safety pass) and the
`sm` wrapper (interpreter resolution + running the CLI)."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from semantic_model import cli  # noqa: E402


def _model(root: Path) -> None:
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "org.yaml").write_text(yaml.safe_dump({
        "organization": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"},
            {"storage_connection": "c", "schema": "public", "table": "order_items"}]}))
    (root / "subject_areas" / "s" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "o", "default_filters": ["{alias}.deleted_at IS NULL"],
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "deleted_at", "type": "timestamp"},
                    {"name": "total", "type": "decimal"}]}))
    (root / "subject_areas" / "s" / "tables" / "order_items.yaml").write_text(yaml.safe_dump({
        "name": "order_items", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "oi",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "order_id", "type": "integer"}, {"name": "qty", "type": "integer"}]}))
    (root / "subject_areas" / "s" / "relationships.yaml").write_text(yaml.safe_dump({
        "relationships": [{"from_table": "order_items", "from_column": "order_id",
                           "to_table": "orders", "to_column": "id", "relationship": "many_to_one",
                           "review_state": "approved", "confidence": "confirmed",
                           "signed_off_by": "x", "signed_off_role": "system",
                           "signed_off_at": "t"}]}))


def _run(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(args)
    return rc, buf.getvalue()


def test_prepare_allow_applies_default_filters(tmp_path):
    _model(tmp_path)
    rc, out = _run(["prepare", str(tmp_path), "--area", "s", "--sql", "SELECT SUM(total) FROM orders"])
    d = json.loads(out)
    assert rc == 0 and d["action"] == "allow"
    assert d["applied_filters"] == ["orders.deleted_at IS NULL"]
    assert "deleted_at IS NULL" in d["sql"]


def test_prepare_auto_rewrites_fan_trap(tmp_path):
    _model(tmp_path)
    rc, out = _run(["prepare", str(tmp_path), "--area", "s", "--sql",
                    "SELECT SUM(orders.total) FROM orders JOIN order_items ON order_items.order_id=orders.id"])
    d = json.loads(out)
    assert rc == 0 and d["action"] == "auto_rewrite" and d["rewritten"] is True
    assert "order_items" not in d["sql"]              # fan-out join dropped
    assert "deleted_at IS NULL" in d["sql"]           # + default_filter applied


def test_prepare_refuses_shape_changing_trap(tmp_path):
    _model(tmp_path)
    rc, out = _run(["prepare", str(tmp_path), "--area", "s", "--sql",
                    "SELECT orders.id, orders.deleted_at, SUM(orders.total) FROM orders "
                    "JOIN order_items ON order_items.order_id=orders.id "
                    "GROUP BY orders.id, orders.deleted_at"])
    d = json.loads(out)
    assert rc == 1 and d["action"] == "refuse" and d["suggestion"]


# --- sm wrapper ---


def test_sm_wrapper_resolves_and_runs(tmp_path):
    """The wrapper runs the CLI through a resolved, deps-present interpreter even
    though bare `python3` on PATH may lack the model deps. We point it at the
    current test interpreter (which has them) via AGAMI_PYTHON."""
    _model(tmp_path)
    sm = SCRIPTS / "sm"
    proc = subprocess.run(
        ["bash", str(sm), "validate", str(tmp_path)],
        capture_output=True, text=True, env={**__import__("os").environ, "AGAMI_PYTHON": sys.executable},
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout or "error" not in proc.stdout.lower()
