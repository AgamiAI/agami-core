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
    remove-example <root> --area A --question Q — mark example(s) rejected by question
                                            (kept in the file for audit, not deleted)

`seed-examples`, `seed-validate` and `remove-example` take `--area` repeated, or none for every
area; then they print one combined result (per-area entries labelled with the area, plus totals).

Every subcommand emits JSON on stdout (so callers parse one shape) except
`validate`, which prints a human report and sets the exit code (0 ok, 1 errors).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import build_settings as BS
from . import failure_kinds as FK
from . import introspect as INTRO
from . import loader as L
from . import metadata_sources as MS
from . import platform_joins as PJ
from . import runtime as RT
from . import validator as V
from .plain_names import column_label, distinct_labels


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def _read_json_file(path: str) -> Any:
    """Read a command's JSON input, or stop with a JSON error naming the RESOLVED path. A traceback
    on stdout (a relative path that didn't resolve where the caller thought, a half-written file)
    read as "nothing to apply" to a skill parsing the output, so it went on as if the call had worked.
    Under the scratch-only guard (`AGAMI_SCRATCH_ONLY=1`) the file must be one of agami's scratch
    files (semantic_model/scratch.py)."""
    from . import scratch
    if scratch.guarded():
        try:
            data = scratch.read_scratch(path, scratch.INPUT_CAP, engine_ok=True)
            return json.loads(data.decode("utf-8"))
        except scratch.NotScratch as exc:
            _print_json({"error": str(exc)})
        except ValueError as exc:
            _print_json({"error": f"could not read JSON from {path}: {exc}"})
        raise SystemExit(2)
    p = Path(path).expanduser()
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        _print_json({"error": f"file not found: {p.resolve()}"})
    except (OSError, ValueError) as exc:
        _print_json({"error": f"could not read JSON from {p.resolve()}: {exc}"})
    raise SystemExit(2)


def _read_text_file(path: str) -> str:
    """A person's free text from a file the skill wrote with its Write tool, verbatim (UTF-8) minus one
    trailing newline. Free text never goes on the command line: a quote, `$` or `;` in a company name
    or a question would be read by the shell, and escaping it changed the person's words.

    Only one of agami's scratch files, always (semantic_model/scratch.py): this text is saved into
    the org record and datasource.yaml, so a call pointed at any other file would copy it there."""
    from . import scratch
    try:
        text = scratch.read_scratch(path, scratch.TEXT_CAP).decode("utf-8")
    except scratch.NotScratch as exc:
        _print_json({"error": str(exc)})
        raise SystemExit(2)
    except ValueError as exc:
        _print_json({"error": f"could not read text from {Path(path).resolve()}: {exc}"})
        raise SystemExit(2)
    for end in ("\r\n", "\n"):
        if text.endswith(end):
            return text[: -len(end)]
    return text


def _utc_now_iso() -> str:
    """ISO-8601 UTC (`...Z`) — the sign-off timestamp format the validator expects."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp_approve_ops(ops: list) -> list:
    """Stamp `at` on any `approve` op that lacks one. curate.apply deliberately can't read
    the clock itself (`op.get("at")` — "caller stamps"); an approve op without `at` records
    `signed_off_at: null` and the validator then rejects the whole batch. The CLI process
    *can* read the clock, so it stamps here — one place, for every CLI caller."""
    now = _utc_now_iso()
    for op in ops:
        if isinstance(op, dict) and op.get("op") == "approve" and not op.get("at"):
            op["at"] = now
    return ops


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
    org = L.load_datasource(args.root, include_rejected=True)
    res = V.validate(org)
    unknown = L.unknown_datasource_keys(args.root)
    if unknown:
        # A warning, not an error (#575): the model still loads, but those fields are not served.
        res.warn("unknown_datasource_key", L.unknown_keys_message(unknown))
    print(V.format_result(res))
    ok = res.ok
    # Deployment-level pass (F16 / ACE-072): when the profile sits under a deployment with an
    # OrgRecord, ALSO validate the cross-datasource bridges — they resolve endpoints ACROSS profiles,
    # so a single-profile validate can't see them. A no-op (empty, ok) on a pre-F16 layout with no
    # record, so this stays a superset of the old behaviour. The artifacts dir is the profile's parent.
    dres = V.validate_deployment(Path(args.root).parent)
    if dres.findings:
        print(V.format_result(dres))
    return 0 if (ok and dres.ok) else 1


def cmd_snapshot(args) -> int:
    """Stamp the model_version snapshot for a profile tree. Introspect/curate do this
    automatically; this command is for paths that write a model WITHOUT going through
    them — e.g. agami-connect's sample copy (6A) drops the prebuilt model into place,
    then calls this so the answer receipt has a real model_version."""
    from . import snapshot as SN
    h = SN.write_snapshot(args.root)
    print(h or "(no model to snapshot)")
    return 0 if h else 1


def cmd_ensure_org_id(args) -> int:
    """Mint + persist the deployment org_id into <root>/datasource.yaml if absent (F14 / ACE-056; relocated
    by F15 / ACE-067), then print it. Idempotent — an already-minted id is returned unchanged. The
    introspect/curate paths mint inline via write_tree; this command is for paths that write a model
    WITHOUT them — e.g. agami-connect's sample copy (6A) drops a prebuilt model that carries no id.
    Deployment-scoped: the id now comes from the root ``organization.yaml`` record (minted on first
    use), so every profile in the dir resolves the same one — no sibling scan."""
    from pathlib import Path

    from . import build
    root = Path(args.root)
    datasource_path = root / "datasource.yaml"
    doc = L._read_yaml(datasource_path) or {}
    existing = doc.get("org_id")
    oid = build.ensure_org_id(root, existing or None)
    if oid != existing:
        doc["org_id"] = oid
        datasource_path.write_text(build._dump(doc), encoding="utf-8")
    print(oid)
    return 0


def cmd_context(args) -> int:
    org = L.load_datasource(args.root)
    out = L.get_table_context(
        org,
        args.tables,
        area=args.area,
        columns=args.columns,
        include=args.include,
    )
    _print_json(out)
    return 0


def _within_model(handler):
    """Run `handler` with every file read held inside `args.root`, links resolved (OutsideModel
    otherwise). For the verbs the plugin's permission hook (scripts/approve_own_calls.py, VERBS)
    runs with no prompt: the hook checks only the folder a call names, and the model can name more."""
    def run(args) -> int:
        with L.contained(args.root):
            return handler(args)
    run.within_model = True
    run.__name__ = handler.__name__
    run.__doc__ = handler.__doc__
    return run


def _reads_no_file(handler):
    """Mark `handler` as safe for the hook's approved path the way `_within_model` does, for a verb
    that takes no model folder and opens no file (`batch-id`): every read it makes is inside the
    model because it makes none. `sm` runs an approved verb only when its handler carries the mark,
    so without it the verb would stop with the install step on a library that is current."""
    handler.within_model = True
    return handler


@_within_model
def cmd_bundle(args) -> int:
    org = L.load_datasource(args.root)
    _print_json(L.get_subject_area_bundle(org, args.area))
    return 0


@_within_model
def cmd_areas(args) -> int:
    org = L.load_datasource(args.root)
    _print_json(RT.list_subject_areas(org))
    return 0


def cmd_org_draft(args) -> int:
    # A human-narrative STARTER for datasource.md (skip path) — a prompt only, no model
    # facts. Those are derived at read time (see `org-context`), so they never get baked into
    # the editable prose file where a human could clobber them.
    from . import org_draft
    org = L.load_datasource(args.root, include_rejected=False)
    sys.stdout.write(org_draft.starter_datasource_md(org))
    return 0


def cmd_org_context(args) -> int:
    # The full domain context for the LLM, two-level (F15 / ACE-069): the shared COMPANY block from the
    # deployment record (<artifacts_dir>/organization.yaml + the root organization.md), then this
    # datasource's source-specific narrative + model-derived summary. With no record it degrades to the
    # pre-F15 per-profile assembly (byte-identical). This is what the query path injects as context.
    from . import org_draft
    from . import org_record as OR
    root = Path(args.root)
    org = L.load_datasource(root, include_rejected=False)
    src_md = root / "datasource.md"
    source_narrative = src_md.read_text(encoding="utf-8") if src_md.exists() else ""
    art = root.parent  # the artifacts dir holds the deployment-level record + company narrative
    record = OR.load_org_record(art)
    comp_md = OR.narrative_path(art)
    company_narrative = comp_md.read_text(encoding="utf-8") if comp_md.exists() else ""
    sys.stdout.write(
        org_draft.compose_org_context(
            record, [org], company_narrative=company_narrative, source_narratives=[source_narrative]
        )
    )
    return 0


def cmd_set_org(args) -> int:
    # Write the human-authored company fields (name / short description) into the deployment-level
    # organization record. Onboarding calls this after asking the company question, so the record
    # carries more than just its org_id. Only the flags passed are updated; others are left as-is.
    from . import org_record as OR

    name = _read_text_file(args.name_file) if args.name_file else args.name
    description = _read_text_file(args.description_file) if args.description_file else args.description
    record = OR.set_org_fields(args.artifacts_dir, name=name, description=description)
    _print_json(
        {"org_id": record.org_id, "name": record.name, "description": record.description}
    )
    return 0


def cmd_examples(args) -> int:
    if not args.query and not args.metric:
        print("examples: give --query or --metric (or both)", file=sys.stderr)
        return 2
    examples = L.list_prompt_examples(args.root, args.area)
    org = None
    if args.metric:
        try:
            org = L.load_datasource(args.root)
        except Exception:  # noqa: BLE001 - an unreadable model only costs the selector resolution
            org = None
    if not args.query:
        # By metric alone: the examples tagged with any of them, the most tags first. With no
        # question there is nothing to rank the rest by, so they are left out.
        wanted = RT.metric_tags([str(m) for m in args.metric], org)
        examples = [ex for ex in examples
                    if RT.example_metric_hits(ex, wanted, ex.get("area") or args.area)]
    matches = RT.get_prompt_examples(args.query or "", examples, top_k=args.top_k,
                                     metrics=args.metric, org=org, area=args.area)
    _print_json(
        {
            "high_confidence": RT.is_high_confidence(matches),
            "matches": [{"score": round(m.score, 3), "example": m.example} for m in matches],
        }
    )
    return 0


def cmd_preflight(args) -> int:
    org = L.load_datasource(args.root)
    result = RT.pre_flight_check(args.sql, org)
    _print_json(result.as_dict())
    return 0


def cmd_prepare(args) -> int:
    """Tier-independent safety pass: run the fan/chasm pre-flight and return the SQL to
    actually execute. The query skill calls this on EVERY tier before handing SQL to
    psql/mysql/etc.

    **This is not a gate and never was.** It runs the fan/chasm and aggregation-semantics
    checks, which describe; it does NOT run table scope, the `SELECT *` ban or column scope,
    which refuse. Those live in `execute_sql` alone. A tier that runs its own SQL after
    calling this has had the checks, not the gates, and `--no-safety` is therefore never the
    right flag to pair with it.

    Because the caller RUNS what comes back, this command echoes the statement it was given and
    never a statement of ours. It used to answer an aggregation-only fan trap with a rewritten
    string plus a `rewritten: true` flag, which made every non-`execute_sql` tier execute something
    the caller never wrote.

    **It is now total: exit 0, always.** It refused a fan or chasm trap until ACE-094, which was the
    last non-zero exit here. Correctness is not a refusal — whether a multiplied total is a bug
    depends on the question, and this command has the statement but not the question. So it reports
    what it found and lets the caller, who has both, decide.

    `aggregates` is that report: one entry per aggregate the statement computes, saying whether a
    join multiplies the rows behind it and which join does. `findings` is the same information
    projected flat, kept because a caller that only wants "what went wrong" should not have to walk
    the roster to find it — but it is the roster that carries the cleared aggregates, and a surface
    reading only `findings` reports a clean number by saying nothing about it.

    **An empty `findings` is not by itself a clean bill of health.** The analysis does not run when
    sqlglot is missing, when the statement does not parse, or when there is no SELECT, and each of
    those yields the same empty list a genuinely clean statement does. `unchecked` is what separates
    them: null when the checks ran, a sentence when they could not. Read both or read neither.

    A table's declared `default_filters` are NOT applied here — ACE-042 deleted the injection, and
    nothing has replaced it — so this command emits no `applied_filters` key. An always-empty list
    would read as "we checked, none applied", which is the silence that reads as clean. Which
    declared filters a statement DID satisfy is reported per table reference by `sm receipt`, on
    `tables.items[].filters`; it is a fact about the finished statement, so it is assembled where
    the receipt is and not here."""
    sql = args.sql
    if args.sql_file:
        sql = Path(args.sql_file).read_text(encoding="utf-8")
    org = L.load_datasource(args.root)
    pf = RT.pre_flight_check(sql, org)
    _print_json({
        "sql": sql,
        # One entry per aggregate, cleared ones included — the same roster the receipt's
        # `aggregates` section carries, from the same analysis, so the two surfaces cannot describe
        # one statement two ways.
        "aggregates": [a.as_dict() for a in pf.aggregates],
        "findings": [f.as_dict() for f in pf.findings],
        # Null when the checks ran. A caller that ignores this reads "skipped" as "clean".
        "unchecked": pf.unchecked,
        # {output_column: unit}, traced through the caller's own statement — feed straight to
        # `format-table --units` so summed/aliased currency formats correctly.
        "units": RT.resolve_result_units(org, sql),
        # the output columns holding a year, a code or an id — `format-table --labels`
        "labels": sorted(RT.resolve_result_labels(org, sql)),
    })
    return 0


def cmd_receipt(args) -> int:
    """Assemble the trust receipt for an executed query — deterministically, from the
    SQL + the model. Replaces the LLM hand-building the receipt JSON in prose: the five
    sections (columns, tables, joins, aggregates, assumptions) and the model_version pin
    all come from parsing the SQL against the model (the SAME `runtime.assemble_receipt`
    the MCP server uses), so the skill and the server describe one statement one way."""
    from . import snapshot as SN
    sql = args.sql
    if args.sql_file:
        sql = Path(args.sql_file).read_text(encoding="utf-8")
    org = L.load_datasource(args.root)
    receipt = RT.assemble_receipt(
        org, sql,
        model_version=SN.newest_version(args.root),
        freshness=args.freshness,
    )
    _print_json(receipt)
    return 0


# --- the four verbs that grade a statement a PERSON supplied ----------------------------------
#
# `agami-reconcile` takes a trusted query as evidence, never as the answer. These verbs are the
# deterministic half of grading it: none of them runs a probe, and none writes anything. The skill
# runs every emitted probe through the execution tier a question takes, then hands the CSVs back.


def _grammar(org) -> Optional[str]:
    """The sqlglot grammar to read statements in, or None when the semantic model cannot say which
    engine its SQL runs on. Every verb below reports the same value under `dialect`, so a caller
    reads one spelling of "engine unknown" across all four."""
    return RT._dialect_of(org)[0]


def _read_sql_file(path: str) -> Optional[str]:
    """The statement in `path`, or None after printing `{"error": "unreadable_sql_file"}`: a file that
    is not there must not become a traceback with an empty stdout, because the skill redirects stdout
    to a file and a zero-byte file downstream reads as a probe that failed."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        _print_json({"error": "unreadable_sql_file", "detail": str(exc).splitlines()[0]})
        return None


def cmd_claims(args) -> int:
    """Where two statements differ, in the eight claims the golden runner already compares.
    `golden_claims.compare_statements` has been reachable from the runner and the save door and
    from no command; this is that command. A side that could not be read says so, rather than
    leaving eight `unknown` claims to explain themselves."""
    from .golden_claims import compare_statements, count_temporal_predicates, read_claims
    org = L.load_datasource(args.root)
    grammar = _grammar(org)
    left = _read_sql_file(args.sql_file)
    if left is None:
        return 2
    right = _read_sql_file(args.against_sql_file)
    if right is None:
        return 2
    diff = compare_statements(left, right, dialect=grammar or "")
    out = diff.as_dict()
    out["unreadable"] = {
        "sql_file": read_claims(left, dialect=grammar or "").unreadable,
        "against_sql_file": read_claims(right, dialect=grammar or "").unreadable,
    }
    # How many conjuncts on each side speak of time. Two zeros beside a `date_window` that reads
    # `unknown` mean neither statement filtered on a date; anything else leaves the window open.
    out["temporal_predicates"] = {
        "sql_file": count_temporal_predicates(left, dialect=grammar or ""),
        "against_sql_file": count_temporal_predicates(right, dialect=grammar or ""),
    }
    out["dialect"] = grammar
    _print_json(out)
    return 0


def cmd_compare_results(args) -> int:
    """Whether two result sets say the same thing, through the one comparator the golden runner
    uses, so a table-shaped answer is judged the way an answer key is and not by a second rule. The
    match level defaults to the comparator's own (`exact`) for the same reason."""
    import dataclasses

    from .comparator import compare_result_sets, result_from_csv
    from .golden import GoldenBounds
    org = L.load_datasource(args.root)
    bounds = None
    if args.bounds:
        try:
            bounds = GoldenBounds(**json.loads(args.bounds))
        except (ValueError, TypeError) as exc:  # pydantic's ValidationError is a ValueError
            _print_json({"error": "bad_bounds", "detail": str(exc).splitlines()[0]})
            return 2
    try:
        golden = result_from_csv(args.golden_csv)
        generated = result_from_csv(args.generated_csv)
    except (OSError, ValueError) as exc:
        _print_json({"error": "unreadable_csv", "detail": str(exc)})
        return 2
    golden_sql = None
    if args.golden_sql_file:
        golden_sql = _read_sql_file(args.golden_sql_file)
        if golden_sql is None:
            return 2
    score = compare_result_sets(golden, generated, match=args.match, golden_sql=golden_sql,
                                bounds=bounds, dialect=_grammar(org),
                                ordered=False if args.unordered else None)
    _print_json(dataclasses.asdict(score))
    return 0


def cmd_join_probes(args) -> int:
    """Every join a statement wrote: declared or not, on the declared key or not, and the probe SQL
    that would test whether the keys resolve. Emitted, never run."""
    from . import probes
    org = L.load_datasource(args.root)
    sql = _read_sql_file(args.sql_file)
    if sql is None:
        return 2
    _print_json(probes.join_probes(org, sql))
    return 0


