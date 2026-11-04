"""Loader for the agami semantic-model-v2 on-disk layout.

On-disk tree (design doc's "Storage layout on disk"), rooted at the profile dir
``<artifacts_dir>/<profile>/``:

    <root>/
      datasource.yaml                                 # org desc + storage_connections + subject_areas refs
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

The loader parses the tree into a single `Datasource` model (so the validator
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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from .models import (
    Column,
    CrossSubjectAreaRelationship,
    Datasource,
    Entity,
    Metric,
    Relationship,
    StorageConnection,
    SubjectArea,
    Table,
    TableRef,
    bare_name,
    stored_identity,
    table_key,
)

# The default `include` set for get_table_context / `sm context`. performance_hints
# is in it so estimated_row_count reaches the answer receipt (the "≈N rows"
# provenance) and the scan-risk check — without it the receipt falls back to "rows
# unknown" even when the model has the count. Single source so the three call sites
# (get_table_context, get_subject_area_bundle, the `sm context --include` default)
# can't drift. The bundle adds "metrics" on top (see DEFAULT_BUNDLE_INCLUDE).
DEFAULT_CONTEXT_INCLUDE = (
    "default_filters", "relationships", "caveats", "value_transforms", "performance_hints",
)
DEFAULT_BUNDLE_INCLUDE = DEFAULT_CONTEXT_INCLUDE + ("metrics",)


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# The top-level keys `load_datasource` reads, which are exactly `Datasource`'s fields. Every other
# key in datasource.yaml is dropped by that read, so a model written for a newer agami-core lost a
# field it declared without a word, and a deploy fed by that load stored a model without it (#575).
# Reported by the callers a person reads (`sm validate`, the deploy), never from the load itself: the
# executor loads the model in a child process whose stderr is its output contract.
_DATASOURCE_KEYS = frozenset(Datasource.model_fields)


def unknown_datasource_keys(root: str | Path) -> list[str]:
    """The top-level keys in `<root>/datasource.yaml` this agami-core does not read, sorted."""
    doc = _read_yaml(Path(root) / "datasource.yaml")
    if not isinstance(doc, dict):
        return []  # malformed: `load_datasource` reports it
    return sorted(k for k in doc if isinstance(k, str) and k not in _DATASOURCE_KEYS)


def unknown_keys_message(keys: list[str]) -> str:
    return (f"datasource.yaml has keys this agami-core does not read, so they are ignored: "
            f"{', '.join(keys)}. The model may need a newer agami-core.")


def load_datasource(root: str | Path, *, include_rejected: bool = False) -> Datasource:
    """Parse a v2 profile directory into a Datasource model.

    By default, entries the curator excluded (`review_state: rejected`) are dropped
    so the runtime never sees them. Pass `include_rejected=True` for the curation
    tools (agami-model), which must show excluded entries to toggle them.
    """
    root = Path(root)
    datasource_path = root / "datasource.yaml"
    if not datasource_path.exists():
        raise FileNotFoundError(f"no datasource.yaml at {datasource_path}")
    ds_doc: dict[str, Any] = _read_yaml(datasource_path) or {}

    # storage connections — inline list OR refs into datasources/<c>/storage.yaml
    connections: list[StorageConnection] = []
    for sc in ds_doc.get("storage_connections", []) or []:
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
    hidden = _Hidden()   # what is on disk but not served, by (schema, table)
    for sa_ref in ds_doc.get("subject_areas", []) or []:
        sa_dir = root / (sa_ref if isinstance(sa_ref, str) else sa_ref.get("path", ""))
        if not sa_dir.exists():
            # also accept a bare name under subject_areas/
            sa_dir = root / "subject_areas" / str(sa_ref)
        subject_areas.append(_load_subject_area(sa_dir, include_rejected=include_rejected, hidden=hidden))

    org = Datasource(
        org_id=ds_doc.get("org_id"),  # F14: minted uuid4 (None for pre-F14 files)
        datasource=ds_doc.get("datasource", root.name),
        version=ds_doc.get("version", 1),
        description=ds_doc.get("description", ""),
        fiscal_year_start_month=ds_doc.get("fiscal_year_start_month", 1),
        storage_connections=connections,
        subject_areas=subject_areas,
        cross_subject_area_relationships=_load_cross_rels(root, ds_doc),
        cross_subject_area_entities=_load_cross_entities(root, ds_doc),
        cross_subject_area_metrics=_load_cross_metrics(root, ds_doc),
        key_terminology=ds_doc.get("key_terminology", {}) or {},
        ambiguities=ds_doc.get("ambiguities", []) or [],
    )
    if not include_rejected:
        before = _metric_names(org)
        # The cross-area joins, entities and metrics need the same filter the per-area loader applies
        # (#543): without it a join rejected at sign-off between two areas reached `sm context`, the
        # schema reply and a deploy with no review status, and looked usable.
        org.cross_subject_area_relationships = [
            r for r in org.cross_subject_area_relationships if not _rejected(r)
        ]
        org.cross_subject_area_entities = [
            e for e in org.cross_subject_area_entities if not _rejected(e)
        ]
        org.cross_subject_area_metrics = [
            mm for mm in org.cross_subject_area_metrics if not _rejected(mm)
        ]
        _drop_uses_of_unserved(org, hidden)
        hidden.metrics_removed |= before - _metric_names(org)
        # Always, not only when a table or column is hidden (#571): a derived metric whose base was
        # rejected would fail to expand, and validation reported "references unknown base metric".
        _drop_metrics_on_removed_bases(org, hidden.metrics_removed)
        # Two areas may share a metric name: rejecting one copy doesn't remove the name.
        hidden.metrics_removed -= _metric_names(org)
        # Always, not only when a table or column is hidden: a rejected metric hides nothing else.
        org.ambiguities = _ambiguities_served(org.ambiguities, hidden, org)
    return org


def _metric_names(org: Datasource) -> set[str]:
    return {m.name for sa in org.subject_areas for m in sa.metrics} | {
        m.name for m in org.cross_subject_area_metrics}


def _key(schema: Optional[str], table: str) -> tuple[str, str]:
    from .models import table_key

    return table_key(table, schema)


@dataclass
class _Hidden:
    """What a runtime load leaves out, by (schema, table) — `sales.orders` and `archive.orders` differ.

    Filled per area copy of a table and merged: two areas may each define the same table, so a
    column (or table) is hidden only when NO copy serves it, and only on positive evidence — it is
    on disk and excluded or stale. A column that simply isn't listed is not evidence of anything."""
    served: dict = field(default_factory=dict)     # table -> columns some copy serves
    on_disk: dict = field(default_factory=dict)    # table -> columns some copy excluded or found stale
    dropped: set = field(default_factory=set)      # tables some copy excluded or found stale whole
    metrics_removed: set = field(default_factory=set)  # metric names on disk but not served (#477)
    # (table key, folded group): a group some copy emptied on load, and one some copy still serves.
    # Removed means emptied and served by no copy, as for columns.
    groups_emptied: set = field(default_factory=set)
    groups_served: set = field(default_factory=set)

    def group_removed(self, table: str, group: str) -> bool:
        k = self._resolve(None, table)
        g = (k, group.lower())
        return k is not None and g in self.groups_emptied and g not in self.groups_served

    def keep(self, schema: Optional[str], name: str, kept: list, all_cols: list) -> None:
        k = _key(schema, name)
        self.served.setdefault(k, set()).update(c.name.lower() for c in kept)
        self.on_disk.setdefault(k, set()).update(c.name.lower() for c in all_cols if c not in kept)

    def drop(self, schema: Optional[str], name: str) -> None:
        self.dropped.add(_key(schema, name))

    def _resolve(self, schema: Optional[str], table: str) -> Optional[tuple[str, str]]:
        """The table a reference means — by its schema when given (a stored table without one also
        matches, as the validator reads it), else by bare name when exactly one table has it."""
        known = set(self.served) | self.dropped
        k = _key(schema, table)
        if k[0]:
            if k in known:
                return k
            k = ("", k[1])
            return k if k in known else None
        same = [x for x in known if x[1] == k[1]]
        return same[0] if len(same) == 1 else None

    def table_hidden(self, schema: Optional[str], table: str) -> bool:
        k = self._resolve(schema, table)
        return k is not None and k in self.dropped and k not in self.served

    def column_hidden(self, schema: Optional[str], table: str, column: Optional[str]) -> bool:
        if column is None:
            return False
        k = self._resolve(schema, table)
        if k is None:
            return False   # not a table of this model, or ambiguous: not ours to judge
        c = column.lower()
        return c in self.on_disk.get(k, set()) and c not in self.served.get(k, set())

    def any_hidden(self) -> bool:
        return any(self.table_hidden(None, n) or self.table_hidden(s, n) for s, n in self.dropped) or any(
            cols - self.served.get(k, set()) for k, cols in self.on_disk.items())


