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
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

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
    Datasource,
    PerformanceHints,
    Relationship,
    StorageConnection,
    Table,
    bare_name,
    table_key,
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
# A list declared in the catalog (an enum type, a CHECK ... IN) is exact, so it is kept far past the
# cap on a list read from the data.
DECLARED_MAX_VALUES = 200
# Each value list read from the data is one DISTINCT query; this bounds them per run, so a wide model
# can't fire thousands.
DISTINCT_READ_CAP = 1000
PROBE_TABLE_CAP = 200  # cap value-overlap probing work
# Safety valve for confirming DECLARED-but-unenforced catalog FKs (Redshift/Databricks/Trino)
# by value-overlap: each confirmation is one sequential COUNT probe, so a schema that declares
# hundreds of FKs could otherwise fire hundreds of round-trips. Past the budget we stop probing
# and leave the remaining declared FKs inferred/unreviewed (they still surface for sign-off).
FK_OVERLAP_PROBE_CAP = 300
# Above this FROM-table row estimate we SKIP the value-overlap scan and leave the join
# unverified (inferred/unreviewed) rather than scan millions of rows over the network — the
# guard that stops a 50-table Redshift introspect from crawling for half an hour on big fact
# tables (incident/task/metric_instance). Mirrors the grain-probe size guard.
_OVERLAP_PROBE_MAX_ROWS = 3_000_000
# Per-run cap on the uniqueness probes that check a join's many side on the data (one
# COUNT(DISTINCT) each), on the same budget as the declared-FK overlap confirmation.
_UNIQUE_PROBE_CAP = FK_OVERLAP_PROBE_CAP
_DATA_UNIQUE_NOTE = "no constraint declares it — looks one-to-one, confirm"


def _progress(path: Optional[Path], msg: str) -> None:
    """Append a FLUSHED progress line so a tailing skill can surface a heartbeat during a long
    introspection (the engine is otherwise silent until it returns — which reads as 'stuck' on a
    big Redshift schema). Best-effort; never raises."""
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(msg.rstrip("\n") + "\n")
            fh.flush()
    except Exception:
        pass


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
    # Tables at or above the large-table line: name -> (estimated rows, its date columns).
    # Listed so whoever builds the model confirms the filters; the seed is every date column, and on
    # a warehouse that is often load timestamps nobody asks a question about.
    large_tables: dict[str, tuple[int, list[str]]] = field(default_factory=dict)
    sensitive_columns: int = 0
    notes: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    files_unchanged: int = 0
    # On a refresh of an existing model: what changed structurally, per table. Empty on a first build.
    changes: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    new_tables: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"# introspection report — {self.profile} ({self.db_type})",
            f"out_dir: {self.out_dir}   (dry_run={self.dry_run})",
            f"capability modes: {self.mode_per_capability}",
            f"schemas: {self.schemas}",
            f"tables: {self.table_count}   relationships: {self.relationship_count}",
            f"subject_areas ({len(self.subject_areas)}): {', '.join(self.subject_areas)}",
            *([f"new tables: {', '.join(self.new_tables)}"] if self.new_tables else []),
            *(f"changed {t}: " + "; ".join(f"{k} {', '.join(v)}" for k, v in c.items())
              for t, c in self.changes.items()),
            f"deep tables: {', '.join(self.deep_tables) or '(none)'}",
            *(f"large table {t}: about {rows:,} rows; date columns: {', '.join(cols) or '(none)'}"
              for t, (rows, cols) in self.large_tables.items()),
            f"sensitive columns: {self.sensitive_columns}",
            "## notes",
            *[f"  - {n}" for n in self.notes],
            f"## files ({len(self.files_written)} written, {self.files_unchanged} already up to date)",
            *[f"  {f}" for f in self.files_written[:60]],
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Default runner: shell to execute_sql.py, parse CSV
# ---------------------------------------------------------------------------


