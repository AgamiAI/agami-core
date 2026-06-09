"""Live-DB introspection → the agami semantic model.

Replaces the OSI introspection (`information_schema`-only, LLM-authored YAMLs)
with a deterministic, capability-aware engine that builds the structural model
directly from a live database, across every supported dialect. The skill layers
LLM enrichment (prose descriptions, entities, metrics, value_patterns) on top.

Two modes, auto-detected **per capability** (never block on a missing catalog):

  * Catalog mode — `information_schema` / PRAGMA / data-dictionary reachable →
    precise structure (declared types, PKs, FKs, row estimates).
  * Probe mode — catalog denied → recover structure from the data itself through
    the same CSV executor: column names via the universal `WHERE 1=0` header,
    types from a value sample, grain from uniqueness probes, FKs from name+type
    match confirmed by value-overlap. Everything inferred lands at lower
    confidence / unreviewed → the trust-layer review queue.

The DB is reached through an injected `runner(sql) -> list[dict]` so the engine
is testable without a live database (tests pass a canned runner). The default
runner shells out to the sibling `execute_sql.py` and parses its CSV — the same
local-only execution path the rest of agami uses; no new egress.
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Introspection-run timestamp for system sign-offs on auto-approved structure.
_NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

from . import build
from . import dialects as D
from .models import (
    Column,
    ForeignKey,
    Organization,
    PerformanceHints,
    Relationship,
    StorageConnection,
    Table,
)

Runner = Callable[[str], list[dict]]

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # plugins/agami/scripts
SAMPLE_ROWS = 500
ENUM_MAX_DISTINCT = 25
PROBE_TABLE_CAP = 200  # cap value-overlap probing work


@dataclass
class IntrospectReport:
    profile: str
    db_type: str
    out_dir: str
    dry_run: bool
    mode_per_capability: dict[str, str] = field(default_factory=dict)  # capability -> catalog|probe
    schemas: list[str] = field(default_factory=list)
    table_count: int = 0
    relationship_count: int = 0
    subject_areas: list[str] = field(default_factory=list)
    deep_tables: list[str] = field(default_factory=list)
    sensitive_columns: int = 0
    notes: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"# introspection report — {self.profile} ({self.db_type})",
            f"out_dir: {self.out_dir}   (dry_run={self.dry_run})",
            f"capability modes: {self.mode_per_capability}",
            f"schemas: {self.schemas}",
            f"tables: {self.table_count}   relationships: {self.relationship_count}",
            f"subject_areas ({len(self.subject_areas)}): {', '.join(self.subject_areas)}",
            f"deep tables: {', '.join(self.deep_tables) or '(none)'}",
            f"sensitive columns: {self.sensitive_columns}",
            "## notes",
            *[f"  - {n}" for n in self.notes],
            f"## files ({len(self.files_written)})",
            *[f"  {f}" for f in self.files_written[:60]],
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Default runner: shell to execute_sql.py, parse CSV
# ---------------------------------------------------------------------------


def make_execute_sql_runner(profile: str, python: Optional[str] = None) -> Runner:
    exe = python or sys.executable
    script = str(SCRIPT_DIR / "execute_sql.py")

    def run(sql: str) -> list[dict]:
        proc = subprocess.run(
            [exe, script, "--profile", profile, "--sql", sql],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"execute_sql exit {proc.returncode}")
        return list(csv.DictReader(io.StringIO(proc.stdout)))

    return run


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def introspect(
    profile: str,
    db_type: str,
    *,
    runner: Runner,
    artifacts_dir: str | Path,
    out_dir: Optional[str | Path] = None,
    tables: Optional[list[str]] = None,
    dry_run: bool = False,
    bigquery_region: str = "region-us",
) -> tuple[Organization, IntrospectReport]:
    """Introspect a live DB into the semantic model and (unless dry_run) write the
    canonical tree. `tables` (optional) is a caller-supplied allowlist for the
    no-catalog case where table enumeration itself is denied.
    """
    dialect = D.get_dialect(db_type)
    if isinstance(dialect, D.BigQuery):
        dialect = D.BigQuery(region=bigquery_region)
    conn_name = f"{profile}_{dialect.name.lower()}"
    artifacts_dir = Path(artifacts_dir)
    out = Path(out_dir) if out_dir else (artifacts_dir / profile)

    report = IntrospectReport(profile=profile, db_type=db_type, out_dir=str(out), dry_run=dry_run)

    # 1. discover (schema, table) pairs
    pairs = _discover_tables(dialect, runner, tables, report)
    if not pairs:
        raise RuntimeError(
            "no tables discovered. If the catalog is locked down, pass an explicit "
            "table allowlist (schema.table) so probe mode can describe them."
        )

    # 2. per-table columns + grain
    built: list[Table] = []
    grain_by_table: dict[str, set[str]] = {}
    for schema, table in pairs:
        t = _build_table(dialect, runner, schema, table, conn_name, report)
        built.append(t)
        grain_by_table[t.name] = set(t.grain)

    # 3. relationships (catalog FKs, else probe by name+type+overlap)
    rels = _build_relationships(dialect, runner, pairs, built, grain_by_table, report)
    report.table_count = len(built)
    report.relationship_count = len(rels)

    # 4. propose subject areas + cross-area edges
    areas, notes = build.propose_subject_areas(built, rels, conn_name, profile)
    report.notes.extend(notes)
    report.subject_areas = [a.name for a in areas]
    cross = build.extract_cross_area_relationships(areas, rels)

    storage = StorageConnection(
        name=conn_name,
        storage_type=dialect.name,
        storage_config={"profile": profile, "credentials_ref": "~/.agami/credentials"},
    )
    org = Organization(
        organization=profile,
        version=1,
        storage_connections=[storage],
        subject_areas=areas,
        cross_subject_area_relationships=cross,
    )

    # 5. write (backing up any legacy OSI at the profile root)
    if out == artifacts_dir / profile and not dry_run:
        _backup_legacy_osi(out)
    wr = build.write_tree(org, out, dry_run=dry_run)
    report.files_written = wr.files_written
    return org, report


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _try(runner: Runner, sql: str) -> Optional[list[dict]]:
    try:
        return runner(sql)
    except Exception:
        return None


def _discover_tables(
    dialect: D.Dialect, runner: Runner, allowlist: Optional[list[str]], report: IntrospectReport
) -> list[tuple[Optional[str], str]]:
    if allowlist:
        report.mode_per_capability["tables"] = "allowlist"
        out = []
        for item in allowlist:
            if "." in item:
                s, t = item.split(".", 1)
                out.append((s, t))
            else:
                out.append((None, item))
        return out

    # catalog mode: schemas -> tables
    schemas = _try(runner, dialect.sql_schemas())
    if schemas is not None:
        report.mode_per_capability["tables"] = "catalog"
        report.schemas = [r["schema_name"] for r in schemas]
        pairs: list[tuple[Optional[str], str]] = []
        for s in report.schemas:
            trows = _try(runner, dialect.sql_tables(s)) or []
            for r in trows:
                pairs.append((r.get("schema_name", s), r["table_name"]))
        return pairs

    report.mode_per_capability["tables"] = "unavailable"
    report.notes.append(
        "catalog table enumeration denied and no allowlist provided — cannot "
        "discover tables; re-run with an explicit table list."
    )
    return []


def _build_table(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str,
    conn_name: str, report: IntrospectReport,
) -> Table:
    # columns: catalog first, probe fallback
    cols, mode = _columns(dialect, runner, schema, table)
    report.mode_per_capability.setdefault("columns", mode)

    # grain: catalog PKs, else uniqueness probe
    grain, gmode = _grain(dialect, runner, schema, table, cols)
    report.mode_per_capability.setdefault("grain", gmode)
    pk_set = set(grain)
    for c in cols:
        c.primary_key = c.name in pk_set
        if build.detect_sensitive(table, c.name):
            c.sensitive = True
            report.sensitive_columns += 1

    column_groups = build.maybe_column_groups(cols)
    if column_groups:
        report.deep_tables.append(table)

    perf = None
    est = _try(runner, dialect.sql_row_estimate(schema, table)) if dialect.sql_row_estimate(schema, table) else None
    if est and est and est[0].get("estimated_rows") not in (None, ""):
        try:
            perf = PerformanceHints(estimated_row_count=int(float(est[0]["estimated_rows"])))
        except (ValueError, TypeError):
            perf = None

    return Table(
        name=table,
        schema=schema,
        storage_connection=conn_name,
        grain=grain,
        description="",  # LLM enrichment fills this in
        column_groups=column_groups,
        performance_hints=perf,
        columns=cols,
    )


def _columns(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str
) -> tuple[list[Column], str]:
    rows = _try(runner, dialect.sql_columns(schema, table))
    if rows:
        cols: list[Column] = []
        for r in rows:
            scale = r.get("numeric_scale")
            scale_i = int(scale) if scale not in (None, "", "NULL") else None
            ctype = dialect.map_type(r.get("data_type", ""), numeric_scale=scale_i)
            cols.append(Column(name=r["column_name"], type=ctype, description=""))
        return cols, "catalog"
    # probe: header for names, sample for types
    return _probe_columns(dialect, runner, schema, table), "probe"


def _probe_columns(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str
) -> list[Column]:
    header = _try(runner, dialect.header_sql(schema, table)) or []
    names: list[str]
    if header and isinstance(header, list):
        # DictReader on a 0-row result yields no rows but we need the fieldnames;
        # the runner returns [] for 0 rows, so fall back to a sample for names too.
        names = list(header[0].keys()) if header else []
    else:
        names = []
    sample = _try(runner, dialect.sample_sql(schema, table, SAMPLE_ROWS)) or []
    if not names and sample:
        names = list(sample[0].keys())
    cols: list[Column] = []
    for n in names:
        values = [row.get(n) for row in sample if row.get(n) not in (None, "")]
        cols.append(Column(name=n, type=_infer_value_type(values), description="",
                            choice_field=_maybe_choice(values)))
    return cols


def _grain(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str, cols: list[Column]
) -> tuple[list[str], str]:
    pks = _try(runner, dialect.sql_primary_keys(schema, table))
    if pks:
        return [r["column_name"] for r in pks], "catalog"
    # probe: a single id-ish column that is unique + non-null
    candidates = [c.name for c in cols if c.name.lower() == "id" or c.name.lower().endswith("_id")]
    candidates = candidates or [c.name for c in cols[:3]]
    for cand in candidates:
        res = _try(runner, dialect.count_distinct_sql(schema, table, cand))
        if not res:
            continue
        row = res[0]
        try:
            total = int(float(row.get("total", 0)))
            distinct = int(float(row.get("distinct_count", 0)))
            nulls = int(float(row.get("null_count", 0)))
        except (ValueError, TypeError):
            continue
        if total > 0 and distinct == total and nulls == 0:
            return [cand], "probe"
    return [], "probe"


def _build_relationships(
    dialect: D.Dialect, runner: Runner, pairs: list[tuple[Optional[str], str]],
    tables: list[Table], grain_by_table: dict[str, set[str]], report: IntrospectReport,
) -> list[Relationship]:
    # catalog FKs per schema
    schemas = {s for s, _ in pairs if s}
    catalog_fks: list[dict] = []
    catalog_ok = False
    if isinstance(dialect, D.SQLite):
        for _, t in pairs:
            rows = _try(runner, dialect.sql_foreign_keys_for_table(t))
            if rows is not None:
                catalog_ok = True
                catalog_fks.extend(rows)
    else:
        for s in (schemas or {None}):
            rows = _try(runner, dialect.sql_foreign_keys(s)) if s else None
            if rows is not None:
                catalog_ok = True
                catalog_fks.extend(rows)

    rels: list[Relationship] = []
    if catalog_ok and catalog_fks:
        report.mode_per_capability["relationships"] = "catalog"
        for fk in catalog_fks:
            ft, fc, tt, tc = fk.get("from_table"), fk.get("from_column"), fk.get("to_table"), fk.get("to_column")
            if not (ft and fc and tt and tc):
                continue
            card = build.infer_cardinality(ft, tt, [fc], [tc], grain_by_table)
            # declared-but-unenforced FKs (Redshift/Databricks/Trino) -> confirm by overlap
            confidence = "confirmed" if dialect.fk_enforced else "inferred"
            # enforced-FK joins are trustworthy structure -> auto-approve with a system
            # sign-off (don't flood the review queue); unenforced/probed ones need a glance.
            kw: dict = {}
            if dialect.fk_enforced:
                kw = {"review_state": "approved", "signed_off_by": "agami_introspect",
                      "signed_off_role": "system", "signed_off_at": _NOW}
            else:
                kw = {"review_state": "unreviewed"}
            rels.append(Relationship(
                from_table=ft, from_column=fc, to_table=tt, to_column=tc,
                relationship=card, join_type="LEFT", confidence=confidence, **kw,
            ))
        return rels

    # probe: infer FKs from name+type match, confirm by value-overlap
    report.mode_per_capability["relationships"] = "probe"
    rels.extend(_probe_relationships(dialect, runner, tables, grain_by_table, report))
    return rels


def _probe_relationships(
    dialect: D.Dialect, runner: Runner, tables: list[Table],
    grain_by_table: dict[str, set[str]], report: IntrospectReport,
) -> list[Relationship]:
    by_name = {t.name: t for t in tables}
    # candidate target keys: single-column grains
    targets = {name: next(iter(g)) for name, g in grain_by_table.items() if len(g) == 1}
    rels: list[Relationship] = []
    for t in tables:
        for c in t.columns:
            low = c.name.lower()
            if not low.endswith("_id") and low != "id":
                continue
            stem = low[:-3] if low.endswith("_id") else None
            # match a target table whose name resembles the stem
            for tgt_name, tgt_col in targets.items():
                if tgt_name == t.name:
                    continue
                tn = tgt_name.lower()
                if stem and (stem in tn or tn in stem or tn.rstrip("s") == stem.rstrip("s")):
                    if _overlaps(dialect, runner, t, c.name, by_name[tgt_name], tgt_col):
                        rels.append(Relationship(
                            from_table=t.name, from_column=c.name,
                            to_table=tgt_name, to_column=tgt_col,
                            relationship=build.infer_cardinality(
                                t.name, tgt_name, [c.name], [tgt_col], grain_by_table),
                            join_type="LEFT", confidence="proposed", review_state="unreviewed",
                            description="inferred by name+type match + value-overlap probe",
                        ))
                        break
    return rels


def _overlaps(dialect: D.Dialect, runner: Runner, ft: Table, fc: str, tt: Table, tc: str) -> bool:
    """Sample from-column values and check they exist in the target key."""
    fq_from = dialect.qualified(ft.schema_name, ft.name)
    fq_to = dialect.qualified(tt.schema_name, tt.name)
    col_f = dialect.quote_ident(fc)
    col_t = dialect.quote_ident(tc)
    sql = (
        f"SELECT COUNT(*) AS matched FROM (SELECT DISTINCT {col_f} AS v FROM {fq_from} "
        f"WHERE {col_f} IS NOT NULL {('LIMIT 50' if dialect.limit_style=='limit' else '')}) src "
        f"WHERE EXISTS (SELECT 1 FROM {fq_to} t WHERE t.{col_t} = src.v)"
    )
    res = _try(runner, sql)
    if not res:
        return False
    try:
        return int(float(res[0].get("matched", 0))) > 0
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Value-based inference (probe mode)
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")
_BOOL_VALUES = {"true", "false", "t", "f", "0", "1", "yes", "no"}


def _infer_value_type(values: list) -> str:
    vals = [str(v).strip() for v in values if str(v).strip() != ""][:200]
    if not vals:
        return "string"
    if all(v.lower() in _BOOL_VALUES for v in vals) and len({v.lower() for v in vals}) <= 2:
        return "boolean"
    if all(_INT_RE.match(v) for v in vals):
        return "integer"
    if all(_FLOAT_RE.match(v) for v in vals):
        return "decimal"
    if all(_TS_RE.match(v) for v in vals):
        return "timestamp"
    if all(_DATE_RE.match(v) for v in vals):
        return "date"
    return "string"


def _maybe_choice(values: list) -> Optional[dict[str, str]]:
    vals = [str(v).strip() for v in values if str(v).strip() != ""]
    distinct = sorted(set(vals))
    if 0 < len(distinct) <= ENUM_MAX_DISTINCT and len(vals) >= max(10, 2 * len(distinct)):
        # low-cardinality relative to sample size -> likely an enum
        return {d: "" for d in distinct}  # labels filled in by LLM enrichment
    return None


def _backup_legacy_osi(profile_root: Path) -> None:
    """Move any legacy OSI artifacts at the profile root into .osi_backup/ before
    writing the new tree, so re-onboarding never silently clobbers the old model."""
    legacy = ["index.yaml"]
    backup = profile_root / ".osi_backup"
    moved = False
    for name in legacy:
        src = profile_root / name
        if src.exists():
            backup.mkdir(parents=True, exist_ok=True)
            src.rename(backup / name)
            moved = True
    # per-schema OSI dirs contain a _schema.yaml; move those too
    if profile_root.exists():
        for child in list(profile_root.iterdir()):
            if child.is_dir() and (child / "_schema.yaml").exists():
                backup.mkdir(parents=True, exist_ok=True)
                child.rename(backup / child.name)
                moved = True
    return None


__all__ = ["introspect", "make_execute_sql_runner", "IntrospectReport", "Runner"]
