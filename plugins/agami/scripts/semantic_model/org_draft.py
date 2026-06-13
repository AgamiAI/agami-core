"""Deterministic, evidence-grounded ORGANIZATION.md draft from the semantic model.

So ORGANIZATION.md is never blank: agami-connect persists this on the "skip" path
(after enrichment), and the model explorer falls back to it when the file is empty.

It states only what the model factually CONTAINS — tables, metrics, entities, units —
never invented business semantics. Domain vocabulary (what "MRR" means, who the users
are) only a human knows, so that stays a prompt under `## Key terminology`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Organization


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def draft_organization_md(org: "Organization") -> str:
    areas = list(org.subject_areas)
    lines: list[str] = [
        "# About this database",
        "",
        "<!-- Auto-generated from your schema — a factual summary of what this database",
        "     contains. Edit freely: add what the company / product is, who the users",
        "     are, and the domain vocabulary + KPI definitions only you know. -->",
        "",
    ]

    n_tables = sum(len(sa.tables_defined) for sa in areas)
    area_bits = ", ".join(
        f"{sa.name} ({_plural(len(sa.tables_defined), 'table')})" for sa in areas
    )
    summary = f"**{org.organization}** has {_plural(n_tables, 'table')} across {_plural(len(areas), 'subject area')}"
    lines.append(summary + (f": {area_bits}." if area_bits else "."))
    lines.append("")

    # What the data contains — tables per area (factual: name + description + row count)
    lines.append("## What the data contains")
    lines.append("")
    for sa in areas:
        live = [t for t in sa.tables_defined if t.review_state != "rejected"]
        if not live:
            continue
        hdr = f"### {sa.name}"
        if sa.description:
            hdr += f" — {sa.description}"
        lines.append(hdr)
        for t in live:
            row = f"- **{t.name}**"
            if t.description:
                row += f" — {t.description}"
            rc = t.performance_hints.estimated_row_count if t.performance_hints else None
            if rc:
                row += f"  [~{rc:,} rows]"
            lines.append(row)
        lines.append("")

    metrics = [m for sa in areas for m in sa.metrics if m.review_state != "rejected"]
    if metrics:
        lines.append("## Metrics")
        lines.append("")
        for m in metrics:
            row = f"- **{m.name}**"
            if m.calculation:
                row += f" — {m.calculation}"
            if m.unit:
                row += f" [{m.unit}]"
            if m.other_names:
                row += f" (also called: {', '.join(m.other_names)})"
            lines.append(row)
        lines.append("")

    entities = [e for sa in areas for e in sa.entities if e.review_state != "rejected"]
    if entities:
        lines.append("## Entities")
        lines.append("")
        for e in entities:
            maps = ", ".join(f"{m.table}.{m.column}" for m in e.maps_to)
            row = f"- **{e.name}**"
            if e.plural and e.plural != e.name:
                row += f" ({e.plural})"
            if maps:
                row += f" — identified by {maps}"
            if e.other_names:
                row += f"; also called: {', '.join(e.other_names)}"
            lines.append(row)
        lines.append("")

    unit_cols = [
        (f"{t.name}.{c.name}", c.unit)
        for sa in areas for t in sa.tables_defined for c in t.columns if c.unit
    ]
    if unit_cols:
        lines.append("## Units & currency")
        lines.append("")
        for qname, unit in unit_cols:
            lines.append(f"- **{qname}** — {unit}")
        lines.append("")

    _key_terminology(lines, org, areas)
    return "\n".join(lines)


# How much of an enum column's value→meaning map to inline before truncating, and how
# many such columns to auto-seed — enough to be useful, capped so a code-heavy schema
# doesn't bury the curated glossary in machine-readable enum dumps.
_MAX_ENUM_VALUES = 10
_MAX_ENUM_COLS = 25


def _key_terminology(lines: list[str], org: "Organization", areas: list) -> None:
    """Seed `## Key terminology` from structured evidence so it's never a bare prompt:
    the curated glossary (`org.key_terminology` — decoded abbreviations enrichment wrote)
    first, then auto-derived enum legends from `choice_field` columns. Falls back to the
    user-prompt placeholder only when there's genuinely nothing structured to show."""
    glossary = {str(k).strip(): str(v).strip()
                for k, v in (getattr(org, "key_terminology", {}) or {}).items()
                if str(k).strip() and str(v).strip()}

    enum_lines: list[str] = []
    for sa in areas:
        for t in sa.tables_defined:
            if t.review_state == "rejected":
                continue
            for c in t.columns:
                cf = getattr(c, "choice_field", None)
                if not cf or getattr(c, "review_state", "approved") == "rejected":
                    continue
                items = list(cf.items())
                shown = "; ".join(f"`{k}` = {v}" for k, v in items[:_MAX_ENUM_VALUES])
                if len(items) > _MAX_ENUM_VALUES:
                    shown += "; …"
                enum_lines.append(f"- **{t.name}.{c.name}** — {shown}")
                if len(enum_lines) >= _MAX_ENUM_COLS:
                    break
            if len(enum_lines) >= _MAX_ENUM_COLS:
                break
        if len(enum_lines) >= _MAX_ENUM_COLS:
            break

    lines.append("## Key terminology")
    lines.append("")
    if not glossary and not enum_lines:
        lines.append("<!-- Domain vocabulary the skill should know — only you can fill this in.")
        lines.append('     e.g. "MRR" = monthly recurring revenue; "active user" = signed in within 30 days. -->')
        lines.append("")
        return

    lines.append("<!-- Auto-seeded from decoded codes + enum columns. Add domain terms only you know "
                 '(e.g. "MRR" = monthly recurring revenue). -->')
    for term, definition in glossary.items():
        lines.append(f"- **{term}** — {definition}")
    if glossary and enum_lines:
        lines.append("")
        lines.append("Coded value legends:")
    lines.extend(enum_lines)
    lines.append("")
