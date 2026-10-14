"""Regrouping a wide table carries every area's view of it over to the new groups (#465).

A table shown by several subject areas has a reference in each, and each may expose only some of
its column groups. A regroup renames the groups, so every reference has to move to the new names,
in every area, keeping the same columns visible. Before, only the owning area was updated: a
reference elsewhere kept the old names, validation failed, and the whole batch was rolled back.

Synthetic, generic names only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate as C  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model.loader import load_datasource  # noqa: E402

from catalog_helpers import col, make_catalog_runner  # noqa: E402

NAMES = [f"c{i:02d}" for i in range(30)]
SHOWN = sorted(["id"] + NAMES[:10])  # what the second area shows: group g1


def _edit_groups(root, area, groups):
    res = C.apply(
        root,
        [
            {
                "op": "edit",
                "kind": "table",
                "area": area,
                "name": "wide",
                "field": "column_groups",
                "value": groups,
            }
        ],
    )
    assert not res.errors and not res.skipped, (res.errors, res.skipped)


def _model(tmp_path):
    """A 31-column table in three groups, owned by one area and listed by a second area that shows
    only the first group."""
    cols = [col("id", "integer", nullable=False)] + [col(n, "varchar") for n in NAMES]
    run = make_catalog_runner(tables=["wide"], columns={"wide": cols}, estimate="10")
    org, _ = I.introspect("shop", "postgres", runner=run, artifacts_dir=tmp_path)
    root = tmp_path / "shop"
    owner = org.subject_areas[0].name
    table = org.subject_areas[0].defined_table("wide")
    _edit_groups(root, owner, {"g1": ["id"] + NAMES[:10], "g2": NAMES[10:20], "g3": NAMES[20:]})
    other = root / "subject_areas" / "other"
    (other / "tables").mkdir(parents=True)
    ref = {
        "storage_connection": table.storage_connection,
        "schema": table.schema_name,
        "table": "wide",
        "expose_column_groups": ["g1"],
    }
    (other / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {"name": "other", "description": "a second view of the wide table", "tables": [ref]}
        )
    )
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["subject_areas"].append("subject_areas/other")
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds, sort_keys=False))
    assert _exposed_columns(root, "other") == SHOWN  # valid, and showing g1, before the regroup
    return root, owner


def _exposed_columns(root, area):
    org = load_datasource(root)
    sa = next(a for a in org.subject_areas if a.name == area)
    table = next(t for a in org.subject_areas for t in a.tables_defined if t.name == "wide")
    groups = next(r for r in sa.tables if r.table == "wide").expose_column_groups
    return None if groups is None else sorted(c for g in groups for c in table.column_groups[g])


def test_a_regroup_moves_every_area_to_the_new_groups(tmp_path):
    root, owner = _model(tmp_path)
    _edit_groups(root, owner, {"keys": ["id"], "first": NAMES[:10], "rest": NAMES[10:]})
    assert _exposed_columns(root, "other") == SHOWN


def test_a_partly_overlapping_group_is_shown_rather_than_hidden(tmp_path):
    """A new group holding any column the area showed is exposed, so nothing it showed disappears."""
    root, owner = _model(tmp_path)
    _edit_groups(root, owner, {"a": ["id"] + NAMES[:5], "b": NAMES[5:15], "c": NAMES[15:]})
    after = _exposed_columns(root, "other")
    assert set(SHOWN) <= set(after) and "c29" not in after


def test_a_same_named_table_in_another_schema_is_left_alone(tmp_path):
    root, owner = _model(tmp_path)
    sap = root / "subject_areas" / "other" / "subject_area.yaml"
    sa = yaml.safe_load(sap.read_text())
    sa["tables"][0]["schema"] = "archive"  # now a reference to archive.wide, not this table
    sap.write_text(yaml.safe_dump(sa))
    C._reconcile_expose_groups(root, "wide", "public", {"g1": SHOWN}, {"x": SHOWN}, {})
    assert yaml.safe_load(sap.read_text())["tables"][0]["expose_column_groups"] == ["g1"]
