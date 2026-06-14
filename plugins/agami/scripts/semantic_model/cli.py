#!/usr/bin/env python3
"""CLI entrypoint for the agami semantic model.

One dispatch surface shared by the skills and the test suite, so behavior is
identical wherever the model is exercised. Subcommands:

    validate  <root>                      — parse + validate a profile tree
    context   <root> --area A --tables ... — assemble get_table_context output
    bundle    <root> --area A             — one-shot subject-area bundle
    areas     <root>                      — list subject areas
    examples  <root> --area A --query Q   — rank prompt examples for a query
    preflight <root> --sql SQL            — fan-trap / chasm-trap pre-flight check
    seed-validate <root> --area A --profile P — run every written seed via execute_sql
                                            (safety pass on) → examples-validation items
    remove-example <root> --area A --question Q — reject example(s) by question
                                            (status: rejected — kept for audit, off runtime)

Every subcommand emits JSON on stdout (so callers parse one shape) except
`validate`, which prints a human report and sets the exit code (0 ok, 1 errors).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Optional

from . import loader as L
from . import runtime as RT
from . import validator as V


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def cmd_validate(args) -> int:
    # Validate the model AS AUTHORED, including curator-excluded entries
    # (include_rejected=True) — matching the curate engine's own pre-write check.
    # Excluding a table flips its tables/<T>.yaml to review_state: rejected but
    # intentionally leaves the subject_area.yaml `tables:` ref in place (the loader
    # tolerates the dangling ref at runtime, dropping both). Loading with the default
    # include_rejected=False here would drop the rejected table from org_tables while
    # its ref remains, so _check_table_refs_resolve would false-flag every intentional
    # exclusion as orphan_table_ref — even though curate passed and the runtime is fine.
    # A GENUINE orphan (a ref to a never-defined table) still fails, since no YAML
    # exists for it even with rejected included.
    org = L.load_organization(args.root, include_rejected=True)
    res = V.validate(org)
    print(V.format_result(res))
    return 0 if res.ok else 1


def cmd_context(args) -> int:
    org = L.load_organization(args.root)
    out = L.get_table_context(
        org,
        args.tables,
        area=args.area,
        columns=args.columns,
        include=args.include,
    )
    _print_json(out)
    return 0


def cmd_bundle(args) -> int:
    org = L.load_organization(args.root)
    _print_json(L.get_subject_area_bundle(org, args.area))
    return 0


def cmd_areas(args) -> int:
    org = L.load_organization(args.root)
    _print_json(RT.list_subject_areas(org))
    return 0


def cmd_org_draft(args) -> int:
    # A human-narrative STARTER for ORGANIZATION.md (skip path) — a prompt only, no model
    # facts. Those are derived at read time (see `org-context`), so they never get baked into
    # the editable prose file where a human could clobber them.
    from . import org_draft
    org = L.load_organization(args.root, include_rejected=False)
    sys.stdout.write(org_draft.starter_organization_md(org))
    return 0


def cmd_org_context(args) -> int:
    # The full domain context for the LLM: the human's ORGANIZATION.md narrative (comments
    # stripped) + the model-derived summary (subject areas, conventions, decoded glossary),
    # assembled fresh. This is what the query path injects as `## Organization context`.
    from . import org_draft
    org = L.load_organization(args.root, include_rejected=False)
    org_md = Path(args.root) / "ORGANIZATION.md"
    human = org_md.read_text(encoding="utf-8") if org_md.exists() else ""
    sys.stdout.write(org_draft.compose_context(human, org))
    return 0


def cmd_examples(args) -> int:
    examples = L.list_prompt_examples(args.root, args.area)
    matches = RT.get_prompt_examples(args.query, examples, top_k=args.top_k)
    _print_json(
        {
            "high_confidence": RT.is_high_confidence(matches),
            "matches": [{"score": round(m.score, 3), "example": m.example} for m in matches],
        }
    )
    return 0


def cmd_preflight(args) -> int:
    org = L.load_organization(args.root)
    result = RT.pre_flight_check(args.sql, org)
    _print_json(result.as_dict())
    return 0


def cmd_prepare(args) -> int:
    """Tier-independent safety pass: run the fan/chasm pre-flight, then (unless it
    refuses) apply the area's default_filters. Returns the SQL to actually execute.
    The query skill calls this on EVERY tier before handing SQL to psql/mysql/etc.,
    so the safety guarantees don't depend on going through execute_sql.py."""
    sql = args.sql
    if args.sql_file:
        sql = Path(args.sql_file).read_text()
    org = L.load_organization(args.root)
    pf = RT.pre_flight_check(sql, org)
    if pf.risk and pf.action == "refuse":
        _print_json({"action": "refuse", "risk": pf.risk, "reason": pf.reason,
                     "suggestion": pf.suggestion, "sql": sql})
        return 1
    run_sql = pf.rewritten_sql if (pf.action == "auto_rewrite" and pf.rewritten_sql) else sql
    final_sql, applied = RT.apply_default_filters(run_sql, org, area=args.area)
    _print_json({
        "action": pf.action,
        "risk": pf.risk,
        "sql": final_sql,
        "rewritten": bool(pf.action == "auto_rewrite"),
        "applied_filters": applied,
        # {output_column: unit}, traced through the final SQL — feed straight to
        # `format-table --units` so summed/aliased currency formats correctly.
        "units": RT.resolve_result_units(org, final_sql),
        "reason": pf.reason if pf.risk else None,
    })
    return 0