def make_execute_sql_runner(
    profile: str, python: Optional[str] = None, *, building_the_model: bool = False
) -> Runner:
    """A runner that sends each statement through `execute_sql.py`, and returns its rows.

    `building_the_model` is for the reads that MAKE the model: the catalog, and probing a table the
    model doesn't list yet. Those skip the semantic-model pass, which confines a statement to the
    tables an existing model declares, so once any model exists it refuses every one of them (#428):
    a second batch, a re-introspection and a new schema all came back with no readable columns. The
    always-on gate still runs, so these reads stay read-only. Everything that answers or validates
    against the model (seed examples, enrichment) keeps the default and is confined as before."""
    exe = python or sys.executable
    script = str(SCRIPT_DIR / "execute_sql.py")
    unscoped = ["--no-safety"] if building_the_model else []

    def run(sql: str) -> list[dict]:
        proc = subprocess.run(
            [exe, script, "--profile", profile, "--sql", sql, *unscoped],
            capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
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
    progress_path: Optional[str | Path] = None,
    append: bool = False,
) -> tuple[Datasource, IntrospectReport]:
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
    sg = _sqlglot_name(dialect)
    artifacts_dir = Path(artifacts_dir)
    out = Path(out_dir) if out_dir else (artifacts_dir / profile)

    report = IntrospectReport(profile=profile, db_type=db_type, out_dir=str(out), dry_run=dry_run)

    pp = Path(progress_path) if progress_path else None
    if pp is not None:
        try:
            pp.parent.mkdir(parents=True, exist_ok=True)
            pp.write_text("", encoding="utf-8")  # fresh log per run
        except Exception:
            pp = None

    # 1. discover (schema, table) pairs
    pairs = _discover_tables(dialect, runner, tables, report)
    if not pairs:
        raise RuntimeError(
            "no tables discovered. If the catalog is locked down, pass an explicit "
            "table allowlist (schema.table) so probe mode can describe them."
        )
    _progress(pp, f"discovered {len(pairs)} tables — reading columns, grain, keys")

    # 2. per-table columns + grain
    built: list[Table] = []
    _step = max(1, len(pairs) // 10)   # throttle the heartbeat to ~10 lines, not one per table
    budget = _CountBudget(_COUNT_BUDGET_S)
    reads = _ReadBudget(DISTINCT_READ_CAP)
    keys_unread: set[tuple] = set()   # (schema, table) whose unique keys couldn't be read
    for i, (schema, table) in enumerate(pairs, 1):
        t = _build_table(dialect, runner, schema, table, conn_name, report, budget, reads,
                         keys_unread)
        built.append(t)
        if i % _step == 0 or i == len(pairs):
            _progress(pp, f"columns+grain {i}/{len(pairs)}")
    if budget.skipped:
        report.notes.append(
            f"{budget.skipped} table(s) left unsized: exact row counts stopped after {_COUNT_BUDGET_S:.0f}s "
            "in total; build those tables in a separate run to size them")
    if reads.skipped:
        report.notes.append(
            f"{reads.skipped} column(s) got no value list: the run's {DISTINCT_READ_CAP} DISTINCT reads were "
            "used up; build those tables in a separate run to list their values")

    # Fail-fast on tables that yielded NO columns — a bogus allowlist entry (e.g. a malformed
    # --tables blob that mis-joined 52 names into one) describes nothing, and we must NOT persist
    # a garbage table. Drop them with a clear note; if that leaves nothing, raise rather than
    # write an empty model.
    refreshing = append and (out / "datasource.yaml").exists()
    unreadable = [t for t in built if not t.columns]   # on a refresh: possibly dropped by the database
    empty = [t.name for t in unreadable]
    if empty:
        built = [t for t in built if t.columns]
        report.notes.append(
            f"dropped {len(empty)} table(s) with no readable columns (not found / bad allowlist): "
            + ", ".join(empty[:8]) + (" …" if len(empty) > 8 else ""))
    if not built and not refreshing:
        raise RuntimeError(
            "no allowlisted table could be described — every name yielded zero columns. "
            "Check the --tables list (a shell-quoting issue can mis-join all names into one); "
            "or pass --tables-file with one schema.table per line.")

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

    # 2c. APPEND onto an existing model: a later onboarding batch, or a refresh after the database
    # changed. The existing model is the starting point, not something to rebuild: its tables are
    # folded back in from disk (never re-queried), and a batch table it already has is MERGED — new
    # columns added, dropped ones marked stale, changed types updated — so its descriptions, flags and
    # sign-offs survive (#437). Runs on a dry run too, which is how a refresh is previewed.
    prev: Optional[Datasource] = None
    prev_rels: list[Relationship] = []
    unread: list[Table] = []   # merged tables whose keys were kept, not re-read
    probe_budget = _ProbeBudget()   # one cap across the refresh's recheck and the new joins
    skip_keys: set = set()
    fresh_keys: set[tuple] = set()   # (schema, name): a bare name can exist in two schemas
    if refreshing:
        from .loader import load_datasource
        prev = load_datasource(out, include_rejected=True)
        # Two areas may each define the same table (one serving it, one excluding its copy), so every
        # area's copy is merged on its own and written back to its own area. `existing` holds one copy
        # per table — a served one when there is — for matching, joins and grain.
        copies: dict[tuple, list[tuple[str, Table]]] = {}
        for sa in prev.subject_areas:
            for t in sa.tables_defined:
                copies.setdefault((t.schema_name, t.name), []).append((sa.name, t))
        existing = {k: next((t for _, t in cs if t.review_state != "rejected"), cs[0][1]) for k, cs in copies.items()}
        per_area: dict[tuple, Table] = {}   # (area, (schema, table)) -> that area's merged copy
        merged: list[Table] = []
        retyped: dict[tuple, str] = {}   # (schema, table, column) whose type changed -> schema.table
        for t in built:
            _refuse_sql_table(existing, t)
            old = _existing_table(existing, t)
            if old is None:
                _refuse_ambiguous(existing, t)
                merged.append(t)
                fresh_keys.add((t.schema_name, t.name))
                report.new_tables.append(_qualified(t))
                continue
            keys_read = (t.schema_name, t.name) not in keys_unread
            if not keys_read:
                report.notes.append(f"unique keys not re-read for {_qualified(old)}: kept the stored ones")
            m, change = _merge_table(old, t, keys_read=keys_read)
            if not keys_read:
                unread.append(m)
            # Already large, so its filters were settled when the seed was first offered and the
            # merge keeps them: listing the fresh seed would ask again about a table already answered.
            if old.performance_hints and (old.performance_hints.estimated_row_count or 0) >= _LARGE_TABLE_ROWS:
                report.large_tables.pop(t.name, None)
            merged.append(m)
            for area, copy in copies[(old.schema_name, old.name)]:
                per_area[(area, (old.schema_name, old.name))] = (
                    m if copy is old else _merge_table(copy, t, keys_read=keys_read)[0])
            for r in change.get("retyped", []):
                retyped[((m.schema_name or "").lower(), m.name.lower(), r.split(":")[0].lower())] = _qualified(m)
            if change:
                report.changes[_qualified(m)] = change
        # A table the database no longer has. Only a successful catalog listing that leaves it out is
        # evidence of that: a named table that merely READ nothing may be a failed read (a network
        # blip, a revoked permission — `_columns` returns no columns for both), and marking it stale
        # would hide it and everything built on it. Such a table is left exactly as it was.
        read_schemas = {s for s, _ in pairs}
        gone_keys = set()
        for t in unreadable:
            old = _existing_table(existing, t)
            if old is None:
                _refuse_ambiguous(existing, t)   # never treat an ambiguous name as merely unreadable
            else:
                report.notes.append(
                    f"couldn't read {_qualified(old)} — left unchanged. If the database dropped it, "
                    "refresh without --tables so the catalog listing can confirm it")
        if tables is None:
            # Listed by the catalog (a stored table with no schema matches any listed schema) — the
            # listing, not a read, decides; a table in a schema this run didn't list is never touched.
            listed = {(sch, tbl.lower()) for sch, tbl in pairs}

            def gone(t: Table) -> bool:
                if t.source_type == "sql":
                    return False
                if t.schema_name is None:
                    return bool(read_schemas) and not any(tbl == t.name.lower() for _, tbl in listed)
                return t.schema_name in read_schemas and (t.schema_name, t.name.lower()) not in listed

            gone_keys |= {k for k, t in existing.items() if gone(t)}
        for key in gone_keys:
            old = existing[key]
            if old.review_state not in ("stale", "rejected"):
                existing[key] = old.model_copy(update={"review_state": "stale"})
                report.changes[_qualified(old)] = {"table": ["no longer in the database"]}
            for area, copy in copies[key]:
                if copy.review_state not in ("stale", "rejected"):
                    per_area[(area, key)] = copy.model_copy(update={"review_state": "stale"})
        # A new table carries the model's own connection name, not the one this run generated (they
        # differ once a model's connection was renamed); entity resolution reads the table's field.
        model_conn = prev.storage_connections[0].name if prev.storage_connections else conn_name
        merged = [t if (t.schema_name, t.name) not in fresh_keys else t.model_copy(update={"storage_connection": model_conn})
                  for t in merged]
        conn_name = model_conn
        built = merged
        batch_keys = {(t.schema_name, t.name) for t in built}
        for key, t in existing.items():
            if key not in batch_keys:        # keep prior tables (don't re-query)
                built.append(t)
        for sa in prev.subject_areas:
            prev_rels.extend(sa.relationships)
        prev_rels.extend(prev.cross_subject_area_relationships)
        # Joins on a column whose type changed go back to review: a confirmed join on mismatched
        # types is refused by the validator, which would make the refresh impossible to write.
        for r in prev_rels:
            hit = next((h for fc, tc in _column_pairs(r, sg) or []
                        for h in (_retyped_end(retyped, r.from_schema, r.from_table, fc),
                                  _retyped_end(retyped, r.to_schema, r.to_table, tc)) if h), None)
            # The validator type-checks only the column-pair form; an `on:` join someone wrote,
            # signed or edited (stale) is theirs, so only one introspection itself signed goes back.
            if hit and r.confidence == "confirmed" and (
                    not r.on or (r.signed_off_role == "system" and r.review_state != "stale")):
                r.confidence = "proposed"
                # A stale join is already back in review, and `stale` is what says a person changed it.
                if r.review_state != "stale":
                    r.review_state = "unreviewed"
                report.changes.setdefault(hit, {}).setdefault("joins to review", []).append(_join_label(r, sg))

    # 3. relationships (catalog FKs, else probe by name+type+overlap). Use the FULL built set's
    # (schema, table) pairs so an --append batch builds relationships across the whole union, not
    # just its own batch (covers every schema present).
    _progress(pp, f"building relationships ({len(built)} tables; FK + value-overlap probes)…")
    rel_pairs = [(t.schema_name, t.name) for t in built]
    rels = _build_relationships(dialect, runner, rel_pairs, built, report, pp=pp)
    if prev_rels:
        # A database key over several columns replaces the engine's own split joins for it (#510).
        dropped, rels = _merge_split_joins(rels, prev_rels, report, sg)
        if dropped:
            prev = _without_joins(prev, dropped)
            prev_rels = [r for r in prev_rels if id(r) not in dropped]
        # After the merge, so a split join about to be replaced isn't also listed as relabelled.
        _recheck_engine_joins(dialect, runner, prev_rels, built, report, probe_budget, unread, sg)
        # keep prior edges; add only genuinely-new ones (dedup by endpoint+column)
        rels = [r for r in rels if not any(_same_join(r, p, sg) for p in prev_rels)]
    _suggest_data_unique(dialect, runner, rels, built, report, probe_budget)
    if prev_rels:
        rels = list(prev_rels) + rels
    if refreshing:   # counted after the join merge and relabel, which are changes too
        report.notes.append(
            f"append: {len(report.new_tables)} new table(s), {len(report.changes)} changed, "
            f"{len(built)} total")
    report.table_count = len(built)
    report.relationship_count = len(rels)
    _progress(pp, f"done: {len(built)} tables, {len(rels)} relationships")

    # 3b. now that joins are known, re-derive deep-table column_groups so FK (reference) columns
    # group under `references` instead of scattering through `misc` — references live on the
    # Relationship, not on the column, so the initial per-table grouping couldn't see them.
    if prev is not None and _areas_untouched(prev, out):
        # A batched first build: the areas so far are only the engine's proposal for earlier batches,
        # so propose again over the union, exactly as a one-shot build would.
        prev_for_areas: Optional[Datasource] = None
    else:
        prev_for_areas = prev
    if prev_for_areas is None:
        # Every table, as a one-shot build would: a later batch's join can reveal an FK column in a
        # table an earlier batch built, and nobody has curated the grouping yet.
        _regroup_columns_with_references(built, rels, sg)
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
        org = Datasource(
            datasource=profile,
            version=1,
            storage_connections=[storage],
            subject_areas=areas,
            cross_subject_area_relationships=cross,
        )
        if prev is not None:   # keep what the datasource already carries (description, ids, …)
            org = prev.model_copy(update={"subject_areas": areas, "cross_subject_area_relationships": cross})
    else:
        # Regroup only the tables this run created: an existing table's column groups are curation.
        _regroup_columns_with_references([t for t in built if (t.schema_name, t.name) in fresh_keys], rels, sg)
        org = _refresh_existing(prev, built, rels[len(prev_rels):], fresh_keys, profile, report, per_area)

    # 5. write (backing up any legacy model at the profile root). An append onto an existing model is
    # validated first: writing an invalid merge would replace a curated model with a broken one.
    if prev is not None and not dry_run:
        from . import validator as V
        unreadable = build.unloadable(org)
        if unreadable:
            report.notes.insert(0, "NOT WRITTEN: the merged model wouldn't load once written "
                                   f"({unreadable[0]}); the existing model is unchanged")
            dry_run = True
            report.dry_run = True
        elif not V.validate(org).ok:
            report.notes.insert(0, "NOT WRITTEN: the merged model doesn't validate; the existing model is unchanged")
            dry_run = True
            report.dry_run = True
    if out == artifacts_dir / profile and not dry_run:
        _backup_legacy_model(out)
    if prev is not None and prev_for_areas is None and not dry_run:
        _remove_moved_table_files(org, out)
    wr = build.write_tree(org, out, dry_run=dry_run)
    report.files_written = wr.files_written
    report.files_unchanged = len(wr.files_unchanged)
    return org, report


def _retyped_end(retyped: dict[tuple, str], schema: Optional[str], table: str, column: Optional[str]) -> Optional[str]:
    """The retyped table (as `schema.table`) a join end names, or None. Schemas must agree where the
    join states one, so a change to `sales.orders.customer_id` leaves `archive.orders`' join alone."""
    if not column:
        return None
    if not schema and "." in table:
        schema, table = table.rsplit(".", 1)
    t, c = bare_name(table).lower(), column.lower()
    for (s, rt, rc), q in retyped.items():
        if rt == t and rc == c and (not schema or not s or s == schema.lower()):
            return q
    return None


def _same_join(a: Relationship, b: Relationship, dialect: Optional[str] = None) -> bool:
    """Whether two joins are the same edge, for de-duplicating an append. Both ends' tables (bare,
    whatever `schema.table` spelling was stored) and column pairs must match; schemas must agree where
    both joins state one — so `crm.products` isn't taken for `billing.products` — while a join
    stored without a schema (curation may omit it) still matches the catalog's copy of itself.
    An `on:` join compares by the column pairs it reads (`dialect` is the engine's sqlglot name),
    and by its text when it isn't plain column equalities."""
    return _same_tables(a, b) and _pair_set(a, dialect) == _pair_set(b, dialect)


def _same_tables(a: Relationship, b: Relationship) -> bool:
    """Whether two joins connect the same two tables. A schema embedded in the table name
    (`crm.shipments`) counts as stated, so it isn't matched to `billing.shipments`."""
    def same(x: tuple[str, str], y: tuple[str, str]) -> bool:
        return x[1] == y[1] and (not x[0] or not y[0] or x[0] == y[0])

    return (same(table_key(a.from_table, a.from_schema), table_key(b.from_table, b.from_schema))
            and same(table_key(a.to_table, a.to_schema), table_key(b.to_table, b.to_schema)))


def _pair_set(r: Relationship, dialect: Optional[str]) -> frozenset:
    pairs = _key_pairs(r, dialect)
    if pairs is None:
        return frozenset({" ".join((r.on or "").lower().split())})
    return frozenset((f.lower(), t.lower()) for f, t in pairs)


def _key_pairs(r: Relationship, dialect: Optional[str]) -> Optional[list[tuple[str, str]]]:
    from .runtime import join_key_pairs
    return join_key_pairs(r, dialect)


def _column_pairs(r: Relationship, dialect: Optional[str]) -> Optional[list[tuple[str, str]]]:
    """The columns a join is keyed on, passing over a fixed-value condition (`x.flag = 'Y'`)."""
    from .runtime import join_column_pairs
    return join_column_pairs(r, dialect)


def _sqlglot_name(dialect: D.Dialect) -> Optional[str]:
    """The sqlglot grammar for this engine's model text (an `on:` join), or None when unmapped."""
    from .sql_dialect import DialectUnresolved, sqlglot_dialect
    try:
        return sqlglot_dialect(dialect.name)
    except DialectUnresolved:
        return None


def _join_label(r: Relationship, dialect: Optional[str] = None) -> str:
    """How a refresh report names a join: `a.x -> b.y`, or every column of a composite one."""
    pairs = _key_pairs(r, dialect)
    if pairs is None:
        return f"{r.from_table} -> {r.to_table} ON {r.on}"
    return (f"{r.from_table}.({', '.join(f for f, _ in pairs)}) -> {r.to_table}.({', '.join(t for _, t in pairs)})"
            if len(pairs) > 1 else f"{r.from_table}.{pairs[0][0]} -> {r.to_table}.{pairs[0][1]}")


_MERGED_JOINS = "joins merged into one composite join (the database's foreign key spans all their columns)"
_UNMERGED_JOINS = "composite foreign key not applied"


def _merge_split_joins(
    fresh: list[Relationship], prev_rels: list[Relationship], report: IntrospectReport,
    dialect: Optional[str],
) -> tuple[set[int], list[Relationship]]:
    """On a refresh, a freshly read composite foreign key against the model's joins between the same
    two tables whose columns are part of it — the split joins an older build wrote, each of which
    repeats rows used alone. When the engine approved every one of them they go and the composite
    join replaces them; when a person signed off any of them nothing changes for that key and the
    composite join is not added. Both are listed in the refresh report. Returns the ids of the
    model's joins to drop and the fresh joins to keep."""
    dropped: set[int] = set()
    kept: list[Relationship] = []
    for c in fresh:
        pairs = _key_pairs(c, dialect) if c.on else None
        want = {(f.lower(), t.lower()) for f, t in pairs or []}
        # A join the catalog still declares on its own (a single-column FK beside the composite
        # one) is not a split of this key: it stays.
        covered = [p for p in prev_rels
                   if len(want) > 1 and id(p) not in dropped and _same_tables(c, p)
                   and (got := _pair_set(p, dialect)) and got < want
                   and not any(_same_join(p, f, dialect) for f in fresh)]
        if not covered:
            kept.append(c)
            continue
        where = f"{c.from_schema}.{c.from_table}" if c.from_schema else c.from_table
        key = " AND ".join(f for f, _ in pairs)
        people = [p for p in covered if not _engine_approved(p)]
        if people:
            why = ", ".join(sorted({_not_merged_reason(p) for p in people}))
            report.changes.setdefault(where, {}).setdefault(_UNMERGED_JOINS, []).append(
                f"{c.from_table} → {c.to_table} ({key}): not changed: {why}")
            continue
        parts = " + ".join(f"({', '.join(f for f, _ in _key_pairs(p, dialect) or [])})" for p in covered)
        report.changes.setdefault(where, {}).setdefault(_MERGED_JOINS, []).append(
            f"{c.from_table} → {c.to_table}: {parts} → {key}")
        dropped.update(id(p) for p in covered)
        kept.append(c)
    return dropped, kept


def _not_merged_reason(p: Relationship) -> str:
    # A stale join still carries the signer of the approval it lost, so say it was edited instead.
    if p.review_state == "stale":
        return "edited since its approval"
    return f"signed off by {p.signed_off_by}" if p.signed_off_by else "not approved by introspection"


def _without_joins(org: Datasource, dropped: set[int]) -> Datasource:
    """`org` without the joins whose ids are in `dropped`, every other object shared."""
    areas = [sa.model_copy(update={"relationships": [r for r in sa.relationships if id(r) not in dropped]})
             for sa in org.subject_areas]
    cross = [r for r in org.cross_subject_area_relationships if id(r) not in dropped]
    return org.model_copy(update={"subject_areas": areas, "cross_subject_area_relationships": cross})


def _qualified(t: Table) -> str:
    """How a refresh report names a table: `schema.table`, so two same-named tables stay apart."""
    return f"{t.schema_name}.{t.name}" if t.schema_name else t.name


def _remove_moved_table_files(org: Datasource, out: Path) -> None:
    """After areas are re-proposed, delete each area folder's table files for tables it no longer
    defines. `write_tree` writes the new files but never removes old ones, and the loader reads every
    file in a listed area's folder, so a table that moved would be defined in two areas."""
    for sa in org.subject_areas:
        tdir = out / "subject_areas" / sa.name / "tables"
        keep = {f"{t.name}.yaml" for t in sa.tables_defined}   # write_tree's own file name
        for f in tdir.glob("*.yaml") if tdir.exists() else []:
            if f.name not in keep:
                f.unlink()


def _refuse_sql_table(existing: dict[tuple, Table], fresh: Table) -> None:
    """Stop when the database now has a table or view named like a SQL-defined table in the model.
    Merging the two would mark the SQL table's computed columns stale and retype the rest."""
    for t in existing.values():
        if t.source_type == "sql" and t.name.lower() == fresh.name.lower():
            raise RuntimeError(
                f"the model's {t.name!r} is defined by SQL, and the database now has a table or view "
                "with the same name; rename one of them, or leave it out of this refresh")


def _refuse_ambiguous(existing: dict[tuple, Table], fresh: Table) -> None:
    """Stop when a bare name matches several tables in the model: never guess which one is meant,
    and never add a third, unqualified copy of it."""
    if fresh.schema_name is not None:
        return
    same = sorted(f"{s}.{n}" for (s, n) in existing if n.lower() == fresh.name.lower())
    if len(same) > 1:
        raise RuntimeError(f"{fresh.name!r} matches {len(same)} tables in the model ({', '.join(same)}); "
                           "name the one to refresh as schema.table")


def _existing_table(existing: dict[tuple, Table], fresh: Table) -> Optional[Table]:
    """The model's table `fresh` is a new reading of: by (schema, name), or — for a bare name such as
    `--tables customers` against a stored `main.customers` — by name alone when exactly one matches."""
    hit = existing.get((fresh.schema_name, fresh.name))
    if hit is not None:
        return hit
    same = [t for (s, n), t in existing.items() if n.lower() == fresh.name.lower()]
    return same[0] if len(same) == 1 and (fresh.schema_name is None or same[0].schema_name is None) else None


def _refs(refs: list) -> list:
    """Table references as a comparable set: the loader reads table files in name order, while an
    area's reference list keeps the order it was written in, so order alone says nothing."""
    return sorted((repr(sorted(r.model_dump().items())) for r in refs))


def _areas_untouched(prev: Datasource, root: Path) -> bool:
    """Whether every area is EXACTLY what the engine generates for its tables, field for field.

    Only then is it safe for a later batch of a first build to re-propose the areas over the union
    (a one-shot build would have). Any difference — a description someone wrote (even one starting
    like the generated text), a default time window, an entity or metric, a table reference or its
    exposed column groups changed or added — means a person or a spec shaped it, and it is kept."""
    if prev.cross_subject_area_entities or prev.cross_subject_area_metrics or not prev.storage_connections:
        return False
    # Saved examples live under their area's name, and any curation leaves a log or a description:
    # a batched first build has none of these until it finishes, so their presence means "curated".
    from .curate import has_model_curation
    if has_model_curation(root) or any((root / "prompt_examples").rglob("*.yaml")):
        return False
    if any(t.description or any(c.description for c in t.columns)
           for sa in prev.subject_areas for t in sa.tables_defined):
        return False
    conn = prev.storage_connections[0].name
    for sa in prev.subject_areas:
        generated = build.make_area(sa.name, sa.tables_defined, [], conn)
        if (sa.description != generated.description or sa.default_time_window is not None
                or sa.entities or sa.metrics
                or _refs(sa.tables) != _refs(generated.tables)):
            return False
    return True


def _merge_table(old: Table, fresh: Table, *, keys_read: bool = True) -> tuple[Table, dict[str, list[str]]]:
    """`old` with the structure `fresh` read from the database, and the curation kept.

    A column the database still has keeps every field but its type; a new one arrives as
    introspection built it; one the database no longer has stays, marked `stale`, so a description or
    sign-off is never silently lost and a person decides. `keys_read` False keeps `old`'s unique keys,
    since a failed read is not the database dropping them. Returns the merged table and what changed."""
    fresh_by = {c.name.lower(): c for c in fresh.columns}
    old_names = {c.name.lower() for c in old.columns}
    cols: list[Column] = []
    change: dict[str, list[str]] = {"added": [], "dropped": [], "retyped": []}
    for c in old.columns:
        f = fresh_by.get(c.name.lower())
        if f is None:
            if c.review_state not in ("stale", "rejected"):
                change["dropped"].append(c.name)
                c = c.model_copy(update={"review_state": "stale"})
        else:
            if c.review_state == "stale":   # the database has it again
                change.setdefault("restored", []).append(c.name)
                c = c.model_copy(update={"review_state": "approved"})
            if f.type != c.type:
                change["retyped"].append(f"{c.name}: {c.type} -> {f.type}")
                c = _retyped(c, f, change)
        cols.append(c)
    added = [f for f in fresh.columns if f.name.lower() not in old_names]
    cols.extend(added)
    change["added"] = [f.name for f in added]
    groups = {k: list(v) for k, v in old.column_groups.items()}
    if groups:  # a deep table puts every column in a group, the new ones too
        for f in added:
            # Always a group the table already has: an area exposes groups by name, so a column put in
            # a group none of them lists would never be served.
            group = next((k for k, v in fresh.column_groups.items() if f.name in v), "misc")
            if group not in groups:
                group = "misc" if "misc" in groups else next(iter(groups))
            groups[group].append(f.name)
    cols, grain = _merged_key(old, fresh, cols, change)
    restored_table = old.review_state == "stale"
    if restored_table:
        change.setdefault("table", []).append("back in the database")
    unique_keys = _merged_unique_keys(fresh if keys_read else old, cols, change)
    if unique_keys != old.unique_keys:
        change.setdefault("unique keys", []).append(
            "; ".join(", ".join(k) for k in unique_keys) or "(none)")
    update = {"columns": cols, "column_groups": groups, "grain": grain, "unique_keys": unique_keys,
              "performance_hints": _merged_hints(old.performance_hints, fresh.performance_hints)}
    if restored_table:
        update["review_state"] = "approved"
    if not groups and len(cols) >= build.DEEP_TABLE_COLUMN_THRESHOLD:
        # It became deep: a deep table must group every column, the stale ones included.
        live = [c for c in cols if c.review_state != "stale"]
        groups = build.derive_column_groups(live) if live else {}
        stale = [c.name for c in cols if c.review_state == "stale"]
        if stale:
            groups.setdefault("misc", []).extend(stale)
        update["column_groups"] = groups
        update["column_group_descriptions"] = {**build.column_group_descriptions(groups), **old.column_group_descriptions}
    merged = old.model_copy(update=update)
    return merged, {k: v for k, v in change.items() if v}


def _merged_unique_keys(fresh: Table, cols: list[Column], change: dict) -> list[list[str]]:
    """`fresh`'s unique keys (the database's as it reports them now, or the stored ones when they
    couldn't be read), in the merged table's column spelling. A key on a dropped column is gone with it."""
    gone = {n.lower() for n in change["dropped"]}
    by_lower = {c.name.lower(): c.name for c in cols}
    return [[by_lower[k.lower()] for k in key] for key in fresh.unique_keys
            if all(k.lower() in by_lower and k.lower() not in gone for k in key)]


def _retyped(c: Column, f: Column, change: dict) -> Column:
    """Column `c` at the database's new type, with what was derived from the old type re-derived.

    The date encoding and timezone come from the new reading. The aggregation class follows only
    when the engine set it (it still equals the class the old type implies). A value list whose
    keys no longer fit is replaced by the new reading's, or cleared, and reported."""
    update: dict = {"type": f.type, "date_format": f.date_format, "timezone": f.timezone}
    if c.aggregation == build.classify_aggregation(c.name, c.type, is_key=c.primary_key):
        update["aggregation"] = f.aggregation
    if c.choice_field and c.choice_field != f.choice_field:
        update["choice_field"] = f.choice_field
        change.setdefault("values reset", []).append(c.name)
    return c.model_copy(update=update)


def _merged_hints(old, fresh):
    """Row-count hints from the new reading (a table grows); a curated filter list is kept.

    Kept even when empty once the table was already large: that is when the seed was offered, and an
    empty list then is the answer "it is narrowed through a joined table, not a column of its own",
    which re-seeding every date column would undo. Otherwise an empty list takes the fresh seed, so
    a table that has only now grown large gets one."""
    if fresh is None:
        return old
    if old is None:
        return fresh
    was_large = (old.estimated_row_count or 0) >= _LARGE_TABLE_ROWS
    return fresh.model_copy(update={
        "recommended_filters": (old.recommended_filters if old.recommended_filters or was_large
                                else fresh.recommended_filters),
        "indexes": fresh.indexes or old.indexes,
    })


def _merged_key(old: Table, fresh: Table, cols: list[Column], change: dict) -> tuple[list[Column], list[str]]:
    """The primary key and grain after a refresh.

    The database owns the key: when it reports one (`fresh.grain`, from the catalog or a uniqueness
    probe), every column's `primary_key` follows it. When it reports none — the probe is skipped on a
    very large table — nothing is known to have changed, and the old flags stay.

    The grain was the engine's when it equals the old key (introspection writes the key as the
    grain), and then it follows the new key too. Otherwise a person or a model spec stated it; it is
    kept, minus any column the database dropped, falling back to the new key if nothing is left."""
    gone = {n.lower() for n in change["dropped"]}
    old_key = sorted(c.name for c in old.columns if c.primary_key)
    new_key = sorted(fresh.grain)
    if new_key and [k.lower() for k in new_key] != [k.lower() for k in old_key]:
        change["key"] = [f"{', '.join(old_key) or '(none)'} -> {', '.join(new_key)}"]
        key = {k.lower() for k in new_key}
        cols = [c if c.primary_key == (c.name.lower() in key) else c.model_copy(update={"primary_key": c.name.lower() in key})
                for c in cols]
    engine_grain = sorted(g.lower() for g in old.grain) == sorted(k.lower() for k in old_key)
    if engine_grain and new_key:
        return cols, list(fresh.grain)
    kept = [g for g in old.grain if g.lower() not in gone]
    return cols, kept or list(fresh.grain)


def _refresh_existing(
    prev: Datasource,
    built: list[Table],
    new_rels: list[Relationship],
    fresh_keys: set[tuple],
    profile: str,
    report: IntrospectReport,
    per_area: Optional[dict[tuple, Table]] = None,
) -> Datasource:
    """The existing model with `built`'s merged tables in place, new tables placed, new joins added.

    Subject areas, their descriptions, entities and metrics, and every existing join stay as they are:
    re-proposing areas would undo the ones a person (or a model spec) defined (#437). A new table goes
    in the area named for its schema when there is one, else the only area, else a new area named
    for its schema, which the person can move in the model explorer."""
    by_key = {(t.schema_name, t.name): t for t in built}
    areas = []
    conn_name = prev.storage_connections[0].name  # the model's own, whatever this run would call it
    for sa in prev.subject_areas:
        tables = [(per_area or {}).get((sa.name, (t.schema_name, t.name)), by_key.get((t.schema_name, t.name), t))
                  for t in sa.tables_defined]
        areas.append(sa.model_copy(update={"tables_defined": tables}))
    names = {a.name: i for i, a in enumerate(areas)}

    def holds(i: int, t: Table) -> bool:   # one area can't hold two tables with the same bare name
        return any(x.name.lower() == t.name.lower() for x in areas[i].tables_defined)

    from .validator import SIZING_ERROR

    def room(i: int) -> bool:   # the validator refuses an area of more than SIZING_ERROR tables
        return len(areas[i].tables) < SIZING_ERROR

    for t in (t for t in built if (t.schema_name, t.name) in fresh_keys):
        key = build._area_key(t.schema_name) if t.schema_name else profile.lower()
        if key in names and not holds(names[key], t) and room(names[key]):
            i = names[key]
        elif len(areas) == 1 and not holds(0, t) and room(0):
            i = 0
        else:
            while key in names:
                key = f"{key}_"
            areas.append(build.make_area(key, [], [], conn_name))
            i = names[key] = len(areas) - 1
            report.notes.append(f"new subject area {key!r} for new tables — move them in /agami-model if they belong elsewhere")
        a = areas[i]
        areas[i] = a.model_copy(update={
            "tables_defined": [*a.tables_defined, t],
            "tables": [*a.tables, build.make_table_ref(conn_name, t)],
        })
    # New joins only: one inside an area goes there, one between two areas becomes a cross-area join.
    intra: set[int] = set()
    for i, a in enumerate(areas):
        keys = {(t.schema_name, t.name) for t in a.tables_defined}
        bare = {t.name for t in a.tables_defined}
        mine = [r for r in new_rels if build._rel_in_area(r, keys, bare)]
        if mine:
            areas[i] = a.model_copy(update={"relationships": [*a.relationships, *mine]})
            intra.update(id(r) for r in mine)
    between = [r for r in new_rels if id(r) not in intra]
    cross = [*prev.cross_subject_area_relationships, *build.extract_cross_area_relationships(areas, between)]
    report.subject_areas = [a.name for a in areas]
    return prev.model_copy(update={"subject_areas": areas, "cross_subject_area_relationships": cross})


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
    conn_name: str, report: IntrospectReport, budget: Optional[_CountBudget] = None,
    reads: Optional[_ReadBudget] = None, keys_unread: Optional[set] = None,
) -> Table:
    # columns: catalog first, probe fallback
    cols, mode = _columns(dialect, runner, schema, table)
    report.mode_per_capability.setdefault("columns", mode)

    # Estimate row count ONCE up front (a catalog stat — instant, zero scan). Reused for
    # the grain-probe size guard below AND for performance_hints, so we never fetch twice.
    est_rows = _estimate_rows(dialect, runner, schema, table, budget)

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
    unique_keys = _unique_keys(dialect, runner, schema, table, cols, grain, est_rows=est_rows)
    if unique_keys is None:
        # Unknown, not none: a refresh keeps the stored keys rather than read this as their removal.
        unique_keys = []
        if keys_unread is not None and cols:
            keys_unread.add((schema, table))
    pk_set = set(grain)
    for c in cols:
        c.primary_key = c.name in pk_set
        # column-intrinsic aggregation class (additive / averageable / dimension / unknown).
        # Keys are dimensions; the rest is a name+type heuristic the curator can refine.
        c.aggregation = build.classify_aggregation(c.name, c.type, is_key=c.primary_key)
        if build.detect_sensitive(table, c.name):
            c.sensitive = True
            report.sensitive_columns += 1

    # Value lists the catalog declares, on a table of any size; then one live sample → date
    # encoding/timezone, and value lists read from the data on a small table.
    declared = _apply_declared_values(dialect, runner, schema, table, cols)
    _enrich_from_sample(dialect, runner, schema, table, cols, est_rows=est_rows, reads=reads,
                        skip=declared.rejected, nocase=declared.nocase)

    column_groups = build.maybe_column_groups(cols)
    column_group_descriptions = build.column_group_descriptions(column_groups) if column_groups else {}
    if column_groups:
        report.deep_tables.append(table)

    perf = None
    if est_rows is not None:
        perf = PerformanceHints(
            estimated_row_count=est_rows,
            estimated_row_count_at=_NOW,  # so the receipt can show "estimated as of <date>"
        )

    # On a large table, seed `recommended_filters` with the date/time columns — the natural
    # way to narrow a scan, shown to the caller beside the table in `large_tables`. A seed, not a
    # decision: the report lists it so the person building the model keeps the column a question
    # would actually filter on. Real index / partition / clustering keys are layered in
    # per-dialect separately.
    if perf and (perf.estimated_row_count or 0) >= _LARGE_TABLE_ROWS:
        date_cols = [c.name for c in cols if c.type in ("date", "timestamp", "time")]
        if 1 <= len(date_cols) <= _MAX_DATE_FILTER_COLS:
            perf.recommended_filters = date_cols
        # Every large table, whatever the seed did: the question is which of its dates a question
        # means, and a table with more dates than the seed takes still has one.
        report.large_tables[table] = (perf.estimated_row_count, date_cols)

    return Table(
        name=table,
        schema=schema,
        storage_connection=conn_name,
        grain=grain,
        unique_keys=unique_keys,
        description="",  # LLM enrichment fills this in
        column_groups=column_groups,
        column_group_descriptions=column_group_descriptions,
        performance_hints=perf,
        columns=cols,
    )


