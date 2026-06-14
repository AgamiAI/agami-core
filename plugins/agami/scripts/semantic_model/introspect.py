"""Live-DB introspection → the agami semantic model.

Replaces the legacy introspection (`information_schema`-only, LLM-authored YAMLs)
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

# At/above this row count a full scan is slow enough that the query path warns + suggests
# narrowing. For such tables we record the date/time columns as `recommended_filters` (the
# natural "narrow by date" answer) so the warning fires only when a query lacks one.
_LARGE_TABLE_ROWS = 1_000_000
# ...but only when there's a clear handful of date columns. A wide mart with a dozen+ date
# columns (e.g. per-category open/close dates) has no single obvious scan key, so leave it
# empty for the real partition/index pass rather than listing noise.
_MAX_DATE_FILTER_COLS = 6
# Above this row count we SKIP the COUNT(DISTINCT) grain-uniqueness probe. With no catalog
# PK, that probe full-scans the table once PER id-ish candidate column to find a unique one.
# On a 40M-row / 40GB fact table with a dozen *_id candidates and a composite (or absent)
# grain, that's a dozen fruitless full scans — tens of minutes of pure waste — to arrive at
# the empty grain that is the CORRECT answer for such a table anyway (its real grain is
# composite, which a single-column probe can't discover). A catalog PK is always honored
# regardless of size; this guard only governs the scan-heavy fallback. Tuned to clear all
# ordinary dimension/operational tables while catching the giant fact tables.
_GRAIN_PROBE_MAX_ROWS = 5_000_000

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


class _Row(dict):
    """Case-insensitive row lookup that preserves original key casing.

    Catalog metadata queries read fixed lowercase keys (``r["column_name"]``),
    but uppercasing dialects (Snowflake, Oracle, …) return the header as
    ``COLUMN_NAME``. Lookups here fold case so the engine finds them, while
    iteration / ``.keys()`` still yields the original casing — probe mode reads
    real column names off ``.keys()`` and must keep the DB's true casing.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ci = {k.lower(): k for k in self.keys()}

    def __getitem__(self, key):
        if super().__contains__(key):  # exact hit first (preserves dict semantics)
            return super().__getitem__(key)
        return super().__getitem__(self._ci[key.lower()])

    def __contains__(self, key):
        return super().__contains__(key) or key.lower() in self._ci

    def get(self, key, default=None):
        return self[key] if key in self else default


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
        return [_Row(r) for r in csv.DictReader(io.StringIO(proc.stdout))]

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
    exclude_columns: Optional[list[str]] = None,
    dry_run: bool = False,
    bigquery_region: str = "region-us",
) -> tuple[Organization, IntrospectReport]:
    """Introspect a live DB into the semantic model and (unless dry_run) write the
    canonical tree. `tables` (optional) is the allowlist of `schema.table` to build —
    the prune step's kept set (also the no-catalog case where enumeration is denied).
    `exclude_columns` (optional, `schema.table.column`) marks those columns excluded
    (the prune step's dropped columns).
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

    # 2b. apply prune-step column exclusions (the user dropped these in the prune
    # view). Match on schema.table.column, with a schema-less table.column fallback.
    # Excluded columns are marked `rejected` so they're kept for audit but off the
    # runtime — same state the model explorer's "exclude columns" produces.
    if exclude_columns:
        ex = set(exclude_columns)
        for t in built:
            for c in t.columns:
                if (f"{t.schema_name}.{t.name}.{c.name}" in ex
                        or f"{t.name}.{c.name}" in ex):
                    c.review_state = "rejected"

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
        storage_config={"profile": profile, "credentials_ref": "<artifacts_dir>/local/credentials"},
    )
    org = Organization(
        organization=profile,
        version=1,
        storage_connections=[storage],
        subject_areas=areas,
        cross_subject_area_relationships=cross,
    )

    # 5. write (backing up any legacy model at the profile root)
    if out == artifacts_dir / profile and not dry_run:
        _backup_legacy_model(out)
    wr = build.write_tree(org, out, dry_run=dry_run)
    report.files_written = wr.files_written
    return org, report


def discover_inventory(
    profile: str,
    db_type: str,
    *,
    runner: Runner,
    tables: Optional[list[str]] = None,
    schemas: Optional[list[str]] = None,
    bigquery_region: str = "region-us",
) -> dict:
    """Cheap first-pass discovery for the prune UI: every (schema, table) and its
    columns — and NOTHING expensive. No grain count-distincts, no FK-overlap probes,
    no row-count scans, no date sniffing. The user prunes the returned table list and
    the kept set then flows to `introspect(..., tables=<kept>)` for the full build.

    `schemas` (optional) restricts discovery to those schemas (the user's 1.3 schema
    pick) so a 50-schema warehouse isn't fully enumerated just to prune.

    Returns a JSON-serializable inventory:
        {profile, db_type, schemas:[...], table_count, column_mode,
         tables:[{schema, table, columns:[{name, type}]}, ...]}
    """
    dialect = D.get_dialect(db_type)
    if isinstance(dialect, D.BigQuery):
        dialect = D.BigQuery(region=bigquery_region)

    report = IntrospectReport(profile=profile, db_type=db_type, out_dir="", dry_run=True)
    pairs = _discover_tables(dialect, runner, tables, report)
    if schemas:
        keep = {s for s in schemas}
        pairs = [(s, t) for s, t in pairs if s in keep]
    if not pairs:
        raise RuntimeError(
            "no tables discovered. If the catalog is locked down, pass an explicit "
            "table allowlist (schema.table) so the prune view can describe them."
        )

    # One bulk catalog read for all columns where the dialect supports it (so a
    # 500-table DB is a single round-trip), else per-table.
    schemas = sorted({s for s, _ in pairs if s})
    cols_by_tt: dict[tuple[Optional[str], str], list[dict]] = {}
    used_bulk = False
    bulk_sql = dialect.sql_columns_bulk(schemas) if schemas else None
    if bulk_sql:
        rows = _try(runner, bulk_sql)
        if rows:
            used_bulk = True
            for r in rows:
                key = (r.get("table_schema"), r.get("table_name"))
                scale = r.get("numeric_scale")
                scale_i = int(scale) if scale not in (None, "", "NULL") else None
                ctype = dialect.map_type(r.get("data_type", ""), numeric_scale=scale_i)
                cols_by_tt.setdefault(key, []).append({"name": r["column_name"], "type": ctype})

    inv_tables: list[dict] = []
    column_mode = "catalog-bulk" if used_bulk else "per-table"
    for schema, table in pairs:
        cols = cols_by_tt.get((schema, table))
        if cols is None:
            # bulk missed this table (or no bulk support) → per-table catalog/probe read
            built, cmode = _columns(dialect, runner, schema, table)
            cols = [{"name": c.name, "type": c.type} for c in built]
            if not used_bulk:
                column_mode = cmode
        inv_tables.append({"schema": schema, "table": table, "columns": cols})

    return {
        "profile": profile,
        "db_type": db_type,
        "schemas": report.schemas or schemas,
        "table_count": len(inv_tables),
        "column_mode": column_mode,
        "tables": inv_tables,
    }


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _try(runner: Runner, sql: str) -> Optional[list[dict]]:
    try:
        return runner(sql)
    except Exception:
        return None


# Supabase (hosted Postgres) ships system schemas the user almost never wants modeled — and
# some hold secrets (vault, pgsodium). We detect Supabase by its signature schemas and drop
# the whole system set, so onboarding models only the user's app tables (e.g. public).
_SUPABASE_SYS_SCHEMAS = frozenset({
    "auth", "storage", "vault", "realtime", "_realtime", "extensions",
    "graphql", "graphql_public", "pgsodium", "pgsodium_masks", "pgbouncer",
    "supabase_functions", "supabase_migrations", "net", "cron", "pgtle",
    "_analytics", "_supavisor", "supabase_admin",
})
# Require several signature schemas together — a plain-Postgres DB with a lone `auth` or
# `storage` schema of its own is NOT Supabase and must not be filtered.
_SUPABASE_SIGNATURE = ("auth", "storage", "extensions", "realtime", "vault")


def _filter_supabase_system_schemas(schemas: list[str], report: IntrospectReport) -> list[str]:
    low = {s.lower() for s in schemas}
    if sum(sig in low for sig in _SUPABASE_SIGNATURE) < 3:
        return schemas  # not Supabase — leave every schema as-is
    kept = [s for s in schemas if s.lower() not in _SUPABASE_SYS_SCHEMAS]
    skipped = [s for s in schemas if s.lower() in _SUPABASE_SYS_SCHEMAS]
    if skipped:
        report.notes.append(
            "Supabase detected — skipped its system schemas (kept your app schemas): "
            + ", ".join(sorted(skipped)) + ". Pass an explicit --tables allowlist to include any."
        )
    return kept


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
        report.schemas = _filter_supabase_system_schemas(
            [r["schema_name"] for r in schemas], report)
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

    # Estimate row count ONCE up front (a catalog stat — instant, zero scan). Reused for
    # the grain-probe size guard below AND for performance_hints, so we never fetch twice.
    est_rows = _estimate_rows(dialect, runner, schema, table)

    # grain: catalog PKs, else uniqueness probe (skipped on very large tables — see _grain)
    grain, gmode = _grain(dialect, runner, schema, table, cols, est_rows=est_rows)
    report.mode_per_capability.setdefault("grain", gmode)
    if gmode == "probe_skipped_large":
        report.notes.append(
            f"{table}: skipped grain probe (~{est_rows:,} rows > "
            f"{_GRAIN_PROBE_MAX_ROWS:,}) — left grain empty rather than full-scanning "
            f"each id column. A composite/absent grain is expected for a fact table this "
            f"size; set it by hand if you know the key."
        )
    pk_set = set(grain)
    for c in cols:
        c.primary_key = c.name in pk_set
        # column-intrinsic aggregation class (additive / averageable / dimension / unknown).
        # Keys are dimensions; the rest is a name+type heuristic the curator can refine.
        c.aggregation = build.classify_aggregation(c.name, c.type, is_key=c.primary_key)
        if build.detect_sensitive(table, c.name):
            c.sensitive = True
            report.sensitive_columns += 1

    # One live sample → date encoding/timezone AND choice_field skeletons for low-cardinality
    # coded columns (so catalog-mode coded columns get an enum skeleton the LLM labels, not
    # just the probe-mode path).
    _enrich_from_sample(dialect, runner, schema, table, cols)

    column_groups = build.maybe_column_groups(cols)
    if column_groups:
        report.deep_tables.append(table)

    perf = None
    if est_rows is not None:
        perf = PerformanceHints(
            estimated_row_count=est_rows,
            estimated_row_count_at=_NOW,  # so the receipt can show "estimated as of <date>"
        )

    # On a large table, seed `recommended_filters` with the date/time columns — the natural
    # way to narrow a scan. The query path's scan-risk warning then fires only when a query
    # lacks a filter on one of these (and can name the column). Real index / partition /
    # clustering keys are layered in per-dialect separately.
    if perf and (perf.estimated_row_count or 0) >= _LARGE_TABLE_ROWS:
        date_cols = [c.name for c in cols if c.type in ("date", "timestamp", "time")]
        if 1 <= len(date_cols) <= _MAX_DATE_FILTER_COLS:
            perf.recommended_filters = date_cols

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


def _estimate_rows(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str
) -> Optional[int]:
    """Row count from the catalog statistics (pg_class.reltuples, etc.) — instant, no scan.
    None when the dialect has no estimate query or the stat is missing/unparseable."""
    q = dialect.sql_row_estimate(schema, table)
    if not q:
        return None
    est = _try(runner, q)
    if not est or est[0].get("estimated_rows") in (None, ""):
        return None
    try:
        return int(float(est[0]["estimated_rows"]))
    except (ValueError, TypeError):
        return None


def _grain(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str, cols: list[Column],
    *, est_rows: Optional[int] = None,
) -> tuple[list[str], str]:
    pks = _try(runner, dialect.sql_primary_keys(schema, table))
    if pks:
        return [r["column_name"] for r in pks], "catalog"
    # Size guard: the probe below COUNT(DISTINCT)-scans the table once per candidate. On a
    # huge fact table with no catalog PK that's tens of minutes of scans yielding nothing —
    # so skip it above the threshold and report an empty grain (the correct answer here; the
    # real grain is composite, beyond a single-column probe). See _GRAIN_PROBE_MAX_ROWS.
    if est_rows is not None and est_rows >= _GRAIN_PROBE_MAX_ROWS:
        return [], "probe_skipped_large"
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

    tables_by_name: dict[str, list[Table]] = {}
    for t in tables:
        tables_by_name.setdefault(t.name, []).append(t)

    rels: list[Relationship] = []
    if catalog_ok and catalog_fks:
        report.mode_per_capability["relationships"] = "catalog"
        for fk in catalog_fks:
            ft, fc, tt, tc = fk.get("from_table"), fk.get("from_column"), fk.get("to_table"), fk.get("to_column")
            if not (ft and fc and tt and tc):
                continue
            # Schema each endpoint lives in: the dialect's FK query supplies it on schema-ful
            # DBs (Postgres/MySQL); otherwise resolve it from the table list (same-schema first).
            from_schema = fk.get("from_schema") or _schema_of(ft, tables_by_name)
            to_schema = fk.get("to_schema") or _schema_of(tt, tables_by_name, prefer=from_schema, report=report)
            cross = bool(from_schema and to_schema and from_schema != to_schema)
            card = build.infer_cardinality(ft, tt, [fc], [tc], grain_by_table)
            # declared-but-unenforced FKs (Redshift/Databricks/Trino) -> confirm by overlap
            confidence = "confirmed" if dialect.fk_enforced else "inferred"
            # enforced-FK joins are trustworthy structure -> auto-approve with a system
            # sign-off (don't flood the review queue); unenforced/probed ones need a glance.
            # EXCEPTION: a join that spans two schemas is an architectural claim — the model now
            # reaches across namespaces — so surface it for review even when the FK is enforced.
            desc = ""
            if dialect.fk_enforced and not cross:
                kw: dict = {"review_state": "approved", "signed_off_by": "agami_introspect",
                            "signed_off_role": "system", "signed_off_at": _NOW}
            else:
                kw = {"review_state": "unreviewed"}
            if cross:
                desc = f"crosses schemas: {from_schema} → {to_schema} (declared FK) — confirm it belongs in this model"
                report.notes.append(f"cross-schema relationship: {from_schema}.{ft} → {to_schema}.{tt} (declared FK)")
            rels.append(Relationship(
                from_table=ft, from_column=fc, to_table=tt, to_column=tc,
                from_schema=from_schema, to_schema=to_schema,
                relationship=card, join_type="LEFT", confidence=confidence, description=desc, **kw,
            ))
    else:
        # probe: infer FKs from name+type match, confirm by value-overlap
        report.mode_per_capability["relationships"] = "probe"
        rels.extend(_probe_relationships(dialect, runner, tables, grain_by_table, report))

    # Tier 2 — name-based reference promotion. A `<x>_id` column whose target table is
    # identifiable by name becomes an inferred/unreviewed join even WITHOUT value overlap
    # (which under-detects on sparse data). Conservative + type-checked, and `unreviewed` so
    # it's discoverable but never asserted as fact — the user signs it off (or it self-approves
    # through use). This is what lifts a sparse demo from ~9 joins toward the real graph.
    rels.extend(_promote_reference_fields(tables, grain_by_table, rels, report))
    return rels


_REF_ID_RE = re.compile(r"^(.+?)_id$", re.IGNORECASE)


def _infer_reference_target(stem: str, by_name: dict, from_table: Table) -> Optional[Table]:
    """Find the table a `<stem>_id` column points at — by exact name, plural, or singular.
    Skips self-reference and prefers the from-table's own schema."""
    cands = (by_name.get(stem) or by_name.get(stem + "s") or by_name.get(stem + "es")
             or (by_name.get(stem[:-1]) if stem.endswith("s") else None) or [])
    cands = [t for t in cands if t.name != from_table.name]
    if not cands:
        return None
    same = [t for t in cands if t.schema_name == from_table.schema_name]
    return (same or cands)[0]


