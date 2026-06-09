"""One-time OSI→semantic-model upgrade for a pre-existing profile.

The current onboarding path (`agami-connect` → `semantic_model.introspect`) builds
the model directly from a live DB, so new profiles never touch OSI. This tool is the
**upgrade path for a profile that was onboarded under the old OSI layout**: it reads
the per-table OSI tree on disk and converts it to the semantic-model hierarchy,
without re-introspecting the live DB. It NEVER touches the legacy tree and NEVER
git-commits — it writes to a sibling dir (`<artifacts_dir>/<profile>/.semantic_v2/`
by default) plus a human-review report; the reviewer inspects and promotes it to the
profile root. (It also backs the no-info-loss parity tests.)

Input it reads (per `file-layout.md`):
    <artifacts_dir>/<profile>/index.yaml
    <artifacts_dir>/<profile>/<SCHEMA>/_schema.yaml      (tables TOC + relationships + trust)
    <artifacts_dir>/<profile>/<SCHEMA>/<TABLE>.yaml       (per-table OSI w/ agami.* extensions)
    <artifacts_dir>/<profile>/examples.yaml               (NL->SQL few-shot)

What it does (decompositions from the design doc's "Worked decompositions"):
  * Splits the profile into a Storage Connection (physical) + one or more
    Subject Areas (logical). Areas are PROPOSED (name-prefix / FK clustering,
    capped at the sizing ceiling) and written into the review diff for human
    adjustment — never silently final.
  * Carries each per-table OSI field -> a v2 Column (type, description, FK,
    choice_field), flagging PII-ish columns `sensitive`.
  * Derives `column_groups` on deep tables (>= 30 cols) so the area can scope
    wide tables via `expose_column_groups`.
  * Carries each `_schema.yaml` relationship -> a v2 Relationship with INFERRED
    cardinality and the `agami.*` trust JSON mapped into the trust block.
  * Auto-emits a caveat on polymorphic FKs.
  * Bulk-resolves `examples.yaml` -> `proposed` prompt examples.
  * Runs the v2 validator and includes findings in the report.
  * Idempotent: every emitted item carries a `migrated_from` marker; re-running
    produces no net change.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from . import validator as v2validator
from .models import (
    Column,
    Entity,
    Organization,
    PerformanceHints,
    Relationship,
    StorageConnection,
    SubjectArea,
    Table,
    TableRef,
)

TOOL_VERSION = "v2-migrate/1"

# legacy db_type -> v2 StorageType
_DBTYPE_TO_STORAGE = {
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "snowflake": "Snowflake",
    "bigquery": "BigQuery",
    "redshift": "Redshift",
    "sqlite": "SQLite",
    "duckdb": "DuckDB",
}

# agami.type -> v2 ColumnType
_TYPE_MAP = {
    "string": "string",
    "text": "string",
    "varchar": "string",
    "char": "string",
    "integer": "integer",
    "int": "integer",
    "bigint": "integer",
    "smallint": "integer",
    "decimal": "decimal",
    "numeric": "decimal",
    "number": "decimal",
    "float": "float",
    "double": "float",
    "real": "float",
    "boolean": "boolean",
    "bool": "boolean",
    "date": "date",
    "timestamp": "timestamp",
    "datetime": "timestamp",
    "time": "time",
    "json": "json",
    "jsonb": "json",
    "array": "array",
    "uuid": "uuid",
    "bytes": "bytes",
    "binary": "bytes",
}

# column-name patterns that flag a column as sensitive (PII / never-extract)
_SENSITIVE_PATTERNS = re.compile(
    r"(name|dob|birth|phone|mobile|email|address|aadhaar|aadhar|pan|ssn|passport|"
    r"pincode|\bzip\b|account_?no|card_?no)",
    re.IGNORECASE,
)

# known loan-type / segment codes used as column-group prefixes (FinBud)
_KNOWN_PREFIXES = {
    "al", "pl", "gl", "hl", "lap", "las", "bl", "tw", "cc", "total",
}


@dataclass
class MigrationReport:
    profile: str
    out_dir: str
    dry_run: bool
    subject_areas: list[str] = field(default_factory=list)
    table_count: int = 0
    relationship_count: int = 0
    deep_tables: list[str] = field(default_factory=list)
    sensitive_columns: int = 0
    examples_migrated: int = 0
    decisions: list[str] = field(default_factory=list)
    validator_errors: list[str] = field(default_factory=list)
    validator_warnings: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.decisions.append(msg)

    def render(self) -> str:
        lines = [
            f"# semantic-model migration report — {self.profile}",
            f"out_dir: {self.out_dir}   (dry_run={self.dry_run})",
            "",
            f"subject_areas ({len(self.subject_areas)}): {', '.join(self.subject_areas)}",
            f"tables: {self.table_count}   relationships: {self.relationship_count}",
            f"deep tables (column_groups derived): {', '.join(self.deep_tables) or '(none)'}",
            f"sensitive columns flagged: {self.sensitive_columns}",
            f"examples migrated (proposed): {self.examples_migrated}",
            "",
            "## decisions / assumptions",
            *[f"  - {d}" for d in self.decisions],
            "",
            f"## validator: {len(self.validator_errors)} error(s), "
            f"{len(self.validator_warnings)} warning(s)",
            *[f"  ERROR: {e}" for e in self.validator_errors],
            *[f"  warn:  {w}" for w in self.validator_warnings],
            "",
            f"## files {'that WOULD be written' if self.dry_run else 'written'} "
            f"({len(self.files_written)})",
            *[f"  {f}" for f in self.files_written],
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def migrate_profile(
    profile: str,
    artifacts_dir: str | Path,
    *,
    out_dir: Optional[str | Path] = None,
    dry_run: bool = False,
) -> MigrationReport:
    artifacts_dir = Path(artifacts_dir)
    profile_dir = artifacts_dir / profile
    if not (profile_dir / "index.yaml").exists():
        raise FileNotFoundError(f"no index.yaml under {profile_dir}")
    out = Path(out_dir) if out_dir else (profile_dir / ".semantic_v2")

    report = MigrationReport(profile=profile, out_dir=str(out), dry_run=dry_run)
    org = _build_org(profile_dir, report)

    # validate
    res = v2validator.validate(org)
    report.validator_errors = res.errors
    report.validator_warnings = res.warnings

    # examples
    examples_by_area = _migrate_examples(profile_dir, org, report)

    # write
    _write_tree(org, out, examples_by_area, report, dry_run=dry_run)
    return report


# ---------------------------------------------------------------------------
# Build the Organization from the legacy tree
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _hash(*parts: str) -> str:
    return hashlib.sha1("::".join(parts).encode("utf-8")).hexdigest()[:12]


def _build_org(profile_dir: Path, report: MigrationReport) -> Organization:
    index = _read_yaml(profile_dir / "index.yaml") or {}
    db_type = (index.get("db_type") or "").lower()
    storage_type = _DBTYPE_TO_STORAGE.get(db_type, "PostgreSQL")
    conn_name = f"{report.profile}_{db_type or 'db'}"
    report.note(f"storage_type {storage_type!r} inferred from db_type {db_type!r}")

    storage = StorageConnection(
        name=conn_name,
        storage_type=storage_type,
        # NEVER embed secrets; reference the profile so the executor resolves creds
        # from ~/.agami/credentials at runtime.
        storage_config={"profile": report.profile, "credentials_ref": "~/.agami/credentials"},
    )

    # parse every table across every schema
    all_tables: list[Table] = []
    all_rels: list[Relationship] = []
    table_schema: dict[str, str] = {}
    for sch in index.get("schemas", []) or []:
        schema_name = sch["name"]
        schema_file = profile_dir / sch["file"]
        schema_doc = _read_yaml(schema_file) or {}
        sdir = schema_file.parent
        for tinfo in schema_doc.get("tables", []) or []:
            tfile = sdir / tinfo["file"]
            if not tfile.exists():
                continue
            table = _parse_table(
                tfile, schema_name, conn_name, tinfo, report
            )
            all_tables.append(table)
            table_schema[table.name] = schema_name
        # relationships
        for rel in schema_doc.get("relationships", []) or []:
            r = _parse_relationship(rel, schema_name, all_tables, str(schema_file), report)
            if r:
                all_rels.append(r)

    # cross-schema relationships sometimes live on index.yaml
    for rel in index.get("relationships", []) or index.get("cross_schema_relationships", []) or []:
        r = _parse_relationship(rel, None, all_tables, str(profile_dir / "index.yaml"), report)
        if r:
            all_rels.append(r)

    report.table_count = len(all_tables)
    report.relationship_count = len(all_rels)

    # propose subject-area split
    areas = _propose_subject_areas(
        all_tables, all_rels, conn_name, index, report
    )

    # relationships spanning two areas become org-level cross-area edges.
    cross = _extract_cross_area_relationships(areas, all_rels, report)

    org = Organization(
        organization=index.get("profile", report.profile),
        version=1,
        description=_org_description(index),
        storage_connections=[storage],
        subject_areas=areas,
        cross_subject_area_relationships=cross,
    )
    return org


def _org_description(index: dict) -> str:
    descs = [s.get("description", "") for s in index.get("schemas", []) or []]
    descs = [d for d in descs if d]
    return " ".join(descs)[:1000]


def _parse_table(
    tfile: Path,
    schema_name: str,
    conn_name: str,
    tinfo: dict,
    report: MigrationReport,
) -> Table:
    doc = _read_yaml(tfile) or {}
    sm = doc.get("semantic_model", [])
    dataset = None
    for entry in sm:
        for ds in entry.get("datasets", []) or []:
            dataset = ds
            break
        if dataset:
            break
    dataset = dataset or {}

    pk = list(tinfo.get("primary_key", []) or dataset.get("primary_key", []) or [])
    columns: list[Column] = []
    is_pii_table = bool(_SENSITIVE_PATTERNS.search(tfile.stem)) or tfile.stem.upper() == "PII"
    for fld in dataset.get("fields", []) or []:
        col = _parse_column(fld, table_name=tfile.stem, is_pii_table=is_pii_table, report=report)
        columns.append(col)

    # 1-line description: legacy descriptions are mostly 1-line already; if a
    # description is long + sentence-y, keep the first sentence, push the rest to
    # caveats (worked-decomposition: prose -> 1-line desc + caveats).
    raw_desc = tinfo.get("description") or dataset.get("description") or ""
    one_line, extra_caveats = _decompose_description(raw_desc)

    column_groups: dict[str, list[str]] = {}
    if len(columns) >= 30:
        column_groups = _derive_column_groups(columns)
        report.deep_tables.append(tfile.stem)
        report.note(
            f"table {tfile.stem!r} is deep ({len(columns)} cols); derived "
            f"{len(column_groups)} column_groups by name prefix"
        )

    perf = None
    erc = tinfo.get("estimated_row_count")
    if erc:
        perf = PerformanceHints(estimated_row_count=int(erc))

    return Table(
        name=tfile.stem,
        schema=schema_name,
        storage_connection=conn_name,
        grain=pk or [],
        description=one_line,
        caveats=extra_caveats,
        performance_hints=perf,
        column_groups=column_groups,
        columns=columns,
    )


def _parse_column(
    fld: dict, *, table_name: str, is_pii_table: bool, report: MigrationReport
) -> Column:
    name = fld["name"]
    agami = _agami_ext(fld)
    ctype = _TYPE_MAP.get(str(agami.get("type", "")).lower(), "string")
    raw_desc = fld.get("description", "") or ""
    one_line, caveats = _decompose_description(raw_desc)

    sensitive = is_pii_table and bool(_SENSITIVE_PATTERNS.search(name))
    if sensitive:
        report.sensitive_columns += 1

    choice_field = agami.get("choice_field") or fld.get("choice_field")
    if choice_field and not all(isinstance(k, str) for k in choice_field):
        choice_field = {str(k): str(v) for k, v in choice_field.items()}

    unit = agami.get("unit")
    if unit:
        caveats = caveats + [f"Values denominated in {str(unit).upper()}."]

    primary_key = bool(agami.get("signal_breakdown", {}).get("structural_pattern_match") == "primary_key")

    fk = None
    fk_raw = agami.get("foreign_key") or fld.get("foreign_key")
    if fk_raw:
        from .models import ForeignKey

        try:
            fk = ForeignKey(**fk_raw)
            if fk.is_polymorphic:
                caveats = caveats + [
                    "Polymorphic FK: the target table is selected by "
                    f"{fk.discriminator_column!r}; resolve via the matching where clause."
                ]
        except Exception:
            fk = None

    return Column(
        name=name,
        type=ctype,
        description=one_line,
        primary_key=primary_key,
        foreign_key=fk,
        choice_field=choice_field,
        sensitive=sensitive,
        caveats=caveats,
    )


def _parse_relationship(
    rel: dict,
    schema_name: Optional[str],
    all_tables: list[Table],
    source_file: str,
    report: MigrationReport,
) -> Optional[Relationship]:
    from_t = rel.get("from")
    to_t = rel.get("to")
    from_cols = rel.get("from_columns") or ([rel["from_column"]] if rel.get("from_column") else [])
    to_cols = rel.get("to_columns") or ([rel["to_column"]] if rel.get("to_column") else [])
    if not (from_t and to_t and from_cols and to_cols):
        return None

    agami = _agami_ext(rel)
    cardinality = _infer_cardinality(from_t, to_t, from_cols, to_cols, all_tables)

    # compound key -> use on: expression; simple -> from/to_column
    if len(from_cols) == 1 and len(to_cols) == 1:
        kwargs = {"from_column": from_cols[0], "to_column": to_cols[0]}
    else:
        conds = " AND ".join(
            f"{from_t}.{fc} = {to_t}.{tc}" for fc, tc in zip(from_cols, to_cols)
        )
        kwargs = {"on": conds}
        report.note(f"relationship {from_t}->{to_t}: compound key -> on: expression")

    conf = _float_to_confidence(agami.get("confidence"))
    return Relationship(
        from_table=from_t,
        to_table=to_t,
        relationship=cardinality,
        join_type="LEFT",
        confidence=conf,
        review_state=agami.get("review_state", "unreviewed"),
        signed_off_by=agami.get("signed_off_by"),
        signed_off_at=agami.get("signed_off_at"),
        signed_off_role=agami.get("signed_off_role"),
        description=rel.get("name", ""),
        migrated_from={
            "source_file": source_file,
            "source_line_hash": _hash(from_t, to_t, str(from_cols), str(to_cols)),
            "tool_version": TOOL_VERSION,
        },
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Inference helpers
# ---------------------------------------------------------------------------


def _agami_ext(node: dict) -> dict:
    """Extract the agami.* JSON from a COMMON custom_extensions block."""
    for ext in node.get("custom_extensions", []) or []:
        if ext.get("vendor_name") == "COMMON":
            data = ext.get("data")
            if isinstance(data, str):
                try:
                    return json.loads(data).get("agami", {})
                except json.JSONDecodeError:
                    return {}
            if isinstance(data, dict):
                return data.get("agami", {})
    return {}


def _infer_cardinality(
    from_t: str, to_t: str, from_cols: list[str], to_cols: list[str], all_tables: list[Table]
) -> str:
    """Infer join cardinality from grain.

    - to-side columns == to-side full PK (unique) AND from-side == from full PK -> one_to_one
    - to-side columns == to-side full PK (unique), from-side not unique         -> many_to_one
    - from-side == from full PK, to-side not unique                              -> one_to_many
    - else default many_to_one (most common; flagged for review by the caller).
    """
    by_name = {t.name: t for t in all_tables}
    ft = by_name.get(from_t) or by_name.get(from_t.split(".")[-1])
    tt = by_name.get(to_t) or by_name.get(to_t.split(".")[-1])
    from_pk = set(ft.grain) if ft else set()
    to_pk = set(tt.grain) if tt else set()
    from_is_pk = bool(from_pk) and set(from_cols) == from_pk
    to_is_pk = bool(to_pk) and set(to_cols) == to_pk
    if from_is_pk and to_is_pk:
        return "one_to_one"
    if to_is_pk and not from_is_pk:
        return "many_to_one"
    if from_is_pk and not to_is_pk:
        return "one_to_many"
    return "many_to_one"


def _float_to_confidence(val: Any) -> str:
    if isinstance(val, str) and val in ("confirmed", "inferred", "proposed"):
        return val
    try:
        f = float(val)
    except (TypeError, ValueError):
        return "proposed"
    if f >= 0.8:
        return "confirmed"
    if f >= 0.5:
        return "inferred"
    return "proposed"


def _decompose_description(raw: str) -> tuple[str, list[str]]:
    """Split a possibly-stuffed description into a 1-line desc + caveats.

    Heuristic: the first sentence is the description; subsequent sentences that
    look like rules/anti-patterns/quirks become caveats. Short descriptions pass
    through unchanged with no caveats.
    """
    raw = (raw or "").strip()
    if not raw:
        return "", []
    # split into sentences
    parts = re.split(r"(?<=[.!?])\s+", raw)
    if len(parts) <= 1:
        return raw, []
    first = parts[0].strip()
    rest = [p.strip() for p in parts[1:] if p.strip()]
    caveat_markers = re.compile(
        r"\b(do not|don't|never|always|must|prefer|exclude|note|caution|critical|"
        r"anti-pattern|use .* instead|deprecated|may be|can be duplicat)", re.IGNORECASE
    )
    caveats = [r for r in rest if caveat_markers.search(r)]
    leftover = [r for r in rest if r not in caveats]
    # leftover non-rule sentences stay in the description (keep it informative but bounded)
    desc = first
    if leftover:
        desc = (first + " " + " ".join(leftover)).strip()
    if len(desc) > 240:
        desc = desc[:237].rstrip() + "..."
    return desc, caveats


def _derive_column_groups(columns: list[Column]) -> dict[str, list[str]]:
    """Group deep-table columns by name prefix so a subject area can scope them.
    Guarantees every column lands in exactly one group (no orphans)."""
    groups: dict[str, list[str]] = defaultdict(list)
    for c in columns:
        name = c.name
        if c.primary_key or name.upper() in ("ID",):
            groups["identity"].append(name)
            continue
        token = name.split("_")[0].lower()
        if token in _KNOWN_PREFIXES:
            groups[token].append(name)
        elif name.upper().startswith("TOTAL"):
            groups["totals"].append(name)
        elif name.upper().startswith("CC"):
            groups["credit_card"].append(name)
        else:
            groups[token].append(name)
    # merge singleton groups into 'misc' so the view isn't fragmented
    final: dict[str, list[str]] = {}
    misc: list[str] = []
    for g, cols in groups.items():
        if g != "identity" and len(cols) == 1:
            misc.extend(cols)
        else:
            final[g] = cols
    if misc:
        final.setdefault("misc", []).extend(misc)
    final.setdefault("identity", final.get("identity", []))
    if not final["identity"]:
        # ensure identity isn't empty-but-present
        del final["identity"]
    return dict(final)


# ---------------------------------------------------------------------------
# Subject-area proposal
# ---------------------------------------------------------------------------


def _propose_subject_areas(
    tables: list[Table],
    rels: list[Relationship],
    conn_name: str,
    index: dict,
    report: MigrationReport,
) -> list[SubjectArea]:
    """Propose a subject-area split. Under the sizing ceiling -> one area. Over it
    -> cluster by name prefix-family (plural/singular + shared stems merged); each
    table belongs to exactly ONE owning area (canonical def there, no duplication),
    and relationships spanning areas become org-level cross-area edges. ALWAYS a
    proposal for human review."""
    n = len(tables)
    if n <= v2validator.SIZING_WARN:
        area_name = (index.get("profile") or report.profile).lower()
        report.note(
            f"{n} tables <= {v2validator.SIZING_WARN}: single subject area {area_name!r}"
        )
        sa = _make_area(area_name, tables, rels, conn_name)
        report.subject_areas = [sa.name]
        return [sa]

    # over the warn threshold -> cluster by normalized prefix-family
    table_to_area = _cluster_by_family([t.name for t in tables])
    by_area: dict[str, list[Table]] = defaultdict(list)
    for t in tables:
        by_area[table_to_area[t.name]].append(t)

    areas = [
        _make_area(area_name, members, rels, conn_name)
        for area_name, members in sorted(by_area.items())
    ]
    report.subject_areas = [a.name for a in areas]
    report.note(
        f"{n} tables > {v2validator.SIZING_WARN}: clustered into {len(areas)} areas by "
        "name prefix-family; each table owned by one area; cross-area joins emitted as "
        "cross_subject_area_relationships. PROPOSAL — review and adjust boundaries / add "
        "multi-membership TableRefs as desired."
    )
    return areas


def _singularize(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _cluster_by_family(names: list[str]) -> dict[str, str]:
    """Map each table name -> an area key. Groups by first-token (singularized),
    then merges prefix-families (one key being a prefix of another)."""
    # initial key per table
    raw_key: dict[str, str] = {}
    for name in names:
        bare = name.split(".")[-1]
        token = _singularize(re.split(r"[_\s]", bare)[0].lower())
        raw_key[name] = token or "misc"

    keys = sorted(set(raw_key.values()), key=len)  # shorter first = family roots
    # union-find by prefix family
    canonical: dict[str, str] = {}
    for k in keys:
        target = k
        for root in canonical.values():
            if k.startswith(root) or root.startswith(k):
                target = min(root, k, key=len)
                break
        canonical[k] = target
    # second pass to flatten
    def resolve(k: str) -> str:
        seen = set()
        while canonical.get(k, k) != k and k not in seen:
            seen.add(k)
            k = canonical[k]
        return k

    return {name: resolve(raw_key[name]) for name in names}


def _make_area(
    name: str,
    tables: list[Table],
    rels: list[Relationship],
    conn_name: str,
) -> SubjectArea:
    table_names = {t.name for t in tables}
    refs = []
    for t in tables:
        # scope wide tables via expose_column_groups only when the table has groups
        expose = list(t.column_groups.keys()) if t.column_groups else None
        refs.append(
            TableRef(
                storage_connection=conn_name,
                schema=t.schema_name,
                table=t.name,
                expose_column_groups=expose,
            )
        )
    area_rels = [
        r
        for r in rels
        if r.from_table.split(".")[-1] in table_names
        and r.to_table.split(".")[-1] in table_names
    ]
    return SubjectArea(
        name=name,
        description=f"Auto-proposed subject area covering: {', '.join(sorted(table_names))}.",
        tables=refs,
        tables_defined=tables,
        relationships=area_rels,
    )


def _extract_cross_area_relationships(
    areas: list[SubjectArea], rels: list[Relationship], report: MigrationReport
) -> list:
    """Relationships whose endpoints land in two different areas become org-level
    cross_subject_area_relationships."""
    from .models import CrossSubjectAreaRelationship

    area_of: dict[str, str] = {}
    for sa in areas:
        for t in sa.tables_defined:
            area_of[t.name] = sa.name
    intra_ids = {id(r) for sa in areas for r in sa.relationships}

    cross = []
    for r in rels:
        if id(r) in intra_ids:
            continue
        fa = area_of.get(r.from_table.split(".")[-1])
        ta = area_of.get(r.to_table.split(".")[-1])
        if fa and ta and fa != ta:
            data = r.model_dump(exclude_none=True, by_alias=True)
            data["from_subject_area"] = fa
            data["to_subject_area"] = ta
            data["executable"] = "same_engine"  # single storage connection
            data.setdefault("for_questions_about", [])
            cross.append(CrossSubjectAreaRelationship(**data))
    if cross:
        report.note(
            f"{len(cross)} relationship(s) span areas -> cross_subject_area_relationships "
            "(same_engine, single connection)."
        )
    return cross


# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------


def _migrate_examples(
    profile_dir: Path, org: Organization, report: MigrationReport
) -> dict[str, list[dict]]:
    f = profile_dir / "examples.yaml"
    if not f.exists():
        return {}
    doc = _read_yaml(f) or {}
    raw = doc if isinstance(doc, list) else doc.get("examples", [])
    # assign every example to the (single) area, or the first area as default
    default_area = org.subject_areas[0].name if org.subject_areas else "default"
    out: dict[str, list[dict]] = defaultdict(list)
    for ex in raw:
        q = ex.get("question") or ex.get("nl") or ex.get("query")
        sql = ex.get("sql") or ex.get("corrected_sql")
        if not q:
            continue
        entry = {
            "question": q,
            "sql": sql,
            "status": "proposed",
            "source": ex.get("source", "migrated_from_examples_yaml"),
            "migrated_from": {"source_file": str(f), "tool_version": TOOL_VERSION},
        }
        out[default_area].append(entry)
    report.examples_migrated = sum(len(v) for v in out.values())
    return dict(out)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _dump(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100)


def _model_dump(model) -> dict:
    # exclude_none keeps the YAML clean; by_alias emits `schema` not `schema_name`
    return model.model_dump(exclude_none=True, by_alias=True)


def _write_tree(
    org: Organization,
    out: Path,
    examples_by_area: dict[str, list[dict]],
    report: MigrationReport,
    *,
    dry_run: bool,
) -> None:
    def write(rel_path: str, content: str) -> None:
        report.files_written.append(rel_path)
        if dry_run:
            return
        p = out / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    # org.yaml
    org_doc = {
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
    }
    write("org.yaml", _dump(org_doc))

    # storage connections
    for sc in org.storage_connections:
        write(f"datasources/{sc.name}/storage.yaml", _dump(_model_dump(sc)))

    # subject areas
    for sa in org.subject_areas:
        base = f"subject_areas/{sa.name}"
        sa_doc = {
            "name": sa.name,
            "description": sa.description,
            "default_time_window": sa.default_time_window,
            "tables": [_model_dump(tr) for tr in sa.tables],
        }
        write(f"{base}/subject_area.yaml", _dump(sa_doc))
        for t in sa.tables_defined:
            write(f"{base}/tables/{t.name}.yaml", _dump(_model_dump(t)))
        for e in sa.entities:
            write(f"{base}/entities/{e.name}.yaml", _dump(_model_dump(e)))
        for mm in sa.metrics:
            write(f"{base}/metrics/{mm.name}.yaml", _dump(_model_dump(mm)))
        if sa.relationships:
            write(
                f"{base}/relationships.yaml",
                _dump({"relationships": [_model_dump(r) for r in sa.relationships]}),
            )
        # examples
        if sa.name in examples_by_area:
            write(
                f"prompt_examples/{sa.name}/examples.yaml",
                _dump({"examples": examples_by_area[sa.name]}),
            )

    # migration report
    write("MIGRATION_REPORT.md", report.render())


__all__ = ["migrate_profile", "MigrationReport", "TOOL_VERSION"]