def _columns(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str
) -> tuple[list[Column], str]:
    sql = dialect.sql_columns(schema, table)
    # `_try` answers None for a FAILED read and [] for a table with no columns. One failure is
    # retried: on a wide build over a VPN or tunnel a single dropped connection otherwise fell through
    # to the probe, failed again for the same reason, and the table was dropped from the model as
    # "no readable columns" — silently, and too late to recover once the model exists.
    rows = _try(runner, sql)
    if rows is None:
        rows = _try(runner, sql)
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
    # Value lists come later, from `_enrich_from_sample`, as for a catalog-read table.
    for n in names:
        values = [row.get(n) for row in sample if row.get(n) not in (None, "")]
        cols.append(Column(name=n, type=_infer_value_type(values), description=""))
    return cols


# The fallback counts run one table after another, each under the per-statement limit, so a catalog
# of hundreds of views could spend hours on them. One budget per run bounds the total; tables past
# it stay unknown, exactly as before the fallback existed.
_COUNT_BUDGET_S = 600.0


@dataclass
class _CountBudget:
    seconds_left: float
    skipped: int = 0


@dataclass
class _ReadBudget:
    left: int
    skipped: int = 0


def _estimate_rows(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str,
    budget: Optional[_CountBudget] = None,
) -> Optional[int]:
    """Row count from the catalog statistics (pg_class.reltuples, etc.) — instant, no scan — and,
    when the catalog has no usable figure, an exact count instead (#451).

    A view has no statistics on any engine: Redshift's `svv_table_info` lists only tables, and
    Postgres reports `reltuples` 0 or -1 (by version) for a view or a table it has never analysed. Read as-is,
    a fact view with millions of rows came back unknown (or as empty), so it never reached `large_tables` and
    never got the `recommended_filters` the scan-risk check names. The count is a read-only query,
    bounded by the same statement limit as every introspection read; if it is refused or runs out of
    time the size stays unknown, as before. None also when the dialect deliberately offers no
    estimate query at all."""
    q = dialect.sql_row_estimate(schema, table)
    if not q:
        return None
    est = _rows_from(_try(runner, q))
    if est is not None and est > 0:
        return est
    count_q = dialect.sql_row_count(schema, table)
    if count_q and count_q != q:
        # A distinct count was tried, so its answer replaces the catalog's: when it fails, the size
        # is unknown, never the catalog's 0 (an unanalysed table with rows would read as empty).
        if budget is not None and budget.seconds_left <= 0:
            budget.skipped += 1
            return None
        started = time.monotonic()
        try:
            return _rows_from(_try(runner, count_q))
        finally:
            if budget is not None:
                budget.seconds_left -= time.monotonic() - started
    # The estimate query was itself the exact count (no catalog statistics on this engine).
    return est if est is not None and est >= 0 else None


