#!/usr/bin/env python3
"""CLI entrypoint for the agami semantic model.

One dispatch surface shared by the skills and the test suite, so behavior is
identical wherever the model is exercised. Subcommands:

    validate  <root>                      — parse + validate a profile tree
    migrate   --profile P --artifacts DIR — one-time convert a legacy OSI profile (transitional)
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
from . import migrate as M
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


def cmd_migrate(args) -> int:
    report = M.migrate_profile(
        args.profile,
        args.artifacts,
        out_dir=args.out,
        dry_run=args.dry_run,
    )
    print(report.render())
    return 0 if not report.validator_errors else 1


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="semantic_model", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("validate", help="validate a profile tree")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("migrate", help="one-time convert a legacy OSI profile (transitional)")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--artifacts", required=True, help="artifacts_dir containing <profile>/")
    sp.add_argument("--out", default=None, help="output dir (default <profile>/.semantic_v2)")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_migrate)

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

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
