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

Every subcommand emits JSON on stdout (so callers parse one shape) except
`validate`, which prints a human report and sets the exit code (0 ok, 1 errors).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import loader as L
from . import runtime as RT
from . import validator as V


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def cmd_validate(args) -> int:
    org = L.load_organization(args.root)
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
        dry_run=args.dry_run,
        bigquery_region=args.bigquery_region,
    )
    res = V.validate(org)
    print(report.render())
    print(V.format_result(res))
    return 0 if res.ok else 1


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
    sp.add_argument("--scope", default="all", choices=["all", "rule1", "rule2"],
                    help="all (default) | rule1 (metrics/named-filters needing sign-off) | rule2")
    sp.set_defaults(func=cmd_review_items)

    sp = sub.add_parser("model-tree", help="browsable area→table→column tree (incl. rejected)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_model_tree)

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

    sp = sub.add_parser("introspect", help="introspect a live DB into the semantic model")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--db-type", required=True, dest="db_type",
                    help="postgres|mysql|snowflake|bigquery|redshift|sqlite|sqlserver|"
                         "databricks|trino|oracle|duckdb|supabase")
    sp.add_argument("--artifacts", required=True, help="artifacts_dir (output: <artifacts>/<profile>/)")
    sp.add_argument("--out", default=None, help="override output dir")
    sp.add_argument("--tables", nargs="*", default=None,
                    help="explicit schema.table allowlist for the no-catalog (probe-only) case")
    sp.add_argument("--bigquery-region", default="region-us", dest="bigquery_region")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_introspect)

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