def _rows_from(rows: Optional[list[dict]]) -> Optional[int]:
    if not rows or rows[0].get("estimated_rows") in (None, ""):
        return None
    try:
        return int(float(rows[0]["estimated_rows"]))
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


def _unique_keys(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str, cols: list[Column],
    grain: list[str], *, est_rows: Optional[int] = None,
) -> Optional[list[list[str]]]:
    """The table's unique keys other than its grain, in the column spelling `cols` uses, or None
    when a catalog read or a data check failed and the keys are unknown.

    Where the engine declares unique keys without enforcing them, a key is kept only when the data
    bears it out, the way an unenforced foreign key is confirmed by overlap: a single-column key
    whose non-null values are all distinct, on a table under the grain probe's size guard."""
    sql = dialect.sql_unique_keys(schema, table)
    if not sql:
        return []
    rows = _try(runner, sql)
    if rows is None:
        return None
    if not rows:
        return []
    by_lower = {c.name.lower(): c.name for c in cols}
    named: dict[str, list[str]] = {}
    for r in rows:
        name, col = r.get("constraint_name"), r.get("column_name")
        if name in (None, ""):
            continue
        # An expression part has no column name; "" keeps the whole key out, not a shortened one.
        named.setdefault(str(name), []).append(
            "" if col in (None, "") else by_lower.get(str(col).lower(), ""))
    grain_set = {g.lower() for g in grain}
    keys: list[list[str]] = []
    for key in named.values():
        if not all(key) or len(set(key)) != len(key):
            continue   # a column the table doesn't list (an expression, a hidden column)
        if {k.lower() for k in key} == grain_set or any(set(k) == set(key) for k in keys):
            continue
        keys.append(key)
    if dialect.unique_enforced:
        return keys
    if est_rows is not None and est_rows >= _GRAIN_PROBE_MAX_ROWS:
        return []
    kept = []
    for key in keys:
        if len(key) != 1:
            continue
        res = _try(runner, dialect.count_distinct_sql(schema, table, key[0]))
        if res is None:
            return None
        if _all_distinct(res) is not None:
            kept.append(key)
    return kept


def _distinct_rows(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str, column: str,
) -> Optional[int]:
    """How many non-null rows `column` has when every one of their values is distinct, else None."""
    return _all_distinct(_try(runner, dialect.count_distinct_sql(schema, table, column)))


def _all_distinct(res: Optional[list[dict]]) -> Optional[int]:
    """`_distinct_rows` read off a probe's result rows."""
    if not res:
        return None
    try:
        total = int(float(res[0].get("total", 0)))
        distinct = int(float(res[0].get("distinct_count", 0)))
        nulls = int(float(res[0].get("null_count", 0)))
    except (ValueError, TypeError):
        return None
    filled = total - nulls
    return filled if filled > 0 and distinct == filled else None


