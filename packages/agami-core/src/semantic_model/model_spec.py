"""Apply a model spec: the person's own declaration of subject areas, joins and metrics.

Introspection proposes structure from the catalog: areas by table-name prefix, joins from declared
foreign keys or `*_id` names confirmed by a value probe. A warehouse whose joins are not declared and
whose keys follow another convention (a star schema of views joined on `*_key` columns, a key named
for its role such as `ship_to_customer_key`) comes out as disconnected tables in arbitrary areas, and
nothing about the data lets the engine recover what its owners already know. A model spec is that
knowledge written down, and this module applies it as ONE validated step over the introspected model:

- **subject areas**: each table is owned (defined) by exactly one area and may be listed (referenced)
  by others, so a shared dimension serves every area that needs it;
- **tables**: the spec is the kept set; an introspected table the spec does not name is dropped. A
  table's grain (the column or columns that make a row unique) may be stated, which the engine can't
  probe on a large table and the fan-out check relies on;
- **joins**: replace whatever introspection guessed. A join is the simple `column = column` form, or
  an `on:` condition for anything more (a `current_flag = 'Y'` filter, a composite key). It is placed
  where the runtime will serve it: in the area that owns both tables, else as a cross-area join
  between the two owners — so a person never has to know that rule. A join the
  spec marks approved is signed off by the named signer; every other one lands unreviewed;
- **column descriptions** (the person's own, so enrichment won't overwrite them), **metrics**,
  **sensitive columns** and **rules** (prose carried into `datasource.md`). A metric that is a plain
  aggregate of one column is flagged, not refused: its name belongs in that column's description.

Nothing is written unless the whole result validates, and a failure after writing restores the
previous files. The spec is a JSON document; `plugins/agami/scripts/model_spec_workbook.py` turns
the spreadsheet template a person fills in into it. See `plugins/agami/shared/model-spec-format.md`.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import CrossSubjectAreaRelationship, Metric, Relationship, SubjectArea, TableRef

_AREA_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_CARDINALITIES = ("many_to_one", "one_to_many", "one_to_one")
_JOIN_TYPES = ("LEFT", "INNER", "RIGHT", "FULL", "CROSS")
_RULES_START = "<!-- model-spec:rules -->"
_RULES_END = "<!-- /model-spec:rules -->"
_CROSS_FILE = "cross_subject_area_relationships.yaml"


@dataclass
class SpecResult:
    applied: bool = False
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "dry_run": self.dry_run,
            "errors": self.errors,
            "counts": self.counts,
            "notes": self.notes,
        }


def _low(s: Any) -> str:
    return str(s or "").strip().lower()


def _check_spec(
    spec: dict, tables: dict[str, Any], signer: Optional[str], dry_run: bool = False
) -> list[str]:
    """Every problem the spec has on its own or against the introspected model, all at once, so a
    person fixes the sheet in one pass instead of one error per run."""
    errs: list[str] = []
    areas = [_low(a.get("name")) for a in spec.get("subject_areas", [])]
    if not areas:
        errs.append("the spec declares no subject areas")
    for a in areas:
        if not _AREA_NAME.match(a):
            errs.append(
                f"subject area {a!r}: use lowercase letters, digits and underscores, starting with a letter"
            )
    for a in {a for a in areas if areas.count(a) > 1}:
        errs.append(f"subject area {a!r} is declared more than once")
    known = set(areas)

    seen: set[str] = set()
    for row in spec.get("tables", []):
        t, owner = _low(row.get("table")), _low(row.get("owner"))
        if t in seen:
            errs.append(f"table {t!r} is listed more than once in the tables section")
        seen.add(t)
        if t not in tables:
            errs.append(
                f"table {t!r} is not in the introspected model — introspect it first "
                "(pass the spec's tables as the --tables-file allowlist)"
            )
        if owner not in known:
            errs.append(f"table {t!r}: owner {owner!r} is not a declared subject area")
        for b in row.get("also_in") or []:
            if _low(b) not in known:
                errs.append(
                    f"table {t!r}: also listed in {_low(b)!r}, which is not a declared subject area"
                )
        # A wrong grain is an error, not a note: the fan-out check trusts it to decide that a join
        # can't repeat rows.
        for g in _grain_of(row):
            if t in tables and not any(_low(x.name) == g for x in tables[t].columns):
                errs.append(
                    f"table {t!r}: grain column {g!r} does not exist in the introspected table"
                )
    if not seen:
        errs.append("the spec lists no tables")

    # The same join written twice (a role label aside) would be two copies no approval could tell
    # apart: every op naming it would match both and be refused as ambiguous.
    seen_joins: dict[tuple, int] = {}
    for i, j in enumerate(spec.get("joins", []), 1):
        ident = (
            _low(j.get("from_table")),
            _low(j.get("to_table")),
            _low(j.get("from_column")),
            _low(j.get("to_column")),
            " ".join(str(j.get("on") or "").split()).lower(),
        )
        if ident in seen_joins:
            errs.append(
                f"join {i} repeats join {seen_joins[ident]} — the same tables and columns or condition"
            )
        seen_joins.setdefault(ident, i)

    for i, j in enumerate(spec.get("joins", []), 1):
        where = f"join {i} ({_low(j.get('from_table'))} -> {_low(j.get('to_table'))})"
        ft, tt = _low(j.get("from_table")), _low(j.get("to_table"))
        for t in (ft, tt):
            if t not in seen:
                errs.append(f"{where}: table {t!r} is not in the spec's tables section")
        has_on = bool(str(j.get("on") or "").strip())
        # Both forms on one row is ambiguous: building the `on` join would drop the columns, and an
        # `on` holding only the extra filter would join every row to every row.
        if has_on and (_low(j.get("from_column")) or _low(j.get("to_column"))):
            errs.append(
                f"{where}: give either from_column/to_column or an `on` condition, not both — "
                "put the column equality inside `on` if it also needs a filter"
            )
        if not has_on:
            for side, t in (("from_column", ft), ("to_column", tt)):
                c = _low(j.get(side))
                if not c:
                    errs.append(f"{where}: needs {side} (or an `on` condition)")
                elif t in tables and not any(_low(x.name) == c for x in tables[t].columns):
                    errs.append(f"{where}: column {t}.{c} does not exist in the introspected table")
        card = _low(j.get("cardinality")) or "many_to_one"
        if card not in _CARDINALITIES:
            errs.append(f"{where}: cardinality {card!r} is not one of {', '.join(_CARDINALITIES)}")
        jt = str(j.get("join_type") or "LEFT").strip().upper()
        if jt not in _JOIN_TYPES:
            errs.append(f"{where}: join type {jt!r} is not one of {', '.join(_JOIN_TYPES)}")

    members = _area_members(spec)
    # Two metrics whose names make the same file name (`Sales Amount`, `sales-amount`) would overwrite
    # each other in their area.
    from .build import item_file_stem

    stems: dict[tuple[str, str], list[str]] = {}
    for m in spec.get("metrics", []):
        name = str(m.get("name") or "").strip()
        if name:
            stems.setdefault((_low(m.get("area")), item_file_stem(name)), []).append(name)
    for (area, _stem), names in sorted(stems.items()):
        if len(names) > 1:
            errs.append(
                f"metrics {', '.join(repr(n) for n in names)} in subject area {area!r} have the same "
                "name once spaces and punctuation are ignored — rename one"
            )
    # An area's tables are written and looked up by bare name, so sales.date_d and archive.date_d in
    # one area would overwrite each other's file and answer to the same name. Give each its own area.
    for a, names in sorted(members.items()):
        by_bare: dict[str, list[str]] = {}
        for t in sorted(names):
            if t in tables:
                by_bare.setdefault(_low(tables[t].name), []).append(t)
        for bare, same in sorted(by_bare.items()):
            if len(same) > 1:
                errs.append(
                    f"subject area {a!r} would hold {', '.join(same)}: two tables named {bare!r} "
                    "can't share an area — put them in different areas"
                )

    for i, m in enumerate(spec.get("metrics", []), 1):
        name = str(m.get("name") or "").strip()
        label = name or f"metric {i}"
        area = _low(m.get("area"))
        if not name:
            errs.append(f"metric {i}: needs a name")
        if not str(m.get("calculation") or "").strip():
            errs.append(f"metric {label!r}: needs a calculation")
        if area not in known:
            errs.append(f"metric {label!r}: subject area {area!r} is not declared")
        # A source the metric's area can't see would load as a metric no question can reach.
        for t in m.get("source_tables") or []:
            if _low(t) not in members.get(area, set()):
                errs.append(
                    f"metric {label!r}: source table {_low(t)!r} isn't in subject area {area!r}"
                )

    month = spec.get("fiscal_year_start_month")
    if month not in (None, "") and (not str(month).isdigit() or not 1 <= int(month) <= 12):
        errs.append(f"fiscal_year_start_month must be a whole number from 1 to 12, not {month!r}")

    # One line, not one per row: the count is what a person acts on. A dry run reports it instead of
    # refusing, because the dry run is what a skill shows BEFORE asking who signs off.
    approved = sum(_truthy(j.get("approved")) for j in spec.get("joins", [])) + sum(
        _truthy(m.get("approved")) for m in spec.get("metrics", [])
    )
    if approved and not signer and not dry_run:
        errs.append(
            f"{approved} joins/metrics are marked approved, so the run needs --signer (who signs them off)"
        )

    # A sensitive column that doesn't exist is an error: a misspelled privacy flag must not pass
    # silently. A described one is not (see `_stale_descriptions`) — data dictionaries go stale.
    for row in spec.get("sensitive_columns", []):
        t, c = _low(row.get("table")), _low(row.get("column"))
        if t not in seen:
            errs.append(f"sensitive column {t}.{c}: table is not in the spec's tables section")
        elif t in tables and not any(_low(x.name) == c for x in tables[t].columns):
            errs.append(
                f"sensitive column {t}.{c}: column does not exist in the introspected table"
            )
    return errs


def _stale_descriptions(spec: dict, tables: dict[str, Any]) -> list[str]:
    """Columns-sheet rows naming a table or column the model doesn't have — skipped and listed."""
    out = []
    for row in spec.get("columns", []):
        t, c = _low(row.get("table")), _low(row.get("column"))
        if t not in tables or not any(_low(x.name) == c for x in tables[t].columns):
            out.append(f"{t}.{c}")
    return out