def _entities_served(entities: list, end_hidden) -> list:
    """Entities with mappings to hidden columns or tables removed; one left with none is dropped."""
    kept = []
    for e in entities:
        mapped = [mp for mp in e.maps_to if not end_hidden(None, mp.table, mp.column)]
        if e.maps_to and not mapped:
            continue   # every column it named is hidden: nothing left to resolve it to
        e.maps_to = mapped
        kept.append(e)
    return kept


def _drop_uses_of_unserved(org: Datasource, hidden: "_Hidden") -> None:
    """Leave out of the runtime view whatever names a table or column on disk that it doesn't serve.

    A column excluded by a curator or found stale by a refresh is dropped from its table above, and a
    table excluded or stale is dropped whole; anything still naming one would offer the agent SQL the
    model's checks or the database refuse: a join (or a grain column) on it, a reference to the table,
    a default filter, an entity mapping, a metric whose SQL reads it. All stay on disk
    (`include_rejected=True`) for a person to decide. Only positive evidence hides anything: a name
    this model doesn't have, can't tell apart, or never listed is left alone for the validator."""
    if not hidden.any_hidden():
        return   # the common case: nothing is excluded or stale, so nothing changes
    from .validator import _binding_column_refs, _columns_referenced

    def end_hidden(schema, table, column) -> bool:
        return hidden.table_hidden(schema, table) or hidden.column_hidden(schema, table, column)

    from .runtime import join_column_pairs, model_dialect
    sg = model_dialect(org)

    def join_ok(r) -> bool:
        # Every column the join is keyed on: one hidden column of a composite key hides the join,
        # and so does a hidden key column of a join that also filters on a fixed value.
        pairs = join_column_pairs(r, sg) or [(r.from_column, r.to_column)]
        return not any(end_hidden(r.from_schema, r.from_table, fc) or end_hidden(r.to_schema, r.to_table, tc)
                       for fc, tc in pairs)

    def metric_ok(met) -> bool:
        tables = list(met.source_tables or ([met.primary_table] if met.primary_table else []))
        if any(hidden.table_hidden(None, tn) for tn in tables):
            return False
        # The metric's SQL: its per-dialect bindings when it has them — `calculation` is then often
        # prose ("the total of every paid order"), whose words must not match a column — else the
        # calculation itself, which is where a model spec writes the SQL.
        refs: set[str] = set()
        for sql in (met.bindings or {}).values() or [met.calculation]:
            refs |= _binding_column_refs(sql or "")
        # Unqualified refs: hidden only if a source table hides it and no source table serves it.
        return not any(
            any(hidden.column_hidden(None, tn, ref) for tn in tables)
            and not any(ref in hidden.served.get(hidden._resolve(None, tn) or ("", ""), set()) for tn in tables)
            for ref in refs)

    for sa in org.subject_areas:
        for t in sa.tables_defined:
            t.grain = [g for g in t.grain if not hidden.column_hidden(t.schema_name, t.name, g)]
            t.unique_keys = [k for k in t.unique_keys
                             if not any(hidden.column_hidden(t.schema_name, t.name, c) for c in k)]
            if t.default_filters:
                alias = bare_name(t.name)
                t.default_filters = [
                    f for f in t.default_filters
                    if not any(hidden.column_hidden(t.schema_name, t.name, c)
                               for c in _columns_referenced(f.replace("{alias}", alias), t.name))]
        sa.tables = [r for r in sa.tables if not hidden.table_hidden(r.schema_name, r.table)]
        sa.relationships = [r for r in sa.relationships if join_ok(r)]
        sa.entities = _entities_served(sa.entities, end_hidden)
        sa.metrics = [mm for mm in sa.metrics if metric_ok(mm)]
    org.cross_subject_area_entities = _entities_served(org.cross_subject_area_entities, end_hidden)
    org.cross_subject_area_relationships = [r for r in org.cross_subject_area_relationships if join_ok(r)]
    org.cross_subject_area_metrics = [mm for mm in org.cross_subject_area_metrics if metric_ok(mm)]


