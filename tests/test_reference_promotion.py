"""Tier 2: name-based reference-field promotion.

A `<x>_id` column whose target table is identifiable by name becomes an
inferred/unreviewed join even without value-overlap (which under-detects on
sparse data). Conservative: target must exist, single-column grain, key-type match.
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


# No catalog FKs declared; `orders` references `dealers` via dealer_id, plus a
# mystery_id with no target table, and a string note_id (type-mismatch to dealers.id int).
def _runner(sql):
    s = " ".join(sql.split())
    if "information_schema.schemata" in s:
        return [{"schema_name": "public"}]
    if "information_schema.tables" in s and "table_type" in s:
        return [{"schema_name": "public", "table_name": "orders", "table_type": "BASE TABLE"},
                {"schema_name": "public", "table_name": "dealers", "table_type": "BASE TABLE"}]
    if "information_schema.columns" in s:
        if "'orders'" in s:
            return [
                {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                {"column_name": "dealer_id", "data_type": "integer", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""},
                {"column_name": "mystery_id", "data_type": "integer", "is_nullable": "YES", "ordinal_position": "3", "numeric_scale": ""},
            ]
        return [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "ordinal_position": "1", "numeric_scale": ""},
                {"column_name": "name", "data_type": "varchar", "is_nullable": "YES", "ordinal_position": "2", "numeric_scale": ""}]
    if "PRIMARY KEY" in s:
        return [{"column_name": "id"}]
    if "FOREIGN KEY" in s:
        return []                 # NO catalog FKs
    return []


def test_promotes_named_reference_to_inferred_join(tmp_path):
    org, rep = I.introspect("shop", "postgres", runner=_runner, artifacts_dir=tmp_path, dry_run=True)
    assert V.validate(org).ok
    rels = org.subject_areas[0].relationships
    # dealer_id → dealers.id was promoted...
    dealer = [r for r in rels if r.from_table == "orders" and r.from_column == "dealer_id"]
    assert len(dealer) == 1
    r = dealer[0]
    assert r.to_table == "dealers" and r.to_column == "id"
    assert r.confidence == "inferred" and r.review_state == "unreviewed"   # discoverable, not asserted
    # ...but mystery_id (no "mystery" table) was NOT
    assert not any(r.from_column == "mystery_id" for r in rels)


def test_existing_fk_not_duplicated(tmp_path):
    # when a catalog FK already covers the column, promotion must not add a second edge
    def runner_with_fk(sql):
        s = " ".join(sql.split())
        if "FOREIGN KEY" in s:
            return [{"from_table": "orders", "from_column": "dealer_id",
                     "to_table": "dealers", "to_column": "id",
                     "from_schema": "public", "to_schema": "public"}]
        return _runner(sql)
    org, _ = I.introspect("shop", "postgres", runner=runner_with_fk, artifacts_dir=tmp_path, dry_run=True)
    dealer = [r for r in org.subject_areas[0].relationships
              if r.from_table == "orders" and r.from_column == "dealer_id"]
    assert len(dealer) == 1   # the catalog FK only, no duplicate from promotion
