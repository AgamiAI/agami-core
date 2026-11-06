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


def _runner(overlap, cols=None, pk=None):
    """`overlap` is a bool (every probe matches, or none) or a {target table: matched} map."""
    cols = _COLS if cols is None else cols
    pk = _PK if pk is None else pk

    def run(sql):
        s = " ".join(sql.split())
        if "information_schema.schemata" in s:
            return [{"schema_name": "public"}]
        if "information_schema.tables" in s and "table_type" in s:
            return [
                {"schema_name": "public", "table_name": t, "table_type": "BASE TABLE"} for t in cols
            ]
        if "information_schema.columns" in s:
            table = next(t for t in cols if f"'{t}'" in s)
            return [
                {
                    "column_name": c,
                    "data_type": "text",
                    "is_nullable": "YES",
                    "ordinal_position": str(i),
                    "numeric_scale": "",
                }
                for i, c in enumerate(cols[table], 1)
            ]
        if "PRIMARY KEY" in s:
            table = next((t for t in cols if f"'{t}'" in s), None)
            return [{"column_name": pk[table]}] if table in pk else []
        if "FOREIGN KEY" in s:
            return []
        if "matched" in s:
            if isinstance(overlap, dict):
                target = s.rsplit("FROM", 1)[-1]
                hit = next(
                    (
                        n
                        for t, n in sorted(overlap.items(), key=lambda kv: -len(kv[0]))
                        if f'"{t}"' in target
                    ),
                    0,
                )
                return [{"matched": str(hit)}]
            return [{"matched": "50" if overlap else "0"}]
        return []

    return run


def _joins(
    overlap, tmp_path: Path, cols=None, pk=None, report=None
) -> set[tuple[str, str, str, str]]:
    org, rep = I.introspect(
        "edu", "postgres", runner=_runner(overlap, cols, pk), artifacts_dir=tmp_path, dry_run=True
    )
    assert V.validate(org).ok
    if report is not None:
        report.extend(rep.notes)
    return {
        (r.from_table, r.from_column, r.to_table, r.to_column)
        for sa in org.subject_areas
        for r in sa.relationships
    }


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


def test_an_extension_listed_first_does_not_take_the_join(tmp_path):
    # Catalog order used to decide: school_profile sorts before schools, and won the join.
    cols = {
        "school_profile": ["cds_code", "notes"],
        "schools": ["cds_code", "name"],
        "scores": ["cds_code", "score"],
    }
    pk = {"school_profile": "cds_code", "schools": "cds_code"}
    joins = _joins(True, tmp_path, cols, pk)
    assert ("scores", "cds_code", "schools", "cds_code") in joins
    assert not any(to == "school_profile" for _, _, to, _ in joins)


def test_a_table_named_like_the_stem_wins(tmp_path):
    cols = {
        "regions": ["region_code", "label"],
        "warehouses": ["region_code", "city"],
        "sales": ["region_code", "amount"],
    }
    pk = {"regions": "region_code", "warehouses": "region_code"}
    joins = _joins(True, tmp_path, cols, pk)
    assert ("sales", "region_code", "regions", "region_code") in joins
    assert ("sales", "region_code", "warehouses", "region_code") not in joins


def test_two_equally_good_targets_propose_nothing(tmp_path):
    cols = {
        "districts": ["cds_code", "label"],
        "schools": ["cds_code", "name"],
        "scores": ["cds_code", "score"],
    }
    pk = {"districts": "cds_code", "schools": "cds_code"}
    notes: list[str] = []
    joins = _joins(True, tmp_path, cols, pk, notes)
    assert not any(f == "scores" for f, _, _, _ in joins)
    assert any("scores.cds_code" in n and "equally well" in n for n in notes)


def test_the_target_sharing_more_values_wins(tmp_path):
    cols = {
        "districts": ["cds_code", "label"],
        "schools": ["cds_code", "name"],
        "scores": ["cds_code", "score"],
    }
    pk = {"districts": "cds_code", "schools": "cds_code"}
    joins = _joins({"schools": 48, "districts": 3}, tmp_path, cols, pk)
    assert ("scores", "cds_code", "schools", "cds_code") in joins
    assert ("scores", "cds_code", "districts", "cds_code") not in joins


def test_a_generic_key_name_yields_no_join(tmp_path):
    cols = {"regions": ["code", "label"], "stores": ["store_key", "code"]}
    pk = {"regions": "code", "stores": "store_key"}
    assert _joins(True, tmp_path, cols, pk) == set()


def test_a_single_shared_value_is_not_a_join(tmp_path):
    assert _joins({"schools": 1, "subjects": 1}, tmp_path) == set()
