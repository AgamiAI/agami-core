"""Loader for the agami semantic-model-v2 on-disk layout.

On-disk tree (design doc's "Storage layout on disk"), rooted at a v2 profile dir.
For LiteBI this lives at ``<artifacts_dir>/<profile>/.semantic_v2/`` so the legacy
per-table OSI model stays untouched side-by-side:

    <root>/
      org.yaml                                 # org desc + storage_connections + subject_areas refs
      datasources/<connection>/storage.yaml    # physical: storage_type, storage_config
      subject_areas/<name>/
        subject_area.yaml                      # desc, default_time_window, tables (TableRefs)
        tables/<t>.yaml                        # canonical Table definitions
        entities/<e>.yaml
        metrics/<m>.yaml
        relationships.yaml                     # intra-area FK graph (list)
      cross_subject_area_relationships.yaml    # optional, org-level
      cross_subject_area_entities.yaml         # optional, org-level
      cross_subject_area_metrics.yaml          # optional, org-level
      prompt_examples/<subject_area>/examples.yaml

The loader parses the tree into a single `Organization` model (so the validator
and runtime work on one in-memory object). It also provides the context-assembly
functions the runtime depends on:

  - collect_default_filters(...)   — union of in-scope tables' default_filters,
                                     with :param substitution.
  - get_table_context(...)         — the compound call (columns + default_filters
                                     + relationships + caveats + value_transforms).
  - get_table_index(...)           — mode=index column listing honoring
                                     TableRef.expose_column_groups.
  - get_subject_area_bundle(...)   — one-shot for small areas.

Nothing here imports Pydantic-free; the whole module is v2-only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from .models import (
    Column,
    CrossSubjectAreaRelationship,
    Entity,
    Metric,
    Organization,
    Relationship,
    StorageConnection,
    SubjectArea,
    Table,
    TableRef,
)


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_organization(root: str | Path, *, include_rejected: bool = False) -> Organization:
    """Parse a v2 profile directory into an Organization model.

    By default, entries the curator excluded (`review_state: rejected`) are dropped
    so the runtime never sees them. Pass `include_rejected=True` for the curation
    tools (agami-model / agami-review), which must show excluded entries to toggle them.
    """
    root = Path(root)
    org_path = root / "org.yaml"
    if not org_path.exists():
        raise FileNotFoundError(f"no org.yaml at {org_path}")
    org_doc: dict[str, Any] = _read_yaml(org_path) or {}

    # storage connections — inline list OR refs into datasources/<c>/storage.yaml
    connections: list[StorageConnection] = []
    for sc in org_doc.get("storage_connections", []) or []:
        if isinstance(sc, dict) and "ref" in sc:
            ref_path = root / sc["ref"]
            connections.append(StorageConnection(**(_read_yaml(ref_path) or {})))
        elif isinstance(sc, dict) and "storage_type" in sc:
            connections.append(StorageConnection(**sc))
        else:
            # {name, ref} where ref missing -> try datasources/<name>/storage.yaml
            name = sc.get("name") if isinstance(sc, dict) else str(sc)
            guess = root / "datasources" / str(name) / "storage.yaml"
            if guess.exists():
                connections.append(StorageConnection(**(_read_yaml(guess) or {})))

    # subject areas — each referenced by directory name
    subject_areas: list[SubjectArea] = []
    for sa_ref in org_doc.get("subject_areas", []) or []:
        sa_dir = root / (sa_ref if isinstance(sa_ref, str) else sa_ref.get("path", ""))
        if not sa_dir.exists():
            # also accept a bare name under subject_areas/
            sa_dir = root / "subject_areas" / str(sa_ref)
        subject_areas.append(_load_subject_area(sa_dir, include_rejected=include_rejected))

    org = Organization(
        organization=org_doc.get("organization", root.name),
        version=org_doc.get("version", 1),
        description=org_doc.get("description", ""),
        fiscal_year_start_month=org_doc.get("fiscal_year_start_month", 1),
        storage_connections=connections,
        subject_areas=subject_areas,
        cross_subject_area_relationships=_load_cross_rels(root, org_doc),
        cross_subject_area_entities=_load_cross_entities(root, org_doc),
        cross_subject_area_metrics=_load_cross_metrics(root, org_doc),
    )
    return org


def _rejected(obj) -> bool:
    return getattr(obj, "review_state", None) == "rejected"


def _load_subject_area(sa_dir: Path, include_rejected: bool = False) -> SubjectArea:
    sa_doc: dict[str, Any] = _read_yaml(sa_dir / "subject_area.yaml") or {}

    tables_defined: list[Table] = []
    tdir = sa_dir / "tables"
    if tdir.exists():
        for tf in sorted(tdir.glob("*.yaml")):
            t = Table(**(_read_yaml(tf) or {}))
            if not include_rejected:
                if _rejected(t):
                    continue  # whole table excluded by the curator
                # drop per-column exclusions
                t.columns = [c for c in t.columns if not _rejected(c)]
            tables_defined.append(t)

    live_tables = {t.name for t in tables_defined}

    entities: list[Entity] = []
    edir = sa_dir / "entities"
    if edir.exists():
        for ef in sorted(edir.glob("*.yaml")):
            e = Entity(**(_read_yaml(ef) or {}))
            if not include_rejected and _rejected(e):
                continue
            entities.append(e)

    metrics: list[Metric] = []
    mdir = sa_dir / "metrics"
    if mdir.exists():
        for mf in sorted(mdir.glob("*.yaml")):
            mm = Metric(**(_read_yaml(mf) or {}))
            if not include_rejected and _rejected(mm):
                continue
            metrics.append(mm)

    relationships: list[Relationship] = []
    rel_file = sa_dir / "relationships.yaml"
    if rel_file.exists():
        rels_doc = _read_yaml(rel_file) or []
        if isinstance(rels_doc, dict):
            rels_doc = rels_doc.get("relationships", [])
        for r in rels_doc:
            rel = Relationship(**r)
            if not include_rejected:
                # drop rejected joins, and joins whose endpoint table was excluded
                if _rejected(rel):
                    continue
                if (rel.from_table.split(".")[-1] not in live_tables
                        or rel.to_table.split(".")[-1] not in live_tables):
                    continue
            relationships.append(rel)

    # TableRefs are kept as-is — they resolve org-wide (multi-membership), and a
    # ref to a rejected table simply won't resolve at runtime.
    table_refs = [TableRef(**t) for t in (sa_doc.get("tables", []) or [])]

    return SubjectArea(
        name=sa_doc.get("name", sa_dir.name),
        description=sa_doc.get("description", ""),
        default_time_window=sa_doc.get("default_time_window"),
        tables=table_refs,
        tables_defined=tables_defined,
        entities=entities,
        metrics=metrics,
        relationships=relationships,
    )


def _load_cross_rels(root: Path, org_doc: dict) -> list[CrossSubjectAreaRelationship]:
    out: list[CrossSubjectAreaRelationship] = []
    # inline on org.yaml
    for r in org_doc.get("cross_subject_area_relationships", []) or []:
        out.append(CrossSubjectAreaRelationship(**r))
    # or a sidecar file
    f = root / "cross_subject_area_relationships.yaml"
    if f.exists():
        doc = _read_yaml(f) or {}
        for r in doc.get("edges", doc if isinstance(doc, list) else []):
            out.append(CrossSubjectAreaRelationship(**r))
    return out


def _load_cross_entities(root: Path, org_doc: dict) -> list[Entity]:
    out = [Entity(**e) for e in (org_doc.get("cross_subject_area_entities", []) or [])]
    f = root / "cross_subject_area_entities.yaml"
    if f.exists():
        doc = _read_yaml(f) or {}
        for e in doc.get("entities", doc if isinstance(doc, list) else []):
            out.append(Entity(**e))
    return out


def _load_cross_metrics(root: Path, org_doc: dict) -> list[Metric]:
    out = [Metric(**mm) for mm in (org_doc.get("cross_subject_area_metrics", []) or [])]
    f = root / "cross_subject_area_metrics.yaml"
    if f.exists():
        doc = _read_yaml(f) or {}
        for mm in doc.get("metrics", doc if isinstance(doc, list) else []):
            out.append(Metric(**mm))
    return out


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


def _find_table(org: Organization, table_name: str, area: Optional[str] = None) -> Optional[Table]:
    bare = table_name.split(".")[-1]
    areas = [org.subject_area(area)] if area else org.subject_areas
    for sa in areas:
        if sa is None:
            continue
        for t in sa.tables_defined:
            if t.name == table_name or t.name == bare:
                return t
    return None


def _table_alias(table_name: str) -> str:
    return table_name.split(".")[-1]


def collect_default_filters(
    org: Organization,
    table_names: Iterable[str],
    *,
    area: Optional[str] = None,
    params: Optional[dict[str, str]] = None,
) -> list[str]:
    """Union of default_filters for the in-scope tables, with :param substitution.

    `{alias}` placeholders are replaced with the table's bare name; `:param`
    bind markers are replaced from `params` when provided (else left as-is so the
    executor can bind them). Deduped, order-stable.
    """
    params = params or {}
    out: list[str] = []
    seen: set[str] = set()
    for name in table_names:
        table = _find_table(org, name, area)
        if table is None:
            continue
        alias = _table_alias(table.name)
        for flt in table.default_filters:
            resolved = flt.replace("{alias}", alias)
            for k, val in params.items():
                resolved = resolved.replace(f":{k}", str(val))
            if resolved not in seen:
                seen.add(resolved)
                out.append(resolved)
    return out


def get_table_index(
    table: Table, expose_column_groups: Optional[list[str]] = None
) -> dict[str, Any]:
    """mode=index: compact column listing (name + type + 1-line desc), scoped by
    expose_column_groups when set (honors the subject area's view of a wide table)."""
    visible = _visible_columns(table, expose_column_groups)
    return {
        "name": table.name,
        "schema": table.schema_name,
        "description": table.description,
        "grain": table.grain,
        "column_count_total": len(table.columns),
        "column_count_visible": len(visible),
        "columns": [
            {"name": c.name, "type": c.type, "description": c.description} for c in visible
        ],
    }


def _visible_columns(
    table: Table, expose_column_groups: Optional[list[str]]
) -> list[Column]:
    if not expose_column_groups:
        return list(table.columns)
    allowed: set[str] = set()
    for g in expose_column_groups:
        allowed.update(table.column_groups.get(g, []))
    # if a table declares column_groups, restrict; if not, expose all (defensive)
    if not allowed:
        return list(table.columns)
    return [c for c in table.columns if c.name in allowed]


def _exposed_groups_for(sa: SubjectArea, table_name: str) -> Optional[list[str]]:
    for ref in sa.tables:
        if ref.table == table_name or ref.table == _table_alias(table_name):
            return ref.expose_column_groups
    return None


def get_table_context(
    org: Organization,
    tables: list[str],
    *,
    area: Optional[str] = None,
    columns: Optional[list[str]] = None,
    include: Optional[list[str]] = None,
    params: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Compound context fetch — the primary mechanism for keeping inference rounds
    low. Returns columns (full detail for the requested set, honoring
    expose_column_groups), plus any of: default_filters, relationships, caveats,
    value_transforms, metrics.
    """
    include = include or ["default_filters", "relationships", "caveats", "value_transforms"]
    sa = org.subject_area(area) if area else None
    result: dict[str, Any] = {"tables": {}}

    for name in tables:
        table = _find_table(org, name, area)
        if table is None:
            result["tables"][name] = {"error": "not found in scope"}
            continue
        exposed = _exposed_groups_for(sa, table.name) if sa else None
        visible = _visible_columns(table, exposed)
        if columns:
            wanted = set(columns)
            chosen = [c for c in visible if c.name in wanted]
        else:
            chosen = visible

        tinfo: dict[str, Any] = {
            "name": table.name,
            "schema": table.schema_name,
            "description": table.description,
            "grain": table.grain,
            "source_type": table.source_type,
            "columns": [_column_detail(c, include) for c in chosen],
        }
        if "caveats" in include and table.caveats:
            tinfo["caveats"] = table.caveats
        if "default_filters" in include:
            tinfo["default_filters"] = collect_default_filters(
                org, [table.name], area=area, params=params
            )
        if "performance_hints" in include and table.performance_hints:
            tinfo["performance_hints"] = table.performance_hints.model_dump(exclude_none=True)
        result["tables"][table.name] = tinfo

    if "relationships" in include:
        result["relationships"] = _relationships_among(org, tables, area)
    if "metrics" in include:
        result["metrics"] = _metrics_for(org, tables, area)

    return result


def _column_detail(col: Column, include: list[str]) -> dict[str, Any]:
    d: dict[str, Any] = {"name": col.name, "type": col.type, "description": col.description}
    if col.primary_key:
        d["primary_key"] = True
    if col.foreign_key:
        d["foreign_key"] = col.foreign_key.model_dump(exclude_none=True)
    if col.choice_field:
        d["choice_field"] = col.choice_field
    if col.sensitive:
        d["sensitive"] = True
    if col.unit:
        d["unit"] = col.unit
    if col.date_format:
        d["date_format"] = col.date_format   # e.g. epoch_s → convert in SQL + show as a date
    if col.timezone:
        d["timezone"] = col.timezone
    if "value_transforms" in include and col.value_transform:
        d["value_transform"] = col.value_transform
    if col.denormalized_from:
        d["denormalized_from"] = col.denormalized_from.model_dump(exclude_none=True)
    if "caveats" in include and col.caveats:
        d["caveats"] = col.caveats
    return d


def _relationships_among(
    org: Organization, tables: list[str], area: Optional[str]
) -> list[dict[str, Any]]:
    names = {_table_alias(t) for t in tables} | set(tables)
    out: list[dict[str, Any]] = []
    areas = [org.subject_area(area)] if area else org.subject_areas
    for sa in areas:
        if sa is None:
            continue
        for rel in sa.relationships:
            if _table_alias(rel.from_table) in names or _table_alias(rel.to_table) in names:
                out.append(rel.model_dump(exclude_none=True))
    # cross-area edges touching these tables
    for rel in org.cross_subject_area_relationships:
        if _table_alias(rel.from_table) in names or _table_alias(rel.to_table) in names:
            out.append(rel.model_dump(exclude_none=True))
    return out


def _metrics_for(org: Organization, tables: list[str], area: Optional[str]) -> list[dict[str, Any]]:
    names = {_table_alias(t) for t in tables} | set(tables)
    out: list[dict[str, Any]] = []
    areas = [org.subject_area(area)] if area else org.subject_areas
    for sa in areas:
        if sa is None:
            continue
        for met in sa.metrics:
            if not met.source_tables or any(_table_alias(s) in names for s in met.source_tables):
                out.append(met.model_dump(exclude_none=True))
    return out


def get_subject_area_bundle(org: Organization, area: str) -> dict[str, Any]:
    """One-shot bundle for small subject areas (a few dozen tables)."""
    sa = org.subject_area(area)
    if sa is None:
        raise KeyError(f"no subject area {area!r}")
    table_names = [t.name for t in sa.tables_defined]
    bundle = get_table_context(
        org,
        table_names,
        area=area,
        include=["default_filters", "relationships", "caveats", "value_transforms", "metrics"],
    )
    bundle["subject_area"] = {
        "name": sa.name,
        "description": sa.description,
        "default_time_window": sa.default_time_window,
    }
    bundle["entities"] = [e.model_dump(exclude_none=True) for e in sa.entities]
    return bundle


def list_prompt_examples(root: str | Path, area: str) -> list[dict[str, Any]]:
    """Load scope-tagged examples for a subject area (prompt_examples/<area>/examples.yaml)."""
    f = Path(root) / "prompt_examples" / area / "examples.yaml"
    if not f.exists():
        return []
    doc = _read_yaml(f) or {}
    if isinstance(doc, list):
        return doc
    return doc.get("examples", [])


__all__ = [
    "load_organization",
    "collect_default_filters",
    "get_table_index",
    "get_table_context",
    "get_subject_area_bundle",
    "list_prompt_examples",
]