# A plain aggregate of one column: SUM(col), AVG(t.col), COUNT(*). Not DISTINCT, not arithmetic.
_PLAIN_AGG = re.compile(r"^\s*(sum|avg|min|max|count)\s*\(\s*(\*|[a-z_][\w.]*)\s*\)\s*$", re.I)


def _plain_metric_notes(spec: dict) -> list[str]:
    """A declared metric that is a plain aggregate of one column adds a name and nothing else: the
    column's aggregation class is enforced on every statement, and its unit and description reach the
    agent anyway (the rule `suggest_metrics` follows). Say so, rather than refuse — the person decides."""
    out = []
    for m in spec.get("metrics", []):
        calc = str(m.get("calculation") or "")
        if _PLAIN_AGG.match(calc):
            out.append(
                f"metric {m.get('name')!r} is a plain aggregate ({calc.strip()}): consider dropping it "
                "and putting its name at the start of that column's description (Columns sheet)"
            )
    return out


def _grain_of(row: dict) -> list[str]:
    g = row.get("grain") or []
    if isinstance(g, str):
        g = re.split(r"[,\n]", g)
    return [_low(x) for x in g if _low(x)]


def _canonical_tables(spec: dict, tables: dict, by_bare: dict) -> tuple[dict, list[str]]:
    """The spec with every table name rewritten to the key `apply_spec` indexes tables by, and an
    error for each bare name two schemas share. A name the model doesn't have is left as written for
    `_check_spec` to report."""
    errs: list[str] = []

    def key(name: Any, schema: Any = None) -> str:
        n = _low(name)
        if schema and "." not in n:
            n = f"{_low(schema)}.{n}"
        if n in tables:
            return n
        if "." in n:
            sch, bare = n.rsplit(".", 1)
            if bare in tables and _low(tables[bare].schema_name) in ("", sch):
                return bare
            return n
        if len(by_bare.get(n, [])) > 1:
            schemas = sorted(_low(t.schema_name) for t in by_bare[n])
            msg = f"table {n!r} exists in schemas {', '.join(schemas)} — name it as schema.table"
            if msg not in errs:
                errs.append(msg)
        return n

    out = json.loads(json.dumps(spec))
    for row in out.get("tables", []):
        row["table"] = key(row.get("table"), row.get("schema"))
    for j in out.get("joins", []):
        j["from_table"], j["to_table"] = key(j.get("from_table")), key(j.get("to_table"))
    for m in out.get("metrics", []):
        m["source_tables"] = [key(t) for t in m.get("source_tables") or []]
    for section in ("sensitive_columns", "columns"):
        for row in out.get(section, []):
            row["table"] = key(row.get("table"))
    return out, errs