def _ambiguities_served(ambiguities: list, hidden: "_Hidden", org: Datasource) -> list:
    """A declared ambiguity without what the runtime doesn't serve (#473, #477): a hidden table, an
    excluded or stale column, a group whose every column is hidden, a rejected or dropped metric.
    Only on positive evidence: a name the model simply lacks is the validator's to report. An entry
    naming nothing that survives is left out, never widened to the whole datasource. One naming
    nothing at all concerns the whole datasource and stays. The file on disk is untouched."""
    # A ref without its table is malformed: left for the validator, never treated as removed.
    def column_gone(ref: str) -> bool:
        if "." not in ref:
            return False
        t, c = ref.rsplit(".", 1)
        return hidden.table_hidden(None, t) or hidden.column_hidden(None, t, c)

    def group_gone(ref: str) -> bool:
        if "." not in ref:
            return False
        t, g = ref.rsplit(".", 1)
        # Only the schema-aware record: emptied by some copy of this table and served by none.
        return hidden.table_hidden(None, t) or hidden.group_removed(t, g)

    out = []
    for a in ambiguities:
        if not a.anchored():
            out.append(a)
            continue
        # The intact name: `table_hidden` parses any qualifier itself, catalog.schema.table included.
        kept = [t for t in a.applies_to if not hidden.table_hidden(None, t)]
        cols = [c for c in a.columns if not column_gone(c)]
        grps = [g for g in a.column_groups if not group_gone(g)]
        mets = [m for m in a.metrics if m not in hidden.metrics_removed]
        if not (kept or cols or grps or mets):
            continue
        update = {"applies_to": kept, "columns": cols, "column_groups": grps, "metrics": mets}
        same = (kept, cols, grps, mets) == (a.applies_to, a.columns, a.column_groups, a.metrics)
        out.append(a if same else a.model_copy(update=update))
    return out


def _drop_metrics_on_removed_bases(org: Datasource, removed: set[str]) -> None:
    """A derived metric built on a metric that was left out would fail to expand; leave it out too,
    and add its name to `removed`, repeating until no served metric is built on one. Bases are read
    the way expansion reads them — `base_metrics` and `{…}` placeholders, looked up in
    `metric_index`.

    Only a base in `removed` (on disk, but rejected or hidden) drops a metric. A base this model never
    had is a mistake in the model, not something the runtime chose to leave out, so it stays for the
    validator to report rather than vanishing from the served view without a word."""
    from . import derived as D

    def bases(mm) -> list[str]:
        refs = list(mm.base_metrics or [])
        for b in (mm.bindings or {}).values():
            refs += D.binding_refs(b)
        return refs

    while True:
        idx = D.metric_index(org)
        changed = False
        for holder in [*org.subject_areas, org]:
            attr = "metrics" if holder is not org else "cross_subject_area_metrics"
            current = getattr(holder, attr)
            dropped = [mm for mm in current if any(b in removed and b not in idx for b in bases(mm))]
            if dropped:
                removed.update(mm.name for mm in dropped)
                gone = {id(d) for d in dropped}
                setattr(holder, attr, [mm for mm in current if id(mm) not in gone])
                changed = True
        if not changed:
            return


