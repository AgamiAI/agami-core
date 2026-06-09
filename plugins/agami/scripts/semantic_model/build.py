"""Shared model-assembly + on-disk writer for building the semantic model.

These are the dialect-agnostic, source-agnostic pieces used by the introspection
engine (`introspect.py`): infer grain/cardinality, derive column_groups on deep
tables, flag sensitive columns, propose a subject-area split, extract cross-area
edges, and write the canonical on-disk tree. Kept here (not in introspect.py) so
the assembly logic is independently testable and reusable.

(`migrate.py` — the transitional OSI→model converter — has its own copies of the
equivalent helpers; it is deleted in the final cleanup PR, at which point this is
the single home.)
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import (
    Column,
    CrossSubjectAreaRelationship,
    Organization,
    Relationship,
    SubjectArea,
    Table,
    TableRef,
)
from .models import DEEP_TABLE_COLUMN_THRESHOLD

# Strongly-PII column-name patterns — sensitive regardless of table.
STRONG_PII_RE = re.compile(
    r"(dob|birth|phone|mobile|email|address|aadhaar|aadhar|\bpan\b|\bssn\b|passport|"
    r"pincode|\bzip\b|account_?no|card_?no|salary|income)",
    re.IGNORECASE,
)
# Weakly-PII patterns — only sensitive inside a PII-ish table (a "name" column on
# a product table is not PII; on a person table it is).
WEAK_PII_RE = re.compile(r"(\bname\b|first_?name|last_?name|full_?name|surname)", re.IGNORECASE)
# Back-compat alias (some callers import SENSITIVE_RE).
SENSITIVE_RE = STRONG_PII_RE

# loan-type / segment prefixes commonly seen as column-group roots
_KNOWN_PREFIXES = {"al", "pl", "gl", "hl", "lap", "las", "bl", "tw", "cc", "total"}

# sizing thresholds mirror validator.SIZING_WARN
SINGLE_AREA_MAX = 24


def detect_sensitive(table_name: str, column_name: str) -> bool:
    """Strongly-PII column names (email/phone/dob/ssn/address/…) are sensitive
    regardless of table. Weakly-PII names (name/first_name/…) are sensitive only
    inside a PII-ish table, to avoid flagging e.g. a product `name`."""
    if STRONG_PII_RE.search(column_name):
        return True
    table_pii = table_name.upper() == "PII" or bool(STRONG_PII_RE.search(table_name)) \
        or bool(WEAK_PII_RE.search(table_name))
    return bool(WEAK_PII_RE.search(column_name)) and table_pii


def derive_column_groups(columns: list[Column]) -> dict[str, list[str]]:
    """Group deep-table columns by name prefix; every column lands in exactly one
    group (no orphans — the validator enforces this on deep tables)."""
    groups: dict[str, list[str]] = defaultdict(list)
    for c in columns:
        name = c.name
        if c.primary_key or name.upper() == "ID":
            groups["identity"].append(name)
            continue
        token = name.split("_")[0].lower()
        if token in _KNOWN_PREFIXES:
            groups[token].append(name)
        elif name.upper().startswith("TOTAL"):
            groups["totals"].append(name)
        else:
            groups[token].append(name)
    final: dict[str, list[str]] = {}
    misc: list[str] = []
    for g, cols in groups.items():
        if g != "identity" and len(cols) == 1:
            misc.extend(cols)
        else:
            final[g] = cols
    if misc:
        final.setdefault("misc", []).extend(misc)
    return dict(final)


def maybe_column_groups(columns: list[Column]) -> dict[str, list[str]]:
    """column_groups only on deep tables; narrow tables get none."""
    if len(columns) >= DEEP_TABLE_COLUMN_THRESHOLD:
        return derive_column_groups(columns)
    return {}


def infer_cardinality(
    from_table: str,
    to_table: str,
    from_cols: list[str],
    to_cols: list[str],
    grain_by_table: dict[str, set[str]],
) -> str:
    """Infer join cardinality from declared/inferred grain.

    to-side == to PK & from-side == from PK -> one_to_one
    to-side == to PK (unique), from not      -> many_to_one
    from-side == from PK, to not             -> one_to_many
    else                                     -> many_to_one (default; review)
    """
    from_pk = grain_by_table.get(from_table, set())
    to_pk = grain_by_table.get(to_table, set())
    from_is_pk = bool(from_pk) and set(from_cols) == from_pk
    to_is_pk = bool(to_pk) and set(to_cols) == to_pk
    if from_is_pk and to_is_pk:
        return "one_to_one"
    if to_is_pk and not from_is_pk:
        return "many_to_one"
    if from_is_pk and not to_is_pk:
        return "one_to_many"
    return "many_to_one"


# ---------------------------------------------------------------------------
# Subject-area proposal (prefix-family clustering; one table per area)
# ---------------------------------------------------------------------------


def _singularize(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def cluster_by_family(names: list[str]) -> dict[str, str]:
    """Map each table name -> an area key, grouping by first-token (singularized)
    then merging prefix-families (one key being a prefix of another)."""
    raw_key: dict[str, str] = {}
    for name in names:
        bare = name.split(".")[-1]
        token = _singularize(re.split(r"[_\s]", bare)[0].lower())
        raw_key[name] = token or "misc"

    canonical: dict[str, str] = {}
    for k in sorted(set(raw_key.values()), key=len):
        target = k
        for root in canonical.values():
            if k.startswith(root) or root.startswith(k):
                target = min(root, k, key=len)
                break
        canonical[k] = target

    def resolve(k: str) -> str:
        seen: set[str] = set()
        while canonical.get(k, k) != k and k not in seen:
            seen.add(k)
            k = canonical[k]
        return k

    return {name: resolve(raw_key[name]) for name in names}


def make_table_ref(conn: str, table: Table) -> TableRef:
    expose = list(table.column_groups.keys()) if table.column_groups else None
    return TableRef(
        storage_connection=conn,
        schema=table.schema_name,
        table=table.name,
        expose_column_groups=expose,
    )


def make_area(name: str, tables: list[Table], rels: list[Relationship], conn: str) -> SubjectArea:
    table_names = {t.name for t in tables}
    area_rels = [
        r for r in rels
        if r.from_table.split(".")[-1] in table_names
        and r.to_table.split(".")[-1] in table_names
    ]
    return SubjectArea(
        name=name,
        description=f"Auto-proposed subject area covering: {', '.join(sorted(table_names))}.",
        tables=[make_table_ref(conn, t) for t in tables],
        tables_defined=tables,
        relationships=area_rels,
    )


def propose_subject_areas(
    tables: list[Table], rels: list[Relationship], conn: str, profile: str
) -> tuple[list[SubjectArea], list[str]]:
    """Return (areas, notes). <=SINGLE_AREA_MAX tables -> one area; else cluster
    by prefix-family with one owning area per table."""
    notes: list[str] = []
    if len(tables) <= SINGLE_AREA_MAX:
        notes.append(f"{len(tables)} tables -> single subject area {profile.lower()!r}")
        return [make_area(profile.lower(), tables, rels, conn)], notes
    mapping = cluster_by_family([t.name for t in tables])
    by_area: dict[str, list[Table]] = defaultdict(list)
    for t in tables:
        by_area[mapping[t.name]].append(t)
    areas = [make_area(a, members, rels, conn) for a, members in sorted(by_area.items())]
    notes.append(
        f"{len(tables)} tables -> {len(areas)} areas by prefix-family (one owner each); "
        "cross-area joins become cross_subject_area_relationships. PROPOSAL — review."
    )
    return areas, notes


def extract_cross_area_relationships(
    areas: list[SubjectArea], rels: list[Relationship]
) -> list[CrossSubjectAreaRelationship]:
    area_of: dict[str, str] = {}
    for sa in areas:
        for t in sa.tables_defined:
            area_of[t.name] = sa.name
    intra_ids = {id(r) for sa in areas for r in sa.relationships}
    out: list[CrossSubjectAreaRelationship] = []
    for r in rels:
        if id(r) in intra_ids:
            continue
        fa = area_of.get(r.from_table.split(".")[-1])
        ta = area_of.get(r.to_table.split(".")[-1])
        if fa and ta and fa != ta:
            data = r.model_dump(exclude_none=True, by_alias=True)
            data.update(from_subject_area=fa, to_subject_area=ta, executable="same_engine")
            data.setdefault("for_questions_about", [])
            out.append(CrossSubjectAreaRelationship(**data))
    return out


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _dump(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100)


def _model_dump(model) -> dict:
    return model.model_dump(exclude_none=True, by_alias=True)


@dataclass
class WriteReport:
    out_dir: str
    dry_run: bool
    files_written: list[str] = field(default_factory=list)


def write_tree(
    org: Organization,
    out: Path,
    *,
    examples_by_area: Optional[dict[str, list[dict]]] = None,
    dry_run: bool = False,
) -> WriteReport:
    """Write the canonical on-disk tree (org.yaml + datasources + subject_areas +
    optional prompt_examples). Returns the list of files (would-be on dry_run)."""
    examples_by_area = examples_by_area or {}
    rep = WriteReport(out_dir=str(out), dry_run=dry_run)

    def write(rel: str, content: str) -> None:
        rep.files_written.append(rel)
        if dry_run:
            return
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    write("org.yaml", _dump({
        "organization": org.organization,
        "version": org.version,
        "description": org.description,
        "fiscal_year_start_month": org.fiscal_year_start_month,
        "storage_connections": [
            {"name": sc.name, "ref": f"datasources/{sc.name}/storage.yaml"}
            for sc in org.storage_connections
        ],
        "subject_areas": [f"subject_areas/{sa.name}" for sa in org.subject_areas],
        "cross_subject_area_relationships": [
            _model_dump(r) for r in org.cross_subject_area_relationships
        ],
    }))
    for sc in org.storage_connections:
        write(f"datasources/{sc.name}/storage.yaml", _dump(_model_dump(sc)))
    for sa in org.subject_areas:
        base = f"subject_areas/{sa.name}"
        write(f"{base}/subject_area.yaml", _dump({
            "name": sa.name,
            "description": sa.description,
            "default_time_window": sa.default_time_window,
            "tables": [_model_dump(tr) for tr in sa.tables],
        }))
        for t in sa.tables_defined:
            write(f"{base}/tables/{t.name}.yaml", _dump(_model_dump(t)))
        for e in sa.entities:
            write(f"{base}/entities/{e.name}.yaml", _dump(_model_dump(e)))
        for mm in sa.metrics:
            write(f"{base}/metrics/{mm.name}.yaml", _dump(_model_dump(mm)))
        if sa.relationships:
            write(f"{base}/relationships.yaml",
                  _dump({"relationships": [_model_dump(r) for r in sa.relationships]}))
        if sa.name in examples_by_area:
            write(f"prompt_examples/{sa.name}/examples.yaml",
                  _dump({"examples": examples_by_area[sa.name]}))
    return rep


__all__ = [
    "SENSITIVE_RE", "detect_sensitive", "derive_column_groups", "maybe_column_groups",
    "infer_cardinality", "cluster_by_family", "make_area", "make_table_ref",
    "propose_subject_areas", "extract_cross_area_relationships",
    "write_tree", "WriteReport",
]
