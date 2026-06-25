"""Serve the semantic model from the DB — the read path + the deploy-time writer.

The model-loader seam is just "produce an `Organization`": the file adapter is
`semantic_model.loader.load_organization(root)`; this is the DB adapter, which rebuilds the
**identical** `Organization` from rows so every downstream tool (get_datasource_schema incl.
sizing, the receipt) is untouched. YAML stays the source of truth — `write_organization` seeds the
rows from a YAML-loaded `Organization` at deploy time (the `deploy_semantic_model.py` path).

Each object is stored as its key/structural columns + a `doc` (the object's `model_dump`), so the
rebuild is lossless without enumerating every pydantic field. The parent docs exclude their child
collections (those are their own rows); load re-attaches them.
"""

from __future__ import annotations

import json
from typing import Any

from semantic_model.models import Organization
from store import Store

# subject-area model tables this module owns (cleared + re-seeded together).
_MODEL_TABLES = ("relationship", "entity", "metric", "model_table", "subject_area", "organization")


def _est_rows(table_doc: dict[str, Any]) -> int | None:
    ph = table_doc.get("performance_hints")
    return ph.get("estimated_row_count") if isinstance(ph, dict) else None


def write_organization(store: Store, datasource: str, org: Organization) -> None:
    """(Re)seed the serving rows for `datasource` from a loaded Organization. Idempotent — clears
    the datasource's existing model rows first, so re-running the deploy reproduces the served model."""
    for tbl in _MODEL_TABLES:
        store.execute(f"DELETE FROM {tbl} WHERE datasource = ?", (datasource,))

    org_doc = org.model_dump(mode="json", exclude={"subject_areas"})
    store.execute(
        "INSERT INTO organization (datasource, org_name, description, doc) VALUES (?, ?, ?, ?)",
        (datasource, org.organization, org.description or None, json.dumps(org_doc)),
    )

    for sa in org.subject_areas:
        sa_doc = sa.model_dump(
            mode="json", exclude={"tables_defined", "metrics", "entities", "relationships"}
        )
        store.execute(
            "INSERT INTO subject_area (datasource, name, description, default_time_window, "
            "table_count, doc) VALUES (?, ?, ?, ?, ?, ?)",
            (
                datasource,
                sa.name,
                sa.description or None,
                sa.default_time_window,
                len(sa.tables_defined),
                json.dumps(sa_doc),
            ),
        )
        for t in sa.tables_defined:
            tdoc = t.model_dump(mode="json")
            store.execute(
                "INSERT INTO model_table (datasource, area, name, est_row_count, doc) "
                "VALUES (?, ?, ?, ?, ?)",
                (datasource, sa.name, t.name, _est_rows(tdoc), json.dumps(tdoc)),
            )
        for m in sa.metrics:
            store.execute(
                "INSERT INTO metric (datasource, area, name, doc) VALUES (?, ?, ?, ?)",
                (datasource, sa.name, m.name, json.dumps(m.model_dump(mode="json"))),
            )
        for e in sa.entities:
            edoc = e.model_dump(mode="json")
            store.execute(
                "INSERT INTO entity (datasource, area, name, value_pattern, doc) "
                "VALUES (?, ?, ?, ?, ?)",
                (datasource, sa.name, e.name, edoc.get("value_pattern"), json.dumps(edoc)),
            )
        for r in sa.relationships:
            rdoc = r.model_dump(mode="json")
            name = f"{rdoc.get('from_table')}->{rdoc.get('to_table')}"
            store.execute(
                "INSERT INTO relationship (datasource, area, name, doc) VALUES (?, ?, ?, ?)",
                (datasource, sa.name, name, json.dumps(rdoc)),
            )
    store.commit()


def load_organization(store: Store, datasource: str) -> Organization | None:
    """Rebuild the Organization for `datasource` from rows, or None if it isn't seeded."""
    org_rows = store.query("SELECT doc FROM organization WHERE datasource = ?", (datasource,))
    if not org_rows:
        return None
    org_doc: dict[str, Any] = json.loads(org_rows[0]["doc"])

    subject_areas = []
    for sa_row in store.query(
        "SELECT name, doc FROM subject_area WHERE datasource = ? ORDER BY name", (datasource,)
    ):
        sa_doc: dict[str, Any] = json.loads(sa_row["doc"])
        area = sa_row["name"]
        sa_doc["tables_defined"] = [
            json.loads(r["doc"])
            for r in store.query(
                "SELECT doc FROM model_table WHERE datasource = ? AND area = ? ORDER BY name",
                (datasource, area),
            )
        ]
        sa_doc["metrics"] = [
            json.loads(r["doc"])
            for r in store.query(
                "SELECT doc FROM metric WHERE datasource = ? AND area = ? ORDER BY name",
                (datasource, area),
            )
        ]
        sa_doc["entities"] = [
            json.loads(r["doc"])
            for r in store.query(
                "SELECT doc FROM entity WHERE datasource = ? AND area = ? ORDER BY name",
                (datasource, area),
            )
        ]
        sa_doc["relationships"] = [
            json.loads(r["doc"])
            for r in store.query(
                "SELECT doc FROM relationship WHERE datasource = ? AND area = ? ORDER BY name",
                (datasource, area),
            )
        ]
        subject_areas.append(sa_doc)

    org_doc["subject_areas"] = subject_areas
    return Organization.model_validate(org_doc)
