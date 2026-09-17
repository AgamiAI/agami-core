#!/usr/bin/env python3
"""Check one statement a person supplied, for `agami-reconcile` Phase 1.5 (`shared/statement-check.md`).

Every step of the check that reaches the database runs here, in code, and each statement goes
through `execute_sql.execute_guarded` with the built-in executor: the zero-row check, the statement
itself, and every probe. The session used to run those steps by hand, on whatever tier the profile
queries on. On psql, mysql, snowsql, sqlite3 or DuckDB there is no read-only gate, no scope gate
and no row or time bound, so a pasted statement that writes, or a probe, reached the database with
only the database role in its way. Here there is one road, and it is the guarded one.

For the row directory given, holding `statement.sql`, this writes the files the ledger reads:

    run.json                   the statement's outcome: status, exit, kind, rule, detail, remediation
    zero-row.sql               the statement wrapped to return no rows
    statement-prepare.json     `sm prepare`
    statement.csv              the statement's result (empty when it did not run)
    statement-receipt.json     `sm receipt`
    mentions.json              `sm mentions`
    join-probes.json           `sm join-probes`
    filter-values.plan.json    `sm filter-values plan`
    <probe id>.sql / .csv / .run.json, probes.plan.json and its manifest
    probes.folded.plan.json    the near-miss probes, only for a value whose `exists` returned 0
    filter-values.judge.json   `sm filter-values judge`

Nothing here writes `query_log.jsonl`, and `question_fit.json` stays with the session: it is a
judgment made by reading, not a measurement.

Usage:

    "$PY" check_statement.py --profile main --area sales --row-dir <artifacts_dir>/local/reconcile/<ts>/rows/3

Exit codes: `0` the row was checked, whatever the statement's outcome (a refusal is a finding);
`2` cannot start (no `statement.sql`, or this interpreter lacks agami-core with its model extra);
`3` the database could not be reached as configured (`auth`, `dsn`, `network`, `permission`,
`driver_missing`), which stops the whole run. Stdout is one JSON line and never carries SQL.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import logging
import sys
from pathlib import Path
from typing import Any

# The same resolver every runtime script beside this one uses; see run_golden_eval.py for why.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agami_lib  # noqa: E402

_agami_lib.ensure_importable()

try:
    import agami_paths
    import execute_sql
    import sql_guard
    from semantic_model import cli as sm_cli
except ImportError as exc:
    print(
        "check_statement needs agami-core and its model extra (pydantic, sqlglot, pyyaml): run "
        f'`bash "$AGAMI_PLUGIN_ROOT/scripts/sm" install` and call this with "$PY". ({exc})',
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

_CANNOT_START = 2
_STOP_RUN = 3

# The classifier kinds that mean the statement itself is wrong. At the zero-row check they end the
# row: the person's defect, graded `runs: query_defect`, with nothing further to learn by probing.
_DEFECT_KINDS = frozenset({"column_not_found", "table_not_found", "syntax"})
# The kinds that mean the database cannot be reached as configured. Every later row would fail the
# same way, so the run stops, as `agami-query` Phase 3b stops. `driver_missing` is here because this
# door needs the Python driver whatever tier the profile queries on, and its message names the install.
_STOP_KINDS = frozenset({"auth", "dsn", "network", "permission", "driver_missing"})


def _record(env: Any) -> dict[str, Any]:
    """`run.json`'s shape from one Envelope. Every field is value-free by the guard's contract: never
    the statement and never the engine's own error text. A failure carries one sentence, which says
    both what happened and what to do, so it fills `detail` and `remediation` alike."""
    if env.status == "ok":
        return {
            "status": "ok",
            "exit": 0,
            "kind": None,
            "rule": None,
            "detail": None,
            "remediation": None,
        }
    if env.status == "refused":
        return _refused(env.refusal)
    failure = env.failure
    return {
        "status": "failed",
        "exit": execute_sql.FAILURE_KIND_TO_EXIT.get(failure.kind, 6),
        "kind": failure.kind,
        "rule": None,
        "detail": failure.message,
        "remediation": failure.message,
    }


def _refused(refusal: Any) -> dict[str, Any]:
    return {
        "status": "refused",
        "exit": 1,
        "kind": None,
        "rule": refusal.rule,
        "detail": refusal.detail,
        "remediation": refusal.remediation,
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _guarded(sql: str, profile: str, area: str) -> Any:
    return execute_sql.execute_guarded(sql, profile, area, executor=execute_sql.BUILTIN_EXECUTOR)


def _sm(out: Path, *argv: str) -> None:
    """One `sm` verb, in process, its stdout written to `out` as `sm … > out` wrote it.

    In process because the guard above already needs the semantic_model package in this interpreter,
    and `sm` runs this same `cli.main`. A verb that raises leaves what it printed, often nothing: the
    ledger reads an empty file as a part that was not checked, exactly as it read a crashed redirect.
    The exception's text is not relayed, since a parser's message can quote the statement."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            sm_cli.main(list(argv))
    except Exception as exc:
        print(f"check_statement: `sm {argv[0]}` raised {type(exc).__name__}", file=sys.stderr)
    out.write_text(buf.getvalue(), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _probe(row_dir: Path, probe_id: str, sql: str) -> dict[str, str]:
    """Write one probe to its own `.sql` file and return its plan entry, by absolute path."""
    sql_file = row_dir / f"{probe_id}.sql"
    sql_file.write_text(sql, encoding="utf-8")
    return {"id": probe_id, "sql_file": str(sql_file), "out": str(row_dir / f"{probe_id}.csv")}


def _probes(row_dir: Path, joins: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, str]]:
    """Every probe the two planning verbs emitted, named as `shared/part-ledger.md` lists them."""
    entries = []
    for join in joins.get("joins") or []:
        for i, overlap in enumerate((join.get("probes") or {}).get("overlap") or []):
            if overlap and overlap.get("sql"):
                entries.append(_probe(row_dir, f"{join['id']}.overlap.{i}", overlap["sql"]))
        dropped = join.get("dropped_rows_probe")
        if dropped and dropped.get("sql"):
            entries.append(_probe(row_dir, f"{join['id']}.dropped_rows", dropped["sql"]))
    # A null entry is a column the semantic model already declares unique: nothing to count.
    for key, sql in (joins.get("cardinality") or {}).items():
        if sql:
            entries.append(_probe(row_dir, f"cardinality.{key}", sql))
    for key, column in (plan.get("columns") or {}).items():
        if column.get("distinct"):
            entries.append(_probe(row_dir, f"{key}.distinct", column["distinct"]))
    for lit in plan.get("literals") or []:
        exists = (lit.get("probes") or {}).get("exists")
        if exists:
            entries.append(_probe(row_dir, f"{lit['id']}.exists", exists))
    return entries