def _promote_reference_fields(
    tables: list[Table], grain_by_table: dict[str, set[str]],
    existing: list[Relationship], report: IntrospectReport,
) -> list[Relationship]:
    """Promote `<x>_id` reference columns to inferred/unreviewed joins when the target table is
    identifiable by name and key-type-compatible — never requiring value overlap. Conservative:
    target must exist, have a single-column grain, and a key whose type matches the ref column."""
    by_name: dict[str, list[Table]] = {}
    for t in tables:
        by_name.setdefault(t.name.lower(), []).append(t)
    # schema-aware key so a same-named table in another schema doesn't suppress promotion here
    seen = {(r.from_schema, r.from_table, r.from_column) for r in existing}
    out: list[Relationship] = []
    for t in tables:
        grain = set(t.grain)   # THIS table's key (grain_by_table is keyed by bare name → ambiguous)
        for c in t.columns:
            mo = _REF_ID_RE.match(c.name)
            if not mo or c.primary_key or c.name in grain or (t.schema_name, t.name, c.name) in seen:
                continue
            target = _infer_reference_target(mo.group(1).lower(), by_name, t)
            if target is None or len(target.grain) != 1:
                continue
            to_col = target.grain[0]
            to_pk = next((tc for tc in target.columns if tc.name == to_col), None)
            if to_pk is not None and to_pk.type != c.type:   # key-type mismatch → not a real ref
                continue
            card = build.infer_cardinality(t.name, target.name, [c.name], [to_col], grain_by_table)
            out.append(Relationship(
                from_table=t.name, from_column=c.name, to_table=target.name, to_column=to_col,
                from_schema=t.schema_name, to_schema=target.schema_name,
                relationship=card, join_type="LEFT", confidence="inferred", review_state="unreviewed",
                description=f"inferred reference: {c.name} → {target.name}.{to_col} "
                            "(name match; not overlap-verified — confirm)",
            ))
            seen.add((t.schema_name, t.name, c.name))
    if out:
        report.notes.append(
            f"promoted {len(out)} reference field(s) (<x>_id) to inferred joins — review on /agami-model")
    return out