def cmd_mentions(args) -> int:
    """Every prose line in the semantic model (descriptions, caveats, glossary, narrative, prompt
    examples) that mentions a table or column the statement reads. Quoted for a reader, never graded:
    the ledger puts these beside a part that fell short, so the words that shaped the SQL sit next to
    the number that went wrong."""
    from . import mentions
    org = L.load_datasource(args.root)
    sql = _read_sql_file(args.sql_file)
    if sql is None:
        return 2
    _print_json(mentions.prose_mentions(org, Path(args.root), sql, _grammar(org)))
    return 0


def cmd_filter_values_plan(args) -> int:
    """Every value typed into a filter: what the semantic model already knows about its column, and
    the probe SQL that would settle the rest."""
    from . import probes
    org = L.load_datasource(args.root)
    sql = _read_sql_file(args.sql_file)
    if sql is None:
        return 2
    _print_json(probes.filter_values_plan(org, sql))
    return 0


def cmd_filter_values_judge(args) -> int:
    """The probe results read back onto the plan: one grade per value. Takes the profile root like
    every other verb, and reads no model from it; the plan already carries what the semantic model
    knew."""
    from . import probes
    results = Path(args.results)
    if not results.is_dir():
        _print_json({"error": "no_results_dir", "detail": f"{results} is not a directory"})
        return 2
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        out = probes.filter_values_judge(plan, results)
    except (OSError, ValueError) as exc:
        _print_json({"error": "bad_plan", "detail": str(exc)})
        return 2
    _print_json(out)
    return 0


@_within_model
def cmd_review_queue(args) -> int:
    from . import curate
    org = L.load_datasource(args.root)
    _print_json(curate.review_queue(org, root=args.root))
    return 0


@_within_model
def cmd_review_items(args) -> int:
    from . import curate
    # Only the full curation view shows hidden entries; every sign-off scope is the served model's,
    # so its count equals `sm review-queue`'s and what hangs off a hidden table is left out.
    org = L.load_datasource(args.root, include_rejected=args.scope == "all")
    _print_json(curate.all_items(org, scope=args.scope, root=args.root))
    return 0


def cmd_history(args) -> int:
    """The model's curation log, newest first: each change with when it was made, who made it, the
    value it replaced and the model version it produced — the history a profile outside git keeps (#528)."""
    from . import curate
    _print_json(curate.history(args.root, limit=args.limit))
    return 0


def cmd_curate_gate(args) -> int:
    """The Phase-4 curate gate decision in ONE call: count still-queryable PII columns
    + pending pre-seed sign-offs, and say whether to open the explorer. Replaces the
    skill running `sm sensitive` + `sm review-items --scope preseed` and branching by
    hand. Turn-boundary-safe (same answer on a fresh run or a resume)."""
    from . import curate
    org = L.load_datasource(args.root, include_rejected=True)
    pii = curate.sensitive_columns(org).get("count", 0)
    preseed = len(curate.all_items(L.load_datasource(args.root), scope="preseed"))
    _print_json({"pii_count": pii, "preseed_count": preseed,
                 "should_open_explorer": pii > 0 or preseed > 0})
    return 0


@_within_model
def cmd_model_tree(args) -> int:
    from . import curate
    org = L.load_datasource(args.root, include_rejected=True)
    _print_json(curate.model_tree(org))
    return 0


def _progress_line(raw: str) -> dict:
    """One progress-log line as a person's line, with an 8-cell bar drawn from its own counts,
    rounded down so the bar is full only at the end. Never an estimate: no count, no bar."""
    from . import steps
    parsed = steps.parse_progress(raw)
    if not parsed:
        # Posted as is (`then`: "Post `line`"), so a line the engine didn't phrase is quoted like
        # other model text (a tampered log's `# …` must not render as a heading), and a failure's
        # reason (the database's or the model's words) is quoted with the longer failure cap.
        from .say_text import one_line, quoted
        waiting = steps.WAITING.match(raw)
        if waiting:
            return {"line": f"Waiting on the database for {one_line(waiting.group(1))}: "
                            f"{quoted(waiting.group(2))}", "done": False, "failed": False}
        if raw.startswith("failed:"):
            reason = quoted(steps.strip_paths(raw[len("failed:"):].strip()), steps.FAILURE_CAP)
            return {"line": f"The build didn't finish: {reason}", "done": False, "failed": True}
        return {"line": quoted(raw), "done": False, "failed": False}
    m, phrase, unit = parsed
    if unit:
        n, total = int(m.group(1)), int(m.group(2))
        filled = (8 * n) // total if total else 0
        bar = "▰" * filled + "▱" * (8 - filled)
        return {"line": f"{phrase} {bar} {n}/{total} {unit}", "n": n, "total": total,
                "done": False, "failed": False}
    return {"line": phrase.format(*m.groups()), "done": raw.startswith("done:"), "failed": False}


@_within_model
def cmd_progress(args) -> int:
    """The build's latest progress line, ready to post, from <root>/.introspect/progress.log.

    With --wait N it returns as soon as the log moves on, or after N seconds. A skill loops on it
    while the build runs in the background: each call is a pre-approved `sm` command that keeps the
    turn open, and a turn that ends mid-onboarding loses the skill's pre-approvals, so every later
    command would stop for permission."""
    from . import steps
    attempt_at = steps.attempt_started_at(Path(args.root))

    def last() -> str:
        return steps.attempt_progress_line(Path(args.root), attempt_at)

    start = last()
    deadline = time.monotonic() + max(0.0, args.wait)
    current = start
    # A finished or failed log won't move on, so waiting on it would only stall the skill's loop.
    while args.wait and current == start and not current.startswith(("done:", "failed:")) \
            and time.monotonic() < deadline:
        time.sleep(0.5)
        current = last()
    # The compact step list rides along only when it moved since `sm steps` or `sm progress` last
    # printed it, so each poll is one call, and `then` says whether to refresh the task list.
    root = Path(args.root).expanduser()
    view = steps.compact(steps.onboarding_steps(root))
    listed = view["list"]
    shown = _progress_line(current) if current else {"line": "", "done": False, "failed": False}
    before = steps.read_printed(root)
    steps.remember_printed(root, before, {"list": listed, "line": shown["line"]})
    # Nothing is remembered for a folder that doesn't exist yet, and its list is the fresh one every
    # time (`sm steps` prints it): reporting it as moved would ask for a refresh on every poll.
    list_changed = root.is_dir() and before.get("list") != listed
    # A failure is not posted from here: `sm steps` says it as its problem, and saying it in both
    # places posted it twice.
    line_new = bool(shown["line"]) and not shown["failed"] and before.get("line") != shown["line"]
    out: dict[str, Any] = {**shown, "started": bool(current)}
    if current:
        out["changed"] = current != start
    if list_changed:
        out["list"] = listed
    out["list_changed"] = list_changed
    # A problem on the list (a build killed with no `failed:` line) ends the build too: its log
    # never moves on again, so "call this again" would loop for ever.
    ended = shown["done"] or shown["failed"] or bool(view["problem"])
    out["then"] = steps.progress_next(line_new, list_changed, ended)
    _print_json(out)
    return 0


@_reads_no_file
def cmd_batch_id(args) -> int:
    """Print `{"batch": "<YYYYMMDD-HHMMSS>-<4 hex>"}`: a stamp the skill puts in the names of one
    page's scratch files (the page, its items, the decisions it saves). Without it a file left by an
    earlier run (a reintrospect, a sample rebuild, another database with the same area name) reads as
    this run's, and a Write onto a file Claude hasn't read in this session fails. The random part
    keeps two pages started in the same second apart."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    _print_json({"batch": f"{stamp}-{os.urandom(2).hex()}"})
    return 0


def cmd_choose_folder(args) -> int:
    """Save the person's chosen artifacts folder (the pointer) before init-local sets it up. See
    semantic_model/local_setup.py."""
    from . import local_setup as LS
    try:
        _print_json(LS.choose_folder(args.artifacts_dir))
    except LS.SetupRefused as exc:
        _print_json({"error": str(exc)})
        return 2
    return 0


def cmd_init_local(args) -> int:
    """Set up `<artifacts_dir>/local/` (0700), its scratch folder and `.gitignore`; `--sample` also
    writes the sample's credential for promote_credentials.py. See semantic_model/local_setup.py."""
    from agami_paths import NotPrivate

    from . import local_setup as LS
    try:
        _print_json(LS.init_local(args.artifacts_dir, sample=args.sample))
    except (LS.SetupRefused, NotPrivate) as exc:
        _print_json({"error": str(exc)})
        return 2
    return 0


def cmd_set_config(args) -> int:
    """Merge checked fields into `<artifacts_dir>/local/.config` (0600). See local_setup.py."""
    from agami_paths import NotPrivate

    from . import local_setup as LS
    try:
        _print_json(LS.set_config(args.artifacts_dir, tier=args.tier,
                                  active_profile=args.active_profile, python3=args.python3,
                                  tools=args.tool, identity_file=args.identity_file))
    except (LS.SetupRefused, NotPrivate) as exc:
        _print_json({"error": str(exc)})
        return 2
    return 0


@_within_model
def cmd_steps(args) -> int:
    """The onboarding step list read from disk, after applying any --mark: by default the compact
    list the skill copies as is, `--full` the whole dict (ids, labels, counts, timeline path).
    Every mark is checked before any is written, so a bad one leaves the timeline as it was."""
    from . import steps
    if not str(args.root).strip():
        # An unset "$ROOT" arrives as "": it would put a timeline in the working directory.
        print("sm steps: the database folder is empty; pass <artifacts_dir>/<profile>",
              file=sys.stderr)
        return 2
    try:
        marks = [steps.parse_mark(spec) for spec in args.mark]
    except ValueError as exc:
        print(f"sm steps: {exc}", file=sys.stderr)
        return 2
    try:
        for step_id, status in marks:
            steps.mark(Path(args.root), step_id, status)
    except ValueError as exc:
        print(f"sm steps: {exc}", file=sys.stderr)
        return 2
    root = Path(args.root).expanduser()
    result = steps.onboarding_steps(root)
    listed = steps.compact(result)["list"]
    before = steps.read_printed(root)
    changed = before.get("list") != listed
    said = steps.steps_next(changed, result["problem"], before.get("problem"),
                            model_written=result["model_written"] and not marks)
    # The problem is remembered as said: it is said once, not on every call while it lasts.
    steps.remember_printed(root, before, {"list": listed, "problem": result["problem"]})
    # `--full` carries `list` too, so every `then` ("update the task list from `list`") reads right.
    view = {**result, "list": listed} if args.full else steps.compact(result)
    _print_json({**view, "changed": changed, **said})
    return 0


def _steps_refresh(root: Path) -> dict:
    """`{"list": …}` when this model is being onboarded (it has a step timeline) and the step list
    now reads differently from the one last printed, else {}. A command that changes a count the
    list shows carries it, so the task list is refreshed where the count changed: a live run's list
    read "2% described" while the chat said 82%, and "11 examples" after one was dropped."""
    from . import steps
    root = Path(root).expanduser()
    if not (root / ".introspect" / "timeline.jsonl").is_file():
        return {}
    try:
        listed = steps.compact(steps.onboarding_steps(root))["list"]
    except Exception:  # noqa: BLE001 — the list is a view: a command never fails over it
        return {}
    before = steps.read_printed(root)
    if before.get("list") == listed:
        return {}
    steps.remember_printed(root, before, {"list": listed})
    return {"list": listed}


def _with_refresh(out: dict, root: Path) -> dict:
    """`out` with the step list when it changed, and the instruction to show it added to `then`."""
    from . import steps
    fresh = _steps_refresh(root)
    if not fresh:
        return out
    then = out.get("then")
    return {**out, **fresh, "then": (then + steps.THEN_AND_UPDATE) if then else steps.THEN_UPDATE}


@_within_model
def cmd_summary(args) -> int:
    """The onboarding's closing summary as markdown, ready to post as printed; `--json` wraps it
    with the overview's totals."""
    from . import summary
    result = summary.onboarding_summary(Path(args.root))
    if args.json:
        _print_json(result)
    else:
        sys.stdout.write(result["markdown"])
    return 0


@_within_model
def cmd_overview(args) -> int:
    """The numbers an overview card shows: per subject area its tables, columns described, metrics
    and joins (and how many await sign-off) and examples, plus totals. One call, so a card never
    adds up model-tree, coverage and review-items itself."""
    from . import curate
    org = L.load_datasource(args.root)   # the served model, so the counts match the queue's
    _print_json(curate.overview(org, args.root))
    return 0


@_within_model
def cmd_coverage(args) -> int:
    """Per-table column-description coverage + the enrichment-completeness verdict.
    The skill runs this at the end of Phase 2 (and reports it in the Phase 7 summary):
    `ok: false` with untouched columns means enrichment skipped the column pass."""
    from . import curate
    org = L.load_datasource(args.root, include_rejected=True)
    _print_json(_with_refresh(curate.column_coverage(org), Path(args.root)))
    return 0


def cmd_profile(args) -> int:
    """What the build read from the data (#448): the column profile and the join checks from
    `<root>/.introspect/profile.json`, narrowed to `--table` names (bare or `schema.table`) and the
    joins that touch them. Enrichment reads this instead of sampling rows itself."""
    from .introspect import PROFILE_FILE
    path = Path(args.root) / ".introspect" / PROFILE_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _print_json({"error": f"no profile at {path}: run introspect first"})
        return 1
    # The file may predate a flag set since: cut it to what the model serves now before anyone reads it.
    from . import profile as PF
    from .introspect import _check_key, _never_read, served_join_keys
    org = L.load_datasource(args.root, include_rejected=True)
    model = [t for sa in org.subject_areas for t in sa.tables_defined]
    doc["tables"] = PF.scrub(doc.get("tables", {}), model, lambda t, c: _never_read(c))
    # A join's evidence only while the model serves that join, both its tables and its key columns;
    # an entry without its key columns (an older file) can't be matched, so it is left out.
    served = served_join_keys(org)
    doc["joins"] = [j for j in doc.get("joins", []) if j.get("from_columns") and _check_key(
        j.get("from_schema"), j["from_table"], j["from_columns"], j.get("to_schema"), j["to_table"],
        j["to_columns"]) in served]
    if args.table:
        wanted = {t.lower() for t in args.table}
        doc["tables"] = {k: v for k, v in doc.get("tables", {}).items()
                         if k.lower() in wanted or k.lower().rsplit(".", 1)[-1] in wanted}
        names = {k.lower().rsplit(".", 1)[-1] for k in doc["tables"]}
        doc["joins"] = [j for j in doc.get("joins", [])
                        if {j["from_table"].lower(), j["to_table"].lower()} & names]
    _print_json(doc)
    return 0


def cmd_choice_coverage(args) -> int:
    """Coded columns whose `choice_field` skeleton still has blank labels. The enrichment
    runs this to confirm the value-enum decode ran — `ok: false` means coded columns are
    missing their {code:label} maps (the generator can't translate 'high' → 1 without them)."""
    from . import curate
    org = L.load_datasource(args.root, include_rejected=True)
    _print_json(curate.unlabeled_choice_fields(org))
    return 0


def cmd_sensitive(args) -> int:
    """List the columns flagged `sensitive` (PII) that are still queryable — already-
    excluded ones (a rejected column, or any column under a rejected table) are NOT
    counted, since they're no longer in the runtime. The agami-connect Phase 4 curate gate
    uses this count to decide whether to open the explorer (so the gate stops re-opening
    once the user has excluded the flagged columns)."""
    from . import curate
    org = L.load_datasource(args.root, include_rejected=True)
    flagged = curate.sensitive_columns(org)
    # `count`/`columns` keep their meaning (flagged PII — the gate signal); `suspected` is the
    # second review tier (might-be-PII the strict flag missed, for the PII tab to confirm).
    if BS.warehouse_enforces_personal_data(Path(args.root).parent):
        # The warehouse controls it (#675): nothing to suspect. A flag still on a column was set before
        # the setting, possibly by a person, so it is still counted.
        _print_json({**flagged, "suspected": [], "suspected_count": 0,
                     "personal_data": BS.PERSONAL_DATA_ENFORCED})
        return 0
    suspected = curate.suspected_sensitive_columns(org)
    _print_json({**flagged, "suspected": suspected["columns"], "suspected_count": suspected["count"]})
    return 0


def cmd_set_personal_data(args) -> int:
    """Record who keeps people from seeing personal data (#675). Build-only: it lives in the local
    config, so nothing deployed carries it and an older core never meets the key."""
    from agami_paths import NotPrivate

    from .scratch import LinkedTarget
    try:
        BS.set_personal_data(args.artifacts_dir, args.setting)
    except (LinkedTarget, NotPrivate) as exc:
        _print_json({"error": str(exc)})
        return 2
    _print_json({"personal_data": args.setting, "config": str(BS.config_path(args.artifacts_dir))})
    return 0


def cmd_set_terminology(args) -> int:
    """Write the org-level domain glossary (term -> definition) onto datasource.yaml's
    `key_terminology` — the decoded-abbreviation legend enrichment produces. Merges by
    default (layers over a human's edits); --replace overwrites. Validated + committed."""
    from . import curate
    terms = _read_json_file(args.file)
    if isinstance(terms, dict) and "key_terminology" in terms:
        terms = terms["key_terminology"]
    res = curate.set_key_terminology(args.root, terms, merge=not args.replace)
    _print_json({"applied": res.applied, "validated": res.validated,
                 "committed": res.committed, **_commit_note(res), "errors": res.errors})
    return 0 if res.validated else 1


def _commit_note(res) -> dict:
    """`commit_note` only when there is one, the same shape `ApplyResult.as_dict` gives."""
    return {"commit_note": res.commit_note} if res.commit_note else {}


def _provenance_note(res) -> dict:
    """`provenance_changed` only when an op replaced a description's provenance (#549)."""
    return {"provenance_changed": res.provenance_changed} if res.provenance_changed else {}


def cmd_set_description(args) -> int:
    """Write the datasource's one-line description onto datasource.yaml — the line an agent routes a
    question by (#327). Onboarding calls this with the user's line or one generated from the enriched
    model. Validated + committed."""
    from . import curate
    description = _read_text_file(args.description_file) if args.description_file else args.description
    res = curate.set_datasource_description(args.root, description)
    _print_json({"applied": res.applied, "validated": res.validated,
                 "committed": res.committed, **_commit_note(res), "errors": res.errors})
    return 0 if res.validated else 1


