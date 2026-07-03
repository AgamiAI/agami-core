"""Tests for the discover → prune first pass of agami-connect.

`introspect.discover_inventory` must be CHEAP — it lists tables + columns and
nothing else (no grain count-distincts, no FK-overlap probes, no row-count
scans). `render_prune` turns that inventory into the standalone prune HTML.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import render_prune  # noqa: E402
from semantic_model import cli  # noqa: E402
from semantic_model import introspect as I  # noqa: E402

PLUGIN_DIR = REPO_ROOT / "plugins" / "agami"

# --- canned runners ---------------------------------------------------------

SCHEMATA = [{"schema_name": "public"}]
TABLES = [
    {"schema_name": "public", "table_name": "customers", "table_type": "BASE TABLE"},
    {"schema_name": "public", "table_name": "orders", "table_type": "BASE TABLE"},
]
BULK_COLS = [
    {"table_schema": "public", "table_name": "customers", "column_name": "id",
     "data_type": "integer", "numeric_scale": "", "ordinal_position": "1"},
    {"table_schema": "public", "table_name": "customers", "column_name": "email",
     "data_type": "varchar", "numeric_scale": "", "ordinal_position": "2"},
    {"table_schema": "public", "table_name": "orders", "column_name": "id",
     "data_type": "integer", "numeric_scale": "", "ordinal_position": "1"},
    {"table_schema": "public", "table_name": "orders", "column_name": "total",
     "data_type": "numeric", "numeric_scale": "2", "ordinal_position": "2"},
]


def _bulk_runner(calls):
    def run(sql):
        s = " ".join(sql.split())
        calls.append(s)
        if "information_schema.schemata" in s:
            return list(SCHEMATA)
        if "information_schema.tables" in s and "table_type" in s:
            return list(TABLES)
        if "information_schema.columns" in s and "IN (" in s:   # the bulk query
            return list(BULK_COLS)
        return []
    return run


def _per_table_runner(calls):
    def run(sql):
        s = " ".join(sql.split())
        calls.append(s)
        if "information_schema.schemata" in s:
            return list(SCHEMATA)
        if "information_schema.tables" in s and "table_type" in s:
            return list(TABLES)
        if "information_schema.columns" in s and "IN (" in s:
            return []   # pretend the DB rejected the bulk read → force per-table fallback
        if "information_schema.columns" in s and "table_name =" in s:
            if "'customers'" in s:
                return [{"column_name": "id", "data_type": "integer", "numeric_scale": "", "ordinal_position": "1"}]
            return [{"column_name": "id", "data_type": "integer", "numeric_scale": "", "ordinal_position": "1"},
                    {"column_name": "total", "data_type": "numeric", "numeric_scale": "2", "ordinal_position": "2"}]
        return []
    return run


# --- discover_inventory -----------------------------------------------------

# Markers of the EXPENSIVE work the discover pass must never trigger.
_FORBIDDEN = ("PRIMARY KEY", "FOREIGN KEY", "reltuples", "COUNT(DISTINCT", "COUNT(*)")


def test_discover_is_cheap_no_grain_fk_or_count():
    calls = []
    inv = I.discover_inventory("main", "postgres", runner=_bulk_runner(calls))
    assert inv["table_count"] == 2
    for sql in calls:
        for marker in _FORBIDDEN:
            assert marker not in sql, f"discover issued an expensive query: {sql!r}"


def test_discover_bulk_columns_single_round_trip():
    calls = []
    inv = I.discover_inventory("main", "postgres", runner=_bulk_runner(calls))
    assert inv["column_mode"] == "catalog-bulk"
    # exactly one columns query (the bulk one) — not one per table
    col_queries = [s for s in calls if "information_schema.columns" in s]
    assert len(col_queries) == 1
    by = {t["table"]: [c["name"] for c in t["columns"]] for t in inv["tables"]}
    assert by["customers"] == ["id", "email"]
    assert by["orders"] == ["id", "total"]


def test_discover_falls_back_to_per_table_when_bulk_empty():
    calls = []
    inv = I.discover_inventory("main", "postgres", runner=_per_table_runner(calls))
    assert inv["column_mode"] in ("catalog", "per-table")
    by = {t["table"]: [c["name"] for c in t["columns"]] for t in inv["tables"]}
    assert by["customers"] == ["id"]
    assert by["orders"] == ["id", "total"]
    # still cheap
    for sql in calls:
        for marker in _FORBIDDEN:
            assert marker not in sql


def test_discover_raises_when_no_tables():
    with pytest.raises(RuntimeError):
        I.discover_inventory("main", "postgres", runner=lambda sql: [])


# --- render_prune -----------------------------------------------------------


def _inventory():
    return {
        "profile": "main", "db_type": "postgres", "schemas": ["public", "billing"],
        "table_count": 3, "column_mode": "catalog-bulk",
        "tables": [
            {"schema": "public", "table": "customers",
             "columns": [{"name": "id", "type": "integer"}, {"name": "email", "type": "text"}]},
            {"schema": "billing", "table": "invoices",
             "columns": [{"name": "id", "type": "integer"}]},
            {"schema": "public", "table": "orders",
             "columns": [{"name": "id", "type": "integer"}, {"name": "total", "type": "decimal"}]},
        ],
    }


def test_build_manifest_groups_by_schema():
    man = render_prune.build_manifest(_inventory())
    names = [s["name"] for s in man["schemas"]]
    assert names == ["public", "billing"]   # engine schema order preserved
    public = next(s for s in man["schemas"] if s["name"] == "public")
    assert [t["table"] for t in public["tables"]] == ["customers", "orders"]
    assert man["totals"] == {"tables": 3, "columns": 5}


def test_render_writes_prunable_html(tmp_path):
    inv_path = tmp_path / "inventory.json"
    inv_path.write_text(json.dumps(_inventory()))
    out = tmp_path / "prune.html"
    rc = render_prune.main(["--inventory", str(inv_path), "--out", str(out)])
    assert rc == 0
    html = out.read_text()
    # the manifest is embedded and the table names are present
    assert "const MANIFEST =" in html
    assert "customers" in html and "invoices" in html
    # it's the prune page — title + the prune call-to-action are present
    assert "Prune tables" in html
    assert "Generate for Claude" in html
    assert "Uncheck any table you don't need" in html
    # shares the explorer's chrome: logo + theme tokens are injected
    assert "dashboard-header" in html and "--accent" in html
    # {{...}} tokens all substituted
    assert "{{" not in html


# --- _load_render_prune: the AGAMI_PLUGIN_ROOT locator --------------------------
# render_prune lives in the plugin, not the installed package. The production caller (`sm discover` →
# `python -m semantic_model.cli`) can only find it via AGAMI_PLUGIN_ROOT — a package-relative import
# ModuleNotFound'd on every real install. These drive that locator (the DB-driven cmd_discover doesn't).


def _force_relocate(monkeypatch):
    """Undo this module's top-level `render_prune` preload so `_load_render_prune` must genuinely locate
    + insert + import it — otherwise the test would pass even if the locator no-op'd (it wouldn't need to
    do anything, since `render_prune` is already importable)."""
    monkeypatch.delitem(sys.modules, "render_prune", raising=False)
    scripts = str(PLUGIN_DIR / "scripts")
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != scripts])


def test_load_render_prune_via_plugin_root(monkeypatch):
    _force_relocate(monkeypatch)
    monkeypatch.setenv("AGAMI_PLUGIN_ROOT", str(PLUGIN_DIR))
    mod = cli._load_render_prune()
    assert hasattr(mod, "build_manifest") and hasattr(mod, "render")


def test_load_render_prune_raises_without_plugin_root(monkeypatch):
    # No AGAMI_PLUGIN_ROOT → a clear, named error (not a bare ImportError).
    _force_relocate(monkeypatch)
    monkeypatch.delenv("AGAMI_PLUGIN_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="AGAMI_PLUGIN_ROOT"):
        cli._load_render_prune()