def cmd_review_queue(args) -> int:
    from . import curate
    org = L.load_organization(args.root)
    _print_json(curate.review_queue(org))
    return 0


def cmd_review_items(args) -> int:
    from . import curate
    org = L.load_organization(args.root, include_rejected=True)
    _print_json(curate.all_items(org, scope=args.scope))
    return 0


def cmd_model_tree(args) -> int:
    from . import curate
    org = L.load_organization(args.root, include_rejected=True)
    _print_json(curate.model_tree(org))
    return 0


def cmd_coverage(args) -> int:
    """Per-table column-description coverage + the enrichment-completeness verdict.
    The skill runs this at the end of Phase 2 (and reports it in the Phase 7 summary):
    `ok: false` with untouched columns means enrichment skipped the column pass."""
    from . import curate
    org = L.load_organization(args.root, include_rejected=True)
    _print_json(curate.column_coverage(org))
    return 0


def cmd_sensitive(args) -> int:
    """List the columns flagged `sensitive` (PII) that are still queryable — already-
    excluded ones (a rejected column, or any column under a rejected table) are NOT
    counted, since they're no longer in the runtime. The agami-connect Phase 4 curate gate
    uses this count to decide whether to open the explorer (so the gate stops re-opening
    once the user has excluded the flagged columns)."""
    from . import curate
    org = L.load_organization(args.root, include_rejected=True)
    _print_json(curate.sensitive_columns(org))
    return 0


def cmd_set_terminology(args) -> int:
    """Write the org-level domain glossary (term -> definition) onto org.yaml's
    `key_terminology` — the decoded-abbreviation legend enrichment produces. Merges by
    default (layers over a human's edits); --replace overwrites. Validated + committed."""
    from . import curate
    with open(args.file) as fh:
        terms = json.load(fh)
    if isinstance(terms, dict) and "key_terminology" in terms:
        terms = terms["key_terminology"]
    res = curate.set_key_terminology(args.root, terms, merge=not args.replace)
    _print_json({"applied": res.applied, "validated": res.validated,
                 "committed": res.committed, "errors": res.errors})
    return 0 if res.validated else 1


def cmd_curate(args) -> int:
    from . import curate
    with open(args.ops_file) as fh:
        ops = json.load(fh)
    if isinstance(ops, dict):
        ops = ops.get("ops", [])
    res = curate.apply(args.root, ops, signer=args.signer, role=args.role)
    _print_json(res.as_dict())
    return 0 if res.validated else 1


def cmd_add(args) -> int:
    from . import curate
    with open(args.file) as fh:
        items = json.load(fh)
    if isinstance(items, dict):
        items = items.get(args.kind + "s", items.get("items", []))
    res = curate.write_items(args.root, args.area, args.kind, items,
                             signer=args.signer, role=args.role)
    _print_json(res.as_dict())
    return 0 if res.validated else 1