def cmd_set_fiscal_year(args) -> int:
    """Record the month the fiscal year starts in, as the person answered it (ACE-182)."""
    from . import curate
    res = curate.set_fiscal_year_start(args.root, args.month)
    _print_json({"applied": res.applied, "validated": res.validated,
                 "committed": res.committed, **_commit_note(res), "errors": res.errors})
    return 0 if res.validated else 1


def _candidate_ops(doc: dict, wanted: list[str]) -> list[dict]:
    """The `add` ops of the named join candidates in a join_candidates.json (#512), or a JSON error."""
    listed = {c.get("id"): c.get("op") for key in ("candidates", "low_overlap")
              for c in doc.get(key) or [] if isinstance(c, dict)}
    if not wanted:
        _print_json({"error": "this is a join_candidates file; name the joins to add with --candidate <id>",
                     "candidates": [c.get("id") for c in doc.get("candidates") or []]})
        raise SystemExit(2)
    missing = [w for w in wanted if w not in listed]
    if missing:
        _print_json({"error": f"no join candidate {', '.join(missing)} in the file",
                     "candidates": sorted(i for i in listed if i)})
        raise SystemExit(2)
    return [listed[w] for w in wanted]


def cmd_curate(args) -> int:
    from . import curate
    ops = _read_json_file(args.ops_file or args.ops)
    if isinstance(ops, dict) and ("candidates" in ops or "low_overlap" in ops):
        ops = _candidate_ops(ops, args.candidate or [])
    elif args.candidate:
        _print_json({"error": "--candidate picks from a join_candidates.json; this file holds ops"})
        return 2
    if isinstance(ops, dict):
        ops = ops.get("ops", [])
    _stamp_approve_ops(ops)
    res = curate.apply(args.root, ops, signer=args.signer, role=args.role)
    _print_json(res.as_dict())
    return 0 if res.validated else 1


def cmd_approve_queue(args) -> int:
    """Sign off the whole pending review queue in one call — the first-class no-browser
    path (the HTML explorer's CLI equivalent). Reads the same queue `review-queue` builds
    (Rule 1 metrics/named-filters + Rule 2 joins/entities), turns each item into a
    self-stamped `approve` op, and applies via curate.apply. `--kind` narrows to one item
    type; `--dry-run` prints the ops without touching the model.

    Placeholder values are never approved here: what one means is a person's choice, so they are
    listed under `placeholders_awaiting_choice` for the person to answer (#445)."""
    from . import curate
    org = L.load_datasource(args.root)
    queue = curate.review_queue(org, root=args.root)
    waiting = queue.get("placeholders", [])
    held = ({"placeholders_awaiting_choice": [it["question"] for it in waiting],
             "placeholder_note": "not approved: ask what each value means and record it with a "
                                 "curate op {op: approve, kind: placeholder, name, column, value, "
                                 "meaning: unknown|current|not_set}, or reject it as real data"}
            if waiting else {})
    kinds = set(args.kind) if args.kind else None
    items = [it for bucket in ("rule_1", "rule_2") for it in queue.get(bucket, [])
             if kinds is None or it["kind"] in kinds]
    ops = [{"op": "approve", "kind": it["kind"], "area": it["area"],
            "name": it["name"], "at": _utc_now_iso(),
            **{k: it[k] for k in ("from_schema", "to_schema", "from_column", "to_column", "on")
               if it.get(k)}}
           for it in items]
    # What a re-approval of a stale item signs off: the changes made since its last approval (#537).
    changed = [{"kind": it["kind"], "area": it["area"], "name": it["name"],
                "signed_off_by": it.get("signed_off_by"), "signed_off_at": it.get("signed_off_at"),
                "changes": [c["text"] for c in it["changes_since_approval"]["changes"]]
                or [curate.NOT_RECORDED]}
               for it in items if it.get("changes_since_approval")]
    shown = {"changed_since_approval": changed} if changed else {}
    if args.dry_run:
        _print_json({"dry_run": True, "count": len(ops), "ops": ops, **shown, **held})
        return 0
    res = curate.apply(args.root, ops, signer=args.signer, role=args.role)
    _print_json({"approved": len(ops), **shown, **res.as_dict(), **held})
    return 0 if res.validated else 1


# A connection kind means every metric would fail the same way, so none is the binding's fault; a
# statement kind is the binding's fault, never a connection check.
_CONNECTION_KINDS = FK.CONNECTION_KINDS
_STATEMENT_KINDS = FK.STATEMENT_KINDS


def _connection_failed(runner, exc: Exception) -> bool:
    """Whether a failed test run means the database could not be reached, not that the SQL is
    wrong: the failure kind says so, or, when it says nothing, a trivial `SELECT 1` fails too."""
    kind = getattr(exc, "kind", None)
    if kind in _CONNECTION_KINDS:
        return True
    if kind in _STATEMENT_KINDS:
        return False
    try:
        runner("SELECT 1")
    except Exception:  # noqa: BLE001 — any failure of SELECT 1 is the connection's
        return True
    return False


def _metric_test_runs(root: str, profile: str, items: list) -> tuple[dict[str, dict], Optional[str]]:
    """Each metric's binding for this database, run once over zero rows: name → {ok} or {error}.
    The second value is the reason the database could not be reached, which stops the batch.

    A live run signed off two metrics whose SQL named a column the table doesn't have and compared
    a text code to a number; neither was caught, because nothing ran the SQL before the sign-off.
    `WHERE 1 = 0` reads no rows and still makes the database resolve every column and operator.
    A metric over several tables needs their join to run, so it is reported `not_test_run`.
    """
    from . import derived as DV
    from . import dialects as D
    from . import introspect as INTRO
    from .models import table_key
    from .validator import _tables_named
    org = L.load_datasource(root)
    if not org.storage_connections:
        return {}, None
    storage_type = org.storage_connections[0].storage_type
    dialect = D.get_dialect(storage_type)
    # Keyed by (schema, name): two same-named tables in different schemas stay apart.
    defs = list({table_key(t.name, t.schema_name): t for sa in org.subject_areas for t in sa.tables_defined}.items())
    runner = INTRO.make_execute_sql_runner(profile, building_the_model=True)
    batch = _metric_batch(items)
    idx = {**DV.metric_index(org), **batch} if batch else {}
    out: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name")
        binding = (it.get("bindings") or {}).get(storage_type)
        sources = it.get("source_tables") or []
        if not binding:
            out[name] = {"not_test_run": f"no binding for {storage_type}"}
            continue
        met = batch.get(name)
        if met is not None and (DV.is_derived(met) or DV.is_second_order(met)):
            # A `{base}` placeholder is model syntax, not SQL: sent as written, every database
            # refuses it as a syntax error. Run the SQL the metric resolves to at query time.
            try:
                binding = DV.resolve_metric_sql(met, storage_type, idx, org)
            except DV.DerivedError as e:
                out[name] = {"error": f"its SQL could not be built from its base metrics: {e}"}
                continue
            if not sources:
                sources = _base_sources(met, storage_type, idx)
        hits = _tables_named(defs, str(sources[0])) if len(sources) == 1 else []
        if len(sources) != 1 or not hits:
            out[name] = {"not_test_run": "reads more than one table, or a table the model doesn't have"}
            continue
        if len(hits) > 1:
            out[name] = {"not_test_run": f"{sources[0]} names a table in several schemas; qualify it with its schema"}
            continue
        src = dialect.qualified(hits[0].schema_name, hits[0].name)
        # The binding on lines of its own, so a trailing `-- comment` in it can't swallow the FROM.
        sql = f"SELECT\n{binding}\nAS v\nFROM {src}\nWHERE 1 = 0"
        try:
            runner(sql)
            out[name] = {"ok": True}
        except Exception as e:  # noqa: BLE001 — a failed binding is reported per metric, not raised
            reason = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
            if _connection_failed(runner, e):
                return {"connection": {"error": reason}}, reason
            out[name] = {"error": reason}
    return out, None


def _metric_batch(items: list) -> dict:
    """The structurally valid metrics of an add batch by name, so a derived one can be resolved
    against a base written alongside it; one that does not parse is the validator's to report."""
    from .models import Metric
    out: dict = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            met = Metric.model_validate({k: v for k, v in it.items() if k not in ("area", "qname")})
        except Exception:  # noqa: BLE001 — structural problems are reported by the write
            continue
        out[met.name] = met
    return out


def _base_sources(met, storage_type: str, idx: dict) -> list:
    """The tables a derived metric reads through its bases, when it names none itself."""
    from . import derived as DV
    names = DV.binding_refs((met.bindings or {}).get(storage_type))
    tables = [t for n in names if n in idx for t in (idx[n].source_tables or [])]
    return list(dict.fromkeys(tables))


def cmd_add(args) -> int:
    from . import curate
    items = _read_json_file(args.file)
    if isinstance(items, dict):
        items = items.get(args.kind + "s", items.get("items", []))
    test_runs = None
    if args.kind == "metric" and getattr(args, "profile", None):
        test_runs, unreachable = _metric_test_runs(args.root, args.profile, items)
        if unreachable:
            # Not the metrics' fault: say the database could not be reached, and write nothing,
            # since none of the bindings was actually tested.
            _print_json({"applied": [], "validated": False, "test_run": test_runs,
                         "errors": [f"could not reach the database to test the metrics' SQL ({unreachable}); "
                                    "nothing was written. Check the connection and add again"]})
            return 1
        failed = {n for n, r in test_runs.items() if "error" in r}
        if failed:
            # Nothing is written while a binding fails: a metric whose SQL doesn't run would
            # otherwise reach sign-off looking like any other.
            _print_json({"applied": [], "validated": False, "test_run": test_runs,
                         "errors": [f"metric {n}: its SQL failed on the database; fix the binding and add again"
                                    for n in sorted(failed)]})
            return 1
    res = curate.write_items(args.root, args.area, args.kind, items,
                             signer=args.signer, role=args.role)
    out = res.as_dict()
    if test_runs is not None:
        out["test_run"] = test_runs
    _print_json(out)
    return 0 if res.validated else 1


def cmd_apply_spec(args) -> int:
    from . import model_spec
    spec = _read_json_file(args.file)
    if not isinstance(spec, dict):
        _print_json({"error": "a model spec is a JSON object with subject_areas, tables and joins"})
        return 2
    res = model_spec.apply_spec(args.root, spec,
                                signer=args.signer, role=args.role, dry_run=args.dry_run)
    _print_json(res.as_dict())
    return 0 if not res.errors else 1


def cmd_add_example(args) -> int:
    from . import curate
    items = _read_json_file(args.file)
    if isinstance(items, dict):
        items = items.get("examples", items.get("items", []))
    res = curate.add_examples(args.root, args.area, items, signer=args.signer, role=args.role)
    _print_json(_with_refresh(res.as_dict(), Path(args.root)))
    return 0 if (res.applied or not res.skipped) else 1


def cmd_remove_example(args) -> int:
    """Reject prompt example(s) by question — `status: rejected`, kept in examples.yaml for
    audit but dropped from the runtime ranker. The packaged path for the dashboard's
    `reject N`, so a skill never hand-rewrites examples.yaml to drop an example."""
    from . import curate
    from .loader import list_prompt_examples
    res = curate.remove_examples(args.root, args.area, args.question,
                                 signer=args.signer, role=args.role)
    prefix = f"example (rejected) {args.area}/"
    done = {a[len(prefix):] for a in res.applied}
    # An unquoted `id: 0` is an authored id; only a missing or empty one is absent (as write_examples).
    examples = [{"area": args.area, "question": e["question"],
                 **({"id": e["id"]} if e.get("id") is not None and str(e["id"]) != "" else {}),
                 "status": "rejected"}
                for e in list_prompt_examples(args.root, args.area, include_rejected=True)
                if (e or {}).get("question") in done]
    note = ({"note": f"marked rejected (kept for audit): {len(examples)} example(s) stay in "
                     f"prompt_examples/{args.area}/examples.yaml with status: rejected, not deleted, "
                     "and are no longer offered as examples for a question"} if examples else {})
    # `rejected` keeps its old shape (one label per example) for callers that parse it; the
    # detail is added beside it.
    _print_json({"rejected": res.applied, "examples": examples, **note, "skipped": res.skipped,
                 "validated": res.validated, "committed": res.committed, **_commit_note(res)})
    return 0 if (res.applied or not res.skipped) else 1


def cmd_open_questions(args) -> int:
    """The questions the data can't settle, for the build's "ask me as you go" step (#486): candidate
    ambiguities found from patterns in the model and kept only when running both readings gives
    different answers, then coded values and columns the build couldn't read. Read-only: each probe
    is a bounded aggregate through `execute_sql`'s gates, and nothing is declared; each candidate
    carries the `curate` op a person's answer completes."""
    from . import curate
    from . import dialects as D
    from . import introspect as INTRO
    from . import open_questions as OQ
    org = L.load_datasource(args.root)
    if not org.storage_connections:
        _print_json({"error": "the model declares no storage connection, so there is nothing to probe"})
        return 2
    dialect = D.get_dialect(org.storage_connections[0].storage_type)
    # `--all` is the review page's list (#676): every item, the build's guesses, sample values.
    confirmed = OQ.confirmed_code_lists(curate.read_log(args.root)) if args.all else frozenset()
    _print_json(OQ.open_questions(org, INTRO.make_execute_sql_runner(args.profile), dialect,
                                  page=args.all, confirmed=confirmed))
    return 0


def cmd_suggest_units(args) -> int:
    """List the numeric **money** columns (so a currency `unit` can be stamped) using the
    tested name matcher — never a hand-rolled regex that mis-handles `count` inside
    `discount`. Skips columns that already carry a `unit`. Emits
    {money_columns: [{area, table, column, type, label, suggested_unit}]}, where `suggested_unit`
    is the unit the name spells out (`revenue_usd_millions` -> `USD_millions`) or null, for
    the caller to show, confirm and apply, and `label` is the column in plain words for a card
    ("Price on Widgets")."""
    from . import build as B
    org = L.load_datasource(args.root)
    numeric = {"integer", "decimal", "float"}
    money = []
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            for c in t.columns:
                if not c.unit and c.type in numeric and B.detect_money_column(c.name):
                    money.append({"area": sa.name, "table": t.name, "column": c.name, "type": c.type,
                                  "label": column_label(t.name, c.name),
                                  "suggested_unit": B.suggest_unit(c.name)})
    _print_json({"money_columns": distinct_labels(money)})
    return 0


def cmd_set_units(args) -> int:
    """The APPLY half of suggest-units: stamp a currency/unit on detected money columns in
    ONE validated curate batch — so a skill never pipes suggest-units JSON through a hand-rolled
    script (the fragile glue that broke on empty stdin). Detects the same money columns (tested
    matcher); a detected column whose name states its currency gets that unit, the rest get
    `--currency`/`--unit`. Detection only PLANS: it writes nothing until `--yes`, so a guess is
    always reviewed. `--columns` names the columns outright (applying the reviewed plan to just
    those): each keeps the unit its name states, the rest get the given unit; `--area` scopes it."""
    from . import build as B
    from . import curate
    unit = args.currency or args.unit
    explicit = set(args.columns or [])
    org = L.load_datasource(args.root)
    numeric = {"integer", "decimal", "float"}
    plan = []
    for sa in org.subject_areas:
        if args.area and sa.name != args.area:
            continue
        for t in sa.tables_defined:
            for c in t.columns:
                if c.unit:
                    continue
                if explicit:
                    if f"{t.name}.{c.name}" not in explicit and c.name not in explicit:
                        continue
                    col_unit = B.suggest_unit(c.name) or unit
                elif not (c.type in numeric and B.detect_money_column(c.name)):
                    continue
                else:
                    col_unit = B.suggest_unit(c.name) or unit
                plan.append({"area": sa.name, "table": t.name, "column": c.name,
                             "label": column_label(t.name, c.name), "unit": col_unit})
    distinct_labels(plan)
    if not plan:
        _print_json({"set": 0, "unit": unit, "reason": "no matching money columns"})
        return 0
    missing = [f"{p['table']}.{p['column']}" for p in plan if not p["unit"]]
    if missing:
        _print_json({"set": 0, "error": "pass --currency <ISO> or --unit <name>: these names "
                     "state no currency", "columns": missing})
        return 1
    if not (explicit or args.yes):
        _print_json({"set": 0, "planned": plan,
                     "next": "nothing written: show this plan, then re-run with --yes to apply "
                             "it, or with the same --currency/--unit plus --columns <table.col …> to apply "
                             "only some (each keeps the unit planned for it)"})
        return 0
    ops = [{"op": "edit", "kind": "table", "area": p["area"], "name": p["table"],
            "column": p["column"], "field": "unit", "value": p["unit"]} for p in plan]
    res = curate.apply(args.root, ops)
    _print_json({"set": len(res.applied), "unit": unit,
                 "units": {f"{p['table']}.{p['column']}": p["unit"] for p in plan},
                 "errors": res.errors})
    return 0 if res.validated else 1


# A test run reads at most this many rows of a table: enough to see a constant or broken binding,
# bounded so it is never a full scan of a large table.
_TEST_RUN_ROWS = 10000


def _num(v) -> Optional[float]:
    try:
        return None if v is None or str(v).strip() == "" else float(v)
    except ValueError:
        return None


def _test_run_sql(table, dialect, props: list[dict]) -> str:
    """One bounded statement that evaluates every proposal on `table`: each one's own binding with
    the MIN and MAX of the per-row value it aggregates, over a sample of the columns they read.
    The binding, never a re-derived AVG: on a table whose rows repeat each record it counts
    distinct records, and an AVG over the rows would preview a different number than it writes."""
    cols = sorted({c for p in props for c in p["_proposal"]["columns"]})
    if table.source_type == "sql":
        # Newlines keep a trailing `--` comment in the table's SQL from swallowing the `)`.
        src = f"(\n{table.sql.strip().rstrip(';').rstrip()}\n) src"
    else:
        src = dialect.qualified(table.schema_name, table.name)
    inner = dialect.limited(f"SELECT {', '.join(dialect.ident(c) for c in cols)} FROM {src}",
                            _TEST_RUN_ROWS)
    parts = []
    for i, p in enumerate(props):
        v = p["_proposal"]["row_expr"]
        binding = next(iter(p["bindings"].values()))
        parts += [f"{binding} AS m{i}", f"MIN({v}) AS m{i}_min", f"MAX({v}) AS m{i}_max"]
    return f"SELECT {', '.join(parts)}, COUNT(*) AS n_rows FROM ({inner}) sample_rows"