def _truthy(v: Any) -> bool:
    return _low(v) in ("true", "yes", "y", "1", "approved", "x")


def _area_members(spec: dict) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for row in spec.get("tables", []):
        t = _low(row.get("table"))
        out.setdefault(_low(row.get("owner")), set()).add(t)
        for b in row.get("also_in") or []:
            out.setdefault(_low(b), set()).add(t)
    return out


def _relationship(
    j: dict, tables: dict, signer: Optional[str], role: Optional[str], now: str
) -> Relationship:
    ft, tt = _low(j.get("from_table")), _low(j.get("to_table"))
    approved = _truthy(j.get("approved"))
    on = str(j.get("on") or "").strip() or None
    desc = " — ".join(
        x for x in (str(j.get("role") or "").strip(), str(j.get("description") or "").strip()) if x
    )

    def real(t: str, c: str) -> str:  # the catalog's own spelling: Snowflake keeps names upper-case
        return next((x.name for x in tables[t].columns if _low(x.name) == c), c)

    return Relationship(
        from_table=tables[ft].name,
        to_table=tables[tt].name,
        from_column=None if on else real(ft, _low(j.get("from_column"))),
        to_column=None if on else real(tt, _low(j.get("to_column"))),
        from_schema=tables[ft].schema_name,
        to_schema=tables[tt].schema_name,
        on=on,
        join_type=(str(j.get("join_type") or "LEFT").strip().upper()),
        relationship=_low(j.get("cardinality")) or "many_to_one",
        description=desc,
        confidence="confirmed" if approved else "proposed",
        review_state="approved" if approved else "unreviewed",
        signed_off_by=signer if approved else None,
        signed_off_role=(role or "owner") if approved else None,
        signed_off_at=now if approved else None,
    )