def _endpoint(tables: list[Table], name: str, schema: Optional[str]) -> Optional[Table]:
    """The modelled table a join end names. `name` may embed its schema (`legacy.users`); a join
    that states a schema reads only that schema's table (or a schema-less one), never another's;
    a name two schemas share, written with none, reads neither."""
    want_schema, want = table_key(name, schema)
    cands = [t for t in tables if table_key(t.name, t.schema_name)[1] == want]
    if want_schema:
        cands = [t for t in cands if table_key(t.name, t.schema_name)[0] in (want_schema, "")]
    return cands[0] if len(cands) == 1 else None


@dataclass
class _ProbeBudget:
    """The uniqueness probes left for one run, shared by every pass that spends them."""
    left: int = _UNIQUE_PROBE_CAP
    noted: bool = False


def _suggest_data_unique(
    dialect: D.Dialect, runner: Runner, rels: list[Relationship], tables: list[Table],
    report: IntrospectReport, budget: Optional[_ProbeBudget] = None,
) -> None:
    """Check each join's many side on the data when no key settles it, and flag a join whose
    values turn out unique for review. The label is left as it is: data that is unique today is not
    a promise, so a person confirms the join is one-to-one (#511)."""
    budget = budget if budget is not None else _ProbeBudget()
    for r in rels:
        hit = _many_side(r, tables)
        if hit is None:
            continue
        if budget.left <= 0:
            if not budget.noted:
                budget.noted = True
                report.notes.append(
                    f"stopped checking joins' values for uniqueness after {_UNIQUE_PROBE_CAP} probes")
            return
        table, column = hit
        budget.left -= 1
        rows = _distinct_rows(dialect, runner, table.schema_name, table.name, column)
        if rows is not None:
            _flag_one_to_one(r, table, column, rows)


def _many_side(r: Relationship, tables: list[Table]) -> Optional[tuple[Table, str]]:
    """The table and column on the many side of a join whose one side is a key and whose many
    side no key settles, when it is small enough to check on the data."""
    if r.on or not (r.from_column and r.to_column) or r.relationship not in ("many_to_one", "one_to_many"):
        return None
    if _DATA_UNIQUE_NOTE in (r.description or ""):
        return None
    ft = _endpoint(tables, r.from_table, r.from_schema)
    tt = _endpoint(tables, r.to_table, r.to_schema)
    if ft is None or tt is None:
        return None
    one, many, col = (tt, ft, r.from_column) if r.relationship == "many_to_one" else (ft, tt, r.to_column)
    one_col = r.to_column if r.relationship == "many_to_one" else r.from_column
    if not one.unique_on([one_col]) or many.unique_on([col]):
        return None
    perf = many.performance_hints
    if perf is None or perf.estimated_row_count is None or _too_big_to_probe(many):
        return None   # a whole-table scan of a table of unknown size is not one to start blind
    return many, col


def _flag_one_to_one(r: Relationship, table: Table, column: str, rows: int) -> None:
    note = f"values of {table.name}.{column} unique on {rows:,} rows; {_DATA_UNIQUE_NOTE}"
    r.description = f"{r.description}; {note}" if r.description else note
    r.review_state = "unreviewed"
    r.signed_off_by = r.signed_off_at = r.signed_off_role = None


def _engine_approved(r: Relationship) -> bool:
    """A join introspection created and approved itself, which nobody has signed or edited since."""
    return (r.review_state == "approved" and r.signed_off_by == "agami_introspect"
            and r.signed_off_role == "system")


def _key_reason(t: Table, cols: list[str]) -> str:
    return "primary key" if [g.lower() for g in t.grain] == [c.lower() for c in cols] else "unique constraint"


def _recheck_engine_joins(
    dialect: D.Dialect, runner: Runner, rels: list[Relationship], tables: list[Table],
    report: IntrospectReport, budget: Optional[_ProbeBudget] = None,
    keys_unread: Optional[list[Table]] = None, sg: Optional[str] = None,
) -> None:
    """On a refresh, label each join the engine approved itself from the database's keys as they
    are now, and list every one whose label that changes. A join a person signed off or edited is
    never touched, nor one on a table whose keys this run couldn't read (`keys_unread`), and one
    only the data says is one-to-one is flagged for review, not relabelled. A composite `on:` join
    is labelled from all its columns; one that isn't plain column equalities is left alone."""
    unread = {id(t) for t in keys_unread or []}
    for r in rels:
        pairs = _key_pairs(r, sg) if _engine_approved(r) else None
        if not pairs:
            continue
        fc, tc = [f for f, _ in pairs], [t for _, t in pairs]
        ft = _endpoint(tables, r.from_table, r.from_schema)
        tt = _endpoint(tables, r.to_table, r.to_schema)
        if ft is None or tt is None or id(ft) in unread or id(tt) in unread:
            continue
        card = build.infer_cardinality(ft, tt, fc, tc)
        if card == r.relationship:
            continue
        why = ", ".join(sorted({_key_reason(t, c) for t, c in ((ft, fc), (tt, tc))
                                if t.unique_on(c)})) or "no key on the join columns"
        label = (f"{r.from_table}.{r.from_column} → {r.to_table}.{r.to_column}"
                 if r.from_column else _join_label(r, sg))
        report.changes.setdefault(_qualified(ft), {}).setdefault(
            "joins corrected from the database's constraints", []).append(
            f"{label}: {r.relationship} → {card} ({why})")
        r.relationship = card
    engine = [r for r in rels if _engine_approved(r)]
    _suggest_data_unique(dialect, runner, engine, tables, report, budget)
    for r in engine:
        if not _engine_approved(r):
            ft = _endpoint(tables, r.from_table, r.from_schema)
            report.changes.setdefault(_qualified(ft) if ft else r.from_table, {}).setdefault(
                "joins to review", []).append(
                f"{r.from_table}.{r.from_column} -> {r.to_table}.{r.to_column} (values unique; looks one-to-one)")