def _test_run_table(runner, table, dialect, props: list[dict]) -> dict[str, dict]:
    """name → {value, min, max, rows} or {error}. One statement for the table; only when it fails
    does each proposal run on its own, so one broken binding does not hide the others' results."""
    def run(batch: list[dict]) -> dict[str, dict]:
        row = (runner(_test_run_sql(table, dialect, batch)) or [{}])[0]
        return {p["name"]: {"value": _num(row.get(f"m{i}")), "min": _num(row.get(f"m{i}_min")),
                            "max": _num(row.get(f"m{i}_max")), "rows": _num(row.get("n_rows"))}
                for i, p in enumerate(batch)}
    try:
        return run(props)
    except Exception as e:  # noqa: BLE001 — a failed read is reported per proposal, not raised
        if len(props) == 1:
            return {props[0]["name"]: {"error": str(e)}}
    out: dict[str, dict] = {}
    for p in props:
        out.update(_test_run_table(runner, table, dialect, [p]))
    return out


def _test_run_flags(table_name: str, result: dict) -> list[dict]:
    """Flags a successful test run raises: a degenerate result, or too few rows to trust it."""
    from . import build as B
    flags = []
    if result["value"] is None:
        flags.append(B._flag("degenerate", "no row gives a value (the result is NULL)"))
    elif result["min"] is not None and result["min"] == result["max"]:
        flags.append(B._flag("degenerate", f"every row gives {result['min']:g}, so the result is "
                             "a constant (a constant or all-zero column)"))
    rows = result["rows"]
    if rows is not None and rows < B.TINY_TABLE_ROWS:
        flags.append(B._flag("low_confidence", f"{table_name} has {rows:g} rows "
                             f"(under {B.TINY_TABLE_ROWS})"))
    return flags


# How many flag columns the flag_rates summary lists by name; the rest are counted.
_LISTED_FLAG_COLUMNS = 20


def _parse_flag_rates(values: Optional[list[str]]) -> tuple[bool, set[tuple[str, str]], list[str]]:
    """(all, {(table, column)} lowercased, errors) from `--flag-rates` values, each `all` or a
    comma-separated list of [schema.]table.column."""
    every, wanted, errors = False, set(), []
    for tok in (x.strip() for v in values or () for x in v.split(",")):
        if not tok:
            continue
        if tok.lower() == "all":
            every = True
        elif "." in tok and all(tok.split(".")):
            # The column is after the last dot: what precedes it is the table, schema-qualified
            # or a table name that itself holds a dot.
            table, _, column = tok.lower().rpartition(".")
            wanted.add((table, column))
        else:
            errors.append(f"--flag-rates {tok}: name a column as table.column "
                          "(or schema.table.column), or pass all")
    return every, wanted, errors


def _qualified_table(t) -> str:
    return f"{t.schema_name}.{t.name}" if t.schema_name else t.name


def _flag_rate_problem(name: str, c) -> Optional[str]:
    """Why a `--flag-rates` column can get no proposal (un-described or not a flag), else None."""
    from . import build as B
    if c.description_source == "ai_unknown":
        return (f"--flag-rates {name}: the column is un-described (ai_unknown); describe it, "
                "then re-run")
    if c.primary_key or not (c.type == "boolean" or (c.type == "integer"
                                                     and B._FLAG_NAME_RE.match(c.name))):
        return f"--flag-rates {name}: not a boolean or flag column (type {c.type})"
    return None


def _resolve_flag_rates(areas: list, wanted: set[tuple[str, str]], area: Optional[str]
                        ) -> tuple[dict, list[str], dict[str, list[str]]]:
    """Each `--flag-rates` selector's column: ({(area, table, schema): {column}} lowercased,
    errors, {selector: matches} for a selector naming more than one table's column). A selector's
    table is the table name or schema.name."""
    hits: dict = {sel: [] for sel in wanted}
    for sa in areas:
        for t in sa.tables_defined:
            keys = {t.name.lower(), _qualified_table(t).lower()}
            for sel in wanted:
                if sel[0] in keys:
                    hits[sel] += [(sa.name, t, c) for c in t.columns if c.name.lower() == sel[1]]
    asked: dict = {}
    errors: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    for sel in sorted(wanted):
        name = ".".join(sel)
        found = hits[sel]
        if len(found) > 1:
            ambiguous[name] = sorted(f"{a}: {_qualified_table(t)}.{c.name}" for a, t, c in found)
        elif not found:
            errors.append(f"--flag-rates {name}: no such column in the model"
                          + (f" (in --area {area})" if area else ""))
        elif problem := _flag_rate_problem(name, found[0][2]):
            errors.append(problem)
        else:
            a, t, c = found[0]
            asked.setdefault((a, t.name, t.schema_name), set()).add(c.name.lower())
    return asked, errors, ambiguous


def _flag_rate_summary(columns: list[tuple[str, str]]) -> dict:
    """One entry for the flag columns given no rate, in place of a proposal for each (#530).
    `columns` are (`table.column`, plain label) pairs: the name is what `--flag-rates` takes,
    the label what a card says ("In stock on Widgets")."""
    listed = columns[:_LISTED_FLAG_COLUMNS]
    out: dict = {"available": len(columns), "columns": [n for n, _ in listed],
                 "labels": [lbl for _, lbl in listed]}
    if len(columns) > _LISTED_FLAG_COLUMNS:
        out["more"] = len(columns) - _LISTED_FLAG_COLUMNS
    if columns:
        out["hint"] = (f"{len(columns)} yes/no column(s) could each have a metric for the share of "
                       "rows where it is true; add one with --flag-rates <table.column>[,...] or "
                       "--flag-rates all")
    return out


def cmd_suggest_metrics(args) -> int:
    """Infer a sensible per-table metric set — start→end durations, never a plain COUNT/SUM/AVG
    (#406) — and PRINT them; `--write` saves them proposed/unreviewed for bulk
    sign-off in the explorer. An unsigned metric is still used, and the answer warns that it is
    unreviewed (Rule 1), so nothing is written until a person has seen the list (#514).

    `--profile` test-runs every binding first (one bounded read per table, opt-in because a billed
    warehouse charges for it): one that errors is never written, and a degenerate result (constant,
    all zero or NULL) is flagged. A flagged proposal — degenerate, a tiny table, an ambiguous or
    audit-column pairing — is written only when `--names` names it.

    A share-of-true rate per boolean column is proposed only for the columns `--flag-rates` names
    (or every one with `--flag-rates all`): proposed for each by default, they were nearly all the
    suggested metrics on a large onboarding while the business defined one (#530). The output's
    `flag_rates` counts the columns left out instead. `--cite` records where a requested rate is
    defined in its description."""
    from . import build as B
    from . import curate
    from . import dialects as D
    flag_all, flag_wanted, flag_errors = _parse_flag_rates(args.flag_rates)
    cite = (args.cite or "").strip().rstrip(".").strip()
    if cite and not (flag_all or flag_wanted):
        _print_json({"error": "--cite records where a requested flag rate is defined; pass it "
                              "with --flag-rates <table.column>"})
        return 2
    org = L.load_datasource(args.root)
    if not org.storage_connections:
        _print_json({"error": "the model declares no storage connection, so there is no dialect "
                              "to write bindings in"})
        return 2
    conn_type = {sc.name: sc.storage_type for sc in org.storage_connections}
    default_type = org.storage_connections[0].storage_type
    dialects: dict = {}
    for st in set(conn_type.values()):
        try:
            dialects[st] = D.get_dialect(st)
        except ValueError as e:
            _print_json({"error": f"no SQL dialect for storage type {st!r}: {e}"})
            return 2

    areas = [sa for sa in org.subject_areas if not args.area or sa.name == args.area]
    flag_asked, flag_problems, ambiguous = _resolve_flag_rates(areas, flag_wanted, args.area)
    if ambiguous:
        # Refused before anything runs, so a rate is never written to the wrong table as well.
        _print_json({"error": "a --flag-rates name matches more than one column; qualify the "
                              "table as schema.table.column, or pass --area",
                     "ambiguous": ambiguous})
        return 2
    # A name in more than one table is listed schema-qualified, so the summary's names select.
    name_count: dict[str, int] = {}
    for sa in areas:
        for t in sa.tables_defined:
            name_count[t.name.lower()] = name_count.get(t.name.lower(), 0) + 1

    # Every metric name on disk, rejected ones included. `org` drops rejected entries, which is
    # right for choosing what to propose on (never a rejected table or column), but `write_items`
    # overwrites a file of the same name — so checking against `org`'s metrics alone re-proposed a
    # metric a person had rejected and reset it to unreviewed, undoing the rejection.
    on_disk = {sa.name: {m.name for m in sa.metrics}
               for sa in L.load_datasource(args.root, include_rejected=True).subject_areas}
    runner = None
    if args.profile:
        from . import introspect as INTRO
        runner = INTRO.make_execute_sql_runner(args.profile, building_the_model=True)
    named = set(args.names or [])

    proposals: list[dict] = []
    written = skipped_opaque = 0
    errors: list[str] = flag_errors + flag_problems
    flag_available: list[tuple[str, str]] = []
    flag_present: list[str] = []
    for sa in areas:
        existing = set(on_disk.get(sa.name, ()))
        items: list[dict] = []
        for t in sa.tables_defined:
            # columns agami couldn't read yield no metric until described — count them so the
            # user knows describing the "couldn't read" pile unlocks more metrics on a re-run.
            # A start or end timestamp would have been half of a duration; a flag a rate, when
            # every flag's rate was asked for.
            skipped_opaque += sum(
                1 for c in t.columns
                if c.description_source == "ai_unknown" and not c.primary_key
                and ((flag_all and (c.type == "boolean" or (c.type == "integer"
                                                             and B._FLAG_NAME_RE.match(c.name))))
                     or (c.type in B._TS_TYPES and (B._START_NAME_RE.search(c.name)
                                                    or B._END_NAME_RE.search(c.name)))))
            asked_here = flag_asked.get((sa.name, t.name, t.schema_name), set())
            asked = [c for c in B.flag_columns(t) if flag_all or c.name.lower() in asked_here]
            label = _qualified_table(t) if name_count[t.name.lower()] > 1 else t.name
            rate_names = {B.flag_rate_name(t.name, c.name) for c in asked}
            if not flag_all:
                flag_present += sorted(n for n in rate_names if n in existing)
            flag_available += [(f"{label}.{c.name}", column_label(t.name, c.name))
                               for c in B.flag_columns(t)
                               if B.flag_rate_name(t.name, c.name) not in rate_names | existing]
            dialect = dialects[conn_type.get(t.storage_connection, default_type)]
            props = [m for m in B.suggest_metrics(t, dialect, max_per_table=args.max_per_table,
                                                  flag_rates=[c.name for c in asked])
                     if m["name"] not in existing and (not named or m["name"] in named)]
            existing.update(m["name"] for m in props)
            results = _test_run_table(runner, t, dialect, props) if runner and props else {}
            for met in props:
                extra = met.pop("_proposal")
                entry = {"name": met["name"], "subject_area": sa.name, "source_table": t.name,
                         "binding": next(iter(met["bindings"].values())),
                         "calculation": met["calculation"], "flags": extra["flags"]}
                if cite and met["name"] in rate_names:
                    met["description"] = f"Defined in: {cite}."
                    entry["description"] = met["description"]
                res = results.get(met["name"])
                if res and "error" in res and t.source_type == "sql":
                    # The table's own SQL may be what failed, so the binding is unproven rather
                    # than broken: kept out unless named, like any other flagged proposal.
                    entry["test_run"] = None
                    entry["flags"].append(B._flag(
                        "not_test_run", "defined by SQL; its bindings could not be test-run "
                        f"({res['error']})"))
                elif res and "error" in res:
                    entry["error"] = res["error"]
                elif res:
                    entry["test_run"] = res
                    if res["rows"] is not None:
                        # The live count replaces the stored estimate's low_confidence.
                        entry["flags"] = [f for f in entry["flags"] if f["flag"] != "low_confidence"]
                    entry["flags"] += _test_run_flags(t.name, res)
                entry["write"] = "error" not in entry and (not entry["flags"]
                                                           or met["name"] in named)
                proposals.append(entry)
                if entry["write"]:
                    items.append(met)
        if args.write and items:
            res = curate.write_items(args.root, sa.name, "metric", items)
            written += len(res.applied)
            errors += res.errors
    if args.write:
        note = ("metrics written — review & sign off in the explorer (/agami-model); "
                "give each one you keep a one-line description")
    else:
        note = ("dry run: nothing written — show these to the user, then re-run with --write "
                "(add --names <metric> … to keep only some, or to keep a flagged one)")
    if not runner:
        note += (". Bindings were NOT test-run against the database — pass --profile <name> to run "
                 "each one first (one bounded read per table; billed on a pay-per-scan warehouse)")
    if skipped_opaque:
        note += (f". {skipped_opaque} column(s) skipped as un-described (ai_unknown) — describe "
                 "them, then re-run suggest-metrics to pick up their metrics (incremental, no dupes)")
    out = {"dry_run": not args.write, "test_run": runner is not None,
           "suggested": len(proposals), "would_write": sum(p["write"] for p in proposals),
           "written": written, "skipped_opaque": skipped_opaque, "errors": errors,
           "proposals": proposals, "flag_rates": _flag_rate_summary(flag_available),
           "note": note}
    if flag_present:
        # Asked for by name but kept as it is, so a --cite run says why nothing changed.
        out["flag_rates"]["already_in_model"] = flag_present
    unknown = sorted(named - {p["name"] for p in proposals})
    if unknown:
        out["unknown_names"] = unknown
    _print_json(out)
    return 0 if not errors else 1


def cmd_describe_file(args) -> int:
    """Apply many column descriptions from a file as ONE validated curate batch — a TSV
    (`<table.column>` or `<area.table.column>`, a TAB, the description), or a CSV / JSON data
    dictionary. So a skill emits a flat list (cheap, auditable) instead of authoring a Python
    generator script to build ops. `--source ai` (default) → ai_unvalidated (earns trust through
    use); `--source metadata` → metadata, the customer's own dictionary text."""
    from . import curate
    from . import dictionary as DD
    if args.file:
        p = Path(args.file).expanduser()
        try:
            text = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            _print_json({"error": f"file not found: {p.resolve()}"})
            return 2
        except (OSError, ValueError) as exc:  # a directory, no permission, not UTF-8
            _print_json({"error": f"could not read {p.resolve()}: {exc}"})
            return 2
    else:
        text = sys.stdin.read()
    fmt = DD.format_for(args.file, args.format)
    try:
        rows = DD.parse(text, fmt)
    except ValueError as exc:  # bad JSON or CSV quoting, a CSV without the needed header
        _print_json({"error": f"could not read the {fmt.upper()} file: {exc}"})
        return 2
    root = Path(args.root).expanduser()
    if not (root / "datasource.yaml").is_file() or not (root / "subject_areas").is_dir():
        # otherwise every row comes back no_table with exit 0, as if the dictionary were the problem
        _print_json({"error": f"no semantic model at {root.resolve()} (needs datasource.yaml and "
                              "subject_areas/)"})
        return 2
    m = DD.match(root, rows, source=args.source)
    report = {"unmatched": m["unmatched"], "kept_human": m["kept_human"],
              "case_corrected": m["case_corrected"],
              "schema_prefix_stripped": m["schema_prefix_stripped"]}
    if not m["ops"]:
        _print_json({"described": 0, "applied": [], "applied_count": 0,
                     "reason": "no row matched a column of the model", **report,
                     "skipped": [], "errors": []})
        return 0
    res = curate.apply(root, m["ops"])
    # `skipped` carries why an edit didn't land — e.g. a person's or a dictionary's description kept
    # over a generated one — so the skill can say so instead of counting it as described.
    # `applied` / `applied_count` / `validated` are `sm curate`'s own keys (#678): a reader that
    # checked `applied` here found nothing and took a successful write for a no-op.
    _print_json({"described": len(res.applied), "applied": res.applied,
                 "applied_count": len(res.applied), "validated": res.validated, **report,
                 "skipped": res.skipped, **_provenance_note(res), "errors": res.errors})
    return 0 if res.validated else 1


def cmd_dictionary_joins(args) -> int:
    """Turn the references the applied data dictionary states (`FK -> parent.pkey`) into join
    candidates, each checked once on the data (#694). With `--unjoined`, only list the columns the
    dictionary calls references that no join reads from yet: no database read."""
    from . import dialects as D
    from . import dictionary_joins as DJ
    from .loader import load_datasource

    root = Path(args.root).expanduser()
    if not (root / "datasource.yaml").is_file() or not (root / "subject_areas").is_dir():
        _print_json({"error": f"no semantic model at {root.resolve()} (needs datasource.yaml and subject_areas/)"})
        return 2
    org = load_datasource(root)
    dialect = D.get_dialect(args.db_type) if args.db_type else None
    sg = INTRO._sqlglot_name(dialect) if dialect else None
    if args.unjoined:
        cols = DJ.unjoined(org, sg)
        _print_json({"unjoined": cols, "unjoined_count": len(cols)})
        return 0
    if not (args.profile and dialect):
        _print_json({"error": "checking the stated joins reads the database: pass --profile and --db-type "
                              "(or --unjoined to only list them)"})
        return 2
    runner = INTRO.make_execute_sql_runner(args.profile, building_the_model=True)
    from . import profile as PF
    PF.WORKERS = 1 if _may_prompt_to_sign_in(args.profile) else PF.WORKERS
    found = DJ.find(org, runner, dialect, sg)
    path = DJ.store(root, found)
    _print_json({"candidates": found.candidates, "candidate_count": len(found.candidates),
                 "not_proven": found.notes, "unresolved": found.unresolved,
                 "already_joined": found.already_joined, "probes": found.probes,
                 "join_candidates_file": str(path) if path else None})
    return 0