def _schema_of(
    name: str, tables_by_name: dict[str, list[Table]],
    prefer: Optional[str] = None, report: Optional[IntrospectReport] = None,
) -> Optional[str]:
    """The schema a bare table name lives in. When the name exists in several schemas,
    prefer `prefer` (the other endpoint's schema) so a same-schema FK resolves correctly;
    otherwise pick deterministically and note the ambiguity for the user to disambiguate."""
    schemas = sorted({t.schema_name for t in tables_by_name.get(name, []) if t.schema_name})
    if len(schemas) == 1:
        return schemas[0]
    if len(schemas) > 1:
        if prefer and prefer in schemas:
            return prefer
        if report is not None:
            report.notes.append(
                f"ambiguous table name {name!r} across schemas {schemas} — picked {schemas[0]}; "
                "edit the relationship if it should point elsewhere")
        return schemas[0]
    return None


def _pick_target(cands: list[Table], prefer: Optional[str]) -> Optional[Table]:
    """Choose the actual target Table for a probed join, preferring the from-table's own
    schema so a `users` in two schemas doesn't bind to the wrong one (the old bare-name dict
    silently kept whichever was inserted last)."""
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    for t in cands:
        if t.schema_name == prefer:
            return t
    return sorted(cands, key=lambda t: (t.schema_name or ""))[0]


