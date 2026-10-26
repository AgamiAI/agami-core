"""A join rejected at sign-off is never served to an agent (#543).

Rejected joins inside one subject area were already dropped on load, but the cross-area loaders
never applied the filter: a rejected join between two areas, and rejected cross-area entities and
metrics, reached `sm context`, the schema reply and a deploy as if they were usable. A rejected join
is often the single-column piece of a composite key, which repeats rows.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

from semantic_model import build, loader  # noqa: E402
from semantic_model import models as m  # noqa: E402


def _table(name: str) -> m.Table:
    return m.Table(
        name=name,
        schema="public",
        storage_connection="c",
        grain=["id"],
        description=name,
        columns=[
            m.Column(name="id", type="integer", primary_key=True),
            m.Column(name="other_id", type="integer"),
        ],
    )


def _join(frm: str, to: str, state: str, **cross) -> dict:
    return dict(
        from_table=frm,
        to_table=to,
        from_column="other_id",
        to_column="id",
        from_schema="public",
        to_schema="public",
        relationship="many_to_one",
        review_state=state,
        **cross,
    )


@pytest.fixture
def model(tmp_path, monkeypatch):
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    org = m.Datasource(
        datasource="shop",
        storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
        subject_areas=[
            m.SubjectArea(
                name="sales",
                tables_defined=[_table("orders"), _table("lines")],
                relationships=[m.Relationship(**_join("lines", "orders", "rejected"))],
            ),
            m.SubjectArea(name="people", tables_defined=[_table("customers")]),
        ],
        cross_subject_area_relationships=[
            m.CrossSubjectAreaRelationship(
                **_join(
                    "orders",
                    "customers",
                    "rejected",
                    from_subject_area="sales",
                    to_subject_area="people",
                )
            ),
        ],
    )
    out = tmp_path / "shop"
    build.write_tree(org, out)
    return out


def _joins(org) -> list[tuple[str, str]]:
    inner = [(r.from_table, r.to_table) for sa in org.subject_areas for r in sa.relationships]
    cross = [(r.from_table, r.to_table) for r in org.cross_subject_area_relationships]
    return inner + cross


def test_no_rejected_join_is_loaded_for_serving(model):
    assert _joins(loader.load_datasource(model)) == []


def test_the_curation_view_still_sees_them(model):
    # A person deciding whether to restore a join has to see it.
    assert sorted(_joins(loader.load_datasource(model, include_rejected=True))) == [
        ("lines", "orders"),
        ("orders", "customers"),
    ]


@pytest.mark.parametrize("tables", [["orders"], ["orders", "customers"]])
def test_table_context_never_lists_a_rejected_join(model, tables):
    out = loader.get_table_context(loader.load_datasource(model), tables)
    assert "rejected" not in str(out)


def test_rejected_cross_area_entities_and_metrics_are_not_served(tmp_path, monkeypatch):
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    out = tmp_path / "shop"
    build.write_tree(
        m.Datasource(
            datasource="shop",
            storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
            subject_areas=[m.SubjectArea(name="sales", tables_defined=[_table("orders")])],
        ),
        out,
    )
    import yaml

    ds = yaml.safe_load((out / "datasource.yaml").read_text())
    ds["cross_subject_area_entities"] = [
        {
            "name": "buyer",
            "maps_to": [{"table": "orders", "column": "id"}],
            "review_state": "rejected",
        }
    ]
    ds["cross_subject_area_metrics"] = [
        {
            "name": "order_count",
            "calculation": "count",
            "bindings": {"PostgreSQL": "COUNT(*)"},
            "source_tables": ["orders"],
            "review_state": "rejected",
        }
    ]
    (out / "datasource.yaml").write_text(yaml.safe_dump(ds, sort_keys=False))

    served = loader.load_datasource(out)
    assert served.cross_subject_area_entities == []
    assert served.cross_subject_area_metrics == []
    full = loader.load_datasource(out, include_rejected=True)
    assert [e.name for e in full.cross_subject_area_entities] == ["buyer"]
    assert [x.name for x in full.cross_subject_area_metrics] == ["order_count"]