def _metric(
    m: dict, storage_type: str, signer: Optional[str], role: Optional[str], now: str
) -> Metric:
    approved = _truthy(m.get("approved"))
    from .build import item_file_stem  # one naming rule for every writer of metric files

    name = item_file_stem(str(m.get("name") or ""))
    src = [_low(t) for t in (m.get("source_tables") or []) if _low(t)]
    return Metric(
        name=name,
        description=str(m.get("definition") or "").strip(),
        other_names=[str(m.get("name")).strip()] if str(m.get("name") or "").strip() else [],
        calculation=str(m.get("definition") or m.get("name") or "").strip(),
        bindings={storage_type: str(m.get("calculation")).strip()},
        source_tables=src,
        primary_table=src[0] if src else None,
        confidence="confirmed" if approved else "proposed",
        review_state="approved" if approved else "unreviewed",
        signed_off_by=signer if approved else None,
        signed_off_role=(role or "owner") if approved else None,
        signed_off_at=now if approved else None,
    )


def _write_rules(root: Path, rules: list[dict]) -> int:
    """Carry the spec's rules into datasource.md, between markers, so a re-apply replaces them and any
    narrative the person wrote outside the markers stays put. No rules removes the block: a rule
    deleted from the workbook must stop shaping answers."""
    path = root / "datasource.md"
    prior = path.read_text(encoding="utf-8") if path.exists() else ""
    if not rules:
        if _RULES_START in prior and _RULES_END in prior:
            head, rest = prior.split(_RULES_START, 1)
            path.write_text(
                head.rstrip() + "\n" + rest.split(_RULES_END, 1)[1].lstrip("\n"), encoding="utf-8"
            )
        return 0
    lines = [_RULES_START, "## Rules", ""]
    lines += [
        f"- **{str(r.get('rule') or '').strip()}.** {str(r.get('detail') or '').strip()}"
        for r in rules
    ]
    lines += ["", _RULES_END]
    block = "\n".join(lines)
    if _RULES_START in prior and _RULES_END in prior:
        head, rest = prior.split(_RULES_START, 1)
        new = head + block + rest.split(_RULES_END, 1)[1]
    else:
        new = (prior.rstrip() + "\n\n" if prior.strip() else "") + block + "\n"
    path.write_text(new, encoding="utf-8")
    return len(rules)