def _probe_relationships(
    dialect: D.Dialect, runner: Runner, tables: list[Table],
    grain_by_table: dict[str, set[str]], report: IntrospectReport,
) -> list[Relationship]:
    tables_by_name: dict[str, list[Table]] = {}
    for t in tables:
        tables_by_name.setdefault(t.name, []).append(t)
    # candidate target keys: single-column grains. (grain_by_table is bare-name-keyed, so its
    # key column is best-effort under a cross-schema name clash — the actual target Table is
    # resolved schema-aware below, which is what the overlap probe + schema stamp depend on.)
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
                    tgt = _pick_target(tables_by_name.get(tgt_name, []), prefer=t.schema_name)
                    if tgt is None:
                        continue
                    if _overlaps(dialect, runner, t, c.name, tgt, tgt_col):
                        cross = bool(t.schema_name and tgt.schema_name and t.schema_name != tgt.schema_name)
                        desc = "inferred by name+type match + value-overlap probe"
                        if cross:
                            desc += f"; crosses schemas {t.schema_name} → {tgt.schema_name}"
                            report.notes.append(
                                f"cross-schema relationship: {t.schema_name}.{t.name} → "
                                f"{tgt.schema_name}.{tgt_name} (inferred)")
                        rels.append(Relationship(
                            from_table=t.name, from_column=c.name,
                            to_table=tgt_name, to_column=tgt_col,
                            from_schema=t.schema_name, to_schema=tgt.schema_name,
                            relationship=build.infer_cardinality(
                                t.name, tgt_name, [c.name], [tgt_col], grain_by_table),
                            join_type="LEFT", confidence="proposed", review_state="unreviewed",
                            description=desc,
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


# Column names that suggest a date/time value (gates epoch/yyyymmdd detection so a
# random 10-digit id isn't mistaken for a Unix timestamp).
_DATE_NAME_RE = re.compile(
    r"(^|_)(date|time|datetime|timestamp|ts|epoch|created|updated|modified|deleted|"
    r"expir\w*|scheduled|started|ended|completed|occurred|dob|birth\w*)($|_)"
    r"|_at$|_on$|_dt$|_ts$|_time$|_date$", re.I)
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?")
_OFFSET_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")
_INT_ONLY_RE = re.compile(r"-?\d+$")
# (date_format, low, high) epoch ranges by scale — ~2001..2286 for seconds, ×1000 each step
_EPOCH_RANGES = (
    ("epoch_s", 1_000_000_000, 9_999_999_999),
    ("epoch_ms", 1_000_000_000_000, 9_999_999_999_999),
    ("epoch_us", 1_000_000_000_000_000, 9_999_999_999_999_999),
    ("epoch_ns", 1_000_000_000_000_000_000, 9_999_999_999_999_999_999),
)


def _looks_time_named(name: str) -> bool:
    return bool(_DATE_NAME_RE.search(name or ""))


def _sniff_date(name: str, ctype: str, values: list) -> tuple[Optional[str], Optional[str]]:
    """Sniff a column's date storage encoding + timezone from sample values, returning
    (date_format, timezone). Conservative — an encoded date (epoch/yyyymmdd/iso) is only
    claimed for a time-named column whose sample values all fit the shape:
      - native date/timestamp/time → date_format None (DB returns it readable); tz only
        if the sample carries an offset;
      - integer epoch in a plausible range (time-named) → epoch_s/ms/us/ns, tz UTC;
      - integer 20240115 (time-named) → yyyymmdd;
      - ISO-8601 strings (time-named) → iso8601, tz offset-aware if an offset is present.
    """
    vals = [str(v).strip() for v in values if v not in (None, "")][:50]
    if not vals:
        return (None, None)
    if ctype in ("date", "timestamp", "time"):
        if ctype == "timestamp" and any(_OFFSET_RE.search(v) for v in vals):
            return (None, "offset-aware")
        return (None, None)
    if not _looks_time_named(name):
        return (None, None)
    if all(_INT_ONLY_RE.match(v) for v in vals):
        ints = [int(v) for v in vals]
        if all(19000101 <= i <= 29991231 and 1 <= (i // 100) % 100 <= 12 and 1 <= i % 100 <= 31
               for i in ints):
            return ("yyyymmdd", None)
        for fmt, lo, hi in _EPOCH_RANGES:
            if all(lo <= i <= hi for i in ints):
                return (fmt, "UTC")
        return (None, None)
    if all(_ISO_RE.match(v) for v in vals):
        tz = "offset-aware" if any(_OFFSET_RE.search(v) for v in vals) else None
        return ("iso8601", tz)
    return (None, None)


def _enrich_from_sample(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str, cols: list[Column]
) -> None:
    """ONE live sample → (a) date_format/timezone on date-candidate columns, and
    (b) a `choice_field` skeleton `{value: ""}` on low-cardinality CODED columns (the LLM
    enrichment fills the labels). (b) is the catalog-mode counterpart of probe mode's
    `_maybe_choice` — without it, a catalog DB's coded columns (e.g. a ServiceNow integer
    `severity`) never get an enum skeleton, so the decode can only ever live in prose."""
    date_cands = [c for c in cols if c.type in ("date", "timestamp", "time")
                  or (c.type in ("integer", "decimal", "string") and _looks_time_named(c.name))]
    # choice candidates: codeable columns with no choice_field yet, that aren't keys, dates,
    # or free-text-ish. A short integer/string column with few distinct values is an enum.
    # Exclude reference-named `*_id`/`sys_id` columns by NAME: introspection never populates
    # Column.foreign_key, so that guard alone wouldn't catch a low-cardinality `dealer_id`
    # (few dealers) — which is a JOIN target, not an enum to decode.
    choice_cands = [c for c in cols
                    if c.choice_field is None and not c.primary_key and c.foreign_key is None
                    and c.type in ("integer", "string") and not _looks_time_named(c.name)
                    and not _REF_ID_RE.match(c.name)]
    if not date_cands and not choice_cands:
        return
    sample = _try(runner, dialect.sample_sql(schema, table, SAMPLE_ROWS)) or []
    if not sample:
        return
    for c in date_cands:
        df, tz = _sniff_date(c.name, c.type, [row.get(c.name) for row in sample])
        if df:
            c.date_format = df
        if tz:
            c.timezone = tz
    for c in choice_cands:
        ch = _maybe_choice([row.get(c.name) for row in sample])
        # only genuinely code-like: skip long free-text values (names, descriptions, ids).
        if ch and all(len(str(k)) <= 40 for k in ch):
            c.choice_field = ch


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


def _backup_legacy_model(profile_root: Path) -> None:
    """Move any legacy (v1) model artifacts at the profile root into .legacy_backup/ before
    writing the new tree, so re-onboarding never silently clobbers the old model."""
    legacy = ["index.yaml"]
    backup = profile_root / ".legacy_backup"
    moved = False
    for name in legacy:
        src = profile_root / name
        if src.exists():
            backup.mkdir(parents=True, exist_ok=True)
            src.rename(backup / name)
            moved = True
    # per-schema legacy dirs contain a _schema.yaml; move those too
    if profile_root.exists():
        for child in list(profile_root.iterdir()):
            if child.is_dir() and (child / "_schema.yaml").exists():
                backup.mkdir(parents=True, exist_ok=True)
                child.rename(backup / child.name)
                moved = True
    return None


__all__ = ["introspect", "make_execute_sql_runner", "IntrospectReport", "Runner"]