def _build_relationships(
    dialect: D.Dialect, runner: Runner, pairs: list[tuple[Optional[str], str]],
    tables: list[Table], report: IntrospectReport,
    pp: Optional[Path] = None,
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
        unpaired: list[str] = []
        for s in (schemas or {None}):
            rows = _try(runner, dialect.sql_foreign_keys(s)) if s else None
            fallback = dialect.sql_foreign_keys_unpaired(s) if s and rows is None else None
            if fallback:
                rows = _try(runner, fallback)
                if rows is not None:
                    unpaired.append(s)
                    rows = _single_column_fks(rows, report)
            if rows is not None:
                catalog_ok = True
                catalog_fks.extend(rows)
        if unpaired:
            report.notes.append(
                f"foreign keys in {', '.join(sorted(unpaired))} read with the older catalog query: this "
                "database's catalog has no column positions for them, so only single-column keys were kept")

    tables_by_name: dict[str, list[Table]] = {}
    for t in tables:
        tables_by_name.setdefault(t.name, []).append(t)

    rels: list[Relationship] = []
    overlap_probes = 0   # budget guard for declared-FK overlap confirmation (see FK_OVERLAP_PROBE_CAP)
    outside_model = 0
    if catalog_ok and catalog_fks:
        report.mode_per_capability["relationships"] = "catalog"
        fk_keys = _fk_groups(catalog_fks)
        _fk_step = max(1, len(fk_keys) // 10)   # ~10 heartbeat lines over the (slow) FK pass
        for _fi, (key_rows, grouped) in enumerate(fk_keys, 1):
            if _fi % _fk_step == 0 or _fi == len(fk_keys):
                _progress(pp, f"relationships: declared FK {_fi}/{len(fk_keys)}")
            fk = key_rows[0]
            ft, tt = fk.get("from_table"), fk.get("to_table")
            fc = [r.get("from_column") for r in key_rows]
            tc = [r.get("to_column") for r in key_rows]
            if not (ft and tt and all(fc) and all(tc)):
                continue
            # The catalog lists every FK in the schema, including ones to tables the prune step
            # dropped or to a schema not introspected; a join to a table the model lacks is unusable.
            if not (_in_model(ft, fk.get("from_schema"), tables_by_name)
                    and _in_model(tt, fk.get("to_schema"), tables_by_name)):
                outside_model += 1
                continue
            # Schema each endpoint lives in: the dialect's FK query supplies it on schema-ful
            # DBs (Postgres/MySQL); otherwise resolve it from the table list (same-schema first).
            from_schema = fk.get("from_schema") or _schema_of(ft, tables_by_name)
            to_schema = fk.get("to_schema") or _schema_of(tt, tables_by_name, prefer=from_schema, report=report)
            cross = bool(from_schema and to_schema and from_schema != to_schema)
            fobj = _pick_target(tables_by_name.get(ft, []), prefer=from_schema)
            tobj = _pick_target(tables_by_name.get(tt, []), prefer=to_schema)
            card = build.infer_cardinality(fobj, tobj, fc, tc)   # every column of a composite key
            # declared-but-unenforced FKs (Redshift/Databricks/Trino) -> confirm by overlap.
            # The catalog's FK is a *declaration*, not a guarantee, so probe the data: if the
            # child values actually live in the parent key, treat it like an enforced FK
            # (confirmed + auto-approved) instead of dumping it in the review queue. Only FKs we
            # can't confirm against the data stay inferred/unreviewed for a human glance. This is
            # what keeps inheritance-heavy schemas (ServiceNow on Redshift) from flooding review.
            confidence = "confirmed" if dialect.fk_enforced else "inferred"
            overlap_confirmed = False
            if not dialect.fk_enforced and overlap_probes < FK_OVERLAP_PROBE_CAP:
                # Skip the scan on huge FROM tables (the slow case on Redshift fact tables) —
                # leave the declared FK inferred/unreviewed rather than full-scan to confirm it.
                if fobj is not None and tobj is not None and not _too_big_to_probe(fobj):
                    overlap_probes += 1
                    one = len(fc) == 1   # a single column keeps the probe it always had
                    if _overlaps(dialect, runner, fobj, fc[0] if one else fc, tobj, tc[0] if one else tc):
                        confidence = "confirmed"
                        overlap_confirmed = True
            # A key whose rows came without a constraint name can't be told from separate keys,
            # and used alone each column of a composite one repeats rows: never trusted (#510).
            trusted = (dialect.fk_enforced or overlap_confirmed) and grouped
            # trusted joins are sound structure -> auto-approve with a system sign-off (don't
            # flood the review queue); unconfirmed ones need a glance.
            # EXCEPTION: a join that spans two schemas is an architectural claim — the model now
            # reaches across namespaces — so surface it for review even when the FK is trusted.
            desc = ""
            if trusted and not cross:
                kw: dict = {"review_state": "approved", "signed_off_by": "agami_introspect",
                            "signed_off_role": "system", "signed_off_at": _NOW}
                if overlap_confirmed:
                    desc = "declared FK, confirmed by value-overlap probe"
            else:
                kw = {"review_state": "unreviewed"}
            if not grouped:
                desc = _UNGROUPED_FK
            if cross:
                desc = f"crosses schemas: {from_schema} → {to_schema} (declared FK) — confirm it belongs in this model"
                report.notes.append(f"cross-schema relationship: {from_schema}.{ft} → {to_schema}.{tt} (declared FK)")
            ends = ({"from_column": fc[0], "to_column": tc[0]} if len(fc) == 1
                    else {"on": _composite_on(dialect, ft, fc, tt, tc)})
            rels.append(Relationship(
                from_table=ft, to_table=tt, **ends,
                from_schema=from_schema, to_schema=to_schema,
                relationship=card, join_type="LEFT", confidence=confidence, description=desc, **kw,
            ))
        if overlap_probes >= FK_OVERLAP_PROBE_CAP:
            report.notes.append(
                f"declared-FK overlap confirmation capped at {FK_OVERLAP_PROBE_CAP} probes — "
                "remaining declared FKs left unreviewed for sign-off on /agami-model")
        if any(not grouped for _, grouped in fk_keys):
            report.notes.append(
                "the catalog didn't say which foreign-key columns belong to one key, so joins with "
                "several columns between the same two tables were kept apart and left for review")
        if outside_model:
            report.notes.append(
                f"skipped {outside_model} declared foreign key(s) to tables outside the model "
                "(pruned, or in a schema not introspected)")
    else:
        # probe: infer FKs from name+type match, confirm by value-overlap
        report.mode_per_capability["relationships"] = "probe"
        # Said out loud because an empty catalog cannot tell "no FKs declared" from "FKs this role
        # may not see" on every engine, and the inferred joins can bind the wrong parent.
        report.notes.append(
            "no declared foreign keys were visible in the catalog — relationships are inferred "
            "from column names and values; review them before relying on them")
        rels.extend(_probe_relationships(dialect, runner, tables, report))

    # Tier 2 — name-based reference promotion. A `<x>_id` column whose target table is
    # identifiable by name becomes an inferred/unreviewed join even WITHOUT value overlap
    # (which under-detects on sparse data). Conservative + type-checked, and `unreviewed` so
    # it's discoverable but never asserted as fact — the user signs it off (or it self-approves
    # through use). This is what lifts a sparse demo from ~9 joins toward the real graph.
    rels.extend(_promote_reference_fields(tables, rels, report, _sqlglot_name(dialect)))
    return rels


_UNGROUPED_FK = ("declared FK column; the catalog didn't say which columns form one key, so the key's "
                 "columns couldn't be grouped — if it spans several columns, join on all of them")


def _single_column_fks(rows: list[dict], report: IntrospectReport) -> list[dict]:
    """The older FK query's rows, one per single-column key. Its rows pair every column of a
    composite key with every referenced column, so nothing in them says which pairs are right: such
    a key is left out, with a note, rather than joined on a guess."""
    keys: dict[tuple, list[dict]] = {}
    for r in rows:
        name = r.get("constraint_name")
        # Without a name, every key between the same two tables may be one composite key.
        tail = (str(name),) if name is not None and str(name) != "" else (r.get("to_schema"), r.get("to_table"))
        keys.setdefault((r.get("from_schema"), r.get("from_table"), *tail), []).append(r)
    out: list[dict] = []
    for k, g in keys.items():
        label = f"{k[1]}.{k[2]}" if len(k) == 3 else f"{k[1]} → {k[3]}"
        # DuckDB's constraint_column_usage lists the key's own columns, not the ones it references.
        if any((r.get("from_schema"), r.get("from_table"), r.get("from_column"))
               == (r.get("to_schema"), r.get("to_table"), r.get("to_column")) for r in g):
            report.notes.append(f"foreign key {label} not read: the catalog names its own column as the "
                                "one it references")
        elif len({(r.get("from_column"), r.get("to_schema"), r.get("to_table"), r.get("to_column"))
                  for r in g}) == 1:
            out.append({**g[0], "key_ordinal": 1})
        elif len({r.get("from_column") for r in g}) > 1:
            report.notes.append(f"composite foreign key {label} not read: this database's catalog doesn't "
                                "say how its columns pair")
        else:
            report.notes.append(f"foreign key {label} not read: the catalog names more than one column it "
                                "references")
    return out


def _fk_groups(rows: list[dict]) -> list[tuple[list[dict], bool]]:
    """The catalog's FK rows as one entry per key, in catalog order: its rows in key order, and
    whether they could be grouped. A row carries its constraint's name and its column's position in
    the key; grouped by (from_schema, from_table, constraint_name), a key over several columns is one
    entry. A row with no constraint name stands alone, and is ungroupable when another such row
    joins the same two tables — it may be one column of a composite key."""
    def ordinal(r: dict) -> float:
        try:
            return float(r.get("key_ordinal"))
        except (TypeError, ValueError):
            return 0.0

    def tables(r: dict) -> tuple:
        return (r.get("from_schema"), r.get("from_table"), r.get("to_schema"), r.get("to_table"))

    entries: dict[tuple, list[dict]] = {}
    for i, r in enumerate(rows):
        name = r.get("constraint_name")
        named = name is not None and str(name) != ""
        entries.setdefault((r.get("from_schema"), r.get("from_table"), str(name)) if named else (i,), []).append(r)
    loose = [g[0] for k, g in entries.items() if len(k) == 1]
    per_pair: dict[tuple, int] = {}
    for r in loose:
        per_pair[tables(r)] = per_pair.get(tables(r), 0) + 1
    return [(sorted(g, key=ordinal), True) if len(k) > 1 else (g, per_pair[tables(g[0])] == 1)
            for k, g in entries.items()]


# Names written unquoted in a composite join: the spelling an unquoted name folds to on the engine.
_LOWER_IDENT = re.compile(r"[a-z_][a-z0-9_]*")
_UPPER_IDENT = re.compile(r"[A-Z_][A-Z0-9_]*")
_FOLDS_UPPER = {"Snowflake", "Oracle"}
_FOLDS_LOWER = {"PostgreSQL", "Redshift", "Trino"}


def _composite_on(dialect: D.Dialect, ft: str, fc: list[str], tt: str, tc: list[str]) -> str:
    """A composite key's join condition, `ft.a = tt.x AND ft.b = tt.y`, on bare table names (the
    receipt matches a written join to it by bare name). A name an unquoted spelling wouldn't reach
    on this engine, or a word it reserves (`order`), is quoted."""
    def q(name: str) -> str:
        plain = (bool(_UPPER_IDENT.fullmatch(name)) if dialect.name in _FOLDS_UPPER
                 else bool(_LOWER_IDENT.fullmatch(name)) if dialect.name in _FOLDS_LOWER
                 else bool(_LOWER_IDENT.fullmatch(name) or _UPPER_IDENT.fullmatch(name)))
        return name if plain and not dialect.reserves(name) else dialect.quote_ident(name)
    return " AND ".join(f"{q(ft)}.{q(f)} = {q(tt)}.{q(t)}" for f, t in zip(fc, tc))


def _regroup_columns_with_references(
    tables: list[Table], rels: list[Relationship], dialect: Optional[str] = None,
) -> None:
    """Re-derive column_groups (+ descriptions) for deep tables now that relationships exist, so
    each table's FK (FROM-side) columns land in the `references` group — every column of a composite
    key's `on:` join too. Mutates `tables` in place; a no-op for narrow tables (no groups) and tables
    with no outgoing column-keyed joins. Schema-aware so a same-named table in another schema doesn't
    borrow the wrong references."""
    ref_by_table: dict[tuple, set[str]] = {}
    for r in rels:
        for fc, _tc in _column_pairs(r, dialect) or []:   # an `on:` on an expression names no column
            ref_by_table.setdefault((r.from_schema, bare_name(r.from_table)), set()).add(fc)
    for t in tables:
        if not t.column_groups:   # only deep tables carry groups
            continue
        ref_cols = ref_by_table.get((t.schema_name, t.name)) or ref_by_table.get((None, t.name)) or set()
        if not ref_cols:
            continue
        t.column_groups = build.maybe_column_groups(t.columns, reference_columns=ref_cols)
        t.column_group_descriptions = build.column_group_descriptions(t.column_groups)


_REF_ID_RE = re.compile(r"^(.+?)_id$", re.IGNORECASE)

# Columns that are NEVER an enum to decode, regardless of cardinality: join keys / reference
# actors (`*_id`, `*_by` — usually GUID-valued FKs) and counts / quantities (`*_count`, `*_qty`,
# … — measures, not codes). General naming conventions, not vendor-specific. A low-cardinality
# `resolved_by` (few users) or `reopen_count` (0/1/2) would otherwise be mis-seeded as a choice.
_NOT_CHOICE_NAME_RE = re.compile(
    r"^(id|guid|uuid)$|_(id|guid|uuid|by|count|cnt|qty|quantity|seq|num|no)$", re.IGNORECASE)
# A value that looks like an identifier (32-hex ServiceNow sys_id, or a dashed UUID) — its column
# is a join key, never an enum. One such distinct value is enough to disqualify the whole column.
_GUID_VALUE_RE = re.compile(
    r"^[0-9a-f]{32}$|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


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
    tables: list[Table], existing: list[Relationship], report: IntrospectReport,
    dialect: Optional[str] = None,
) -> list[Relationship]:
    """Promote `<x>_id` reference columns to inferred/unreviewed joins when the target table is
    identifiable by name and key-type-compatible — never requiring value overlap. Conservative:
    target must exist, have a single-column grain, and a key whose type matches the ref column."""
    by_name: dict[str, list[Table]] = {}
    for t in tables:
        by_name.setdefault(t.name.lower(), []).append(t)
    # schema-aware key so a same-named table in another schema doesn't suppress promotion here; every
    # column of a composite key counts, or one of them would be promoted to a join of its own
    seen = {(r.from_schema, r.from_table, fc) for r in existing for fc, _tc in _column_pairs(r, dialect) or []}
    out: list[Relationship] = []
    for t in tables:
        grain = set(t.grain)
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
            card = build.infer_cardinality(t, target, [c.name], [to_col])
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


def _in_model(name: str, schema: Optional[str], tables_by_name: dict[str, list[Table]]) -> bool:
    """Whether a declared FK endpoint is a table being modelled. When either side carries no schema
    (SQLite, or a dialect whose FK query omits it) it matches on the bare name."""
    return any(not schema or not t.schema_name or t.schema_name == schema
               for t in tables_by_name.get(name, []))


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
    dialect: D.Dialect, runner: Runner, tables: list[Table], report: IntrospectReport,
) -> list[Relationship]:
    tables_by_name: dict[str, list[Table]] = {}
    for t in tables:
        tables_by_name.setdefault(t.name, []).append(t)
    # candidate targets: names with a single-column grain; the key column is read off the Table
    # resolved schema-aware below, so a same-named table in another schema can't lend its key.
    targets = [name for name, ts in tables_by_name.items() if any(len(x.grain) == 1 for x in ts)]
    rels: list[Relationship] = []
    for t in tables:
        for c in t.columns:
            low = c.name.lower()
            if not low.endswith("_id") and low != "id":
                continue
            stem = low[:-3] if low.endswith("_id") else None
            # match a target table whose name resembles the stem
            for tgt_name in targets:
                if tgt_name == t.name:
                    continue
                tn = tgt_name.lower()
                if stem and (stem in tn or tn in stem or tn.rstrip("s") == stem.rstrip("s")):
                    tgt = _pick_target(tables_by_name.get(tgt_name, []), prefer=t.schema_name)
                    if tgt is None or len(tgt.grain) != 1:
                        continue
                    tgt_col = tgt.grain[0]
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
                            relationship=build.infer_cardinality(t, tgt, [c.name], [tgt_col]),
                            join_type="LEFT", confidence="proposed", review_state="unreviewed",
                            description=desc,
                        ))
                        break
    return rels


def _too_big_to_probe(t: Table) -> bool:
    """True when a table's estimated row count is above the overlap-probe guard — scanning it to
    confirm a join would crawl, so we skip the scan and leave the join unverified."""
    perf = getattr(t, "performance_hints", None)
    rows = getattr(perf, "estimated_row_count", None) if perf else None
    return rows is not None and rows > _OVERLAP_PROBE_MAX_ROWS