def cmd_add_example(args) -> int:
    from . import curate
    with open(args.file) as fh:
        items = json.load(fh)
    if isinstance(items, dict):
        items = items.get("examples", items.get("items", []))
    res = curate.add_examples(args.root, args.area, items, signer=args.signer, role=args.role)
    _print_json(res.as_dict())
    return 0 if (res.applied or not res.skipped) else 1


def cmd_remove_example(args) -> int:
    """Reject prompt example(s) by question — `status: rejected`, kept in examples.yaml for
    audit but dropped from the runtime ranker. The packaged path for the dashboard's
    `reject N`, so a skill never hand-rewrites examples.yaml to drop an example."""
    from . import curate
    res = curate.remove_examples(args.root, args.area, args.question,
                                 signer=args.signer, role=args.role)
    _print_json({"rejected": res.applied, "skipped": res.skipped,
                 "validated": res.validated, "committed": res.committed})
    return 0 if (res.applied or not res.skipped) else 1


def cmd_suggest_units(args) -> int:
    """List the numeric **money** columns (so a currency `unit` can be stamped) using the
    tested name matcher — never a hand-rolled regex that mis-handles `count` inside
    `discount`. Skips columns that already carry a `unit`. Emits
    {money_columns: [{area, table, column, type}]} for the caller to confirm + apply."""
    from . import build as B
    org = L.load_organization(args.root)
    numeric = {"integer", "decimal", "float"}
    money = []
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            for c in t.columns:
                if not c.unit and c.type in numeric and B.detect_money_column(c.name):
                    money.append({"area": sa.name, "table": t.name, "column": c.name, "type": c.type})
    _print_json({"money_columns": money})
    return 0


def cmd_format_table(args) -> int:
    """Format a result CSV into a deterministic markdown table — exact numbers, full
    grouping + currency symbols, never abbreviated. The skill (and later the MCP) emits
    this verbatim so verification numbers don't depend on the LLM's formatting."""
    import csv as _csv
    from . import units
    text = open(args.csv_file, newline="").read() if args.csv_file else sys.stdin.read()
    reader = list(_csv.reader(io.StringIO(text)))
    if not reader:
        print("")
        return 0
    headers, rows = reader[0], reader[1:]
    unit_map = json.loads(args.units) if args.units else {}
    print(units.format_table(headers, rows, unit_map))
    return 0


def _preseed_gate(org) -> Optional[dict]:
    """Phase-4 enforcement, in code rather than prose: return a refusal payload when
    seeds must NOT be generated yet — i.e. metrics/entities the seeds would reference
    are still unreviewed. Generating few-shots on top of a guessed metric definition
    bakes that guess in. Returns None when nothing is pending (count 0). The skill's
    Phase-4c "continue anyway" path bypasses this with --after-review (the only place
    that's sanctioned, AFTER the user has been in the explorer)."""
    from . import curate
    pending = curate.all_items(org, scope="preseed")
    if not pending:
        return None
    return {
        "refused": "preseed_review_pending",
        "pending_count": len(pending),
        "pending": [{"name": it["name"], "entity_type": it["entity_type"]} for it in pending],
        "message": (
            f"{len(pending)} metric(s)/entity(ies) still need review — generating seeds "
            f"now would bake guessed definitions into your few-shots. Open /agami-model "
            f"preseed, sign off (or reject) them, then re-run. If you have already reviewed "
            f"in the explorer and accept proceeding with items still unreviewed, re-run with "
            f"--after-review."
        ),
    }


