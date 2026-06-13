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


# Cap the subject-area summary — a model can have many areas (and tables can run to the
# thousands); list a bounded sample and count the rest rather than dumping everything. The
# full tables/metrics/entities live in the structured model, browsable in the explorer.
_MAX_AREAS_LISTED = 30
_AREA_DESC_CHARS = 110


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def draft_organization_md(org: "Organization") -> str:
    """A concise SUMMARY of the organization — NOT a re-listing of the model.

    The semantic model already holds every table, column, metric, and entity as
    structured data (browsable in the explorer); duplicating that here would be both
    redundant and unusable at scale (a DB with thousands of tables would produce a
    thousand-line file). So this states the *shape* (counts + subject areas), the
    cross-cutting *conventions* (currency/units), and the things the model can't
    express in prose (domain glossary), then leaves the narrative to the human.

    Generated only on the skip path — when the user gave no org context of their own."""
    areas = list(org.subject_areas)
    lines: list[str] = [
        "# About this database",
        "",
        "<!-- Auto-generated SUMMARY (only because no org context was provided). The full",
        "     tables, columns, metrics, and entities live in the semantic model — browse them",
        "     in the model explorer. Edit freely: what the company/product is, who the users",
        "     are, and the domain vocabulary + KPI definitions only you know. -->",
        "",
    ]

    n_tables = sum(len(sa.tables_defined) for sa in areas)
    n_metrics = sum(len(sa.metrics) for sa in areas) + len(org.cross_subject_area_metrics)
    n_entities = sum(len(sa.entities) for sa in areas) + len(org.cross_subject_area_entities)

    summary = (f"**{org.organization}** — {_plural(n_tables, 'table')} across "
               f"{_plural(len(areas), 'subject area')}.")
    lines.append(summary)
    if (org.description or "").strip():
        lines.append("")
        lines.append(_short(org.description, 400))
    lines.append("")

    # Subject areas — the right summary granularity (few, even when tables run to the
    # thousands): name + table count + a SHORT description. Bounded, never the table list.
    if areas:
        lines.append("## Subject areas")
        lines.append("")
        for sa in areas[:_MAX_AREAS_LISTED]:
            live = [t for t in sa.tables_defined if t.review_state != "rejected"]
            row = f"- **{sa.name}** [{_plural(len(live), 'table')}]"
            desc = _short(sa.description, _AREA_DESC_CHARS)
            # skip the engine's auto "covering: <every table>" filler — it's the dump we're avoiding
            if desc and not desc.lower().startswith("auto-proposed subject area covering"):
                row += f" — {desc}"
            lines.append(row)
        if len(areas) > _MAX_AREAS_LISTED:
            lines.append(f"- …and {len(areas) - _MAX_AREAS_LISTED} more subject areas")
        lines.append("")

    # Counts, not lists — point at the model for the detail.
    defined = []
    if n_metrics:
        defined.append(_plural(n_metrics, "metric"))
    if n_entities:
        defined.append(f"{n_entities} entit" + ("y" if n_entities == 1 else "ies"))
    if defined:
        lines.append(f"{' and '.join(defined)} are defined — browse them, with every table "
                     f"and column, in the model explorer.")
        lines.append("")

    # Conventions: summarise the DISTINCT units in play (e.g. "INR"), not per-column rows.
    units = sorted({c.unit for sa in areas for t in sa.tables_defined for c in t.columns if c.unit})
    if units:
        lines.append("## Conventions")
        lines.append("")
        lines.append(f"- Units / currency in use: {', '.join(units)}.")
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
