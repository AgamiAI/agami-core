"""A two-table metric can say how its tables join, and a figure written as its binding over that
join is credited as the metric.

The live shape (2026-11-18): an approved first-contact-resolution count over tickets and their
handoff history, written word for word with a LEFT JOIN, which is right for it — a ticket never
handed off has no history row. Its binding does not say how the tables join, and an outer join's
null-extended rows can change the figure, so the receipt left it `undetermined` with `unconfirmed`
naming the metric (ACE-177 item 8). A metric now declares `joins: [{table, kind}]`; a statement
whose join between the metric's tables is that kind, on a declared relationship's key, is matched.
Any other join stays exactly as before: fail closed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from semantic_model import curate  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402
from semantic_model import validator as V  # noqa: E402

from test_curate_edit_goes_stale import _edit, _write_model  # noqa: E402

BINDING = (
    "COUNT(DISTINCT CASE WHEN state = 'closed' AND (reassignments = 0 OR (reassignments = 1 "
    "AND kind = 'routing' AND value = 'front_desk')) THEN number END)"
)
WRITTEN = (
    BINDING.replace("state", "t.state")
    .replace("reassignments", "t.reassignments")
    .replace("kind", "h.kind")
    .replace("value", "h.value")
    .replace("THEN number", "THEN t.number")
)
# The key alone, and the live shape: the key plus a condition on the history in the ON. Without a
# declaration the second is the one left unconfirmed (an ON condition on the optional side, whose
# null-extended rows still count); the first, undeclared, is still read as a join that keeps only
# the tickets with a history row, unchanged here.
KEYED = (
    f"SELECT {WRITTEN} AS first_contact_resolved FROM public.tickets t "
    "LEFT JOIN public.handoffs h ON h.ticket_id = t.id"
)
LEFT = KEYED + " AND h.kind = 'routing'"
INNER = LEFT.replace("LEFT JOIN", "JOIN")


def _col(name: str, kind: str = "string") -> m.Column:
    return m.Column(name=name, type=kind)


def _org(joins: "list | None" = None, *, related: bool = True) -> m.Datasource:
    metric = {
        "name": "first_contact_resolutions",
        "description": "Tickets closed on first contact.",
        "calculation": "Closed tickets never handed off, or handed off once from the front desk.",
        "bindings": {"PostgreSQL": BINDING},
        "source_tables": ["tickets", "handoffs"],
        "review_state": "approved",
        "signed_off_by": "you@example.com",
        "signed_off_at": "2026-01-01T00:00:00Z",
        "signed_off_role": "data_lead",
    }
    if joins is not None:
        metric["joins"] = joins
    rels = (
        [
            m.Relationship(
                from_table="handoffs",
                to_table="tickets",
                from_column="ticket_id",
                to_column="id",
                relationship="many_to_one",
            )
        ]
        if related
        else []
    )
    return m.Datasource(
        datasource="acme",
        storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
        subject_areas=[
            m.SubjectArea(
                name="support",
                tables_defined=[
                    m.Table(
                        name="tickets",
                        schema="public",
                        storage_connection="c",
                        grain=["id"],
                        columns=[
                            _col("id"),
                            _col("number"),
                            _col("state"),
                            _col("reassignments", "integer"),
                        ],
                    ),
                    m.Table(
                        name="handoffs",
                        schema="public",
                        storage_connection="c",
                        grain=["id"],
                        columns=[_col("id"), _col("ticket_id"), _col("kind"), _col("value")],
                    ),
                ],
                relationships=rels,
                metrics=[m.Metric(**metric)],
            )
        ],
    )


def _first(org: m.Datasource, sql: str) -> dict:
    items = rt.assemble_receipt(org, sql)["columns"]["items"]
    return next(i for i in items if i["kind"] == "output")


UNCONFIRMED = {"name": "first_contact_resolutions", "area": "support"}


# --- the runtime rule ----------------------------------------------------------------------------


def test_a_left_join_the_metric_declares_is_credited_as_the_metric():
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), LEFT)
    assert item["status"] == rt.MATCHED and item["name"] == "first_contact_resolutions"
    assert item["review_state"] == "approved" and item["unconfirmed"] is None
    assert not item["joins_not_declared"]
    # The condition beyond the declared key is said; the declared join itself is not a restriction.
    assert len(item["restrictions"]) == 1 and "routing" in item["restrictions"][0]
    assert not any("matching" in r for r in item["restrictions"])


def test_a_declared_left_join_on_the_key_alone_lists_no_restriction():
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), KEYED)
    assert item["status"] == rt.MATCHED and item["name"] == "first_contact_resolutions"
    # Not "only handoffs rows that have a matching tickets row": the metric keeps a ticket with none.
    assert not item["restrictions"]


def test_a_left_join_with_no_declaration_stays_unconfirmed():
    item = _first(_org(), LEFT)
    assert item["status"] == rt.UNDETERMINED and item["name"] is None
    assert item["unconfirmed"] == UNCONFIRMED


def test_an_inner_join_where_the_metric_declares_left_is_not_the_metric():
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), INNER)
    assert item["status"] == rt.UNDETERMINED and item["name"] is None
    assert item["unconfirmed"] == UNCONFIRMED


@pytest.mark.parametrize("sql", [LEFT, KEYED], ids=["live", "keyed"])
def test_a_left_join_where_the_metric_declares_inner_stays_unconfirmed(sql):
    item = _first(_org([{"table": "handoffs", "kind": "inner"}]), sql)
    assert item["status"] == rt.UNDETERMINED and item["unconfirmed"] == UNCONFIRMED


def test_an_inner_join_the_metric_declares_is_credited():
    item = _first(_org([{"table": "handoffs", "kind": "inner"}]), INNER)
    assert item["status"] == rt.MATCHED and item["name"] == "first_contact_resolutions"


def test_a_left_join_on_another_key_is_not_credited():
    sql = LEFT.replace("h.ticket_id = t.id", "h.ticket_id = t.number")
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), sql)
    assert item["status"] != rt.MATCHED and item["name"] is None


def test_a_left_join_with_no_declared_relationship_is_not_credited():
    item = _first(_org([{"table": "handoffs", "kind": "left"}], related=False), LEFT)
    assert item["status"] != rt.MATCHED and item["name"] is None


def test_a_left_join_from_the_other_side_is_not_credited():
    # The tickets are now the optional side: a different figure from the one declared.
    sql = (
        f"SELECT {WRITTEN} AS first_contact_resolved FROM public.handoffs h "
        "LEFT JOIN public.tickets t ON h.ticket_id = t.id"
    )
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), sql)
    assert item["status"] != rt.MATCHED and item["name"] is None


def test_a_full_join_is_never_credited():
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), LEFT.replace("LEFT", "FULL"))
    assert item["status"] != rt.MATCHED and item["name"] is None


def test_a_windowed_figure_is_never_credited():
    sql = LEFT.replace(" AS first_contact_resolved", " OVER () AS first_contact_resolved")
    item = _first(_org([{"table": "handoffs", "kind": "left"}]), sql)
    assert item["status"] != rt.MATCHED and item["name"] is None


# --- the model and validator ---------------------------------------------------------------------


def test_an_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        _org([{"table": "handoffs", "kind": "full"}])


def test_a_table_outside_source_tables_is_rejected():
    with pytest.raises(ValueError, match="source_tables"):
        _org([{"table": "agents", "kind": "left"}])


def test_a_table_declared_twice_is_rejected():
    with pytest.raises(ValueError, match="once"):
        _org([{"table": "handoffs", "kind": "left"}, {"table": "handoffs", "kind": "inner"}])


def test_a_declared_join_with_no_relationship_is_flagged():
    res = V.validate(_org([{"table": "handoffs", "kind": "left"}], related=False))
    assert any(f.code == "metric_join_unrelated" for f in res.findings), res.findings
    assert V.validate(_org([{"table": "handoffs", "kind": "left"}])).ok


def test_a_metric_without_joins_dumps_without_the_key():
    # So a file written by this release still loads on an older one, which forbids unknown keys.
    assert "joins" not in _org().subject_areas[0].metrics[0].model_dump()
    dumped = _org([{"table": "handoffs", "kind": "left"}]).subject_areas[0].metrics[0].model_dump()
    assert dumped["joins"] == [{"table": "handoffs", "kind": "left"}]


# --- curation ------------------------------------------------------------------------------------


def test_curate_edits_a_metrics_joins(tmp_path):
    root = tmp_path / "demo"
    _write_model(root)
    ops = [
        _edit("metric", "revenue", "source_tables", ["orders", "customers"]),
        _edit("metric", "revenue", "joins", [{"table": "customers", "kind": "left"}]),
    ]
    res = curate.apply(root, ops, signer="editor@example.com", role="analyst")
    assert res.validated, res.errors
    met = L.load_datasource(root, include_rejected=True).subject_areas[0].metrics[0]
    assert [(j.table, j.kind) for j in met.joins] == [("customers", "left")]
    assert met.review_state == "stale", "how the tables join changes what the metric counts"

    res = curate.apply(
        root, [_edit("metric", "revenue", "joins", [])], signer="editor@example.com", role="analyst"
    )
    assert res.validated, res.errors
    doc = yaml.safe_load((root / "subject_areas/sales/metrics/revenue.yaml").read_text())
    assert "joins" not in doc, "clearing the joins removes the key"


def test_curate_refuses_a_join_on_a_table_the_metric_does_not_read(tmp_path):
    root = tmp_path / "demo"
    _write_model(root)
    before = (root / "subject_areas/sales/metrics/revenue.yaml").read_text()
    res = curate.apply(
        root,
        [_edit("metric", "revenue", "joins", [{"table": "customers", "kind": "left"}])],
        signer="editor@example.com",
        role="analyst",
    )
    assert not res.validated
    assert (root / "subject_areas/sales/metrics/revenue.yaml").read_text() == before


def test_the_schema_reply_serves_the_declared_join_with_the_binding():
    import tools

    met = _org([{"table": "handoffs", "kind": "left"}]).subject_areas[0].metrics[0]
    entry = tools._metric_full(met, "support", "PostgreSQL")
    assert entry["binding"] == BINDING
    assert entry["joins"] == [{"table": "handoffs", "kind": "left"}]
    assert "joins" not in tools._metric_full(
        _org().subject_areas[0].metrics[0], "support", "PostgreSQL"
    )