def _coverage_gate(org) -> Optional[dict]:
    """Enrichment-completeness gate: refuse to seed (or finish) a model that has tables
    whose columns enrichment never described at all (0 described + 0 ai_unknown). Naked
    columns degrade the explorer AND every NL→SQL answer (column descriptions are the
    generator's context). Table-level so it never collides with the deliberate
    self-evident-blank rule. NOT bypassable: the fix is to run the column pass."""
    from . import curate
    cov = curate.column_coverage(org)
    if cov["ok"]:
        return None
    tbls = cov["unenriched_tables"]
    return {
        "refused": "columns_unenriched",
        "table_count": len(tbls),
        "unenriched_tables": tbls,
        "coverage_pct": cov["totals"]["coverage_pct"],
        "message": (
            f"{len(tbls)} table(s) have NO column descriptions at all — enrichment wrote the "
            f"table descriptions but skipped the column pass. Run Phase 2's column pass: "
            f"describe each meaningful column from sampled values, mark genuinely-opaque ones "
            f"description_source=ai_unknown (self-evident id/timestamps may stay blank), then "
            f"re-run. Column descriptions are what the explorer shows and what NL→SQL reads. "
            f"Tables: {', '.join(tbls[:10])}" + ("…" if len(tbls) > 10 else "")
        ),
    }


def cmd_seed_examples(args) -> int:
    """Validate candidate seed examples against the live DB and write the passing ones —
    the whole Phase-5 mechanical loop in one call (no throwaway validate-and-write script)."""
    from . import curate
    from .introspect import make_execute_sql_runner
    org = L.load_organization(args.root, include_rejected=True)
    # Gate 1 — enrichment completeness (NOT bypassable): every kept column must be described
    # or explicitly ai_unknown. Catches a model that enriched tables but skipped columns.
    block = _coverage_gate(org)
    if block is not None:
        _print_json(block)
        return 2
    # Gate 2 — preseed review (Phase 4): no seeding on unreviewed metrics/entities. Bypassable
    # only via the Phase-4c --after-review path, after the user has been in the explorer.
    if not getattr(args, "after_review", False):
        block = _preseed_gate(org)
        if block is not None:
            _print_json(block)
            return 2
    with open(args.file) as fh:
        cands = json.load(fh)
    if isinstance(cands, dict):
        cands = cands.get("examples", cands.get("items", []))
    runner = make_execute_sql_runner(args.profile)
    passing, rejected = curate.validate_seeds(cands, runner)
    res = curate.add_examples(args.root, args.area, passing) if passing else curate.ApplyResult()
    _print_json({"added": res.applied, "written": res.validated,
                 "committed": res.committed, "rejected": rejected})
    return 0


def cmd_seed_validate(args) -> int:
    """Phase-6 trust onboarding: run every written seed against the live DB and emit the
    examples-validation items. Each seed runs THROUGH execute_sql.py (the agami-query path)
    so the fan/chasm pre-flight + default_filters always apply — a raw driver could skip
    that and let a fan-out scan the whole table. A refused/errored seed is surfaced with
    its `error`, not faked. Replaces ad-hoc 'run all the seeds' scripts."""
    import os
    import subprocess
    import csv as _csv
    from . import units

    seeds = L.list_prompt_examples(args.root, args.area)
    exe = sys.executable
    script = str(Path(__file__).resolve().parent.parent / "execute_sql.py")
    # the safety pass inside execute_sql.py finds the model via AGAMI_ARTIFACTS_DIR —
    # point it at the profile dir's parent so the right model loads (fan/chasm + filters).
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(Path(args.root).resolve().parent)}
    cap = max(0, args.preview)
    # Load the model once so result numbers are formatted by the SAME units.py the query
    # path uses (currency symbol + grouping). The validation preview must MATCH the real
    # answer — a column with unit: INR shows ₹ here too, not a bare number. If the model
    # can't load, fall back to raw cells (no regression).
    try:
        fmt_org = L.load_organization(args.root)
    except Exception:
        fmt_org = None
    items: list[dict] = []
    for i, ex in enumerate(seeds, 1):
        sql = (ex.get("sql") or "").strip()
        item: dict = {"n": i, "question": ex.get("question", ""), "sql": sql,
                      "state": "unreviewed", "row_headers": [], "row_preview": [], "row_count": 0}
        if not sql:
            item["error"] = "seed has no SQL"
            items.append(item)
            continue
        proc = subprocess.run(
            [exe, script, "--profile", args.profile, "--area", args.area, "--sql", sql],
            capture_output=True, text=True, env=env,
        )
        if proc.returncode != 0:
            # SQL error, or a fan/chasm pre-flight refusal — surface it; never fake a result.
            item["error"] = (proc.stderr or "").strip()[:600] or f"execute_sql exit {proc.returncode}"
        else:
            rows = list(_csv.reader(io.StringIO(proc.stdout)))
            if rows:
                headers = rows[0]
                data = rows[1:]
                # trace each output column → unit through the seed's SQL, then format every
                # cell exactly like the query path (units.format_table): a unit'd column gets
                # its symbol/grouping, a bare number gets grouping; non-numbers pass through.
                unit_map = {}
                if fmt_org is not None:
                    try:
                        unit_map = RT.resolve_result_units(fmt_org, sql)
                    except Exception:
                        unit_map = {}
                if data:
                    # match a column's unit by name OR positional key (#ci) — like
                    # units.format_table — so a dialect that re-cases result headers (e.g.
                    # Snowflake returns TOTAL_OUTSTANDING for alias `total_outstanding`)
                    # still resolves the unit and shows the currency symbol.
                    data = [[units.format_cell(c, unit_map.get(h) or unit_map.get(f"#{ci}"))
                             for ci, (h, c) in enumerate(zip(headers, row))]
                            for row in data]
                item["row_headers"] = headers
                item["row_count"] = len(data)
                item["row_preview"] = data[:cap]
        items.append(item)
    _print_json(items)
    return 0