def cmd_format_table(args) -> int:
    """Format a result CSV into a deterministic markdown table — exact numbers, full
    grouping + currency symbols, never abbreviated. The skill (and later the MCP) emits
    this verbatim so verification numbers don't depend on the LLM's formatting."""
    import csv as _csv

    from . import units
    text = Path(args.csv_file).read_text(encoding="utf-8") if args.csv_file else sys.stdin.read()
    reader = list(_csv.reader(io.StringIO(text)))
    if not reader:
        print("")
        return 0
    headers, rows = reader[0], reader[1:]
    unit_map = json.loads(args.units) if args.units else {}
    labels = json.loads(args.labels) if args.labels else []
    print(units.format_table(headers, rows, unit_map, labels))
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
    """Enrichment-completeness gate: refuse to seed (or finish) a model whose column pass
    didn't finish. Two failure modes (see curate.column_coverage):
      - `columns_unenriched` — a table with NO column descriptions at all (the pass never ran).
      - `columns_underenriched` — a table the pass touched but which still has a wall of blank
        columns with non-self-evident names (skipped meaningful columns like `bptype`).
    Naked columns degrade the explorer AND every NL→SQL answer (descriptions are the
    generator's context). NOT bypassable: the fix is to finish the column pass."""
    from . import curate
    cov = curate.column_coverage(org)
    if cov["ok"]:
        return None
    unenr = cov["unenriched_tables"]
    if unenr:
        return {
            "refused": "columns_unenriched",
            "table_count": len(unenr),
            "unenriched_tables": unenr,
            "coverage_pct": cov["totals"]["coverage_pct"],
            "message": (
                f"{len(unenr)} table(s) have NO column descriptions at all — enrichment wrote the "
                f"table descriptions but skipped the column pass. Run Phase 2's column pass: "
                f"describe each meaningful column from sampled values, mark genuinely-opaque ones "
                f"description_source=ai_unknown (self-evident id/timestamps may stay blank), then "
                f"re-run. Tables: {', '.join(unenr[:10])}" + ("…" if len(unenr) > 10 else "")
            ),
        }
    under = cov["under_enriched_tables"]
    under_set = set(under)   # compute once, not per row
    skipped = {r["table"]: r["blank_meaningful_columns"]
               for r in cov["tables"] if r["table"] in under_set}
    return {
        "refused": "columns_underenriched",
        "table_count": len(under),
        "under_enriched_tables": under,
        "meaningful_blank": cov["totals"]["meaningful_blank"],
        "coverage_pct": cov["totals"]["coverage_pct"],
        "skipped_columns": skipped,
        "message": (
            f"{len(under)} table(s) have many MEANINGFUL columns left blank — the column pass "
            f"described the easy ones and stopped ({cov['totals']['meaningful_blank']} non-self-evident "
            f"blanks total). Describe them from sampled values (or mark genuinely-opaque ones "
            f"description_source=ai_unknown), then re-run. e.g. "
            + "; ".join(f"{t}: {', '.join(cols[:5])}" for t, cols in list(skipped.items())[:3])
        ),
    }


def cmd_seed_examples(args) -> int:
    """Validate candidate seed examples against the live DB and write the passing ones —
    the whole Phase-5 mechanical loop in one call (no throwaway validate-and-write script)."""
    from . import curate
    from .introspect import make_execute_sql_runner
    org = L.load_datasource(args.root, include_rejected=True)
    # Gate 1 — enrichment completeness (NOT bypassable): every kept column must be described
    # or explicitly ai_unknown. Catches a model that enriched tables but skipped columns.
    block = _coverage_gate(org)
    if block is not None:
        _print_json(block)
        return 2
    # Gate 2 — preseed review (Phase 4): no seeding on unreviewed metrics/entities. Bypassable
    # only via the Phase-4c --after-review path, after the user has been in the explorer.
    if not getattr(args, "after_review", False):
        block = _preseed_gate(L.load_datasource(args.root))   # the served model's own list
        if block is not None:
            _print_json(block)
            return 2
    # The every-area form hands each area its own seeds; reading them back from a temp file would
    # be refused under the scratch guard.
    cands = getattr(args, "seeds", None)
    if cands is None:
        cands = _read_json_file(args.file)
    if isinstance(cands, dict):
        cands = cands.get("examples", cands.get("items", []))
    runner = make_execute_sql_runner(args.profile)
    passing, rejected = curate.validate_seeds(cands, runner)
    res = curate.add_examples(args.root, args.area, passing) if passing else curate.ApplyResult()
    # A seed the writer refused (an unknown metric tag) is rejected like one the database refused.
    rejected += [{"question": s["item"], "error": s["reason"]} for s in res.skipped]
    _print_json({"added": res.applied, "written": res.validated,
                 "committed": res.committed, **_commit_note(res), "rejected": rejected})
    return 0


def cmd_seed_validate(args) -> int:
    """Phase-6 trust onboarding: run every written seed against the live DB and emit the
    examples-validation items. Each seed runs THROUGH execute_sql.py (the agami-query path) so the
    scope gates always apply and every seed carries the same receipt a caller's own query would — a
    raw driver could skip both. The gates here are table scope, the `SELECT *` ban and column scope;
    the fan/chasm pre-flight is a report, not a gate, and there is no PII gate, which ACE-094
    deleted. A refused/errored seed is surfaced with its `error`, not faked. Replaces ad-hoc 'run
    all the seeds' scripts."""
    import csv as _csv
    import os
    import subprocess

    from . import units

    seeds = L.list_prompt_examples(args.root, args.area)
    exe = sys.executable
    script = str(Path(__file__).resolve().parent.parent / "execute_sql.py")
    # the safety pass inside execute_sql.py finds the model via AGAMI_ARTIFACTS_DIR —
    # point it at the profile dir's parent so the right model loads (fan/chasm + filters).
    # The child's streams are decoded as UTF-8 below, from its very first byte. PYTHONIOENCODING too,
    # because an inherited one takes precedence over UTF-8 mode for the standard streams.
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(Path(args.root).resolve().parent),
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    cap = max(0, args.preview)
    # Load the model once so result numbers are formatted by the SAME units.py the query
    # path uses (currency symbol + grouping). The validation preview must MATCH the real
    # answer — a column with unit: INR shows ₹ here too, not a bare number. If the model
    # can't load, fall back to raw cells (no regression).
    try:
        fmt_org = L.load_datasource(args.root)
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
            capture_output=True, text=True, encoding="utf-8", env=env,
        )
        if proc.returncode != 0:
            # A SQL error or a scope refusal — surface it; never fake a result. NOT a fan or chasm
            # trap: those are reported on the receipt of an answer that ran, and exit 0 with them.
            item["error"] = (proc.stderr or "").strip()[:600] or f"execute_sql exit {proc.returncode}"
        else:
            rows = list(_csv.reader(io.StringIO(proc.stdout)))
            if rows:
                headers = rows[0]
                data = rows[1:]
                # trace each output column → unit through the seed's SQL, then format every
                # cell exactly like the query path (units.format_table): a unit'd column gets
                # its symbol/grouping, a year or a code is shown as written, a bare number gets
                # grouping; non-numbers pass through.
                unit_map, labels = {}, set()
                if fmt_org is not None:
                    try:
                        unit_map = RT.resolve_result_units(fmt_org, sql)
                        labels = RT.resolve_result_labels(fmt_org, sql)
                    except Exception:
                        unit_map, labels = {}, set()
                if data:
                    data = units.format_rows(headers, data, unit_map, labels)
                item["row_headers"] = headers
                item["row_count"] = len(data)
                item["row_preview"] = data[:cap]
        items.append(item)
    _print_json(items)
    return 0


# --- several subject areas per call (#544) -------------------------------------------------
# One `--area` runs the single-area command above untouched, so its output stays what skills
# parse. Several `--area`, or none (every area), run that same command once per area and fold
# the results into one shape: per-area entries labelled with the area, plus totals.

def _resolve_areas(args) -> Optional[list[str]]:
    """The areas this call covers, in order: the `--area` names, or every area in the model.
    Prints an `unknown_area` error and returns None when a named area isn't in the model."""
    named = list(dict.fromkeys(args.area or []))
    try:
        valid = [sa.name for sa in L.load_datasource(args.root, include_rejected=True).subject_areas]
    except Exception:
        # A model that won't load (a broken or missing area file) can't list its areas; one named
        # area still runs as before. No datasource.yaml at all means there is no model.
        if len(named) == 1 and (Path(args.root) / "datasource.yaml").exists():
            return named
        raise
    unknown = [a for a in named if a not in valid]
    if unknown:
        _print_json({"error": "unknown_area", "unknown": unknown, "valid_areas": valid,
                     "message": f"no subject area named {', '.join(map(repr, unknown))}; "
                                f"valid areas: {', '.join(valid) or '(none)'}"})
        return None
    return named or valid


def _run_one_area(cmd, args, area: str, **overrides) -> tuple[int, Any]:
    """Run a single-area command for `area` and return its exit code and parsed JSON output."""
    sub = argparse.Namespace(**{**vars(args), "area": area, **overrides})
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cmd(sub)
    except SystemExit:
        # A command that stops this way printed why first; dropping the capture said nothing at all.
        sys.stdout.write(buf.getvalue())
        raise
    return rc, json.loads(buf.getvalue())


def cmd_seed_validate_areas(args) -> int:
    areas = _resolve_areas(args)
    if areas is None:
        return 1
    if len(args.area or []) == 1:
        return cmd_seed_validate(argparse.Namespace(**{**vars(args), "area": areas[0]}))
    per_area, items = [], []
    for area in areas:
        _, area_items = _run_one_area(cmd_seed_validate, args, area)
        per_area.append({"area": area, "examples": len(area_items),
                         "errored": sum(1 for it in area_items if it.get("error"))})
        items += [{"area": area, **it} for it in area_items]
    _print_json({"areas": per_area, "items": items,
                 "totals": {"areas": len(areas), "examples": len(items),
                            "errored": sum(a["errored"] for a in per_area)}})
    return 0


def cmd_seed_examples_areas(args) -> int:
    areas = _resolve_areas(args)
    if areas is None:
        return 1
    if len(args.area or []) == 1:
        return cmd_seed_examples(argparse.Namespace(**{**vars(args), "area": areas[0]}))
    cands = _read_json_file(args.file)
    if isinstance(cands, dict):
        cands = cands.get("examples", cands.get("items", []))
    # With several areas each candidate names its own; one that doesn't stops the call before
    # anything is validated or written, so a seed never lands in a guessed area.
    by_area: dict[str, list[dict]] = {}
    misplaced = []
    for c in cands:
        area = (c or {}).get("area")
        if area in areas:
            by_area.setdefault(area, []).append({k: v for k, v in c.items() if k != "area"})
        else:
            misplaced.append({"question": (c or {}).get("question", "?"), "area": area})
    if misplaced:
        _print_json({"error": "seed_area_missing", "seeds": misplaced, "valid_areas": areas,
                     "message": "with several areas (or none named) every seed needs an `area` "
                                f"that is one of: {', '.join(areas)}"})
        return 2
    per_area = []
    for area in areas:
        if area not in by_area:
            continue
        rc, out = _run_one_area(cmd_seed_examples, args, area, seeds=by_area[area])
        if rc != 0:
            # A gate refusal is model-wide and no area has been written yet: report it as is.
            _print_json(out)
            return rc
        per_area.append({"area": area, **out})
    rejected = [{"area": a["area"], **r} for a in per_area for r in a.get("rejected", [])]
    _print_json({"added": [x for a in per_area for x in a.get("added", [])],
                 "written": any(a.get("written") for a in per_area),
                 "committed": _all_committed([a for a in per_area if a.get("written")]),
                 **_commit_notes(per_area),
                 "rejected": rejected, "areas": per_area,
                 "totals": {"areas": len(per_area),
                            "added": sum(len(a.get("added", [])) for a in per_area),
                            "rejected": len(rejected)}})
    return 0


def _all_committed(wrote: list[dict]) -> bool:
    """True when something was written and every area that wrote committed it."""
    return bool(wrote) and all(a.get("committed") for a in wrote)


def _commit_notes(per_area: list[dict]) -> dict:
    """The areas' `commit_note`s, each once, as the single-area output carries its one."""
    notes = list(dict.fromkeys(a["commit_note"] for a in per_area if a.get("commit_note")))
    return {"commit_note": " ".join(notes)} if notes else {}


def _area_prefix(question: str, areas: list[str]) -> tuple[Optional[str], str]:
    """The area a `<area>/<question>` names (the label `rejected` prints), and the question after it;
    (None, question) when the text before the first `/` is no area. Matched ignoring case and the
    spaces around the slash, so `Request / q` names `request` instead of being looked up whole."""
    q = question.strip()
    head, _, rest = q.partition("/")
    by_key = {a.strip().casefold(): a for a in areas}
    area = by_key.get(head.strip().casefold())
    if rest.strip() and area is not None:
        return area, rest.strip()
    return None, q


def _match_question(known: dict[str, set[str]], question: str) -> tuple[list[str], str]:
    """The areas `question` names an example in, and the question text to reject there.
    `<area>/<question>` names the area outright, even when that area has no such question: another
    area's example is never the one meant."""
    area, rest = _area_prefix(question, list(known))
    if area is not None:
        return [area], rest
    q = question.strip()
    return [a for a, questions in known.items() if q in questions], q


def _model_area_names(root: str) -> list[str]:
    """Every subject area the model declares; empty when it does not load."""
    try:
        return [sa.name for sa in L.load_datasource(root, include_rejected=True).subject_areas]
    except Exception:  # noqa: BLE001 — a model that won't load is reported by _resolve_areas
        return []


def cmd_remove_example_areas(args) -> int:
    if args.question_file:
        # one question per line, each verbatim: the questions are the person's own words
        lines = _read_text_file(args.question_file).splitlines()
        args = argparse.Namespace(**{**vars(args), "question": [q for q in lines if q.strip()]})
    areas = _resolve_areas(args)
    if areas is None:
        return 1
    known = {a: {(e or {}).get("question", "").strip()
                 for e in L.list_prompt_examples(args.root, a, include_rejected=True)} for a in areas}
    model_areas = _model_area_names(args.root)
    outside = []
    for question in args.question:
        named, _ = _area_prefix(question, model_areas)
        literal = any(question.strip() in qs for qs in known.values())
        if named is not None and named not in areas and not literal:
            outside.append({"question": question, "area": named})
    if outside:
        # The prefix names an area this call does not cover: looking the text up whole in the
        # covered areas would miss it and report nothing about the area that was meant.
        _print_json({"refused": "area_not_covered", "questions": outside, "areas": areas,
                     "message": "these questions name an area this call does not cover; nothing was "
                                "rejected. Add that --area, or drop --area to cover every area."})
        return 1
    if len(args.area or []) == 1:
        # `<area>/<question>` for the one area named is that area's question, as across several.
        qs = [question if question.strip() in known[areas[0]] else _area_prefix(question, areas)[1]
              for question in args.question]
        return cmd_remove_example(argparse.Namespace(**{**vars(args), "area": areas[0], "question": qs}))
    by_area: dict[str, list[str]] = {}
    ambiguous, missing = [], []
    for question in args.question:
        found, q = _match_question(known, question)
        if len(found) == 1:
            by_area.setdefault(found[0], []).append(q)
        elif found:
            ambiguous.append({"question": q, "areas": found})
        elif q:
            missing.append({"item": q, "reason": "no example with that question in any of the areas"})
    if ambiguous:
        # Rejecting it in every area that has it would drop examples the reviewer never saw.
        _print_json({"refused": "ambiguous_question", "ambiguous": ambiguous,
                     "message": "these questions name an example in more than one area; nothing "
                                "was rejected. Name each as <area>/<question>, or pass --area."})
        return 1
    per_area = []
    for area in areas:
        if area in by_area:
            _, out = _run_one_area(cmd_remove_example, args, area, question=by_area[area])
            per_area.append({"area": area, **out})
    rejected = [x for a in per_area for x in a["rejected"]]
    examples = [x for a in per_area for x in a["examples"]]
    skipped = missing + [s for a in per_area for s in a["skipped"]]
    note = ({"note": f"marked rejected (kept for audit): {len(examples)} example(s) stay in their "
                     "area's prompt_examples/<area>/examples.yaml with status: rejected, not deleted, "
                     "and are no longer offered as examples for a question"} if examples else {})
    _print_json({"rejected": rejected, "examples": examples, **note, "skipped": skipped,
                 "validated": any(a["validated"] for a in per_area),
                 "committed": _all_committed([a for a in per_area if a["rejected"]]),
                 **_commit_notes(per_area),
                 "areas": per_area,
                 "totals": {"areas": len(per_area), "rejected": len(rejected),
                            "skipped": len(skipped)}})
    return 0 if (rejected or not skipped) else 1


def _normalize_table_list(raw: Optional[list[str]]) -> Optional[list[str]]:
    """Split any element that arrived as one whitespace/comma-joined blob into separate names.
    Defends against the zsh gotcha where an unquoted `$TBLS` passes all 52 names as ONE argument
    (`--tables "incident orders …"` → one giant bogus table name). Idempotent on a clean list."""
    if not raw:
        return raw
    out: list[str] = []
    for item in raw:
        out.extend(p for p in re.split(r"[\s,]+", item.strip()) if p)
    return out or None


# Snowflake authenticators that sign in without a person: a password, a key pair, a token. Any other
# value (`externalbrowser`, `username_password_mfa`, an Okta `https://…` URL, one added later) may
# open a window or ask for a code on every connection, and each statement is its own connection.
_NON_PROMPTING_AUTHENTICATORS = frozenset({
    "snowflake", "oauth", "snowflake_jwt", "programmatic_access_token", "oauth_client_credentials",
})


def _may_prompt_to_sign_in(profile: str) -> bool:
    """Whether a connection for `profile` may ask a person to sign in, so the build opens one at a
    time. Reads only the `authenticator` setting, never a secret. No setting is a password or key sign-in
    (Postgres, Redshift, MySQL and the like, or Snowflake's default); a credentials file that can't
    be read is treated as one that may prompt."""
    import configparser

    from agami_paths import credentials_path
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(credentials_path(), encoding="utf-8")
        auth = cp.get(profile, "authenticator", fallback="").strip().lower()
    except (configparser.Error, OSError):
        return True
    return bool(auth) and auth not in _NON_PROMPTING_AUTHENTICATORS


def cmd_introspect(args) -> int:
    from . import introspect as INTRO
    from . import validator as V

    progress = args.progress or str(
        Path(args.artifacts).expanduser() / args.profile / ".introspect" / "progress.log")
    # Everything before the engine starts can fail too (a missing --tables-file, a bad --batch, a
    # refused file): the log must say so, or `sm progress` waits out its silence rule for nothing.
    # The engine's own failures are logged by its decorator.
    try:
        rc, prepared = _introspect_inputs(args)
    except BaseException as exc:
        INTRO.log_failure(progress, exc)
        raise
    if rc:
        INTRO.log_failure(progress, RuntimeError(prepared))
        return rc
    tables, runner, batch = prepared
    try:
        org, report = INTRO.introspect(
            args.profile,
            args.db_type,
            runner=runner,
            artifacts_dir=args.artifacts,
            out_dir=args.out,
            tables=tables,
            exclude_columns=args.exclude_columns,
            dry_run=args.dry_run,
            bigquery_region=args.bigquery_region,
            progress_path=progress,
            append=getattr(args, "append", False),
            batch=batch,
        )
    except (INTRO.ConnectionUnusable, INTRO.TableSchemaUnknown) as exc:
        # the database's own reason, not a traceback: it is a setup problem the person fixes
        sys.stderr.write(f"agami: {exc}\n")
        return 2
    res = V.validate(org)
    print(report.render())
    print(V.format_result(res))
    return 0 if res.ok else 1


