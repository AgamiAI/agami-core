"""`overview` counts, per subject area, the tables the area references from another area (with only the
column groups it exposes), but the datasource totals count each table once."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import curate  # noqa: E402

from test_multi_area_membership import _two_area_org  # noqa: E402


def _by_area(org, tmp_path) -> tuple[dict, dict]:
    out = curate.overview(org, tmp_path)
    return {a["area"]: a for a in out["areas"]}, out["totals"]


def test_an_area_counts_the_tables_it_references_but_totals_count_each_once(tmp_path):
    areas, totals = _by_area(_two_area_org(), tmp_path)
    assert (areas["incidents"]["tables"], areas["incidents"]["columns"]) == (2, 3)
    assert (areas["users"]["tables"], areas["users"]["columns"]) == (1, 2)
    assert (totals["tables"], totals["columns"]) == (2, 3)


def test_a_referenced_table_counts_only_the_column_groups_the_area_exposes(tmp_path):
    org = _two_area_org()
    org.subject_areas[0].tables_defined[0].column_groups = {"basic": ["id"], "contact": ["email"]}
    org.subject_areas[1].tables[1].expose_column_groups = ["basic"]
    areas, totals = _by_area(org, tmp_path)
    assert (areas["incidents"]["tables"], areas["incidents"]["columns"]) == (2, 2)
    assert (totals["tables"], totals["columns"]) == (2, 3)


def test_a_hidden_referenced_table_is_left_out(tmp_path):
    org = _two_area_org()
    org.subject_areas[0].tables_defined[0].review_state = "rejected"
    areas, _ = _by_area(org, tmp_path)
    assert areas["incidents"]["tables"] == 1