def load_org_id(root: str | Path) -> str | None:
    """Return the minted org_id recorded in ``<root>/datasource.yaml``, or ``None`` if the file is absent
    or predates F14 (no ``org_id`` key). This is the serve-time identity read (F14 / ACE-056).

    Deliberately read-only and lenient — unlike ``load_datasource`` it never raises on a missing
    file — so the single-tenant resolver can fall through to its ``"local"`` default. Reads only the
    top-level key; it does NOT build the full pydantic model (identity resolution runs per process,
    not per query, so it stays cheap)."""
    datasource_path = Path(root) / "datasource.yaml"
    if not datasource_path.exists():
        return None
    doc = _read_yaml(datasource_path) or {}
    oid = doc.get("org_id")
    return oid or None


def deployment_org_id(artifacts_dir: str | Path) -> str | None:
    """The deployment-wide org identity: the first ``org_id`` found across ``<artifacts_dir>/*/datasource.yaml``,
    or ``None`` if no profile carries one (F14 / ACE-056, the *deployment-scoped* rule).

    A deployment is ONE tenant even with several datasource profiles, so the minted id is shared across
    every profile's ``datasource.yaml``. Resolving by scanning the artifacts dir (rather than one 'active'
    profile) means the deploy stamp and the serve resolver agree even when ``AGAMI_PROFILE`` is unset and
    the real model lives under a named profile (e.g. ``acme_salesforce``, not ``default``). Read-only;
    never raises. Profiles are expected to agree; the first (sorted) is returned deterministically."""
    root = Path(artifacts_dir)
    if not root.is_dir():
        return None
    for prof in sorted(root.iterdir()):
        if prof.is_dir():
            oid = load_org_id(prof)
            if oid:
                return oid
    return None


def _rejected(obj) -> bool:
    return getattr(obj, "review_state", None) == "rejected"


def _prune_column_groups(t: Table) -> list[str]:
    """Drop excluded/missing columns from a table's `column_groups` (and its descriptions),
    removing any group left empty. Keeps a loaded model self-consistent after per-column
    exclusions — without this, a group still listing a dropped column fails validation. Returns
    the groups it emptied, the evidence an ambiguity tied to one of them needs (#477)."""
    if not t.column_groups:
        return []
    live = {c.name for c in t.columns}
    pruned = {g: [c for c in cols if c in live] for g, cols in t.column_groups.items()}
    emptied = [g for g, cols in pruned.items() if not cols]
    pruned = {g: cols for g, cols in pruned.items() if cols}
    t.column_groups = pruned
    if t.column_group_descriptions:
        t.column_group_descriptions = {g: d for g, d in t.column_group_descriptions.items() if g in pruned}
    return emptied


def _reconcile_table_ref_exposes(refs: "list[TableRef]", tables_defined: "list[Table]") -> None:
    """Drop `expose_column_groups` names that no longer exist on the table (a group emptied by
    column exclusion). Mirrors curate._reconcile_expose_groups: clear the field entirely when it
    would expose every surviving group (or nothing). Keeps a loaded model self-consistent."""
    groups_by_table = {t.name: set(t.column_groups.keys()) for t in tables_defined}
    for r in refs:
        if not r.expose_column_groups:
            continue
        valid = groups_by_table.get(r.table)
        if valid is None:
            continue
        kept = [g for g in r.expose_column_groups if g in valid]
        if not kept or set(kept) == valid:
            r.expose_column_groups = None
        elif kept != r.expose_column_groups:
            r.expose_column_groups = kept


def _load_subject_area(sa_dir: Path, include_rejected: bool = False,
                       hidden: Optional["_Hidden"] = None) -> SubjectArea:
    sa_doc: dict[str, Any] = _read_yaml(sa_dir / "subject_area.yaml") or {}

    tables_defined: list[Table] = []
    tdir = sa_dir / "tables"
    if tdir.exists():
        for tf in sorted(tdir.glob("*.yaml")):
            t = Table(**(_read_yaml(tf) or {}))
            if not include_rejected:
                if _rejected(t) or t.review_state == "stale":
                    if hidden is not None:
                        hidden.drop(t.schema_name, t.name)
                    continue  # whole table excluded by the curator, or no longer in the database
                # drop per-column exclusions, and columns a refresh found the database no longer has
                # (`stale`, #437): kept on disk for a person to decide, never served, since a query
                # naming one would pass the model's checks and then fail at the database.
                kept = [c for c in t.columns if not _rejected(c) and c.review_state != "stale"]
                if hidden is not None:
                    hidden.keep(t.schema_name, t.name, kept, t.columns)
                t.columns = kept
                served = {c.name for c in kept}
                t.unique_keys = [k for k in t.unique_keys if set(k) <= served]
                # ...and prune those dropped columns out of column_groups, else a group still
                # naming an excluded column fails the column_group_missing_column check on load
                # (excluding a deep table's column would otherwise break the whole model).
                emptied = _prune_column_groups(t)
                if hidden is not None:
                    k = _key(t.schema_name, t.name)
                    hidden.groups_emptied.update((k, g.lower()) for g in emptied)
                    hidden.groups_served.update((k, g.lower()) for g in t.column_groups)
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
                if hidden is not None:
                    hidden.metrics_removed.add(mm.name)
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
                if (bare_name(rel.from_table) not in live_tables
                        or bare_name(rel.to_table) not in live_tables):
                    continue
            relationships.append(rel)

    # TableRefs are kept as-is — they resolve org-wide (multi-membership), and a
    # ref to a rejected table simply won't resolve at runtime.
    table_refs = [TableRef(**t) for t in (sa_doc.get("tables", []) or [])]
    if not include_rejected:
        # a column-group that lost all its columns to exclusion was pruned above; drop any
        # TableRef.expose_column_groups still naming it, else `unknown_column_group` fails load.
        _reconcile_table_ref_exposes(table_refs, tables_defined)

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


