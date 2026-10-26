"""Joins inferred from matching values wait for review; only a declared foreign key is engine-approved
(#509).

A database that declares no foreign keys gets its joins from column names and a value-overlap probe.
Those are proposals: written unreviewed, with the evidence in their description, and listed in the
review queue. A join backed by a declared (enforced) foreign key is still signed off by the engine.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import validator as V  # noqa: E402

_COLS = {
    "orders": [("id", "NO"), ("dealer_id", "YES"), ("region_id", "YES")],
    "dealers": [("id", "NO")],
    "regions": [("id", "NO")],
}


def _runner(declared_fks: list[dict]):
    def run(sql):
        s = " ".join(sql.split())
        if "information_schema.schemata" in s:
            return [{"schema_name": "public"}]
        if "information_schema.tables" in s and "table_type" in s:
            return [{"schema_name": "public", "table_name": t, "table_type": "BASE TABLE"} for t in _COLS]
        if "information_schema.columns" in s:
            table = next(t for t in _COLS if f"'{t}'" in s)
            return [{"column_name": c, "data_type": "integer", "is_nullable": n,
                     "ordinal_position": str(i), "numeric_scale": ""}
                    for i, (c, n) in enumerate(_COLS[table], 1)]
        if "PRIMARY KEY" in s:
            return [{"column_name": "id"}]
        if "FOREIGN KEY" in s:
            return declared_fks
        if "matched" in s:          # every sampled value is found in the target key
            return [{"matched": "50"}]
        return []
    return run


def _joins(declared_fks: list[dict], tmp_path: Path):
    org, _ = I.introspect("shop", "postgres", runner=_runner(declared_fks),
                          artifacts_dir=tmp_path, dry_run=True)
    assert V.validate(org).ok
    return org, {r.from_column: r for sa in org.subject_areas for r in sa.relationships}


def test_with_no_declared_foreign_keys_every_join_is_unreviewed_and_queued(tmp_path):
    org, joins = _joins([], tmp_path)
    assert set(joins) == {"dealer_id", "region_id"}
    for r in joins.values():
        assert r.review_state == "unreviewed" and r.confidence != "confirmed"
        assert r.signed_off_by is None and r.signed_off_role is None
        assert "value-overlap" in r.description          # the evidence it was inferred from
    queued = {it["from_column"] for it in curate.review_queue(org)["rule_2"] if it["kind"] == "relationship"}
    assert queued == {"dealer_id", "region_id"}


def test_a_declared_foreign_key_is_still_engine_approved(tmp_path):
    fk = {"from_table": "orders", "from_column": "dealer_id", "to_table": "dealers", "to_column": "id",
          "from_schema": "public", "to_schema": "public"}
    org, joins = _joins([fk], tmp_path)
    declared = joins["dealer_id"]
    assert (declared.review_state, declared.confidence) == ("approved", "confirmed")
    assert (declared.signed_off_by, declared.signed_off_role) == ("agami_introspect", "system")
    # the other join is still only inferred from its name, so it waits for review
    assert joins["region_id"].review_state == "unreviewed"
    queued = {it["from_column"] for it in curate.review_queue(org)["rule_2"] if it["kind"] == "relationship"}
    assert queued == {"region_id"}