def _introspect_inputs(args) -> tuple[int, Any]:
    """(0, (tables, runner, batch)) for `introspect`, or (exit code, the reason) when an input is
    refused. Anything else that goes wrong raises."""
    from . import introspect as INTRO

    tables = _normalize_table_list(args.tables)
    if getattr(args, "tables_file", None):
        # a newline-separated allowlist file — the shell-quoting-proof way to pass a big kept set
        # (no word-splitting to get wrong). `#` comments and blank lines ignored.
        from . import scratch
        if scratch.guarded():
            try:
                text = scratch.read_scratch(args.tables_file, scratch.TEXT_CAP).decode("utf-8")
            except (scratch.NotScratch, ValueError) as exc:
                _print_json({"error": str(exc)})
                return 2, str(exc)
        else:
            text = Path(args.tables_file).expanduser().read_text(encoding="utf-8")
        # One name per line, kept whole: a table name may hold a space, and splitting
        # `public.Order Lines` left a bare `Lines` that reached a catalog read with no schema (#693).
        from_file = [ln.strip() for ln in text.splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#")]
        tables = (tables or []) + from_file or None
    batch = _batch_arg(getattr(args, "batch", None))

    runner = INTRO.make_execute_sql_runner(args.profile, building_the_model=True,
                                           artifacts_dir=args.artifacts)
    # Each statement is its own connection: several at once would open as many sign-in prompts.
    from . import profile as PF
    PF.WORKERS = 1 if _may_prompt_to_sign_in(args.profile) else PF.WORKERS
    return 0, (tables, runner, batch)


def _batch_arg(text: Optional[str]) -> Optional[tuple[int, int]]:
    """`--batch 3/5` -> (3, 5); anything else is refused, so a typo never prints a wrong position."""
    if not text:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", text)
    if not m or not 1 <= int(m.group(1)) <= int(m.group(2)):
        raise SystemExit(f"--batch takes n/total, such as 3/5, not {text!r}")
    return int(m.group(1)), int(m.group(2))


# A metadata source is read whole, so each read's row cap is sized from the source's own count rather
# than left to the deployment's query-time cap, which a platform dictionary can pass by itself. The
# margin covers rows written between the count and the read; past the ceiling the read is refused with
# the count, never attempted.
_METADATA_READ_CEILING = 5_000_000
_METADATA_READ_MARGIN = 1_000


def _metadata_read_cap(runner, fq: str) -> int | str | None:
    """The row cap for reading `fq` whole: its count plus a margin, at most the ceiling. None when
    the count could not be read (the read then runs under the deployment's cap, as before); an error
    message naming the count when it is past the ceiling."""
    try:
        rows = runner(f"SELECT COUNT(*) AS n FROM {fq}") or []
    except Exception:  # noqa: BLE001 — an unread count leaves the read as it was, not failed
        return None
    # The one column, by position: a dialect may fold the alias's case.
    first = rows[0] if rows and isinstance(rows[0], dict) and len(rows[0]) == 1 else None
    n = _num(next(iter(first.values()))) if first else None
    if n is None or n < 0:
        return None
    if n > _METADATA_READ_CEILING:
        return (f"{fq} holds {n:,.0f} rows, more than the {_METADATA_READ_CEILING:,} a metadata "
                "source is read up to; enrich-metadata reads each source whole, so it was not read")
    return min(int(n) + _METADATA_READ_MARGIN + int(n) // 100, _METADATA_READ_CEILING)


def cmd_enrich_metadata(args) -> int:
    """Deterministically enrich columns from the database's OWN metadata/lookup tables —
    descriptions + choice_field labels — in one validated curate batch. No LLM, no generator
    script, no external doc fetch. Recognizes presets (e.g. ServiceNow sys_dictionary + sys_choice)
    or takes --preset; the authoritative platform metadata becomes the model's enrichment."""
    from . import curate
    from . import dialects as D
    from .loader import load_datasource

    root = Path(args.root).expanduser()
    org = load_datasource(root)
    valid = {(t.name.lower(), c.name.lower())
             for sa in org.subject_areas for t in sa.tables_defined for c in t.columns}

    dialect = D.get_dialect(args.db_type)
    # Build mode: the dictionary is read where it lies, never modelled, so the model-scope pass that
    # confines a statement to modelled tables would refuse every read of it (ACE-181). On a hosted
    # server the runner still passes --no-safety but drops --build-profile, so the recon gate refuses
    # the catalog reads that would find an unmodelled dictionary; there only a modelled one is read.
    runner = INTRO.make_execute_sql_runner(args.profile, building_the_model=True)
    # Finding the sources lists the catalog, which a large warehouse holds past the query-time cap,
    # and a refused listing reads as "no such source"; it is a build read, bounded by the ceiling.
    catalog_runner = INTRO.make_execute_sql_runner(args.profile, building_the_model=True,
                                                   max_rows=_METADATA_READ_CEILING)
    preset, sources = _metadata_sources(org, catalog_runner, dialect, args.preset)
    if not preset:
        # most databases carry no in-DB dictionary; that is nothing to do, not a failure (#526)
        _print_json({"enriched": False,
                     "reason": "no metadata preset applies to this database — nothing to do "
                               "(pass --preset to name one)"})
        return 0
    if not sources:
        _print_json({"enriched": False, "preset": preset,
                     "reason": "preset's source table(s) are neither in the model nor in the catalog"})
        return 1

    ops: list[dict] = []
    fetched: dict[str, int] = {}
    dict_rows: list[dict] = []
    dict_cfg: dict = {}
    parents: dict[str, str] = {}
    choice_rows: list[dict] = []
    choice_cfg: dict = {}
    fetched_rows: dict[str, list[dict]] = {}
    for role, (cfg, (sch, src), cols) in sources.items():
        fq = dialect.qualified(sch, src) if sch else dialect.quote_ident(src)
        # Each column is read under the role's own name, whatever the export spells it, so
        # everything after this reads the same keys (`"reference_value" AS "reference"`).
        sel = ", ".join(dialect.quote_ident(a) if a == c else f"{dialect.quote_ident(a)} AS {dialect.quote_ident(c)}"
                        for c, a in cols.items())
        cap = _metadata_read_cap(runner, fq)
        if isinstance(cap, str):
            _print_json({"enriched": False, "preset": preset, "error": cap})
            return 1
        read = runner if cap is None else INTRO.make_execute_sql_runner(
            args.profile, building_the_model=True, max_rows=cap)
        fetched_rows[role] = read(f"SELECT {sel} FROM {fq}") or []
        fetched[role] = len(fetched_rows[role])
    for role, (cfg, _at, _cols) in sources.items():
        rows = fetched_rows[role]
        # A column holding the referenced record's id, not its name, reads through that record.
        for col_key, by_role in cfg.get("resolve_ids", {}).items():
            if by_role in sources and col_key in cfg:
                rows = MS.resolve_record_ids(rows, cfg[col_key],
                                             MS.record_names(fetched_rows[by_role], sources[by_role][0]))
        if role == "choice":
            choice_rows, choice_cfg = rows, cfg
            ops += MS.choice_field_ops(rows, table_col=cfg["table_col"], column_col=cfg["column_col"],
                                       value_col=cfg["value_col"], label_col=cfg["label_col"], valid=valid)
        elif role == "dictionary":
            ops += MS.description_ops(rows, table_col=cfg["table_col"], column_col=cfg["column_col"],
                                      label_col=cfg.get("label_col"), comment_col=cfg.get("comment_col"),
                                      valid=valid)
            dict_rows, dict_cfg = rows, cfg
        elif role == "hierarchy":
            parents = MS.class_parents(rows, table_col=cfg["table_col"], id_col=cfg["id_col"],
                                       parent_col=cfg["parent_col"])

    # No class table: the preset's standard hierarchy stands in, so a child class still inherits
    # the references its parent declares (an incident is a task).
    hierarchy = "class table" if "hierarchy" in sources else "none"
    if "hierarchy" not in sources and dict_rows and "table_col" in dict_cfg:
        parents = MS.preset_class_parents(preset, {str(r.get(dict_cfg["table_col"]) or "") for r in dict_rows})
        hierarchy = "preset (no class table found)" if parents else "none"

    # A renamed export: its tables carry the platform's classes under other names, and its link
    # columns the fields under `<field>_sys_id` (ACE-181). Value labels follow the mapping.
    spec = MS.PRESETS.get(preset, {})
    key, suffix = spec.get("key", ""), spec.get("link_suffix", "")
    mapping = PJ.map_model(org, preset, dict_rows, dict_cfg, parents)
    if choice_rows and mapping:
        labelled = {(o["name"].lower(), o["column"].lower()) for o in ops if o.get("field") == "choice_field"}
        held = {t.name: {c.name: list(c.choice_field) for c in t.columns
                         if c.choice_field and (t.name.lower(), c.name.lower()) not in labelled}
                for sa in org.subject_areas for t in sa.tables_defined if t.name in mapping}
        chains = {t: [m["class"], *MS.ancestors(m["class"], parents)] for t, m in mapping.items()}
        ops += MS.view_choice_ops(choice_rows, table_col=choice_cfg["table_col"],
                                  column_col=choice_cfg["column_col"], value_col=choice_cfg["value_col"],
                                  label_col=choice_cfg["label_col"], held=held, chains=chains, key=key,
                                  link_suffix=suffix)

    # Inheritance: ServiceNow (and any table-inheritance platform) declares a shared field's
    # description ONCE under the parent table (`task`), but the column physically lives on every
    # child (`incident`/`problem`/…). description_ops keys on the declaring table, so the children
    # stay blank. Propagate the unambiguous element→description to any modeled column that's still
    # blank and didn't get a table-specific description above.
    inherited = 0
    if dict_rows and ("label_col" in dict_cfg or "comment_col" in dict_cfg):
        by_el = MS.descriptions_by_element(
            dict_rows, column_col=dict_cfg["column_col"],
            label_col=dict_cfg.get("label_col"), comment_col=dict_cfg.get("comment_col"))
        direct = {(o["name"].lower(), (o.get("column") or "").lower()) for o in ops
                  if o.get("kind") == "table" and o.get("column") and o.get("field") == "description"}
        for sa in org.subject_areas:
            for t in sa.tables_defined:
                for c in t.columns:
                    if (c.description or "").strip():
                        continue  # don't override an existing description
                    if (t.name.lower(), c.name.lower()) in direct:
                        continue  # already got a table-specific dictionary description
                    d = by_el.get(c.name.lower()) or (
                        by_el.get(MS.column_field(c.name, key, suffix) or "") if key else None)
                    if d:
                        ops.append({"op": "edit", "kind": "table", "name": t.name, "column": c.name,
                                    "field": "description", "value": d, "source": "metadata"})
                        inherited += 1

    # sys_choice keys a list by the platform's value; the column's own data values key it instead (#678)
    ops = MS.key_choices_on_held_values(ops, {
        (t.name.lower(), c.name.lower()): [str(v) for v in c.choice_field]
        for sa in org.subject_areas for t in sa.tables_defined for c in t.columns if c.choice_field})
    # 1) descriptions + choice_field — one validated curate batch
    cols_applied = 0
    errors: list[str] = []
    provenance: dict = {}
    if ops:
        res = curate.apply(root, ops)
        cols_applied = len(res.applied)
        errors += res.errors
        provenance = _provenance_note(res)

    # 2) reference/FK edges from the dictionary — the join-graph half. The table/area structure
    # the curate batch above touched only descriptions/choices, so the loaded `org` is still
    # valid for routing references (names, schemas, grain, areas unchanged).
    refs_added = 0
    found = None
    if not args.skip_references:
        found = PJ.find_reference_joins(org, runner, dialect, preset, dictionary_rows=dict_rows,
                                        dictionary_cfg=dict_cfg, parents=parents)
        intra, cross, skipped = _route_references(org, found.written)
        found.skipped_target += skipped
        written: set[tuple[str, str]] = set()
        if intra or cross:
            rres = curate.add_relationships(root, intra=intra, cross=cross)
            refs_added = len(rres.applied)
            errors += rres.errors
            # what curate actually added: a join it refused keeps its stored candidate
            written = {_applied_column(a) for a in rres.applied}
        if found.review or written:
            PJ.store_for_review(root, found.review, written)
    refs = ({"relationships_added": refs_added,
             "relationships_approved": len([s for s in found.written if s["review_state"] == "approved"]),
             "relationships_for_review": len(found.review),
             "tables_mapped_to_platform": {t: m["class"] for t, m in found.mapping.items()},
             "class_hierarchy": hierarchy,
             "reference_checks": found.probes,
             "relationships_skipped_target_not_modelled": found.skipped_target,
             "reference_notes": found.notes} if found is not None else {})

    if not cols_applied and not refs_added and not refs.get("relationships_for_review"):
        _print_json({"enriched": False, "preset": preset, "fetched": fetched, **refs,
                     "reason": "nothing applicable for modelled columns", "errors": errors})
        return 0
    _print_json({"enriched": not errors, "preset": preset, "fetched": fetched,
                 "columns_enriched": cols_applied, "descriptions_inherited": inherited,
                 **refs, **provenance, "errors": errors})
    return 0 if not errors else 1


def _applied_column(applied: str) -> tuple[str, str]:
    """(from table, from column), lower-cased, of one `curate.add_relationships` applied entry:
    `rel <area>/<table>.<column>→<table>` or `xrel <table>.<column>→<table>`."""
    label = applied.split(" ", 1)[1]
    label = label.split("/", 1)[1] if applied.startswith("rel ") else label
    table, column = label.split("→", 1)[0].rsplit(".", 1)
    return table.lower(), column.lower()


def _metadata_sources(org, runner, dialect, preset: Optional[str]) -> tuple[Optional[str], dict]:
    """The preset and `{role: (config, (schema, table), columns)}` for each of its sources found: a
    modelled table first, else the catalog, in any schema and under a renamed layer's name (ACE-181).
    `columns` maps each column the role reads to the table's spelling of it (`reference` may be
    `reference_value` in an export that writes a reference as a value + link pair).

    The catalog is read only for a platform whose shape the model shows (its record-id column, or
    a column named the way an export names a link), or one named with --preset: a database with no
    platform behind it costs no query, as before."""
    modelled = {t.name.lower(): (t.schema_name, t.name) for sa in org.subject_areas for t in sa.tables_defined}
    columns = {c.name.lower() for sa in org.subject_areas for t in sa.tables_defined for c in t.columns}
    schemas = {s for s, _ in modelled.values()}
    catalog: Optional[list] = None

    def shaped(spec: dict) -> bool:
        key, suffix = spec.get("key", "").lower(), spec.get("link_suffix", "").lower()
        return bool(key and key in columns) or bool(suffix and any(c.endswith(suffix) for c in columns))

    model_cols = {t.name.lower(): [c.name for c in t.columns] for sa in org.subject_areas for t in sa.tables_defined}
    resolved: dict[tuple, Optional[dict[str, str]]] = {}

    def fits(cfg: dict, suffixes: list[str]):
        def check(schema: Optional[str], table: str) -> bool:
            rows = INTRO._try(runner, dialect.sql_columns(schema, table)) or []
            have = [str(r.get("column_name", "")) for r in rows]
            resolved[(schema, table)] = MS.resolve_columns(cfg, have, suffixes)
            return resolved[(schema, table)] is not None
        return check

    def where(cfg: dict, look: bool, suffixes: list[str]):
        nonlocal catalog
        if cfg["source"].lower() in modelled:
            at = modelled[cfg["source"].lower()]
            same = {v: v for k, v in cfg.items() if k.endswith("_col")}
            return at, MS.resolve_columns(cfg, model_cols[cfg["source"].lower()], suffixes) or same
        if not look:
            return None
        if catalog is None:
            catalog = []
            for r in INTRO._try(runner, dialect.sql_schemas()) or []:
                s = r.get("schema_name")
                catalog += [(t.get("schema_name") or s, t["table_name"])
                            for t in INTRO._try(runner, dialect.sql_tables(s)) or [] if t.get("table_name")]
        at = MS.locate(cfg["source"], catalog, schemas, fits(cfg, suffixes))
        return (at, resolved[at]) if at else None

    for name in [preset] if preset else list(MS.PRESETS):
        spec = MS.PRESETS.get(name, {})
        look = bool(preset) or shaped(spec)
        suffixes = spec.get("column_suffixes") or [""]
        found = {}
        for role, cfg in spec.items():
            if isinstance(cfg, dict) and cfg.get("source") and (hit := where(cfg, look, suffixes)):
                found[role] = (cfg, *hit)
        if found or preset:
            return name, found
    return None, {}


def _route_references(org, specs: list[dict]) -> tuple[dict, list, int]:
    """Resolve `{from_table, from_column, to_table}` specs into routed relationship dicts:
    `(intra: {area: [rel,…]}, cross: [xrel,…], skipped_target_count)`. SELF-references are kept
    (a hierarchical `parent → same table` is a real join). Edges whose TARGET isn't modelled are
    skipped and COUNTED (so pruning that drops a join target is reported, not silent). Already-
    present edges are skipped. References point at the target's grain, labelled from both sides'
    keys (`build.infer_cardinality`), written
    `inferred`/`unreviewed` (authoritative declaration, signed off in the explorer)."""
    from collections import defaultdict

    from . import build as B
    tindex: dict = {}
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            tindex.setdefault(t.name.lower(), (t, sa.name))
    from .runtime import join_column_pairs, model_dialect
    sg = model_dialect(org)
    existing = set()
    # Every column a join is keyed on, a composite `on:` join's included (#510).
    for r in [*(r for sa in org.subject_areas for r in sa.relationships), *org.cross_subject_area_relationships]:
        for fc, _tc in join_column_pairs(r, sg) or []:
            existing.add((r.from_table.lower(), fc.lower(), r.to_table.lower()))

    intra: dict = defaultdict(list)
    cross: list = []
    skipped_target = 0
    for spec in specs:
        ft, fc, tt = spec["from_table"], spec["from_column"], spec["to_table"]
        key = (ft.lower(), fc.lower(), tt.lower())
        if key in existing:
            continue
        fr, to = tindex.get(ft.lower()), tindex.get(tt.lower())
        if fr is None:
            continue
        if to is None:                 # target table pruned / not in the model → can't join, but count it
            skipped_target += 1
            continue
        ftab, farea = fr
        ttab, tarea = to
        if not ttab.grain:
            continue
        existing.add(key)
        base = {"from_table": ft, "from_column": fc, "to_table": tt, "to_column": ttab.grain[0],
                "from_schema": ftab.schema_name, "to_schema": ttab.schema_name,
                "relationship": B.infer_cardinality(ftab, ttab, [fc], [ttab.grain[0]]), "join_type": "LEFT",
                # per-spec trust (a verified candidate is confirmed/approved); defaults otherwise
                "confidence": spec.get("confidence", "inferred"),
                "review_state": spec.get("review_state", "unreviewed"),
                "description": spec.get("description", f"reference: {fc} → {tt}.{ttab.grain[0]}")}
        for k in ("signed_off_by", "signed_off_role", "signed_off_at"):
            if spec.get(k):
                base[k] = spec[k]
        if farea == tarea:
            intra[farea].append(base)
        else:
            cross.append({**base, "from_subject_area": farea, "to_subject_area": tarea})
    return dict(intra), cross, skipped_target


def cmd_discover(args) -> int:
    """First pass: cheap discovery (tables + columns only) → inventory JSON. The person picks
    tables on a card in the chat, then `introspect --tables <kept>` runs the full build on only
    the kept tables. No grain/FK/row-count probes here. No page is written: the prune page was
    removed, and `--out` is still accepted from older callers but ignored (said in `note`)."""
    from . import introspect as INTRO

    runner = INTRO.make_execute_sql_runner(args.profile, building_the_model=True,
                                           artifacts_dir=args.artifacts)
    try:
        inventory = INTRO.discover_inventory(
            args.profile,
            args.db_type,
            runner=runner,
            tables=args.tables,
            schemas=args.schemas,
            bigquery_region=args.bigquery_region,
            row_estimates=args.row_estimates,
        )
    except INTRO.ConnectionUnusable as exc:
        sys.stderr.write(f"agami: {exc}\n")   # as `sm introspect`: a setup problem, not a traceback
        return 2

    artifacts = Path(args.artifacts).expanduser()
    inv_path = (Path(args.inventory_out).expanduser() if args.inventory_out
                else artifacts / args.profile / ".introspect" / "inventory.json")
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    from .scratch import LinkedTarget, write_replacing
    try:
        # replaced, never written through: a link at the name would carry the write elsewhere
        write_replacing(inv_path, json.dumps(inventory, indent=2, default=str))
    except LinkedTarget as exc:
        _print_json({"error": str(exc)})
        return 2


    _print_json({
        "profile": args.profile,
        "table_count": inventory["table_count"],
        "column_mode": inventory["column_mode"],
        "schemas": inventory["schemas"],
        "inventory_path": str(inv_path),
        "prune_html": None,
        **({"note": "--out is ignored: the prune page was removed; tables are picked on a card in the chat"}
           if args.out else {}),
    })
    return 0


def _add_out(sp: argparse.ArgumentParser) -> None:
    """`--out <path>` on a verb whose output a skill keeps in a file: a shell redirect after the
    launcher made the call a compound command no permission rule matches, so it stopped for
    permission every time."""
    sp.add_argument("--out", dest="out_file", default=None, metavar="PATH",
                    help='write the output to PATH (atomically) instead of stdout, and print '
                         '{"out": PATH, "bytes": N}')


def write_atomically(path: Path, data: bytes) -> None:
    """`data` at `path` through a temp file in the same folder and a rename, so a page or a later
    command reading the file never sees it half written."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        # mkstemp makes the file 0600; a redirect would have made it 0666 less the umask.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _run_to_out(args) -> int:
    """Run the verb with its stdout captured, then write those bytes to `--out`, whatever the exit
    code, as the redirect it replaces did; the exit code passes through. A verb that raises writes
    nothing, so no half answer is left for the next command to read. `--out` is only ever a scratch file or a
    model's own datasource.md (semantic_model/scratch.py), checked before the verb runs."""
    from . import scratch
    try:
        out = scratch.check_out(args.out_file)
    except scratch.NotScratch as exc:
        _print_json({"error": str(exc)})
        return 2
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = args.func(args)
    data = buf.getvalue().encode("utf-8")
    write_atomically(out, data)
    # one line: the skill reads it at a glance
    print(json.dumps({"out": str(out), "bytes": len(data)}, ensure_ascii=False))
    return rc


def _no_abbreviations(parser: argparse.ArgumentParser) -> None:
    """`allow_abbrev=False` on `parser` and every subparser under it. With abbreviations on, `--o`
    meant `--out`, so a check that looked for `--out` (a permission hook, a reviewer) could be
    walked past by writing less of it; an option is now only ever its full name."""
    parser.allow_abbrev = False
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                _no_abbreviations(child)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="semantic_model", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("validate", help="validate a profile tree")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("snapshot", help="stamp the model_version snapshot for a profile (e.g. after copying a model)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_snapshot)

    sp = sub.add_parser("ensure-org-id", help="mint + persist the deployment org_id into datasource.yaml if absent (e.g. after copying a sample model)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_ensure_org_id)

    sp = sub.add_parser("context", help="assemble get_table_context")
    sp.add_argument("root")
    sp.add_argument("--area", default=None)
    sp.add_argument("--tables", nargs="+", required=True)
    sp.add_argument("--columns", nargs="*", default=None)
    sp.add_argument(
        "--include",
        nargs="*",
        default=list(L.DEFAULT_CONTEXT_INCLUDE),
    )
    sp.set_defaults(func=cmd_context)

    sp = sub.add_parser("bundle", help="one-shot subject-area bundle")
    sp.add_argument("root")
    _add_out(sp)
    sp.add_argument("--area", required=True)
    sp.set_defaults(func=cmd_bundle)

    sp = sub.add_parser("areas", help="list subject areas")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_areas)

    sp = sub.add_parser("org-draft", help="print a human-narrative STARTER for datasource.md (prompt only, no facts)")
    sp.add_argument("root")
    _add_out(sp)
    sp.set_defaults(func=cmd_org_draft)

    sp = sub.add_parser("org-context", help="print the full domain context (human narrative + model-derived summary + glossary) for the LLM")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_org_context)

    sp = sub.add_parser("set-org", help="write the company name / description into the deployment organization record")
    sp.add_argument("artifacts_dir")
    name = sp.add_mutually_exclusive_group()
    name.add_argument("--name", default=None)
    name.add_argument("--name-file", default=None,
                      help="a UTF-8 file holding the name, used as written (one trailing newline dropped)")
    desc = sp.add_mutually_exclusive_group()
    desc.add_argument("--description", default=None)
    desc.add_argument("--description-file", default=None,
                      help="a UTF-8 file holding the description, used as written (one trailing newline "
                           "dropped)")
    sp.set_defaults(func=cmd_set_org)

    sp = sub.add_parser("set-personal-data", help="build-only: who keeps people from seeing personal data, "
                        "kept in <artifacts_dir>/local/.config and never deployed")
    sp.add_argument("artifacts_dir")
    sp.add_argument("setting", choices=list(BS.PERSONAL_DATA_CHOICES),
                    help="enforced_by_warehouse: the warehouse's own security does it, so a build flags "
                         "nothing; flagged_by_agami: agami flags and asks (the default)")
    sp.set_defaults(func=cmd_set_personal_data)

    sp = sub.add_parser("examples", help="rank prompt examples for a query")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.add_argument("--query", default=None,
                    help="the question; without it, --metric lists the examples tagged with that metric")
    sp.add_argument("--top-k", type=int, default=5)
    sp.add_argument("--metric", action="append", default=None,
                    help="a metric the question uses, by name or by the selector get_datasource_schema gave "
                         "(repeatable): examples tagged with it rank first; alone, only those are listed")
    sp.set_defaults(func=cmd_examples)

    sp = sub.add_parser("preflight", help="fan-trap / chasm-trap pre-flight check")
    sp.add_argument("root")
    sp.add_argument("--sql", required=True)
    sp.set_defaults(func=cmd_preflight)

    sp = sub.add_parser("prepare", help="tier-independent safety pass: fan/chasm pre-flight → SQL to run")
    sp.add_argument("root")
    sp.add_argument("--area", default=None)
    sp.add_argument("--sql", default=None)
    sp.add_argument("--sql-file", default=None, dest="sql_file")
    sp.set_defaults(func=cmd_prepare)

    sp = sub.add_parser("receipt", help="assemble the trust receipt for a query (deterministic, from SQL + model) — replaces hand-building it")
    sp.add_argument("root")
    sp.add_argument("--sql", default=None)
    sp.add_argument("--sql-file", default=None, dest="sql_file")
    sp.add_argument("--freshness", default=None,
                    help="optional freshness timestamp for the receipt's tables section")
    sp.set_defaults(func=cmd_receipt)

    sp = sub.add_parser("claims", help="where two statements differ: the eight claims the golden runner compares, as a diff")
    sp.add_argument("root")
    sp.add_argument("--sql-file", required=True, dest="sql_file")
    sp.add_argument("--against-sql-file", required=True, dest="against_sql_file")
    sp.set_defaults(func=cmd_claims)

    sp = sub.add_parser("compare-results", help="whether two result CSVs say the same thing, through the golden comparator")
    sp.add_argument("root")
    sp.add_argument("--golden-csv", required=True, dest="golden_csv")
    sp.add_argument("--generated-csv", required=True, dest="generated_csv")
    sp.add_argument("--match", default="exact", choices=["exact", "values", "shape", "bounded", "nonempty"],
                    help="the comparator's own default is exact; reconcile passes values for a number that may carry a float tail")
    sp.add_argument("--golden-sql-file", default=None, dest="golden_sql_file",
                    help="the answer key's statement, read only for whether it ordered its rows; not read when --unordered is given")
    sp.add_argument("--unordered", action="store_true",
                    help="compare the rows as a set whatever ORDER BY either statement wrote; reconcile passes it because the ordering claim carries the order")
    sp.add_argument("--bounds", default=None, help="JSON with min_rows/max_rows/min_value/max_value, for --match bounded")
    sp.set_defaults(func=cmd_compare_results)

    sp = sub.add_parser("join-probes", help="every join a statement wrote: declared or not, on the declared key or not, and the probe SQL that would test it (emitted, never run)")
    sp.add_argument("root")
    sp.add_argument("--sql-file", required=True, dest="sql_file")
    sp.set_defaults(func=cmd_join_probes)
    sp = sub.add_parser("mentions", help="every prose line in the semantic model that mentions a table or column a statement reads (quoted for a reader, never graded)")
    sp.add_argument("root")
    sp.add_argument("--sql-file", required=True, dest="sql_file")
    sp.set_defaults(func=cmd_mentions)

    sp = sub.add_parser("filter-values", help="every value typed into a filter: `plan` emits what to probe, `judge` grades what came back")
    modes = sp.add_subparsers(dest="mode", required=True)
    mp = modes.add_parser("plan", help="what the semantic model knows about each typed value, and the probes to run")
    mp.add_argument("root")
    mp.add_argument("--sql-file", required=True, dest="sql_file")
    mp.set_defaults(func=cmd_filter_values_plan)
    mj = modes.add_parser("judge", help="grade each typed value from the probe CSVs the tier returned")
    mj.add_argument("root")
    mj.add_argument("--plan", required=True, help="the plan JSON `filter-values plan` printed")
    mj.add_argument("--results", required=True, help="directory of <id>.<probe>.csv files the tier returned")
    mj.set_defaults(func=cmd_filter_values_judge)

    sp = sub.add_parser("review-queue", help="trust-review items needing sign-off (Rule 1/2)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_review_queue)

    sp = sub.add_parser("review-items", help="curatable entries, tab-classified (4-tab dashboard)")
    sp.add_argument("root")
    sp.add_argument("--scope", default="all", choices=["all", "rule1", "rule2", "preseed"],
                    help="all | rule1 (metrics/named-filters) | rule2 | preseed (metrics+entities seeds depend on)")
    sp.set_defaults(func=cmd_review_items)

    sp = sub.add_parser("curate-gate", help="Phase-4 gate decision: PII + pre-seed sign-off counts → should the explorer open?")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_curate_gate)

    sp = sub.add_parser("model-tree", help="browsable area→table→column tree (incl. rejected)")
    sp.add_argument("root")
    _add_out(sp)
    sp.set_defaults(func=cmd_model_tree)

    sp = sub.add_parser("batch-id", help="print a fresh stamp for the names of one page's scratch files")
    sp.set_defaults(func=cmd_batch_id)

    sp = sub.add_parser("choose-folder", help="save the folder the person chose as agami's artifacts folder (~/.config/agami/path), checked first; run before init-local")
    sp.add_argument("artifacts_dir", help="the chosen folder, written out in full")
    sp.set_defaults(func=cmd_choose_folder)

    sp = sub.add_parser("init-local", help="create <artifacts_dir>/local/ (0700) and its scratch folder, and make .gitignore ignore local/; --sample also writes the sample database's credential")
    sp.add_argument("artifacts_dir", help="agami's artifacts folder, written out in full")
    sp.add_argument("--sample", action="store_true",
                    help="write local/credentials.example for the sample database (never over an existing one)")
    sp.set_defaults(func=cmd_init_local)

    sp = sub.add_parser("set-config", help="write checked fields into <artifacts_dir>/local/.config (0600), keeping every other key")
    sp.add_argument("artifacts_dir", help="agami's artifacts folder, written out in full")
    sp.add_argument("--tier", default=None, help="cli | duckdb | python")
    sp.add_argument("--active-profile", default=None, dest="active_profile")
    sp.add_argument("--python3", default=None, help="the interpreter agami runs: an absolute path, or $AGAMI_PYTHON as set (saved as the path it names)")
    sp.add_argument("--tool", action="append", default=[], metavar="NAME=PATH",
                    help="a native query tool's absolute path: psql, mysql, snowsql, sqlite3 or duckdb (repeatable)")
    sp.add_argument("--identity-file", default=None, dest="identity_file",
                    help='a scratch file holding {"email": ..., "role": ...}, the reviewer as typed')
    sp.set_defaults(func=cmd_set_config)

    sp = sub.add_parser("progress", help="the build's latest progress line, ready to post, with a bar from its own counts; --wait N returns when the log moves on")
    sp.add_argument("root", help="the model folder, <artifacts_dir>/<profile>")
    sp.add_argument("--wait", type=float, default=0.0,
                    help="wait up to this many seconds for the next line (default 0: answer now); "
                         "adds `list`, the step list `sm steps` prints")
    sp.set_defaults(func=cmd_progress)

    sp = sub.add_parser("steps", help="the onboarding step list, status and counts read from disk, as a short list to copy; --mark <id>=<status> records a step first")
    sp.add_argument("root", help="the model folder, <artifacts_dir>/<profile> (need not exist yet)")
    sp.add_argument("--full", action="store_true",
                    help="print every step's id, label, status and count and the timeline path, not the short list")
    sp.add_argument("--mark", action="append", default=[], metavar="ID=STATUS",
                    help="mark a step in_progress, done or skipped (repeatable, applied in order)")
    sp.set_defaults(func=cmd_steps)

    sp = sub.add_parser("summary", help="the onboarding's closing summary as markdown, ready to post: totals, the per-area table, what waits for sign-off, and the 'Set up in …' line")
    sp.add_argument("root", help="the model folder, <artifacts_dir>/<profile>")
    sp.add_argument("--json", action="store_true",
                    help='print {"markdown": <the same text>, "totals": {...}} instead')
    sp.set_defaults(func=cmd_summary)

    sp = sub.add_parser("overview", help="per-area numbers for an overview card: tables, %% described, metrics/joins (and how many await sign-off), examples")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_overview)


    sp = sub.add_parser("coverage", help="per-table column-description coverage + enrichment-completeness verdict (ok:false ⇒ columns were skipped)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_coverage)

    sp = sub.add_parser("profile", help="what the build read from the data: per-column fill, distinct counts, "
                                        "ranges, frequent values and findings, and every join's check")
    sp.add_argument("root")
    sp.add_argument("--table", nargs="*", default=None, help="only these tables (bare or schema.table)")
    sp.set_defaults(func=cmd_profile)

    sp = sub.add_parser("choice-coverage", help="coded columns whose choice_field labels are still blank (value-enum decode not done)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_choice_coverage)

    sp = sub.add_parser("sensitive", help="list still-queryable columns flagged sensitive/PII + a count (excludes already-rejected columns/tables) — the Phase 4 curate gate signal")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_sensitive)

    sp = sub.add_parser("set-terminology", help="write the org domain glossary (term→definition) onto datasource.yaml key_terminology")
    sp.add_argument("root")
    sp.add_argument("--file", required=True, help="JSON object {term: definition, ...} (or {key_terminology: {...}})")
    sp.add_argument("--replace", action="store_true", help="replace the glossary instead of merging over it")
    sp.set_defaults(func=cmd_set_terminology)

    sp = sub.add_parser("set-description", help="write the datasource's one-line description onto datasource.yaml (what list_datasources routes on)")
    sp.add_argument("root")
    desc = sp.add_mutually_exclusive_group(required=True)
    desc.add_argument("--description", help="one line: what this datasource holds and when to query it")
    desc.add_argument("--description-file",
                      help="a UTF-8 file holding that line, used as written (one trailing newline dropped)")
    sp.set_defaults(func=cmd_set_description)

    sp = sub.add_parser("set-fiscal-year", help="record the month the fiscal year starts in, as the person answered it")
    sp.add_argument("root")
    sp.add_argument("--month", type=int, required=True, help="1 (January) to 12 (December)")
    sp.set_defaults(func=cmd_set_fiscal_year)

    sp = sub.add_parser("curate", help="apply exclude/include/approve/reject/edit/move/add ops (validated)")
    sp.add_argument("root")
    sp.add_argument("ops", nargs="?", metavar="OPS_FILE",
                    help="JSON file: a list of op objects (or {ops:[...]}), or a join_candidates.json; "
                         "relative to the current folder")
    sp.add_argument("--ops-file", help="the same file, named by flag (give this or OPS_FILE, not both)")
    sp.add_argument("--candidate", action="append", default=None, metavar="ID",
                    help="with a join_candidates.json: add the join candidate with this id (repeatable)")
    sp.add_argument("--signer", default=None,
                    help="who made the changes: recorded in the curation log on every op; an approve also "
                         "stamps it on the item as its sign-off (an approve needs it), while an edit to what an "
                         "approved item computes or connects sends it back to review as stale")
    sp.add_argument("--role", default=None, help="the signer's role, recorded and stamped with --signer")
    sp.set_defaults(func=cmd_curate, after_parse=lambda a, extra, sp=sp: _one_ops_file(sp, a, extra))

    sp = sub.add_parser("history", help="the model's curation log, newest first (what changed, when, by whom, the prior value)")
    sp.add_argument("root")
    sp.add_argument("--limit", type=int, default=None, help="show only the newest N entries")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("approve-queue",
                        help="sign off the whole pending review queue in one call (no-browser path)")
    sp.add_argument("root")
    # required: curate._set_trust only stamps signed_off_* when a signer is present, so an
    # approve with no signer records an incomplete trust block and the validator reverts the
    # whole batch. A sign-off must record who — mirror that at the CLI surface.
    sp.add_argument("--signer", required=True, help="sign-off email recorded on each item")
    sp.add_argument("--role", required=True, help="sign-off role recorded on each item")
    sp.add_argument("--kind", action="append", choices=["metric", "entity", "relationship"],
                    help="restrict to one item kind (repeatable); default approves all")
    sp.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="print the approve ops that would be applied, without applying them")
    sp.set_defaults(func=cmd_approve_queue)

    sp = sub.add_parser("apply-spec",
                        help="apply a model spec (subject areas, table ownership, joins, metrics, "
                             "sensitive columns, rules) over the introspected model as one validated, "
                             "revertable step")
    sp.add_argument("root")
    sp.add_argument("--file", required=True, help="the spec as JSON (model_spec_workbook.py makes it from the template)")
    sp.add_argument("--signer", default=None, help="who signs off the joins and metrics the spec marks approved")
    sp.add_argument("--role", default=None)
    sp.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="check the spec and report what would change, without writing")
    sp.set_defaults(func=cmd_apply_spec)

    sp = sub.add_parser("add", help="create metric/entity YAMLs from a JSON file (validated, revertable)")
    sp.add_argument("root")
    sp.add_argument("--kind", required=True, choices=["metric", "entity"])
    sp.add_argument("--area", required=True,
                    help="a subject area datasource.yaml lists, or _cross for items that span areas "
                         "(the datasource's cross_subject_area_metrics / _entities)")
    sp.add_argument("--file", required=True, help="JSON: a list of items (or {metrics:[…]}/{entities:[…]})")
    sp.add_argument("--signer", default=None)
    sp.add_argument("--role", default=None)
    sp.add_argument("--profile", default=None,
                    help="run each metric's SQL over zero rows on this database first; nothing is "
                         "written if one fails (a metric over several tables is reported not_test_run)")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("add-example", help="append/replace NL→SQL examples (prompt_examples/<area>/examples.yaml)")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.add_argument("--file", required=True,
                    help="JSON list of {question, sql, [tables, columns, metrics, source, status, created_at]}")
    sp.add_argument("--signer", default=None)
    sp.add_argument("--role", default=None)
    sp.set_defaults(func=cmd_add_example)

    sp = sub.add_parser(
        "remove-example",
        help="mark prompt example(s) rejected by question: kept in examples.yaml for audit, not deleted",
        description="Marks each named example rejected (status: rejected). It is kept in "
                    "prompt_examples/<area>/examples.yaml for audit, not deleted, and no longer "
                    "offered as an example for a question.")
    sp.add_argument("root")
    sp.add_argument("--area", action="append", default=None,
                    help="subject area (repeatable); omit for every area in the model. "
                         "Several or none print one combined result: per-area entries plus totals")
    questions = sp.add_mutually_exclusive_group(required=True)
    questions.add_argument("--question", action="append",
                           help="exact question text of an example to reject (repeatable for several); "
                                "across several areas name it <area>/<question> unless only one area has it")
    questions.add_argument("--question-file",
                           help="a UTF-8 file with one such question per line, each used as written")
    sp.add_argument("--signer", default=None, help="who rejected it (recorded in the curation log)")
    sp.add_argument("--role", default=None)
    sp.set_defaults(func=cmd_remove_example_areas)

    sp = sub.add_parser("set-units", help="stamp a currency/unit on detected money columns — the apply half of suggest-units, one validated batch; prints the plan and writes nothing without --yes or --columns")
    sp.add_argument("root")
    sp.add_argument("--currency", default=None,
                    help="currency unit: an ISO code, optionally scaled and/or a rate (USD, USD_millions, USD/day, USD_millions/year)")
    sp.add_argument("--unit", default=None, help="non-currency unit (cents/percent/days/…)")
    sp.add_argument("--yes", action="store_true",
                    help="apply the detected plan (without it, detection only prints the plan)")
    sp.add_argument("--area", default=None, help="restrict to one subject area")
    sp.add_argument("--columns", nargs="*", default=None,
                    help="explicit table.column (or bare column) list — overrides money detection; a name that states its currency keeps that unit")
    sp.set_defaults(func=cmd_set_units)

    sp = sub.add_parser("suggest-metrics", help="infer per-table measures (start→end durations, and flag rates only when --flag-rates asks; never a plain count/sum/avg) and print them; --write saves them proposed/unreviewed for bulk sign-off — replaces ask-for-4")
    sp.add_argument("root")
    sp.add_argument("--area", default=None, help="restrict to one subject area")
    sp.add_argument("--max-per-table", type=int, default=10, dest="max_per_table",
                    help="cap measures per table")
    sp.add_argument("--write", action="store_true",
                    help="save the unflagged proposals (and any named with --names); default is a dry run")
    sp.add_argument("--names", nargs="+", default=None, metavar="METRIC",
                    help="only these proposals; a named one is written even when flagged (never when its binding errors)")
    sp.add_argument("--profile", default=None,
                    help="test-run every binding against this profile's database first (one bounded read per table)")
    sp.add_argument("--flag-rates", nargs="+", default=None, dest="flag_rates", metavar="TABLE.COLUMN",
                    help="also propose a share-of-true rate for these boolean columns (table.column or schema.table.column, comma- or space-separated; a name matching more than one table is refused), or 'all' for every one; none by default")
    sp.add_argument("--cite", default=None, metavar="SOURCE",
                    help="with --flag-rates: the document that defines the requested rates; their description says 'Defined in: SOURCE.'")
    sp.set_defaults(func=cmd_suggest_metrics)

    sp = sub.add_parser("describe-file", help="apply many column descriptions from a TSV (loc<TAB>description), CSV or JSON data dictionary (stdin or --file) in one validated batch — no generator script; unmatched rows are listed")
    sp.add_argument("root")
    sp.add_argument("--file", default=None, help="TSV / CSV / JSON path, format from the extension (default: read TSV from stdin)")
    sp.add_argument("--format", choices=["tsv", "csv", "json"], default=None,
                    help="override the format the file extension implies")
    sp.add_argument("--source", choices=["ai", "metadata"], default="ai",
                    help="ai (default): generated text, stamped ai_unvalidated. metadata: taken as written "
                         "from the customer's dictionary, stamped metadata; not checked against the data "
                         "(see #564); never replaces a description a person wrote")
    sp.set_defaults(func=cmd_describe_file)

    sp = sub.add_parser("open-questions", help="what the data can't settle: candidate ambiguities checked by running both readings, plus coded values and columns the build couldn't read (read-only; nothing is declared)")
    sp.add_argument("root")
    sp.add_argument("--profile", required=True, help="the database to probe (bounded aggregates only)")
    sp.add_argument("--all", action="store_true",
                    help="the review page's list: every item, uncapped and without cards, the build's own "
                         "guesses to confirm, and a few sample values of each unclear column")
    _add_out(sp)
    sp.set_defaults(func=cmd_open_questions)

    sp = sub.add_parser("suggest-units", help="list numeric money columns (for currency-unit stamping) via the tested name matcher")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_suggest_units)

    sp = sub.add_parser("format-table", help="format a result CSV into a deterministic markdown table (exact numbers)")
    sp.add_argument("--csv-file", default=None, help="result CSV (header row + rows); omit to read stdin")
    sp.add_argument("--units", default=None, help='JSON header->unit map, e.g. {"outstanding":"INR"}')
    sp.add_argument("--labels", default=None,
                    help='JSON list of label columns shown as written (a year, a code), e.g. ["ship_year"]')
    sp.set_defaults(func=cmd_format_table)

    sp = sub.add_parser("seed-examples", help="validate candidate seeds against the live DB + write the passing ones")
    sp.add_argument("root")
    sp.add_argument("--area", action="append", default=None,
                    help="subject area (repeatable); omit for every area in the model. Several or "
                         "none print one combined result, and each seed then names its `area`")
    sp.add_argument("--profile", required=True, help="credentials profile (for the live-DB validation)")
    sp.add_argument("--file", required=True, help="JSON list of candidate {question, sql, [tables, columns, metrics]}")
    sp.add_argument("--after-review", action="store_true",
                    help="bypass the preseed-review gate (Phase 4c only — the user has already "
                         "been in the explorer and chose to proceed with items still unreviewed)")
    sp.set_defaults(func=cmd_seed_examples_areas)

    sp = sub.add_parser("seed-validate", help="run every written seed against the live DB (through execute_sql's safety pass) + emit examples-validation items")
    sp.add_argument("root")
    sp.add_argument("--area", action="append", default=None,
                    help="subject area (repeatable); omit for every area in the model. "
                         "Several or none print one combined result: per-area entries plus totals")
    sp.add_argument("--profile", required=True, help="credentials profile (for live-DB execution)")
    sp.add_argument("--preview", type=int, default=10, help="max result rows per seed in the dashboard preview (default 10)")
    _add_out(sp)
    sp.set_defaults(func=cmd_seed_validate_areas)

    sp = sub.add_parser("introspect", help="introspect a live DB into the semantic model")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--db-type", required=True, dest="db_type",
                    help="postgres|mysql|snowflake|bigquery|redshift|sqlite|sqlserver|"
                         "databricks|trino|oracle|duckdb|supabase")
    sp.add_argument("--artifacts", required=True, help="artifacts_dir (output: <artifacts>/<profile>/)")
    sp.add_argument("--out", default=None, help="override output dir")
    sp.add_argument("--tables", nargs="*", default=None,
                    help="schema.table allowlist — the prune step's kept set (also the "
                         "no-catalog/probe-only case). A single whitespace/comma-joined blob is "
                         "split defensively (zsh-safe).")
    sp.add_argument("--tables-file", default=None, dest="tables_file",
                    help="path to a newline-separated allowlist file — the shell-quoting-proof way "
                         "to pass a large kept set (no word-splitting to get wrong)")
    sp.add_argument("--exclude-columns", nargs="*", default=None, dest="exclude_columns",
                    help="schema.table.column list to mark excluded (the prune step's dropped columns)")
    sp.add_argument("--bigquery-region", default="region-us", dest="bigquery_region")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--progress", default=None,
                    help="progress-log path (flushed per phase/table; default "
                         "<artifacts>/<profile>/.introspect/progress.log) — tail it for a heartbeat")
    sp.add_argument("--append", action="store_true",
                    help="batched build: introspect only --tables this call, MERGE into the existing "
                         "model (prior tables loaded, not re-queried), write the union. Call once per "
                         "batch for quick foreground progress on a big schema (no background monitor).")
    sp.add_argument("--batch", default=None,
                    help="this call's place in a batched build, as n/total (e.g. 3/5): written as the "
                         "progress log's first line, for the progress page")
    sp.set_defaults(func=cmd_introspect)

    sp = sub.add_parser("dictionary-joins",
                        help="offer the joins the applied data dictionary states (FK -> table.column) as "
                             "join candidates, each checked once on the data; --unjoined lists those "
                             "with no join yet, without reading the database")
    sp.add_argument("root")
    sp.add_argument("--profile", default=None)
    sp.add_argument("--db-type", default=None, dest="db_type")
    sp.add_argument("--unjoined", action="store_true",
                    help="only list the columns the dictionary calls references that no join reads from")
    sp.set_defaults(func=cmd_dictionary_joins)

    sp = sub.add_parser("enrich-metadata",
                        help="enrich columns from the DB's own metadata/lookup tables "
                             "(descriptions + choice_field), e.g. ServiceNow sys_dictionary/sys_choice")
    sp.add_argument("root")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--db-type", required=True, dest="db_type")
    sp.add_argument("--preset", default=None,
                    help="metadata preset (e.g. servicenow); auto-detected from the model if omitted")
    sp.add_argument("--skip-references", action="store_true", dest="skip_references",
                    help="only descriptions + choice_field; do not add reference/FK relationships")
    sp.set_defaults(func=cmd_enrich_metadata)

    sp = sub.add_parser("discover",
                        help="cheap first pass: list tables + columns and render the prune page "
                             "(no grain/FK/row-count probes) — prune, then introspect the kept set")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--db-type", required=True, dest="db_type",
                    help="postgres|mysql|snowflake|bigquery|redshift|sqlite|sqlserver|"
                         "databricks|trino|oracle|duckdb|supabase")
    sp.add_argument("--artifacts", required=True, help="artifacts_dir (inventory: <artifacts>/<profile>/.introspect/)")
    sp.add_argument("--out", default=None, help="ignored (the prune page was removed); kept so older callers don't fail")
    sp.add_argument("--inventory-out", default=None, dest="inventory_out",
                    help="override inventory JSON path (default <artifacts>/<profile>/.introspect/inventory.json)")
    sp.add_argument("--tables", nargs="*", default=None,
                    help="optional schema.table allowlist to scope discovery (probe-only case)")
    sp.add_argument("--schemas", nargs="*", default=None,
                    help="restrict discovery to these schemas (the user's schema pick)")
    sp.add_argument("--bigquery-region", default="region-us", dest="bigquery_region")
    sp.add_argument("--row-estimates", action="store_true", dest="row_estimates",
                    help="add each table's row estimate from one catalog-statistics read, where the "
                         "database keeps one (never a count) — for the table picker page")
    sp.set_defaults(func=cmd_discover)

    _no_abbreviations(p)
    return p