def _load_cross_rels(root: Path, ds_doc: dict) -> list[CrossSubjectAreaRelationship]:
    out: list[CrossSubjectAreaRelationship] = []
    # inline on datasource.yaml
    for r in ds_doc.get("cross_subject_area_relationships", []) or []:
        out.append(CrossSubjectAreaRelationship(**r))
    # or a sidecar file
    f = root / "cross_subject_area_relationships.yaml"
    if f.exists():
        doc = _read_yaml(f) or {}
        for r in doc.get("edges", doc if isinstance(doc, list) else []):
            out.append(CrossSubjectAreaRelationship(**r))
    return out


def _load_cross_entities(root: Path, ds_doc: dict) -> list[Entity]:
    out = [Entity(**e) for e in (ds_doc.get("cross_subject_area_entities", []) or [])]
    f = root / "cross_subject_area_entities.yaml"
    if f.exists():
        doc = _read_yaml(f) or {}
        for e in doc.get("entities", doc if isinstance(doc, list) else []):
            out.append(Entity(**e))
    return out


def _load_cross_metrics(root: Path, ds_doc: dict) -> list[Metric]:
    out = [Metric(**mm) for mm in (ds_doc.get("cross_subject_area_metrics", []) or [])]
    f = root / "cross_subject_area_metrics.yaml"
    if f.exists():
        doc = _read_yaml(f) or {}
        for mm in doc.get("metrics", doc if isinstance(doc, list) else []):
            out.append(Metric(**mm))
    return out


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


def _choose(cands: list[Table], query: str,
            areas: "dict[str, set[int]] | None" = None) -> list[Table]:
    """The tables `query` names among `cands` (every table whose name is `query` or whose bare name
    is `query`'s, in scan order). Several only when an unqualified name is declared under several
    schemas, which is the caller's to show as ambiguous rather than this function's to settle.

    A qualified name that is some table's exact name is that table. Otherwise it resolves by schema
    (#258): with `public.orders` and `archive.orders` declared, `archive.orders` is that table and
    never the other. A qualifier no candidate's schema matches finds a candidate only where it
    cannot be naming another schema: it is a subject area's name (`areas`: folded name -> ids of
    the tables it defines), the prefix `bare_name` has always stripped, which picks that area's
    table; or the lone candidate records no schema.
    """
    if "." not in query:
        distinct: dict[tuple[str, str], Table] = {}
        for t in cands:
            distinct.setdefault(table_key(t.name, t.schema_name), t)
        return list(distinct.values())
    exact = [t for t in cands if t.name == query]
    if exact:
        return exact[:1]
    want = table_key(query)[0]
    same = [t for t in cands if table_key(t.name, t.schema_name)[0] == want]
    if same:
        return same[:1]
    in_area = [t for t in cands if id(t) in (areas or {}).get(want, set())]
    if in_area:
        return in_area[:1]
    if len(cands) == 1 and not table_key(cands[0].name, cands[0].schema_name)[0]:
        return cands
    return []


@dataclass(frozen=True)
class TableIndex:
    """O(1) name→Table lookup that reproduces `_find_table` EXACTLY, so the schema hot path stops
    linear-scanning the whole model per table (ACE-047, scalability-audit finding P12). Match rule:
    `_choose` over the tables whose name is the query or whose bare name is its bare form, in scan
    order; area scoping and the TableRef multi-area fallback are preserved. Built once from the
    cached model and threaded through get_table_context; identical to the scan."""

    org_wide: dict[str, list[Table]]                 # bare name -> tables, in scan order
    per_area: dict[str, dict[str, list[Table]]]      # area -> {bare name -> tables}
    area_refs: dict[str, list[TableRef]]             # area -> its TableRefs, for the fallback
    areas: "dict[str, set[int]]" = field(default_factory=dict)  # for `_choose`

    def _pick(self, m: dict[str, list[Table]], query: str, bare: str) -> list[Table]:
        return _choose(m.get(bare, []), query, self.areas)

    def find_all(self, table_name: str, area: Optional[str] = None) -> list[Table]:
        bare = bare_name(table_name)
        if area:
            hit = self._pick(self.per_area.get(area, {}), table_name, bare)
            if hit:
                return hit
            refs = _refs_naming(self.area_refs.get(area, []), table_name, bare)
            if refs:
                return _choose(_referenced(self.org_wide.get(bare, []), refs), table_name,
                               self.areas)
            return []
        return self._pick(self.org_wide, table_name, bare)