def cmd_introspect(args) -> int:
    from . import introspect as INTRO
    from . import validator as V

    runner = INTRO.make_execute_sql_runner(args.profile)
    org, report = INTRO.introspect(
        args.profile,
        args.db_type,
        runner=runner,
        artifacts_dir=args.artifacts,
        out_dir=args.out,
        tables=args.tables,
        exclude_columns=args.exclude_columns,
        dry_run=args.dry_run,
        bigquery_region=args.bigquery_region,
    )
    res = V.validate(org)
    print(report.render())
    print(V.format_result(res))
    return 0 if res.ok else 1


def cmd_discover(args) -> int:
    """First pass: cheap discovery (tables + columns only) → inventory JSON +
    a prune HTML page. The user prunes, then `introspect --tables <kept>` runs
    the full build on only the kept tables. No grain/FK/row-count probes here."""
    from . import introspect as INTRO

    runner = INTRO.make_execute_sql_runner(args.profile)
    inventory = INTRO.discover_inventory(
        args.profile,
        args.db_type,
        runner=runner,
        tables=args.tables,
        schemas=args.schemas,
        bigquery_region=args.bigquery_region,
    )

    artifacts = Path(args.artifacts).expanduser()
    inv_path = (Path(args.inventory_out).expanduser() if args.inventory_out
                else artifacts / args.profile / ".introspect" / "inventory.json")
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inv_path.write_text(json.dumps(inventory, indent=2, default=str), encoding="utf-8")

    # Render the standalone prune page (import the sibling top-level script).
    scripts_dir = str(Path(__file__).resolve().parent.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import render_prune  # noqa: E402

    manifest = render_prune.build_manifest(inventory)
    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_prune.render(manifest), encoding="utf-8")

    _print_json({
        "profile": args.profile,
        "table_count": inventory["table_count"],
        "column_mode": inventory["column_mode"],
        "schemas": inventory["schemas"],
        "inventory_path": str(inv_path),
        "prune_html": str(out_path),
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="semantic_model", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("validate", help="validate a profile tree")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("context", help="assemble get_table_context")
    sp.add_argument("root")
    sp.add_argument("--area", default=None)
    sp.add_argument("--tables", nargs="+", required=True)
    sp.add_argument("--columns", nargs="*", default=None)
    sp.add_argument(
        "--include",
        nargs="*",
        default=["default_filters", "relationships", "caveats", "value_transforms"],
    )
    sp.set_defaults(func=cmd_context)

    sp = sub.add_parser("bundle", help="one-shot subject-area bundle")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.set_defaults(func=cmd_bundle)

    sp = sub.add_parser("areas", help="list subject areas")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_areas)

    sp = sub.add_parser("org-draft", help="print a human-narrative STARTER for ORGANIZATION.md (prompt only, no facts)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_org_draft)

    sp = sub.add_parser("org-context", help="print the full domain context (human narrative + model-derived summary + glossary) for the LLM")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_org_context)

    sp = sub.add_parser("examples", help="rank prompt examples for a query")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.add_argument("--query", required=True)
    sp.add_argument("--top-k", type=int, default=5)
    sp.set_defaults(func=cmd_examples)

    sp = sub.add_parser("preflight", help="fan-trap / chasm-trap pre-flight check")
    sp.add_argument("root")
    sp.add_argument("--sql", required=True)
    sp.set_defaults(func=cmd_preflight)

    sp = sub.add_parser("prepare", help="tier-independent safety pass: pre-flight + default_filters → SQL to run")
    sp.add_argument("root")
    sp.add_argument("--area", default=None)
    sp.add_argument("--sql", default=None)
    sp.add_argument("--sql-file", default=None, dest="sql_file")
    sp.set_defaults(func=cmd_prepare)

    sp = sub.add_parser("review-queue", help="trust-review items needing sign-off (Rule 1/2)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_review_queue)

    sp = sub.add_parser("review-items", help="curatable entries, tab-classified (4-tab dashboard)")
    sp.add_argument("root")
    sp.add_argument("--scope", default="all", choices=["all", "rule1", "rule2", "preseed"],
                    help="all | rule1 (metrics/named-filters) | rule2 | preseed (metrics+entities seeds depend on)")
    sp.set_defaults(func=cmd_review_items)

    sp = sub.add_parser("model-tree", help="browsable area→table→column tree (incl. rejected)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_model_tree)

    sp = sub.add_parser("coverage", help="per-table column-description coverage + enrichment-completeness verdict (ok:false ⇒ columns were skipped)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_coverage)

    sp = sub.add_parser("sensitive", help="list still-queryable columns flagged sensitive/PII + a count (excludes already-rejected columns/tables) — the Phase 4 curate gate signal")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_sensitive)

    sp = sub.add_parser("set-terminology", help="write the org domain glossary (term→definition) onto org.yaml key_terminology")
    sp.add_argument("root")
    sp.add_argument("--file", required=True, help="JSON object {term: definition, ...} (or {key_terminology: {...}})")
    sp.add_argument("--replace", action="store_true", help="replace the glossary instead of merging over it")
    sp.set_defaults(func=cmd_set_terminology)

    sp = sub.add_parser("curate", help="apply exclude/include/approve/reject/edit ops (validated)")
    sp.add_argument("root")
    sp.add_argument("--ops-file", required=True, help="JSON file: a list of op objects (or {ops:[...]})")
    sp.add_argument("--signer", default=None, help="sign-off email for approve ops")
    sp.add_argument("--role", default=None, help="sign-off role for approve ops")
    sp.set_defaults(func=cmd_curate)

    sp = sub.add_parser("add", help="create metric/entity YAMLs from a JSON file (validated, revertable)")
    sp.add_argument("root")
    sp.add_argument("--kind", required=True, choices=["metric", "entity"])
    sp.add_argument("--area", required=True)
    sp.add_argument("--file", required=True, help="JSON: a list of items (or {metrics:[…]}/{entities:[…]})")
    sp.add_argument("--signer", default=None)
    sp.add_argument("--role", default=None)
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("add-example", help="append/replace NL→SQL examples (prompt_examples/<area>/examples.yaml)")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.add_argument("--file", required=True,
                    help="JSON list of {question, sql, [tables, columns, metric, source, status, created_at]}")
    sp.add_argument("--signer", default=None)
    sp.add_argument("--role", default=None)
    sp.set_defaults(func=cmd_add_example)

    sp = sub.add_parser("remove-example", help="reject prompt example(s) by question (status: rejected — kept for audit, dropped from the runtime ranker)")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.add_argument("--question", required=True, action="append",
                    help="exact question text of an example to reject (repeatable for several)")
    sp.add_argument("--signer", default=None, help="who rejected it (recorded in the curation log)")
    sp.add_argument("--role", default=None)
    sp.set_defaults(func=cmd_remove_example)

    sp = sub.add_parser("suggest-units", help="list numeric money columns (for currency-unit stamping) via the tested name matcher")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_suggest_units)

    sp = sub.add_parser("format-table", help="format a result CSV into a deterministic markdown table (exact numbers)")
    sp.add_argument("--csv-file", default=None, help="result CSV (header row + rows); omit to read stdin")
    sp.add_argument("--units", default=None, help='JSON header->unit map, e.g. {"outstanding":"INR"}')
    sp.set_defaults(func=cmd_format_table)

    sp = sub.add_parser("seed-examples", help="validate candidate seeds against the live DB + write the passing ones")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.add_argument("--profile", required=True, help="credentials profile (for the live-DB validation)")
    sp.add_argument("--file", required=True, help="JSON list of candidate {question, sql, [tables, columns, metric]}")
    sp.add_argument("--after-review", action="store_true",
                    help="bypass the preseed-review gate (Phase 4c only — the user has already "
                         "been in the explorer and chose to proceed with items still unreviewed)")
    sp.set_defaults(func=cmd_seed_examples)

    sp = sub.add_parser("seed-validate", help="run every written seed against the live DB (through execute_sql's safety pass) + emit examples-validation items")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.add_argument("--profile", required=True, help="credentials profile (for live-DB execution)")
    sp.add_argument("--preview", type=int, default=10, help="max result rows per seed in the dashboard preview (default 10)")
    sp.set_defaults(func=cmd_seed_validate)

    sp = sub.add_parser("introspect", help="introspect a live DB into the semantic model")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--db-type", required=True, dest="db_type",
                    help="postgres|mysql|snowflake|bigquery|redshift|sqlite|sqlserver|"
                         "databricks|trino|oracle|duckdb|supabase")
    sp.add_argument("--artifacts", required=True, help="artifacts_dir (output: <artifacts>/<profile>/)")
    sp.add_argument("--out", default=None, help="override output dir")
    sp.add_argument("--tables", nargs="*", default=None,
                    help="schema.table allowlist — the prune step's kept set (also the "
                         "no-catalog/probe-only case)")
    sp.add_argument("--exclude-columns", nargs="*", default=None, dest="exclude_columns",
                    help="schema.table.column list to mark excluded (the prune step's dropped columns)")
    sp.add_argument("--bigquery-region", default="region-us", dest="bigquery_region")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_introspect)

    sp = sub.add_parser("discover",
                        help="cheap first pass: list tables + columns and render the prune page "
                             "(no grain/FK/row-count probes) — prune, then introspect the kept set")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--db-type", required=True, dest="db_type",
                    help="postgres|mysql|snowflake|bigquery|redshift|sqlite|sqlserver|"
                         "databricks|trino|oracle|duckdb|supabase")
    sp.add_argument("--artifacts", required=True, help="artifacts_dir (inventory: <artifacts>/<profile>/.introspect/)")
    sp.add_argument("--out", required=True, help="output HTML path for the prune page")
    sp.add_argument("--inventory-out", default=None, dest="inventory_out",
                    help="override inventory JSON path (default <artifacts>/<profile>/.introspect/inventory.json)")
    sp.add_argument("--tables", nargs="*", default=None,
                    help="optional schema.table allowlist to scope discovery (probe-only case)")
    sp.add_argument("--schemas", nargs="*", default=None,
                    help="restrict discovery to these schemas (the user's schema pick)")
    sp.add_argument("--bigquery-region", default="region-us", dest="bigquery_region")
    sp.set_defaults(func=cmd_discover)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        # No model at this root yet. This is an expected, clean signal (not a crash):
        # callers fold the "does a model exist?" check into their first real command
        # (e.g. `sm areas`) instead of a separate filesystem probe. Exit 3 = "no model
        # here — run agami-connect"; distinct from validation errors (1) and usage (2).
        if "org.yaml" in str(e):
            _print_json({"error": "no_model", "detail": str(e)})
            return 3
        raise


if __name__ == "__main__":
    raise SystemExit(main())