def apply_spec(
    root: str | Path,
    spec: dict,
    *,
    signer: Optional[str] = None,
    role: Optional[str] = None,
    dry_run: bool = False,
) -> SpecResult:
    from . import build, curate, loader, snapshot, validator

    root = Path(root)
    res = SpecResult(dry_run=dry_run)
    org = loader.load_datasource(root, include_rejected=True)
    # Tables are keyed by bare name, or by `schema.name` where two schemas share a name — so a spec
    # can say which one it means, and a bare name that could mean either is an error, not a guess.
    defined = [t for sa in org.subject_areas for t in sa.tables_defined]
    by_bare: dict[str, list] = {}
    for t in defined:
        by_bare.setdefault(_low(t.name), []).append(t)
    tables: dict[str, Any] = {}
    for bare, ts in by_bare.items():
        for t in ts:
            tables[bare if len(ts) == 1 else f"{_low(t.schema_name)}.{bare}"] = t
    key_of = {(_low(t.schema_name), _low(t.name)): k for k, t in tables.items()}
    ref_of: dict[str, TableRef] = {}
    for sa in org.subject_areas:
        for r in sa.tables:
            k = key_of.get((_low(r.schema_name), _low(r.table)))
            if k:
                ref_of.setdefault(k, r)

    spec, ambiguous = _canonical_tables(spec, tables, by_bare)
    res.errors = ambiguous + _check_spec(spec, tables, signer, dry_run)
    if res.errors:
        return res

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # A dry run validates the approvals with a stand-in signer — it writes nothing, and the real run
    # refuses without a signer — so the dry run can report the model before anyone is asked to sign.
    sign_as = signer or ("(to be signed off)" if dry_run else None)
    storage_type = org.storage_connections[0].storage_type if org.storage_connections else ""
    rows = {_low(r.get("table")): r for r in spec.get("tables", [])}
    sensitive = {
        (_low(r.get("table")), _low(r.get("column"))) for r in spec.get("sensitive_columns", [])
    }
    for t, c in sensitive:
        for col in tables[t].columns:
            if _low(col.name) == c:
                col.sensitive = True
    grains = 0
    for t, r in rows.items():
        g = _grain_of(r)
        if g:
            tables[t].grain = [
                next(x.name for x in tables[t].columns if _low(x.name) == c) for c in g
            ]
            grains += 1
    described = 0
    stale = set(_stale_descriptions(spec, tables))
    for row in spec.get("columns", []):
        t, c = _low(row.get("table")), _low(row.get("column"))
        if f"{t}.{c}" in stale:
            continue
        name, desc = str(row.get("name") or "").strip(), str(row.get("description") or "").strip()
        text = " — ".join(x for x in (name, desc) if x)
        for col in tables[t].columns:
            if _low(col.name) == c and text:
                # The person's own words: `human` outranks enrichment, which won't overwrite it.
                col.description, col.description_source = text, "human"
                described += 1

    old_areas = {sa.name: sa for sa in org.subject_areas}
    new_areas: list[SubjectArea] = []
    members = _area_members(spec)
    for a in spec["subject_areas"]:
        name = _low(a.get("name"))
        owned = [tables[t] for t, r in rows.items() if _low(r.get("owner")) == name]
        listed = sorted(
            members.get(name, set()), key=lambda t: (t not in {x.name.lower() for x in owned}, t)
        )
        refs = []
        for t in listed:
            base = ref_of.get(t)
            refs.append(
                TableRef(
                    storage_connection=base.storage_connection
                    if base
                    else org.storage_connections[0].name,
                    schema=tables[t].schema_name,
                    table=tables[t].name,
                    expose_column_groups=base.expose_column_groups if base else None,
                )
            )
        prev = old_areas.get(name)
        new_areas.append(
            SubjectArea(
                name=name,
                description=str(a.get("description") or "").strip(),
                default_time_window=prev.default_time_window if prev else None,
                tables=refs,
                tables_defined=owned,
                entities=list(prev.entities) if prev else [],
            )
        )
    by_name = {sa.name: sa for sa in new_areas}

    # Metrics the spec doesn't redefine stay where they were (enrichment and `sm add` put them there,
    # some signed off); only an area the spec drops takes its metrics with it, and that is reported.
    # Keyed by area: a metric file lives in its area, so orders.sales_amount redefines only itself,
    # not a same-named metric in another area.
    spec_metric_files = {
        (_low(m.get("area")), build.item_file_stem(str(m.get("name") or "")))
        for m in spec.get("metrics", [])
    }
    metrics_kept, metrics_dropped = 0, []
    for name, prev in old_areas.items():
        for mm in prev.metrics:
            if (name, build.item_file_stem(mm.name)) in spec_metric_files:
                continue
            if name in by_name:
                by_name[name].metrics.append(mm)
                metrics_kept += 1
            else:
                metrics_dropped.append(mm)

    discarded = sum(len(sa.relationships) for sa in org.subject_areas) + len(
        org.cross_subject_area_relationships
    )
    # A join lives in an area only when that area OWNS both tables: the loader serves an area's joins
    # among its own tables, so a join to a table the area merely lists would be dropped at query time.
    # Every other join is a cross-area join between the two owners, which is what that list is for.
    owner = {t: _low(r.get("owner")) for t, r in rows.items()}
    cross: list[CrossSubjectAreaRelationship] = []
    for j in spec.get("joins", []):
        fa, ta = owner[_low(j.get("from_table"))], owner[_low(j.get("to_table"))]
        rel = _relationship(j, tables, sign_as, role, now)
        if fa == ta:
            by_name[fa].relationships.append(rel)
        else:
            cross.append(
                CrossSubjectAreaRelationship(
                    **rel.model_dump(), from_subject_area=fa, to_subject_area=ta
                )
            )
    for m in spec.get("metrics", []):
        by_name[_low(m.get("area"))].metrics.append(_metric(m, storage_type, sign_as, role, now))

    dropped = sorted(set(tables) - set(rows))
    org.subject_areas = new_areas
    org.cross_subject_area_relationships = cross
    if spec.get("fiscal_year_start_month"):
        org.fiscal_year_start_month = int(spec["fiscal_year_start_month"])

    v = validator.validate(org)
    res.errors = v.errors
    res.counts = {
        "subject_areas": {
            sa.name: {
                "owns": len(sa.tables_defined),
                "lists": len(sa.tables),
                "joins": len(sa.relationships),
                "metrics": len(sa.metrics),
            }
            for sa in new_areas
        },
        "joins": len(spec.get("joins", [])),
        "cross_area_joins": len(cross),
        "joins_approved": sum(_truthy(j.get("approved")) for j in spec.get("joins", [])),
        "metrics_approved": sum(_truthy(m.get("approved")) for m in spec.get("metrics", [])),
        "joins_with_on_condition": sum(
            bool(str(j.get("on") or "").strip()) for j in spec.get("joins", [])
        ),
        "metrics": len(spec.get("metrics", [])),
        "sensitive_columns": len(sensitive),
        "columns_described": described,
        "tables_with_grain": grains,
        "rules": len(spec.get("rules", [])),
        "tables_dropped_not_in_spec": len(dropped),
        "introspected_joins_discarded": discarded,
        "metrics_kept": metrics_kept,
        "metrics_dropped_with_their_area": len(metrics_dropped),
    }
    res.notes = [f"warning: {w}" for w in v.warnings] + _plain_metric_notes(spec)
    if stale:
        res.notes.append(
            f"skipped {len(stale)} column description(s) for columns the database doesn't have: "
            + ", ".join(sorted(stale))
        )
    if dropped:
        res.notes.append("dropped (not in the spec): " + ", ".join(dropped))
    if metrics_dropped:
        approved = sum(mm.review_state == "approved" for mm in metrics_dropped)
        res.notes.append(
            f"{len(metrics_dropped)} metric(s) in areas the spec doesn't declare are dropped "
            f"({approved} signed off): " + ", ".join(mm.name for mm in metrics_dropped)
        )
    if res.errors or dry_run:
        return res

    # Everything write_tree replaces, plus the cross-area joins file add_relationships keeps beside
    # datasource.yaml: the spec replaces those joins too, and a failure must put all of it back.
    kept = ("datasource.yaml", "datasource.md", "subject_areas", "datasources", _CROSS_FILE)
    backup = Path(tempfile.mkdtemp(prefix="agami-spec-backup-"))
    for rel in kept:
        src = root / rel
        if src.is_dir():
            shutil.copytree(src, backup / rel)
        elif src.exists():
            shutil.copy2(src, backup / rel)
    try:
        if (root / "subject_areas").exists():
            shutil.rmtree(root / "subject_areas")
        # The spec's cross-area joins go into datasource.yaml; the old file would keep the replaced ones.
        (root / _CROSS_FILE).unlink(missing_ok=True)
        build.write_tree(org, root)
        _write_rules(root, spec.get("rules", []))
        reread = validator.validate(loader.load_datasource(root, include_rejected=True))
        if reread.errors:
            raise RuntimeError("; ".join(reread.errors))
    except Exception as exc:  # restore the previous model rather than leave a half-written one
        for rel in kept:
            target, saved = root / rel, backup / rel
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            if saved.is_dir():
                shutil.copytree(saved, target)
            elif saved.exists():
                shutil.copy2(saved, target)
        # write_tree stamped a model_version for the model that was just rolled back; stamp the
        # restored one so receipts and the stale-model check name what is actually on disk.
        snapshot.write_snapshot(root)
        res.errors = [f"writing the model failed and the previous model was restored: {exc}"]
        return res
    finally:
        shutil.rmtree(backup, ignore_errors=True)

    curate._append_curation_log(
        root,
        [{"op": "apply_spec", **{k: v for k, v in res.counts.items() if k != "subject_areas"}}],
        signer,
        role,
    )
    curate._git_commit(root, "apply model spec")
    res.applied = True
    return res


__all__ = ["apply_spec", "SpecResult"]