def build_table_index(org: Datasource) -> TableIndex:
    """Build the O(1) name→Table index once per schema call (ACE-047). Scan order (areas, then
    tables) matches `_find_table`, so a clash resolves identically."""
    org_wide: dict[str, list[Table]] = {}
    per_area: dict[str, dict[str, list[Table]]] = {}
    area_refs: dict[str, list[TableRef]] = {}
    for sa in org.subject_areas:
        amap: dict[str, list[Table]] = {}
        for t in sa.tables_defined:
            org_wide.setdefault(bare_name(t.name), []).append(t)
            amap.setdefault(bare_name(t.name), []).append(t)
        # Area names are NOT enforced unique; `_find_table` resolves an area via
        # `org.subject_area(area)`, which returns the FIRST area of that name — so the per-area maps
        # must be first-wins too (setdefault), or a duplicate name would resolve the wrong area.
        per_area.setdefault(sa.name, amap)
        area_refs.setdefault(sa.name, list(sa.tables))
    return TableIndex(org_wide=org_wide, per_area=per_area, area_refs=area_refs,
                      areas=_area_names(org))


def _area_names(org: Datasource) -> "dict[str, set[int]]":
    """Folded area name -> ids of the tables the areas of that name define, for `_choose`."""
    areas: dict[str, set[int]] = {}
    for sa in org.subject_areas:
        areas.setdefault(sa.name.lower(), set()).update(id(t) for t in sa.tables_defined)
    return areas


def _find_tables(
    org: Datasource, table_name: str, area: Optional[str] = None,
    *, index: Optional[TableIndex] = None,
) -> list[Table]:
    """Every table `table_name` names (see `_choose`): one, none, or each schema's copy of an
    ambiguous unqualified name."""
    if index is not None:
        return index.find_all(table_name, area)
    bare = bare_name(table_name)
    areas = [org.subject_area(area)] if area else org.subject_areas
    cands = [t for sa in areas if sa is not None for t in sa.tables_defined
             if bare_name(t.name) == bare]
    found = _choose(cands, table_name, _area_names(org))
    if found:
        return found
    # Multi-area membership: a table is DEFINED in exactly one area but may be REFERENCED from
    # others via a TableRef (a shared dimension like `sys_user`). When the named area lists it
    # among its `tables`, fall back to the org-wide definition instead of reporting "not found".
    if area:
        sa = org.subject_area(area)
        refs = _refs_naming(sa.tables, table_name, bare) if sa else []
        if refs:
            everywhere = [t for other in org.subject_areas for t in other.tables_defined
                          if bare_name(t.name) == bare]
            return _choose(_referenced(everywhere, refs), table_name, _area_names(org))
    return []


def _refs_naming(refs: list[TableRef], table_name: str, bare: str) -> list[TableRef]:
    """The TableRefs that name `table_name`, as written or by its bare name."""
    return [r for r in refs if r.table == table_name or r.table == bare]


def _referenced(cands: list[Table], refs: list[TableRef]) -> list[Table]:
    """The tables among `cands` those refs reference: by the ref's schema where it gives one, so an
    area listing `public.orders` does not also reach `archive.orders` (#258)."""
    wanted = {table_key(r.table, r.schema_name)[0] for r in refs}
    if "" in wanted:
        return cands
    exact = [t for t in cands if table_key(t.name, t.schema_name)[0] in wanted]
    # A table recording no schema cannot contradict the ref's, as the validator accepts it.
    return exact or [t for t in cands if not table_key(t.name, t.schema_name)[0]]


def _find_table(
    org: Datasource, table_name: str, area: Optional[str] = None,
    *, index: Optional[TableIndex] = None,
) -> Optional[Table]:
    found = _find_tables(org, table_name, area, index=index)
    return found[0] if found else None


def display_name(org: Datasource, table: Table, *, index: Optional[TableIndex] = None) -> str:
    """The name a reply keys `table` by: its own, unless another schema declares the same bare name,
    in which case `schema.name` — the spelling `dataset_names` resolves back to this table (#258).
    Pass `index` to read the same-named tables from it rather than scanning the model (ACE-047)."""
    schema, bare = stored_identity(table.name, table.schema_name)
    if not schema or "." in table.name:
        return table.name
    same = (index.org_wide.get(bare, []) if index is not None else
            [t for sa in org.subject_areas for t in sa.tables_defined if bare_name(t.name) == bare])
    schemas = {table_key(t.name, t.schema_name)[0] for t in same}
    return f"{schema}.{bare}" if len(schemas) > 1 else table.name


def _table_alias(table_name: str) -> str:
    return bare_name(table_name)


