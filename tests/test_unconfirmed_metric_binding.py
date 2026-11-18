"""A figure written as a metric's binding, word for word, but joined in a way the check cannot
compare, names the metric it could not be confirmed as.

The live shape (2026-11-18): a metric over two tables, its binding written unqualified, and a
statement that used the binding verbatim with the two tables' aliases, LEFT JOINing the many side
with an extra condition in the ON. The binding does not say how its tables are joined, and under a
LEFT JOIN the rows with no match still count (`handoffs = 0` needs no event), so the number can
differ from the metric's: it stays `undetermined` (ACE-177 item 8). What changes is that the item
says WHICH metric it is written as (`unconfirmed`), so a reader can say the figure could not be
confirmed as that measure instead of leaving it nameless. Never a match.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

BINDING = (
    "COUNT(DISTINCT CASE WHEN state = 'closed' AND (handoffs = 0 OR (handoffs = 1 "
    "AND kind = 'routing' AND value = 'front_desk')) THEN number END)"
)
WRITTEN = BINDING.replace("state", "t.state").replace("handoffs", "t.handoffs")
WRITTEN = WRITTEN.replace("kind", "e.kind").replace("value", "e.value")
WRITTEN = WRITTEN.replace("THEN number", "THEN t.number")


def _org() -> m.Datasource:
    def col(name: str, kind: str = "string") -> m.Column:
        return m.Column(name=name, type=kind)

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
                            col("id"),
                            col("number"),
                            col("state"),
                            col("handoffs", "integer"),
                        ],
                    ),
                    m.Table(
                        name="ticket_events",
                        schema="public",
                        storage_connection="c",
                        grain=["id"],
                        columns=[col("id"), col("ticket_id"), col("kind"), col("value")],
                    ),
                ],
                relationships=[
                    m.Relationship(
                        from_table="ticket_events",
                        to_table="tickets",
                        from_column="ticket_id",
                        to_column="id",
                        relationship="many_to_one",
                    )
                ],
                metrics=[
                    m.Metric(
                        name="first_touch_resolutions",
                        description="Tickets closed on first touch.",
                        calculation="Closed tickets with no handoff, or one out of the front desk.",
                        bindings={"PostgreSQL": BINDING},
                        source_tables=["tickets", "ticket_events"],
                        review_state="approved",
                    )
                ],
            )
        ],
    )


def _first(sql: str) -> dict:
    items = rt.assemble_receipt(_org(), sql)["columns"]["items"]
    return next(i for i in items if i["kind"] == "output")


LIVE = (
    f"SELECT {WRITTEN} AS resolved_first_touch FROM public.tickets t "
    "LEFT JOIN public.ticket_events e ON e.ticket_id = t.id AND e.kind = 'routing'"
)


def test_the_binding_under_a_left_join_names_the_metric_it_could_not_be_confirmed_as():
    item = _first(LIVE)
    assert item["status"] == rt.UNDETERMINED
    assert item["name"] is None and item["review_state"] is None, "never a match"
    assert item["unconfirmed"] == {"name": "first_touch_resolutions", "area": "support"}


def test_the_same_binding_under_an_inner_join_is_the_metric():
    item = _first(LIVE.replace("LEFT JOIN", "JOIN"))
    assert item["status"] == rt.MATCHED and item["name"] == "first_touch_resolutions"
    assert item["unconfirmed"] is None


def test_a_value_written_as_no_binding_names_nothing():
    sql = (
        "SELECT COUNT(DISTINCT CASE WHEN e.kind = 'routing' THEN t.number END) AS routed "
        "FROM public.tickets t LEFT JOIN public.ticket_events e ON e.ticket_id = t.id"
    )
    item = _first(sql)
    assert item["status"] != rt.MATCHED and item["unconfirmed"] is None