def overlap_sql(dialect: D.Dialect, ft: Table, fc: str | list[str], tt: Table, tc: str | list[str]) -> str:
    """The overlap probe as one statement: 50 sampled distinct values of `ft.fc`, counted against
    `tt.tc`. Split out of `_overlaps` so a caller that must not run SQL itself, the reconcile skill's
    join check, which hands every probe to the execution tier, emits exactly the probe introspection
    already trusts rather than a second spelling of it.

    The sample is bounded through `Dialect.limited`, so the 50-row cap now holds on TOP and FETCH
    engines too; it used to apply only where the row-limit keyword was `LIMIT`.

    A key over several columns (lists) samples their value combinations and matches all of them."""
    fq_from = dialect.qualified(ft.schema_name, ft.name)
    fq_to = dialect.qualified(tt.schema_name, tt.name)
    if isinstance(fc, str) and isinstance(tc, str):
        col_f = dialect.quote_ident(fc)
        col_t = dialect.quote_ident(tc)
        sample = dialect.limited(f"SELECT DISTINCT {col_f} AS v FROM {fq_from} WHERE {col_f} IS NOT NULL", 50)
        return (
            f"SELECT COUNT(*) AS matched FROM ({sample}) src "
            f"WHERE EXISTS (SELECT 1 FROM {fq_to} t WHERE t.{col_t} = src.v)"
        )
    fcs = [fc] if isinstance(fc, str) else fc
    tcs = [tc] if isinstance(tc, str) else tc
    cols_f = [dialect.quote_ident(c) for c in fcs]
    picked = ", ".join(f"{c} AS v{i}" for i, c in enumerate(cols_f))
    present = " AND ".join(f"{c} IS NOT NULL" for c in cols_f)
    match = " AND ".join(f"t.{dialect.quote_ident(c)} = src.v{i}" for i, c in enumerate(tcs))
    sample = dialect.limited(f"SELECT DISTINCT {picked} FROM {fq_from} WHERE {present}", 50)
    return f"SELECT COUNT(*) AS matched FROM ({sample}) src WHERE EXISTS (SELECT 1 FROM {fq_to} t WHERE {match})"


def dropped_rows_sql(dialect: D.Dialect, left: Table, lc: str, right: Table, rc: str) -> str:
    """How many rows of `left` an inner join to `right` on `left.lc = right.rc` leaves behind: the rows
    with no partner, a null key among them, counted over the whole table before any filter the
    statement wrote. Emitted for the reconcile skill's join check and never run here.

    Shaped as a correlated `NOT EXISTS` in WHERE beside an uncorrelated scalar subquery, the one
    form every supported engine runs: a correlated subquery inside `SUM(CASE WHEN EXISTS ...)` is
    refused by Snowflake and restricted on Oracle and BigQuery. Not sampled, because a sample gives
    a rate where a reader wants a count; the size guard bounds the cost instead."""
    fq_left = dialect.qualified(left.schema_name, left.name)
    fq_right = dialect.qualified(right.schema_name, right.name)
    col_l = dialect.quote_ident(lc)
    col_r = dialect.quote_ident(rc)
    return (
        f"SELECT (SELECT COUNT(*) FROM {fq_left}) AS total, COUNT(*) AS dropped FROM {fq_left} l "
        f"WHERE NOT EXISTS (SELECT 1 FROM {fq_right} r WHERE r.{col_r} = l.{col_l})"
    )


def _overlaps(dialect: D.Dialect, runner: Runner, ft: Table, fc: str | list[str], tt: Table, tc: str | list[str]) -> bool:
    """Sample from-column values and check they exist in the target key."""
    res = _try(runner, overlap_sql(dialect, ft, fc, tt, tc))
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


# A whole value that is an ISO date or timestamp, nothing before or after it.
_STRICT_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?"
    r"(?:Z|[+-](\d{2}):?(\d{2}))?)?$")
# A text column whose name says nothing about dates is claimed only on this much evidence.
_ISO_TEXT_MIN_VALUES = 20


def _is_strict_iso(value: str) -> bool:
    m = _STRICT_ISO_RE.match(value)
    if not m:
        return False
    y, mo, d, hh, mi, ss, oh, om = m.groups()
    try:
        date(int(y), int(mo), int(d))
    except ValueError:
        return False  # 2024-13-45 has the shape but is no date
    if oh is not None and (int(oh) > 23 or int(om) > 59):
        return False  # +25:00 / +05:99 is no UTC offset
    return hh is None or (int(hh) < 24 and int(mi) < 60 and (ss is None or int(ss) < 60))


def _sniff_iso_text(values: list) -> tuple[Optional[str], Optional[str]]:
    """(date_format, timezone) for a text column whose name is not date-like: iso8601 only when
    at least _ISO_TEXT_MIN_VALUES sampled values are present and every one is a real ISO date or
    timestamp. Without the name to go on, a single version string or note rules the column out."""
    vals = [str(v).strip() for v in values if v not in (None, "")]
    if len(vals) < _ISO_TEXT_MIN_VALUES or not all(_is_strict_iso(v) for v in vals):
        return (None, None)
    return ("iso8601", "offset-aware" if any(_OFFSET_RE.search(v) for v in vals) else None)


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


# A value list read from the data is a DISTINCT over the whole column, so it is complete; it is
# still read only on a table small enough for that to be cheap: the sample was the whole table, or
# the catalog says the table is small.
_CHOICE_FULL_TABLE_MAX_ROWS = SAMPLE_ROWS * 20


def _sample_covers_values(sample: list, est_rows: Optional[int]) -> bool:
    if len(sample) < SAMPLE_ROWS:
        return True  # the engine returned every row
    # a negative estimate (a view or a table never analyzed reports -1) is unknown, not small
    return est_rows is not None and 0 <= est_rows <= _CHOICE_FULL_TABLE_MAX_ROWS


def _enrich_from_sample(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str, cols: list[Column],
    *, est_rows: Optional[int] = None, reads: Optional[_ReadBudget] = None,
    skip: frozenset[str] | set[str] = frozenset(), nocase: frozenset[str] | set[str] = frozenset(),
) -> None:
    """ONE live sample → (a) date_format/timezone on date-candidate columns, and
    (b) on a small table, a `choice_field` skeleton `{value: ""}` on low-cardinality CODED columns
    the catalog declared nothing for (the LLM enrichment fills the labels), read with one bounded
    DISTINCT per column — without it, a catalog DB's coded columns (e.g. a ServiceNow integer
    `severity`) never get an enum skeleton, so the decode can only ever live in prose. `skip` names
    (lowercased) the columns whose declared list was rejected: they get no list at all."""
    date_cands = [c for c in cols if c.type in ("date", "timestamp", "time")
                  or (c.type in ("integer", "decimal", "string") and _looks_time_named(c.name))]
    iso_text_cands = [c for c in cols if c.type == "string" and not _looks_time_named(c.name)]
    choice_cands = [c for c in cols if _data_choice_candidate(c) and c.name.lower() not in skip]
    if not date_cands and not choice_cands and not iso_text_cands:
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
    for c in iso_text_cands:
        df, tz = _sniff_iso_text([row.get(c.name) for row in sample])
        if df:
            c.date_format = df
        if tz:
            c.timezone = tz
    # a column of dates is a date, not a list of codes to decode
    choice_cands = [c for c in choice_cands if not c.date_format]
    if not _sample_covers_values(sample, est_rows):
        choice_cands = []  # a big table: DISTINCT would scan it, and a partial list would mislead
    for c in choice_cands:
        if reads is not None:
            if reads.left <= 0:
                reads.skipped += 1
                continue
            reads.left -= 1
        rows = _try(runner, dialect.distinct_sql(schema, table, c.name, ENUM_MAX_DISTINCT + 1))
        if rows is None or len(rows) > ENUM_MAX_DISTINCT:
            continue  # more values than a list holds: none, rather than an incomplete one
        seen = sum(1 for row in sample if row.get(c.name) not in (None, ""))
        ch = _maybe_choice([r.get("v") for r in rows], rows=seen)
        # only genuinely code-like: skip long free-text values (names, descriptions, ids).
        if ch and all(len(str(k)) <= 40 for k in ch):
            _set_choice(c, ch, case_insensitive=dialect.case_insensitive_compare or c.name.lower() in nocase,
                        accents=dialect.accent_insensitive_compare)
    # Deterministic descriptions for universal coded formats (currency/country/timezone/locale) —
    # fills a column the LLM would otherwise leave `ai_unknown`, on any database. Only when blank.
    for c in cols:
        if (c.description or "").strip() or c.primary_key:
            continue
        cd = build.canonical_description(c.name, [row.get(c.name) for row in sample])
        if cd:
            c.description = cd
            c.description_source = "metadata"  # authoritative deterministic fact (not an LLM guess)


# Keys, surrogate keys and years: low-cardinality on a small table, never codes to decode.
_NOT_DATA_CHOICE_NAME_RE = re.compile(r"_(key|sk|fk)$|(^|_)year$", re.IGNORECASE)


def _data_choice_candidate(c: Column) -> bool:
    """A column whose values may be read from the data into a list: a codeable column with no list
    yet that isn't a key, a measure, a date or a year. Reference-named `*_id` columns are excluded by
    NAME: introspection never populates Column.foreign_key, so that guard alone wouldn't catch a
    low-cardinality `dealer_id` (few dealers) — a JOIN target, not an enum to decode."""
    return (c.choice_field is None and not c.primary_key and c.foreign_key is None
            and c.type in ("integer", "string")
            and c.aggregation not in ("additive", "averageable")
            and not _looks_time_named(c.name)
            and not _NOT_CHOICE_NAME_RE.search(c.name)
            and not _NOT_DATA_CHOICE_NAME_RE.search(c.name))


def _set_choice(
    c: Column, choice: dict[str, str], *, case_insensitive: bool = False, accents: bool = False,
) -> None:
    c.choice_field = choice
    notes = [_lookalike_caveat(list(choice))]
    if case_insensitive:
        notes.append(_case_insensitive_caveat(list(choice), accents=accents))
    for note in notes:
        if note and note not in c.caveats:
            c.caveats.append(note)


def _case_insensitive_caveat(values: list[str], *, accents: bool = False) -> Optional[str]:
    """On an engine that compares strings ignoring case, a listed value also finds its other
    capitalisations (and DISTINCT returns only one of them), so the list is not every spelling."""
    v = next((v for v in values if any(ch.isalpha() for ch in v)), None)
    if v is None:
        return None
    if accents:
        return (f"this database compares values case- and accent-insensitively: '{v}' also matches "
                "other capitalisations and accented spellings")
    return f"this database compares values case-insensitively: '{v}' also matches other capitalisations"


def _lookalike_caveat(values: list[str]) -> Optional[str]:
    """A caveat naming the stored values that differ only by spacing or case: a query must match the
    one it means exactly, and 'Open' finds none of the 'Open ' rows."""
    groups: dict[str, list[str]] = {}
    for v in values:
        groups.setdefault(" ".join(v.split()).casefold(), []).append(v)
    clashes = [", ".join(f"'{v}'" for v in g) for g in groups.values() if len(g) > 1]
    if not clashes:
        return None
    return f"values differ only by spacing or case: {'; '.join(clashes)} — match them exactly"


@dataclass
class _Declared:
    values: dict[str, list[str]]  # lowercased column name -> the values declared for it
    rejected: set[str]            # a declaration was found but no list can be kept: none at all
    checked: set[str]             # the list came from a CHECK, so it compares as the column does
    nocase: set[str]              # SQLite columns declared COLLATE NOCASE