def _utf8_stdio() -> None:
    """Write stdout and stderr as UTF-8 whatever the platform. A piped stream on Windows uses the
    ANSI code page, so the help text's arrows and a currency symbol such as ₹ raise instead of
    printing, and every caller of `sm` already decodes UTF-8. A replaced stream (a test's capture)
    may have no `reconfigure`, and needs none."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _enter_caller_cwd() -> None:
    """The `sm` launcher runs us from `/` so nothing in the caller's folder can shadow this package,
    and names that folder in AGAMI_CALLER_CWD. Moving back once the package is imported makes a
    relative path argument resolve where the caller ran `sm` (#542)."""
    cwd = os.environ.pop("AGAMI_CALLER_CWD", "")
    if cwd:
        try:
            os.chdir(cwd)
        except OSError:
            pass  # the folder is gone: stay put, and a relative path's error names where it looked


def _one_ops_file(sp: argparse.ArgumentParser, args: argparse.Namespace, extra: list[str]) -> list[str]:
    """Take `curate`'s OPS_FILE from the leftovers and require exactly one ops file. argparse fills an
    optional positional in the first run of positionals, so in `curate root --signer x ops.json` the
    file is left over. Python 3.12 also leaves a `--` and what follows it: each of those is a
    positional, even one that starts with `-`."""
    cut = extra.index("--") if "--" in extra else len(extra)
    before, after = extra[:cut], extra[cut + 1:]
    if args.ops is None:
        at = next((i for i, a in enumerate(before) if not a.startswith("-")), None)
        if at is not None:
            args.ops = before.pop(at)
        elif after:
            args.ops = after.pop(0)
    extra = before + after
    if args.ops is not None and args.ops_file is not None:
        sp.error("argument --ops-file: not allowed with argument OPS_FILE")
    if args.ops is None and args.ops_file is None:
        sp.error("one of the arguments OPS_FILE --ops-file is required")
    return extra


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    after = getattr(args, "after_parse", None)
    if after is not None:
        extra = after(args, extra)
    if extra:
        parser.error("unrecognized arguments: " + " ".join(extra))
    return args


# Every option that names a file a verb reads, and every one that names a file or folder it writes
# besides the model it is given. Under the scratch-only guard each must be a scratch file (or, out,
# a model's datasource.md): the readers check again as they open, these catch the rest up front.
_NAMED_INPUTS = ("ops", "ops_file", "file", "tables_file", "sql_file", "plan", "csv_file",
                 "name_file", "description_file", "question_file", "spec", "items", "identity_file")
_NAMED_OUTPUTS = ("out", "out_file", "inventory_out", "progress")


def _scratch_only_refusal(args) -> Optional[str]:
    """Under `AGAMI_SCRATCH_ONLY=1`, why the call names a file it may not, else None."""
    from . import scratch
    if not scratch.guarded():
        return None
    try:
        for attr in _NAMED_INPUTS + _NAMED_OUTPUTS:
            value = getattr(args, attr, None)
            for v in value if isinstance(value, list) else [value]:
                if isinstance(v, str) and v:
                    if attr in _NAMED_OUTPUTS:
                        scratch.check_out(v)
                    else:
                        scratch.check_input(v)
    except scratch.NotScratch as exc:
        return str(exc)
    return None


def main(argv=None) -> int:
    _utf8_stdio()
    _enter_caller_cwd()
    args = _parse_args(argv)
    refused = _scratch_only_refusal(args)
    if refused:
        _print_json({"error": refused})
        return 2
    try:
        if getattr(args, "out_file", None):
            return _run_to_out(args)
        return args.func(args)
    except L.OutsideModel as e:
        # The path as the model wrote it, never what is in the file.
        _print_json({"error": "outside_model", "detail": str(e)})
        return 1
    except FileNotFoundError as e:
        # No model at this root yet. This is an expected, clean signal (not a crash):
        # callers fold the "does a model exist?" check into their first real command
        # (e.g. `sm areas`) instead of a separate filesystem probe. Exit 3 = "no model
        # here — run agami-connect"; distinct from validation errors (1) and usage (2).
        if "datasource.yaml" in str(e):
            _print_json({"error": "no_model", "detail": str(e)})
            return 3
        raise


if __name__ == "__main__":
    raise SystemExit(main())
