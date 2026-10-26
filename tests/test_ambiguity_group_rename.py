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


def _defs(root, *tables, unreferenced=()):
    """Table files on disk as (name, schema) pairs, in areas datasource.yaml lists (and, for
    `unreferenced`, in a directory it does not); the reconcile resolves anchors against them."""
    listed = []
    for n, (name, schema) in enumerate([*tables, *unreferenced]):
        d = root / "subject_areas" / f"a{n}" / "tables"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name.split('.')[-1]}.yaml").write_text(
            yaml.safe_dump({"name": name, **({"schema": schema} if schema else {})})
        )
        if n < len(tables):
            listed.append(f"subject_areas/a{n}")
    root.joinpath("areas.txt").write_text("\n".join(listed))


def _reconcile(root, anchors, table, schema):
    areas = (root / "areas.txt").read_text().split()
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "subject_areas": areas,
                "ambiguities": [{"name": "which_date", "column_groups": list(anchors)}],
            }
        )
    )
    edited = root / areas[0] / "tables" / f"{table.split('.')[-1]}.yaml"
    C._reconcile_ambiguity_groups(
        root, edited, table, schema, {"dates": NAMES[:10]}, {"calendar": NAMES[:10]}, {}
    )
    return _anchors(root)[0]


def test_an_anchor_on_another_schema_is_left_alone(tmp_path):
    _defs(tmp_path, ("wide", "public"), ("wide", "archive"))
    got = _reconcile(
        tmp_path, ["public.wide.dates", "archive.wide.dates", "wide.dates"], "wide", "public"
    )
    # `wide.dates` is ambiguous between the two schemas (the validator rejects it), so it stays too.
    assert got == ["public.wide.calendar", "archive.wide.dates", "wide.dates"]


def test_a_qualified_anchor_follows_a_table_stored_without_a_schema(tmp_path):
    # The validator falls back to the schema-less table when no exact qualified one exists.
    _defs(tmp_path, ("wide", None))
    assert _reconcile(tmp_path, ["public.wide.dates"], "wide", None) == ["public.wide.calendar"]


def test_an_exact_qualified_table_wins_over_the_schema_less_one(tmp_path):
    # With `wide` and `archive.wide` both defined, `archive.wide.dates` is the archive table's anchor.
    _defs(tmp_path, ("wide", None), ("wide", "archive"))
    assert _reconcile(tmp_path, ["archive.wide.dates"], "wide", None) == ["archive.wide.dates"]


def test_a_table_stored_with_its_schema_in_the_name_is_matched(tmp_path):
    _defs(tmp_path, ("public.wide", None))
    got = _reconcile(tmp_path, ["wide.dates", "catalog.public.wide.dates"], "public.wide", None)
    assert got == ["wide.calendar", "catalog.public.wide.calendar"]


def test_a_directory_the_model_does_not_list_is_not_a_definition(tmp_path):
    # An unreferenced `archive.wide` is not served, so `wide.dates` is not ambiguous.
    _defs(tmp_path, ("wide", "public"), unreferenced=[("wide", "archive")])
    assert _reconcile(tmp_path, ["wide.dates"], "wide", "public") == ["wide.calendar"]


def test_another_areas_copy_that_keeps_the_group_keeps_its_anchor(tmp_path):
    # Two areas each define their own copy of `wide`; only the first copy's group is renamed.
    _defs(tmp_path, ("wide", "public"), ("wide", "public"))
    second = tmp_path / "subject_areas" / "a1" / "tables" / "wide.yaml"
    second.write_text(
        yaml.safe_dump({"name": "wide", "schema": "public", "column_groups": {"dates": NAMES[:10]}})
    )
    assert _reconcile(tmp_path, ["wide.dates"], "wide", "public") == ["wide.dates", "wide.calendar"]


def test_a_rename_through_apply_on_a_file_named_with_its_schema(tmp_path):
    """The op names the table bare (`wide`) while its file declares `public.wide`: the anchor must
    still move, end to end through `apply`."""
    root, owner = _model(tmp_path, ["wide.dates"])
    path = root / "subject_areas" / owner / "tables" / "wide.yaml"
    doc = yaml.safe_load(path.read_text())
    schema = doc.pop("schema", None) or "public"
    doc["name"] = f"{schema}.wide"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    sa_path = root / "subject_areas" / owner / "subject_area.yaml"
    sa = yaml.safe_load(sa_path.read_text())
    for ref in sa["tables"]:
        if ref.get("table") == "wide":
            ref["table"] = f"{schema}.wide"
            ref.pop("schema", None)
    sa_path.write_text(yaml.safe_dump(sa, sort_keys=False))
    res = _edit_groups(root, owner, {"keys": ["id"], "calendar": NAMES[:10], "rest": NAMES[10:]})
    assert not res.errors, res.errors
    assert _anchors(root) == [["wide.calendar"]]