def _apply_declared_values(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str, cols: list[Column],
) -> _Declared:
    """A `choice_field` skeleton, in declared order, on each column whose values the catalog declares.
    A list with a value the column's type can't hold is rejected, so the model stays writable."""
    from .validator import _value_matches_type

    declared = _declared_values(dialect, runner, schema, table)
    # Declarations are matched by lowercased name, so with both `status` and "STATUS" on one table
    # neither can be told apart: no declared list for either, and the data step may read each.
    folded = [c.name.lower() for c in cols]
    collide = {k for k in folded if folded.count(k) > 1}
    declared.rejected -= collide
    for c in cols:
        key = c.name.lower()
        values = declared.values.get(key)
        if not values or c.choice_field is not None or key in collide:
            continue
        if not all(_value_matches_type(v, c.type) for v in values):
            declared.rejected.add(key)
            continue
        _set_choice(c, {v: "" for v in values}, case_insensitive=key in declared.checked and (
            dialect.case_insensitive_compare or key in declared.nocase),
            accents=dialect.accent_insensitive_compare)
    return declared


def _declared_values(dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str) -> _Declared:
    """The values each column's enum type or a simple CHECK constraint allows.

    Catalog reads only, so they run on a table of any size. A column two constraints give different
    lists, or more than DECLARED_MAX_VALUES values, is rejected: it gets no list, not even one read
    from the data, since the data would show only the values in use."""
    found: dict[str, list[str]] = {}
    clash: set[str] = set()
    checked: set[str] = set()
    nocase: set[str] = set()

    def put(name: str, values: list[str]) -> None:
        values = list(dict.fromkeys(v for v in values if v != ""))
        key = name.lower()
        if key in found and found[key] != values:
            clash.add(key)
        found.setdefault(key, values)

    q = dialect.sql_enum_values(schema, table)
    if q:
        per: dict[str, list[str]] = {}
        for r in _try(runner, q) or []:
            name = r.get("column_name")
            if not name:
                continue
            if r.get("enum_value") is not None:
                per.setdefault(name, []).append(str(r.get("enum_value")))
            elif r.get("column_type"):
                per[name] = _enum_type_values(str(r.get("column_type")))
        for name, values in per.items():
            put(name, values)
    q = dialect.sql_check_constraints(schema, table)
    if q:
        rows = _try(runner, q)
        fallback = dialect.sql_check_constraints_fallback(schema, table)
        if rows is None and fallback:
            rows = _try(runner, fallback)
        for r in rows or []:
            text = str(r.get("check_clause") or "")
            if dialect.name == "MySQL":
                text = _mysql_check_text(text)
            for name, values in _check_lists(text, dialect):
                put(name, values)
                checked.add(name.lower())
            if dialect.name == "SQLite":
                nocase |= _nocase_columns(text)
    rejected = {k for k, v in found.items() if k in clash or len(v) > DECLARED_MAX_VALUES}
    values = {k: v for k, v in found.items() if k not in rejected and v}
    return _Declared(values, rejected, checked, nocase)


def _mysql_check_text(text: str) -> str:
    """MySQL 8's information_schema escapes each quote and backslash of a CHECK once more
    (`_utf8mb4\\'open\\'`); undo that when every quote is escaped. MariaDB's text is plain."""
    if "'" not in text or re.search(r"(?<!\\)'", text):
        return text
    return re.sub(r"\\(['\\])", r"\1", text)


def _nocase_columns(create_sql: str) -> set[str]:
    """Lowercased names of the columns a SQLite CREATE TABLE declares COLLATE NOCASE."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel

    try:
        tree = sqlglot.parse_one(create_sql, dialect="sqlite", error_level=ErrorLevel.RAISE)
    except Exception:
        return set()
    return {cd.name.lower() for cd in tree.find_all(exp.ColumnDef)
            for k in (con.kind for con in cd.constraints)
            if isinstance(k, exp.CollateColumnConstraint) and k.this is not None
            and k.this.name.upper() == "NOCASE"}


_ENUM_TYPE_RE = re.compile(r"^\s*enum\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL)
_QUOTED_RE = re.compile(r"'((?:[^'\\]|''|\\.)*)'", re.DOTALL)


# MySQL's string-literal escapes; any other `\x` stands for `x`.
_MYSQL_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "Z": "\x1a", "\\": "\\", "'": "'"}


def _enum_type_values(column_type: str) -> list[str]:
    """The values of a MySQL/MariaDB `enum('a','b')` column type, quotes and escapes undone."""
    m = _ENUM_TYPE_RE.match(column_type)
    if not m:
        return []
    return [re.sub(r"\\(.)", lambda e: _MYSQL_ESCAPES.get(e.group(1), e.group(1)), v.replace("''", "'"))
            for v in _QUOTED_RE.findall(m.group(1))]


_CHECK_WORD_RE = re.compile(r"^\s*CHECK\b", re.IGNORECASE)
_CHECK_TAIL_RE = re.compile(r"\s+(NOT\s+VALID|NO\s+INHERIT)(\s+(NOT\s+VALID|NO\s+INHERIT))?\s*$", re.IGNORECASE)


def _check_lists(text: str, dialect: D.Dialect) -> list[tuple[str, list[str]]]:
    """(column, values) for each CHECK in `text` that only lists one column's allowed literals —
    `col IN ('a', 'b')`, `col = 'a' OR col = 'b'`, Postgres's `col = ANY (ARRAY[...])` — parsed in
    the engine's dialect. Any other CHECK (a range, two columns, a function) declares no list.
    `text` is one CHECK's expression, or SQLite's whole CREATE TABLE statement."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel

    from .sql_dialect import DialectUnresolved, sqlglot_dialect

    try:
        sg = sqlglot_dialect(dialect.name)
    except DialectUnresolved:
        return []
    body = _CHECK_TAIL_RE.sub("", _CHECK_WORD_RE.sub("", text.strip()))
    if not body:
        return []
    try:
        tree = sqlglot.parse_one(body, dialect=sg, error_level=ErrorLevel.RAISE)
    except Exception:
        return []
    if isinstance(tree, exp.Create):
        exprs = [c.this for c in tree.find_all(exp.CheckColumnConstraint)]
    else:
        exprs = [tree]
    exact_decimal = dialect.name in _EXACT_DECIMAL_DIALECTS
    return [hit for hit in (_listed_values(e, exact_decimal=exact_decimal) for e in exprs) if hit]


# Engines whose NUMERIC/DECIMAL with no precision or scale holds any number exactly (MySQL's and SQL
# Server's default to a scale of 0, DuckDB's to 3).
_EXACT_DECIMAL_DIALECTS = frozenset({"PostgreSQL", "Oracle", "SQLite"})


def _listed_values(e, *, exact_decimal: bool = False) -> Optional[tuple[str, list[str]]]:
    from sqlglot import exp

    T = exp.DataType.Type

    def lossless(to, x) -> bool:  # casting the number literal `x` to `to` keeps its value
        while isinstance(x, exp.Paren):
            x = x.this
        num = x.this if isinstance(x, exp.Neg) else x
        if not isinstance(num, exp.Literal) or num.is_string:
            return False
        if re.fullmatch(r"\d+", str(num.this)) or to.this in (T.FLOAT, T.DOUBLE):
            return True
        return exact_decimal and to.this in (T.DECIMAL, T.BIGDECIMAL) and not to.expressions

    def stringy(to) -> bool:  # a string type with no length, which can't truncate a value
        return to.this in exp.DataType.TEXT_TYPES | {exp.DataType.Type.BPCHAR} and not to.expressions

    def bare(x, literal_side: bool = False):
        # Unwrap only casts that can't change which values match: to a string type (Postgres
        # writes `(status)::text = ANY (...)`), or, for a literal, a number to a number type or an
        # array to an array of either. CAST(status AS INTEGER) IN (1, 2) also admits '01'.
        while isinstance(x, (exp.Paren, exp.Cast)):
            if isinstance(x, exp.Cast):
                to, inner = x.to, x.this
                while isinstance(inner, exp.Paren):
                    inner = inner.this
                ok = stringy(to)
                if literal_side and not ok:
                    if to.this == exp.DataType.Type.ARRAY:
                        el = to.expressions[0] if to.expressions else None
                        ok = el is not None and (stringy(el) or (
                            el.this in exp.DataType.NUMERIC_TYPES and isinstance(inner, exp.Array)
                            and all(lossless(el, v) for v in inner.expressions)))
                    elif to.this in exp.DataType.NUMERIC_TYPES:
                        ok = lossless(to, inner)
                if not ok:
                    return x
            x = x.this
        return x

    def terms(x):
        x = bare(x)
        if isinstance(x, exp.Or):
            return terms(x.this) + terms(x.expression)
        return [x]

    def literal(x) -> Optional[str]:
        x = bare(x, literal_side=True)
        if isinstance(x, exp.Introducer):  # MySQL's _utf8mb4'low'
            x = x.expression
        if isinstance(x, exp.Neg) and isinstance(x.this, exp.Literal) and not x.this.is_string:
            return f"-{x.this.this}"
        if isinstance(x, exp.National):  # T-SQL's N'low'
            return str(x.this)
        if isinstance(x, exp.Literal):
            return str(x.this)
        return None

    name: Optional[str] = None
    values: list[str] = []
    for t in terms(e):
        if isinstance(t, exp.In) and not t.args.get("query") and not t.args.get("unnest"):
            col, lits = bare(t.this), list(t.expressions)
        elif isinstance(t, exp.EQ):
            col, rhs = bare(t.this), bare(t.expression)
            if not isinstance(col, exp.Column):
                col, rhs = rhs, col
            if isinstance(rhs, exp.Any):  # Postgres stores IN (...) as = ANY (ARRAY[...])
                arr = bare(rhs.this, literal_side=True)
                if not isinstance(arr, exp.Array):
                    return None
                lits = list(arr.expressions)
            else:
                lits = [rhs]
        else:
            return None
        if not isinstance(col, exp.Column) or not lits:
            return None
        if name is None:
            name, quoted = col.name, col.this.quoted
        elif (name != col.name if quoted or col.this.quoted else name.lower() != col.name.lower()):
            return None  # a quoted name is exact: `status` and "STATUS" are two columns
        for lit in lits:
            v = literal(lit)
            if v is None:
                return None
            values.append(v)
    return (name, values) if name else None


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


def _maybe_choice(values: list, rows: Optional[int] = None) -> Optional[dict[str, str]]:
    """A `{value: ""}` skeleton from a column's values, or None when they don't look like codes.

    `rows` is how many rows hold them (default: one per value). Values are kept exactly as stored:
    a query must match 'Open ' to find those rows, so trimming it would list a value that isn't there."""
    vals = [str(v) for v in values if v is not None and str(v) != ""]
    distinct = sorted(set(vals))
    # At least TWO distinct values: a list of one ("current_flag: N") says nothing a filter needs.
    # And enough rows that the values repeat — a few rows of unique names are not codes.
    n = len(vals) if rows is None else rows
    if not (1 < len(distinct) <= ENUM_MAX_DISTINCT and n >= max(10, 2 * len(distinct))):
        return None
    # GUID/UUID-valued columns are join keys, not enums — a real enum (severity 1/2/3, status
    # open/closed) never holds identifier values. One such distinct value disqualifies the column.
    if any(_GUID_VALUE_RE.match(d) for d in distinct):
        return None
    # low-cardinality, non-identifier values -> likely an enum
    return {d: "" for d in distinct}  # labels filled in by LLM enrichment


def _free_path(path: Path) -> Path:
    """`path`, or the first `path.N` that does not exist yet. A second re-onboarding finds the first
    one's backup already there; moving onto it raises (a file on Windows, a non-empty directory
    everywhere), and replacing it would lose the older model."""
    n = 1
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{n}")
        n += 1
    return candidate


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
            src.rename(_free_path(backup / name))
            moved = True
    # per-schema legacy dirs contain a _schema.yaml; move those too
    if profile_root.exists():
        for child in list(profile_root.iterdir()):
            if child.is_dir() and (child / "_schema.yaml").exists():
                backup.mkdir(parents=True, exist_ok=True)
                child.rename(_free_path(backup / child.name))
                moved = True
    return None


__all__ = ["introspect", "make_execute_sql_runner", "IntrospectReport", "Runner"]