def collect_default_filters(
    org: Datasource,
    table_names: Iterable[str],
    *,
    area: Optional[str] = None,
    params: Optional[dict[str, str]] = None,
    index: Optional[TableIndex] = None,
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
        table = _find_table(org, name, area, index=index)
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


def _exposed_groups_for(sa: SubjectArea, table: Table) -> Optional[list[str]]:
    """The column groups `sa` exposes of `table`: from the TableRef naming it, by schema where the
    ref gives one, so a same-named table in another schema does not lend it its view (#258)."""
    schema, bare = table_key(table.name, table.schema_name)
    for ref in sa.tables:
        ref_schema, ref_bare = table_key(ref.table, ref.schema_name)
        if ref_bare == bare and (not ref_schema or not schema or ref_schema == schema):
            return ref.expose_column_groups
    return None


def get_table_context(
    org: Datasource,
    tables: list[str],
    *,
    area: Optional[str] = None,
    columns: Optional[list[str]] = None,
    include: Optional[list[str]] = None,
    params: Optional[dict[str, str]] = None,
    index: Optional[TableIndex] = None,
) -> dict[str, Any]:
    """Compound context fetch — the primary mechanism for keeping inference rounds
    low. Returns columns (full detail for the requested set, honoring
    expose_column_groups), plus any of: default_filters, relationships, caveats,
    value_transforms, metrics.

    Pass `index` (from `build_table_index`) to resolve tables in O(1) instead of a per-table linear
    scan — used by the schema hot path on wide models (ACE-047). Behaviour is identical either way.
    """
    include = include or list(DEFAULT_CONTEXT_INCLUDE)
    sa = org.subject_area(area) if area else None
    result: dict[str, Any] = {"tables": {}}

    for name in tables:
        found = _find_tables(org, name, area, index=index)
        if not found:
            result["tables"][name] = {"error": "not found in scope"}
        for table in found:
            result["tables"][display_name(org, table, index=index)] = _table_detail(
                org, table, sa, area=area, columns=columns, include=include, params=params,
                index=index)

    if "relationships" in include:
        result["relationships"] = _relationships_among(org, tables, area)
    if "metrics" in include:
        result["metrics"] = _metrics_for(org, tables, area)

    return result


def _table_detail(
    org: Datasource, table: Table, sa: Optional[SubjectArea], *, area: Optional[str],
    columns: Optional[list[str]], include: list[str], params: Optional[dict[str, str]],
    index: Optional[TableIndex],
) -> dict[str, Any]:
    """One table's entry in a `get_table_context` reply."""
    exposed = _exposed_groups_for(sa, table) if sa else None
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
            org, [display_name(org, table, index=index)], area=area, params=params, index=index
        )
    if "performance_hints" in include and table.performance_hints:
        tinfo["performance_hints"] = table.performance_hints.model_dump(exclude_none=True)
    return tinfo


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
    served = col.served_placeholders() if "caveats" in include else []
    if served:
        # What a person said each placeholder value means, and how to write SQL for it (#445).
        from .placeholders import MEANINGS
        d["placeholders"] = [{"value": p.value, "meaning": p.meaning, "how": MEANINGS[p.meaning]}
                             for p in served]
    return d


# The join, and what governs whether it can run. Everything an agent needs to WRITE the join —
# so an edge reduced to these keys is still usable, which is why the lean tier below is a
# projection and not a removal.
#
# `on` is here because an edge carries EITHER from_column/to_column OR that SQL-expression escape
# hatch (CAST, compound and function-based joins), never both. Omitting it would leave the lean
# form of such an edge with no join condition at all.
_JOIN_KEYS = ("from_table", "to_table", "from_column", "to_column", "on",
              "from_schema", "to_schema", "join_type", "relationship")


def _lean_edge(d: dict[str, Any]) -> dict[str, Any]:
    """An edge projected to its join, for a table the caller did not ask for.

    Dropped: the sign-off block, `review_state`/`confidence`, the subject-area labels, and
    `description` — whose GENERATED form restates the two columns present in the same object
    (a curator can write a real one, and that is the case this trades away).

    Dropping the trust block costs the agent nothing, which is the part worth stating: the
    receipt recomputes `review_state` and the sign-off for each join the STATEMENT actually
    wrote, from the model, at execution time (`runtime.py`). An unreviewed-join warning does not
    depend on these fields having ridden along in the schema response.

    `executable` survives whenever it is NOT `same_engine`: it says whether one statement can
    perform this join at all, so dropping it on a `split` edge invites a join that cannot run.
    Omitting the common value keeps the ordinary case free and the cross-engine case correct.
    (`federated` is not reachable here — `Relationship` rejects it outright as metric-only, so
    the values that survive this branch are `split` and `informational`.)
    """
    out = {k: d[k] for k in _JOIN_KEYS if k in d}
    # `executable` has a non-optional default, so `exclude_none` always leaves it present.
    if d["executable"] != "same_engine":
        out["executable"] = d["executable"]
    return out


def _full_edge(d: dict[str, Any]) -> dict[str, Any]:
    """An edge for a table pair the caller asked for — everything, minus what says nothing.

    `for_questions_about` is deprecated and never written (see its note on `Relationship`), so it
    rides along as an empty list on every edge. `executable` is dropped at its default for the
    reason `_lean_edge` drops it: naming the ordinary case on every edge is repetition, and a
    response that omits it on one edge and states it on the next is worse than either rule.
    """
    out = {k: v for k, v in d.items() if k != "for_questions_about"}
    if out.get("executable") == "same_engine":
        del out["executable"]
    return out


