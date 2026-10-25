"""A column-group rename carries the ambiguity anchors that name the group (#487).

#477 let an ambiguity anchor to `table.group`, so its full entry arrives only with a reply that opens
that group. Renaming the group through curate then left the anchor naming a group that no longer
exists: validation failed with `ambiguity_unknown_anchor` and the whole edit rolled back, so the
rename could not be made at all. A rename (a new group with exactly the old group's columns) now moves
the anchor with it; anything else is left for validation to reject, naming the ambiguity.
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

from catalog_helpers import col, make_catalog_runner  # noqa: E402

NAMES = [f"c{i:02d}" for i in range(30)]
GROUPS = {"keys": ["id"], "dates": NAMES[:10], "rest": NAMES[10:]}


def _edit_groups(root, area, groups):
    return C.apply(
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


def _anchors(root):
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    return [a.get("column_groups") for a in ds.get("ambiguities", [])]


def _model(tmp_path, anchors):
    cols = [col("id", "integer", nullable=False)] + [col(n, "varchar") for n in NAMES]
    run = make_catalog_runner(tables=["wide"], columns={"wide": cols}, estimate="10")
    org, _ = I.introspect("shop", "postgres", runner=run, artifacts_dir=tmp_path)
    root = tmp_path / "shop"
    owner = org.subject_areas[0].name
    res = _edit_groups(root, owner, GROUPS)
    assert not res.errors and not res.skipped, (res.errors, res.skipped)
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["ambiguities"] = [
        {
            "name": "which_date",
            "when": "a question about dates without saying which one",
            "options": [
                {"label": "Created", "how": "use c00"},
                {"label": "Closed", "how": "use c01"},
            ],
            "column_groups": anchors,
        }
    ]
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds, sort_keys=False))
    return root, owner


def test_a_renamed_group_takes_its_anchor_with_it(tmp_path):
    root, owner = _model(tmp_path, ["wide.dates"])
    res = _edit_groups(root, owner, {"keys": ["id"], "calendar": NAMES[:10], "rest": NAMES[10:]})
    assert not res.errors and not res.skipped, (res.errors, res.skipped)
    assert _anchors(root) == [["wide.calendar"]]


def test_a_split_group_is_left_for_validation_to_name(tmp_path):
    # Which of two new groups should open the entry is a person's call, so the anchor is not guessed.
    root, owner = _model(tmp_path, ["wide.dates"])
    res = _edit_groups(
        root, owner, {"keys": ["id"], "early": NAMES[:5], "late": NAMES[5:10], "rest": NAMES[10:]}
    )
    assert res.errors, "the edit must roll back"
    assert "which_date" in str(res.errors)
    assert _anchors(root) == [["wide.dates"]]


def test_an_anchor_on_another_schema_is_left_alone(tmp_path):
    # `archive.wide` is a different table from the `wide` being regrouped, so its anchor stays.
    root = tmp_path
    anchors = ["wide.dates", "public.wide.dates", "archive.wide.dates"]
    (root / "datasource.yaml").write_text(
        yaml.safe_dump({"ambiguities": [{"name": "which_date", "column_groups": list(anchors)}]})
    )
    C._reconcile_ambiguity_groups(
        root,
        "wide",
        "public",
        {"dates": NAMES[:10]},
        {"calendar": NAMES[:10]},
        {},
    )
    assert _anchors(root) == [["wide.calendar", "public.wide.calendar", "archive.wide.dates"]]


def test_a_qualified_anchor_follows_a_table_stored_without_a_schema(tmp_path):
    # The validator accepts `public.wide.dates` for a `wide` stored with no schema, so the rename must
    # carry it too or the edit still rolls back.
    (tmp_path / "datasource.yaml").write_text(
        yaml.safe_dump(
            {"ambiguities": [{"name": "which_date", "column_groups": ["public.wide.dates"]}]}
        )
    )
    C._reconcile_ambiguity_groups(
        tmp_path, "wide", None, {"dates": NAMES[:10]}, {"calendar": NAMES[:10]}, {}
    )
    assert _anchors(tmp_path) == [["public.wide.calendar"]]