def _counted_zero(path: Path) -> bool:
    """Whether an `exists` probe ran and counted no rows. A probe that did not run is not a zero."""
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        return len(rows) > 1 and float(rows[1][0]) == 0
    except (OSError, IndexError, ValueError):
        return False


def _run_plan(
    plan_path: Path, entries: list[dict[str, str]], profile: str, area: str
) -> dict[str, Any]:
    """Run a plan through `execute_sql`'s own batch door, in process: every item through
    `execute_guarded` on its own, one connection kept open, each CSV, `.run.json` and the manifest
    written by the door. Its stdout copy of the manifest is dropped; the file is the record."""
    _write_json(plan_path, entries)
    with contextlib.redirect_stdout(io.StringIO()):
        execute_sql._batch_main(
            plan_path, profile, default_area=area, no_safety=False, manifest_path=None
        )
    return _read_json(plan_path.with_name(plan_path.name + ".manifest.json"))


def check(row_dir: Path, statement: str, profile: str, area: str) -> tuple[int, dict[str, Any]]:
    """Steps 1 to 8 of `shared/statement-check.md` for the row whose `statement.sql` holds `statement`.
    Returns the exit code and the summary."""
    statement_file = row_dir / "statement.sql"
    root = str(agami_paths.profile_dir(profile))
    summary: dict[str, Any] = {"row_dir": str(row_dir), "probes": 0, "probes_ok": 0}

    # 1. Read-only first, by the guard's own gates in the chokepoint's own order. Nothing runs after
    #    a statement that is not one read-only SELECT, not even the zero-row wrap around it.
    refusal = sql_guard.check_read_only(statement) or sql_guard.check_no_recon(statement)
    if refusal is not None:
        run = _refused(refusal)
        _write_json(row_dir / "run.json", run)
        return 0, {**summary, "run": run}

    # 2. Does it run at all? The newlines keep a trailing line comment from swallowing the wrapper.
    body = statement.strip().rstrip(";").rstrip()
    zero_row = f"SELECT 1 FROM (\n{body}\n) AS _agami_check WHERE 1=0"
    (row_dir / "zero-row.sql").write_text(zero_row, encoding="utf-8")
    zero = _record(_guarded(zero_row, profile, area))
    if zero["status"] == "failed" and zero["kind"] in _DEFECT_KINDS | _STOP_KINDS:
        _write_json(row_dir / "run.json", zero)
        return (_STOP_RUN if zero["kind"] in _STOP_KINDS else 0), {**summary, "run": zero}

    # 3. What the semantic model says about its aggregates. Describes, never refuses.
    sql_arg = ("--sql-file", str(statement_file))
    _sm(row_dir / "statement-prepare.json", "prepare", root, "--area", area, *sql_arg)

    # 4 to 6. The statement itself. A refusal is written down as a grade, never retried or rewritten.
    env = _guarded(statement, profile, area)
    with (row_dir / "statement.csv").open("w", newline="", encoding="utf-8") as fh:
        if env.status == "ok" and env.data.columns:
            writer = csv.writer(fh)
            writer.writerow(env.data.columns)
            writer.writerows(env.data.rows)
    run = _record(env)
    _write_json(row_dir / "run.json", run)
    summary["run"] = run
    if run["status"] == "failed" and run["kind"] in _STOP_KINDS:
        return _STOP_RUN, summary

    # 7. The receipt, and the semantic model's own words about what the statement reads.
    _sm(row_dir / "statement-receipt.json", "receipt", root, *sql_arg)
    _sm(row_dir / "mentions.json", "mentions", root, *sql_arg)

    # 8. The probes, as one plan; then the folded near misses for the values that counted zero.
    _sm(row_dir / "join-probes.json", "join-probes", root, *sql_arg)
    _sm(row_dir / "filter-values.plan.json", "filter-values", "plan", root, *sql_arg)
    plan = _read_json(row_dir / "filter-values.plan.json")
    manifests = []
    entries = _probes(row_dir, _read_json(row_dir / "join-probes.json"), plan)
    if entries:
        manifests.append(_run_plan(row_dir / "probes.plan.json", entries, profile, area))
    folded = [
        _probe(row_dir, f"{lit['id']}.exists_folded", lit["probes"]["exists_folded"])
        for lit in plan.get("literals") or []
        if (lit.get("probes") or {}).get("exists_folded")
        and _counted_zero(row_dir / f"{lit['id']}.exists.csv")
    ]
    if folded:
        manifests.append(_run_plan(row_dir / "probes.folded.plan.json", folded, profile, area))
    _sm(
        row_dir / "filter-values.judge.json",
        "filter-values",
        "judge",
        root,
        "--plan",
        str(row_dir / "filter-values.plan.json"),
        "--results",
        str(row_dir),
    )
    summary["probes"] = len(entries) + len(folded)
    summary["probes_ok"] = sum(m.get("ok") or 0 for m in manifests)
    return 0, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check one statement a person supplied, every execution through the guard."
    )
    parser.add_argument(
        "--profile", required=True, help="the profile whose semantic model and credentials to use"
    )
    parser.add_argument(
        "--area", required=True, help="the subject area the scope gates check against"
    )
    parser.add_argument("--row-dir", required=True, help="the row directory holding statement.sql")
    args = parser.parse_args(argv)

    # The chokepoint logs raw driver text for an operator. On this entry point stderr reaches the
    # session, and that text can carry the statement and the engine's own words, so it is dropped here
    # exactly as `execute_sql.main` drops it.
    for name in ("execute_sql", "execute_sql.raw"):
        logger = logging.getLogger(name)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False

    row_dir = Path(args.row_dir).expanduser().resolve()
    try:
        statement = (row_dir / "statement.sql").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        print(f"check_statement: no readable statement.sql in {row_dir}", file=sys.stderr)
        return _CANNOT_START
    code, summary = check(row_dir, statement, args.profile, args.area)
    print(json.dumps(summary))
    return code


if __name__ == "__main__":
    sys.exit(main())
