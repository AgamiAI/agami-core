"""Joins on natural keys: a column named exactly like another table's single-column key.

A live onboarding of a public schools database found 0 joins: every fact table carries `cds_code`
and `schools` is keyed on it, but the probe only tried `<thing>_id` columns whose prefix looks like a
table name. A same-named key match, confirmed by the value-overlap probe, is now proposed too.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import introspect as I  # noqa: E402
from semantic_model import validator as V  # noqa: E402

_COLS = {
    "schools": ["cds_code", "name"],
    "scores": ["cds_code", "test_id", "score"],
    "subjects": ["test_id", "label"],
    "school_profile": ["cds_code", "notes"],  # keyed on cds_code itself: a 1:1 extension
}
_PK = {"schools": "cds_code", "subjects": "test_id", "school_profile": "cds_code"}


def _runner(overlap: bool):
    def run(sql):
        s = " ".join(sql.split())
        if "information_schema.schemata" in s:
            return [{"schema_name": "public"}]
        if "information_schema.tables" in s and "table_type" in s:
            return [{"schema_name": "public", "table_name": t, "table_type": "BASE TABLE"} for t in _COLS]
        if "information_schema.columns" in s:
            table = next(t for t in _COLS if f"'{t}'" in s)
            return [{"column_name": c, "data_type": "text", "is_nullable": "YES",
                     "ordinal_position": str(i), "numeric_scale": ""}
                    for i, c in enumerate(_COLS[table], 1)]
        if "PRIMARY KEY" in s:
            table = next((t for t in _COLS if f"'{t}'" in s), None)
            return [{"column_name": _PK[table]}] if table in _PK else []
        if "FOREIGN KEY" in s:
            return []
        if "matched" in s:
            return [{"matched": "50" if overlap else "0"}]
        return []
    return run


def _joins(overlap: bool, tmp_path: Path) -> set[tuple[str, str, str, str]]:
    org, _ = I.introspect("edu", "postgres", runner=_runner(overlap), artifacts_dir=tmp_path, dry_run=True)
    assert V.validate(org).ok
    return {(r.from_table, r.from_column, r.to_table, r.to_column) for sa in org.subject_areas for r in sa.relationships}


def test_a_column_named_like_another_tables_key_is_proposed_as_a_join(tmp_path):
    joins = _joins(True, tmp_path)
    assert ("scores", "cds_code", "schools", "cds_code") in joins
    assert ("scores", "test_id", "subjects", "test_id") in joins


def test_a_tables_own_key_is_not_made_a_reference(tmp_path):
    joins = _joins(True, tmp_path)
    assert not any(f == "school_profile" and c == "cds_code" for f, c, _, _ in joins)
    assert not any(f == "schools" and c == "cds_code" for f, c, _, _ in joins)


def test_no_join_without_overlapping_values(tmp_path):
    assert _joins(False, tmp_path) == set()