def _relationships_among(
    org: Datasource, tables: list[str], area: Optional[str]
) -> list[dict[str, Any]]:
    """Every edge touching a requested table — in full where BOTH ends were requested.

    An edge to a table the caller did not ask for cannot SELECT from the other side: the caller
    has no columns for it. It is still worth sending, because the scope gate admits any table the
    model declares and a correlated `EXISTS` needs only the join key — so dropping those edges
    would force a round trip for the subquery case. Sending them in full is the other extreme: on
    a hub table (a user or group dimension half the warehouse references) the block reached
    98,724 chars for a two-table request, of which four edges joined the two tables asked for.

    So: full detail where the caller can read both sides, the join itself everywhere else.
    """
    names = {_table_alias(t) for t in tables} | set(tables)

    # The TIER test compares schema-qualified identity, which the membership test above cannot:
    # `names` is bare, so with `orders` declared in two schemas a request for `public.orders`
    # would classify an `archive.orders` edge as full and resend the governance block for a table
    # the caller never asked for. `table_key` parses a qualifier out of the name when the schema
    # field is unset, so the legacy `public.orders` shape compares the same as the split form.
    #
    # Schema-less requests keep the bare behaviour: a caller that names no schema cannot be asking
    # for one in particular, and a model with no schemas at all (SQLite) has nothing to compare.
    from .models import table_key

    requested = {table_key(t) for t in tables}
    requested_bare = {bare for _sch, bare in requested}

    def _requested(name: str, schema: Optional[str]) -> bool:
        sch, bare = table_key(name, schema)
        if any(s for s, _b in requested):          # the caller qualified at least one table
            return (sch, bare) in requested or (not sch and bare in requested_bare)
        return bare in requested_bare

    def _both_ends_requested(rel) -> bool:
        return (_requested(rel.from_table, rel.from_schema)
                and _requested(rel.to_table, rel.to_schema))

    out: list[dict[str, Any]] = []
    areas = [org.subject_area(area)] if area else org.subject_areas
    for sa in areas:
        if sa is None:
            continue
        for rel in sa.relationships:
            if _table_alias(rel.from_table) in names or _table_alias(rel.to_table) in names:
                d = rel.model_dump(exclude_none=True)
                out.append(_full_edge(d) if _both_ends_requested(rel) else _lean_edge(d))
    # cross-area edges touching these tables
    for rel in org.cross_subject_area_relationships:
        if _table_alias(rel.from_table) in names or _table_alias(rel.to_table) in names:
            d = rel.model_dump(exclude_none=True)
            out.append(_full_edge(d) if _both_ends_requested(rel) else _lean_edge(d))
    return out


# What makes two lean edges the same kind of join between the same two tables. An edge with an
# `on:` expression or a non-default `executable` is never merged: its condition is its own.
_PAIR_KEYS = ("from_table", "to_table", "from_schema", "to_schema", "join_type", "relationship")


def _merge_lean_pairs(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lean edges joining the same two tables the same way, as ONE entry listing every column
    pair (#449). Applied by the schema reply (`tools._table_contexts`), after it dedupes the raw
    edges; `get_table_context` itself still returns one entry per edge.

    A hub reaches a dimension many ways: an employee row has two dozen date keys into the date
    table, a fact table a dozen person roles. Sent one edge each, that was most of a scoped reply
    for joins to tables the caller didn't even request. The merged entry keeps every join: each
    column pair is still there, under `column_pairs`, so nothing becomes unreachable. A pair with a
    single edge keeps the ordinary `from_column`/`to_column` shape, and full edges (both tables
    requested) are never merged: they carry the detail a written join needs."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    order: list[Any] = []
    for e in edges:
        mergeable = ("review_state" not in e and "on" not in e and "executable" not in e
                     and "from_column" in e and "to_column" in e)
        if not mergeable:
            order.append(e)
            continue
        key = tuple(e.get(k) for k in _PAIR_KEYS)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)
    out: list[dict[str, Any]] = []
    for item in order:
        if not isinstance(item, tuple):
            out.append(item)
            continue
        members = groups[item]
        if len(members) == 1:
            out.append(members[0])
            continue
        merged = {k: v for k, v in zip(_PAIR_KEYS, item) if v is not None}
        merged["column_pairs"] = [[e["from_column"], e["to_column"]] for e in members]
        out.append(merged)
    return out


def _metrics_for(org: Datasource, tables: list[str], area: Optional[str]) -> list[dict[str, Any]]:
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


def get_subject_area_bundle(org: Datasource, area: str) -> dict[str, Any]:
    """One-shot bundle for small subject areas (a few dozen tables)."""
    sa = org.subject_area(area)
    if sa is None:
        raise KeyError(f"no subject area {area!r}")
    table_names = [t.name for t in sa.tables_defined]
    # multi-area membership: also include tables this area REFERENCES (TableRef) but that are
    # defined in another area — they resolve via _find_table's org-wide fallback.
    defined = set(table_names)
    for ref in sa.tables:
        if ref.table not in defined:
            table_names.append(ref.table)
            defined.add(ref.table)
    bundle = get_table_context(
        org,
        table_names,
        area=area,
        include=list(DEFAULT_BUNDLE_INCLUDE),
    )
    bundle["subject_area"] = {
        "name": sa.name,
        "description": sa.description,
        "default_time_window": sa.default_time_window,
    }
    bundle["entities"] = [e.model_dump(exclude_none=True) for e in sa.entities]
    return bundle


def list_prompt_examples(root: str | Path, area: str,
                         *, include_rejected: bool = False) -> list[dict[str, Any]]:
    """Load scope-tagged examples for a subject area (prompt_examples/<area>/examples.yaml).

    Examples the curator rejected (`status: rejected`) are dropped by default so the
    runtime ranker never anchors on them — mirroring how `load_datasource` drops
    `review_state: rejected` model entries. Pass `include_rejected=True` for the curation
    view (re-render, dedup, audit), where a rejected example must still be visible.
    """
    f = Path(root) / "prompt_examples" / area / "examples.yaml"
    if not f.exists():
        return []
    doc = _read_yaml(f) or {}
    items = doc if isinstance(doc, list) else doc.get("examples", [])
    if include_rejected:
        return items
    return [e for e in items if (e or {}).get("status") != "rejected"]


__all__ = [
    "load_datasource",
    "load_org_id",
    "deployment_org_id",
    "collect_default_filters",
    "get_table_index",
    "get_table_context",
    "get_subject_area_bundle",
    "list_prompt_examples",
]
