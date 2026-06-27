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
import re
from typing import Any
from uuid import uuid4

from semantic_model.models import Organization
from store import Store

# The per-datasource model tables write_organization clears before a re-seed (so a redeploy
# reproduces the served model rather than appending duplicates / hitting PK conflicts). Must stay in
# sync with migrations/core/001_serving.sql's serving tables; examples/memory/model_version are
# re-seeded by their own writers, so they're not in this list.
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


# ---------------------------------------------------------------------------
# Memory (ORGANIZATION.md / USER_MEMORY.md) + model_version — served from the DB too, so a DB-only
# deploy reads NO files at runtime (get_datasource_schema's domain context + the receipt's version
# pin come from these tables, not disk).
# ---------------------------------------------------------------------------


# ORGANIZATION.md is per-datasource; USER_MEMORY.md is install-global (cross-datasource, mirroring
# the file layout: <artifacts_dir>/<profile>/ORGANIZATION.md vs <artifacts_dir>/USER_MEMORY.md). So
# user memory is stored once under this sentinel datasource, not duplicated per datasource.
_GLOBAL_DATASOURCE = ""


def write_memory(
    store: Store, datasource: str, *, organization: str | None = None, user: str | None = None
) -> None:
    """Seed the domain-context docs. `organization` is per-datasource; `user` is install-global
    (one row, shared across datasources). Pass either/both; each replaces its row."""
    if organization is not None:
        store.execute(
            "DELETE FROM memory WHERE datasource = ? AND kind = 'organization'", (datasource,)
        )
        store.execute(
            "INSERT INTO memory (datasource, kind, content) VALUES (?, 'organization', ?)",
            (datasource, organization),
        )
    if user is not None:
        store.execute(
            "DELETE FROM memory WHERE datasource = ? AND kind = 'user'", (_GLOBAL_DATASOURCE,)
        )
        store.execute(
            "INSERT INTO memory (datasource, kind, content) VALUES (?, 'user', ?)",
            (_GLOBAL_DATASOURCE, user),
        )
    store.commit()


def load_memory(store: Store, datasource: str) -> dict[str, str]:
    """{'organization': <per-datasource ORGANIZATION.md>, 'user': <global USER_MEMORY.md>} —
    missing keys absent."""
    out: dict[str, str] = {}
    org = store.query(
        "SELECT content FROM memory WHERE datasource = ? AND kind = 'organization'", (datasource,)
    )
    if org:
        out["organization"] = org[0]["content"]
    usr = store.query(
        "SELECT content FROM memory WHERE datasource = ? AND kind = 'user'", (_GLOBAL_DATASOURCE,)
    )
    if usr:
        out["user"] = usr[0]["content"]
    return out


def write_model_version(
    store: Store, datasource: str, version: str, created_at: str | None = None
) -> None:
    """Record a model version (the snapshot content hash the receipt pins). Idempotent per version."""
    store.execute(
        "DELETE FROM model_version WHERE datasource = ? AND version = ?", (datasource, version)
    )
    store.execute(
        "INSERT INTO model_version (datasource, version, created_at) VALUES (?, ?, ?)",
        (datasource, version, created_at),
    )
    store.commit()


def newest_model_version(store: Store, datasource: str) -> str | None:
    """The newest recorded version for a datasource (what the receipt pins), or None."""
    rows = store.query(
        "SELECT version FROM model_version WHERE datasource = ? "
        "ORDER BY created_at DESC, version DESC",
        (datasource,),
    )
    return rows[0]["version"] if rows else None


# ---------------------------------------------------------------------------
# Prompt examples — write at deploy; serve scoped + ranked + capped at query time.
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")
_EXAMPLES_CHAR_BUDGET = 20_000


def _tokens(s: str | None) -> set[str]:
    return set(_WORD_RE.findall((s or "").lower()))


def write_examples(store: Store, datasource: str, examples: list[dict[str, Any]]) -> None:
    """(Re)seed the prompt-example rows for a datasource. Each example is {area, question, sql, …};
    area None ⇒ the org-level cross-datasource bucket."""
    store.execute("DELETE FROM prompt_example WHERE datasource = ?", (datasource,))
    for ex in examples:
        # Keep a stable id across re-seeds when the example carries one (so per-example identity
        # survives a redeploy); mint one only when absent.
        ex_id = str(ex.get("id") or uuid4().hex)
        store.execute(
            "INSERT INTO prompt_example (datasource, area, id, question, doc) VALUES (?, ?, ?, ?, ?)",
            (datasource, ex.get("area"), ex_id, ex.get("question", ""), json.dumps(ex)),
        )
    store.commit()


def select_examples(
    store: Store,
    datasource: str,
    query: str | None = None,
    area: str | None = None,
    top_k: int = 10,
    char_budget: int = _EXAMPLES_CHAR_BUDGET,
) -> list[dict[str, Any]]:
    """Scope to the datasource (+ area, plus the org-level bucket), rank by word-overlap on the
    question, and cap to top-K within a char budget — so a large library never floods the context.
    No embeddings (that tier is deploy-time + off by default)."""
    if area:
        rows = store.query(
            "SELECT question, doc FROM prompt_example WHERE datasource = ? "
            "AND (area = ? OR area IS NULL)",
            (datasource, area),
        )
    else:
        rows = store.query(
            "SELECT question, doc FROM prompt_example WHERE datasource = ?", (datasource,)
        )
    q = _tokens(query)
    if q:
        rows = sorted(rows, key=lambda r: len(q & _tokens(r["question"])), reverse=True)
    out: list[dict[str, Any]] = []
    used = 0
    for r in rows[:top_k]:
        doc = json.loads(r["doc"])
        size = len(json.dumps(doc))
        if out and used + size > char_budget:
            break
        out.append(doc)
        used += size
    return out


# ---------------------------------------------------------------------------
# Runtime write path — the DB-backed ActivitySink (conforms to ports.ActivitySink by shape).
# ---------------------------------------------------------------------------


class DbActivitySink:
    """Write `query_executions` + `feedback` to the DB (one class, any backend the Store opens —
    not a Postgres/SQLite pair). Conforms structurally to the `ports.ActivitySink` Protocol; the
    server's single execute_sql chokepoint logs one row per query through it."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def record_query_execution(self, record: Any) -> None:
        self._store.execute(
            "INSERT INTO query_executions (id, ts, datasource, question, sql, row_count, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                record.ts,
                record.profile,
                record.question,
                record.sql,
                record.row_count,
                record.source,
            ),
        )
        self._store.commit()

    def record_feedback(self, record: Any) -> None:
        self._store.execute(
            "INSERT INTO feedback (id, ts, datasource, question, rating, notes, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                record.ts,
                record.profile,
                record.question,
                record.rating,
                record.notes,
                record.source,
            ),
        )
        self._store.commit()

    def record_tool_call(self, record: Any) -> None:
        # `success` is a portable 0/1 (no boolean literal across SQLite/Postgres).
        self._store.execute(
            "INSERT INTO tool_calls (id, ts, actor, tool_name, datasource, sql, row_count, "
            "execution_ms, success, error_kind, source, user_question, agent_query, thread_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                record.ts,
                record.actor,
                record.tool_name,
                record.datasource,
                record.sql,
                record.row_count,
                record.execution_ms,
                1 if record.success else 0,
                record.error_kind,
                record.source,
                record.user_question,
                record.agent_query,
                record.thread_id,
            ),
        )
        self._store.commit()
