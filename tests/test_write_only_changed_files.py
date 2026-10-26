"""A refresh writes only the files whose content changed (#472).

`sm introspect --append --tables <one>` changed one table and re-dumped every file in the model: keys
reordered, empty defaults added, lines re-wrapped, no value different. A one-table refresh became a
whole-model diff, the real change was buried in it, and a model kept in git got a noisy commit every
time. `write_tree` now compares what it would write with what is on disk, as parsed data, and leaves
an equal file alone, so its line endings and formatting survive too.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

from semantic_model import build  # noqa: E402
from semantic_model import models as m  # noqa: E402


def _table(name: str, description: str = "d") -> m.Table:
    return m.Table(
        name=name,
        schema="public",
        storage_connection="c",
        grain=["id"],
        description=description,
        columns=[m.Column(name="id", type="integer", primary_key=True)],
    )


def _org(orders_description: str = "d") -> m.Datasource:
    return m.Datasource(
        datasource="crm",
        storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
        subject_areas=[
            m.SubjectArea(
                name="sales",
                description="Sales area",
                tables_defined=[_table("orders", orders_description), _table("customers")],
            )
        ],
    )


@pytest.fixture
def model(tmp_path, monkeypatch):
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    out = tmp_path / "crm"
    build.write_tree(_org(), out)
    return out


def _bytes(out):
    return {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*.yaml")}


def test_an_unchanged_model_writes_nothing(model):
    before = _bytes(model)
    report = build.write_tree(_org(), model)
    assert report.files_written == []
    assert "subject_areas/sales/tables/orders.yaml" in report.files_unchanged
    assert {k: v for k, v in _bytes(model).items() if not k.startswith(".")} == {
        k: v for k, v in before.items() if not k.startswith(".")
    }


def test_one_changed_table_rewrites_only_its_file(model):
    customers = model / "subject_areas/sales/tables/customers.yaml"
    before = customers.read_bytes()
    report = build.write_tree(_org(orders_description="the orders, one row per order"), model)
    assert report.files_written == ["subject_areas/sales/tables/orders.yaml"]
    assert customers.read_bytes() == before
    assert "one row per order" in (model / "subject_areas/sales/tables/orders.yaml").read_text()


def test_formatting_and_line_endings_of_an_equal_file_survive(model):
    # Written by someone else's editor: CRLF line endings, keys in another order, an empty default
    # left out. Same data, so a refresh must not touch it.
    customers = model / "subject_areas/sales/tables/customers.yaml"
    import yaml

    doc = yaml.safe_load(customers.read_text())
    doc = {k: doc[k] for k in reversed(list(doc)) if doc[k] != []}
    hand_written = yaml.safe_dump(doc, sort_keys=False).replace("\n", "\r\n").encode()
    customers.write_bytes(hand_written)

    report = build.write_tree(_org(), model)

    assert "subject_areas/sales/tables/customers.yaml" in report.files_unchanged
    assert customers.read_bytes() == hand_written


def test_a_file_that_does_not_parse_is_rewritten(model):
    customers = model / "subject_areas/sales/tables/customers.yaml"
    customers.write_text("name: [unclosed\n")
    report = build.write_tree(_org(), model)
    assert "subject_areas/sales/tables/customers.yaml" in report.files_written


def test_a_value_emptied_is_a_change(model):
    # Dropping empties applies to comparing two dumps of the same data, never to a real edit.
    assert build._meaningful({"caveats": ["x"]}) != build._meaningful({"caveats": []})
    assert build._meaningful({"caveats": [None]}) == {"caveats": [None]}


def test_an_empty_mapping_is_not_the_same_as_none():
    # `choice_field: {}` is a declared enum with no labels yet; no `choice_field` is not one.
    assert build._meaningful({"choice_field": {}}) != build._meaningful({"choice_field": None})


def test_a_dry_run_lists_only_what_would_change(model):
    report = build.write_tree(_org(orders_description="new"), model, dry_run=True)
    assert report.files_written == ["subject_areas/sales/tables/orders.yaml"]
