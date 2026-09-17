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
    zero-row.run.json          that wrap's own outcome, in `run.json`'s shape, whatever it was
    statement-prepare.json     `sm prepare`
    statement.csv              the statement's result (absent or empty when it did not run)
    statement-receipt.json     `sm receipt`
    mentions.json              `sm mentions`
    join-probes.json           `sm join-probes`
    filter-values.plan.json    `sm filter-values plan`
    <probe id>.sql / .csv / .run.json, probes.plan.json and its manifest
    probes.folded.plan.json    the near-miss probes, only for a value whose `exists` returned 0
    filter-values.judge.json   `sm filter-values judge`

Checking a row again first clears every file an earlier check wrote there, so nothing a previous
statement left is graded as this one's. Nothing here writes `query_log.jsonl`, and
`question_fit.json` stays with the session: it is a judgment made by reading, not a measurement.

Usage:

    "$PY" check_statement.py --profile main --area sales --row-dir <artifacts_dir>/local/reconcile/<ts>/rows/3

Exit codes: `0` the row was checked, whatever the statement's outcome (a refusal is a finding);
`2` cannot start (no `statement.sql`, or this interpreter lacks agami-core with its model extra);
`3` the database cannot be queried as configured, which stops the whole run: a failure of kind
`auth`, `dsn`, `network`, `permission` or `driver_missing`, or an `engine_mismatch` refusal. Any
other exit is a crash in this script. Stdout is one JSON line and never carries SQL.
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

try:
    # Inside the try because it raises ImportError too, when no layout provides the library. Outside
    # it, that raise left a traceback and exit 1 where this script promises exit 2.
    _agami_lib.ensure_importable()

    import agami_paths
    import execute_sql
    import sql_guard
    from semantic_model import cli as sm_cli
except ImportError as exc:
    # Only the error's type, never its text: an import error's message can name an absolute path,
    # and this script keeps a path out of every line it prints.
    print(
        "check_statement needs agami-core and its model extra (pydantic, sqlglot, pyyaml): run "
        f'`bash "$AGAMI_PLUGIN_ROOT/scripts/sm" install` and call this with "$PY". '
        f"({type(exc).__name__})",
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
# The one refusal that stops the run. It says the semantic model declares an engine its credentials
# do not connect to, and the guard refuses every statement on that datasource until an operator
# fixes the configuration, so every later row would be refused the same way.
_STOP_RULES = frozenset({"engine_mismatch"})

# Every file this script writes into a row directory: the fixed names, then the probe files in the
# shapes `_probes` names them. A row is checked again in place when the person rewords its
# statement, and the ledger grades the files it finds whatever `run.json` says, so a file the last
# statement left would be graded as this one's. Only these are cleared: `statement.sql`,
# `question_fit.json` and the files Phase 2 writes beside them are not this script's.
_OWN_FILES = (
    "run.json",
    "zero-row.sql",
    "zero-row.run.json",
    "statement.csv",
    "statement-prepare.json",
    "statement-receipt.json",
    "mentions.json",
    "join-probes.json",
    "filter-values.plan.json",
    "filter-values.judge.json",
    "probes.plan.json",
    "probes.plan.json.manifest.json",
    "probes.folded.plan.json",
    "probes.folded.plan.json.manifest.json",
)
_OWN_PROBE_FILES = (
    "join-*.overlap.*",
    "join-*.dropped_rows.*",
    "cardinality.*",
    "*.distinct.*",
    "lit-*.exists*",
)


class _OwnErrors(logging.Handler):
    """`execute_sql`'s own log records, one value-free line each on stderr, and the type of every
    error they carry, for `run.json`.

    That log is where the chokepoint reports a break in agami's own code, such as a credentials file
    it cannot parse, and this entry point has no server log behind it. Dropping it, as
    `execute_sql.main` does, hid the break: the row read `failed` and pointed at a log nobody has.
    So each record is kept, but only its fixed message and its error's type. The error's own text
    can carry an absolute path or a value, and a record's arguments can carry the driver's words."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.errors: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        error = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else None
        if error:
            self.errors.append(error)
        print(
            f"check_statement: execute_sql: {record.msg}" + (f" ({error})" if error else ""),
            file=sys.stderr,
        )


_OWN_ERRORS = _OwnErrors()


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
    message = failure.message
    if message == execute_sql.UNEXPECTED_FAILURE_MESSAGE:
        message = _unexpected(_OWN_ERRORS.errors)
    return {
        "status": "failed",
        "exit": execute_sql.FAILURE_KIND_TO_EXIT.get(
            failure.kind, execute_sql._DEFAULT_FAILURE_EXIT
        ),
        "kind": failure.kind,
        "rule": None,
        "detail": message,
        "remediation": message,
    }


def _unexpected(errors: list[str]) -> str:
    """The sentence for a failure the chokepoint could not classify. Its own sentence sends the reader
    to a server log, and there is none on this entry point, so this one says what is known instead:
    the type of the error agami's own code raised, or else that the database's words were dropped."""
    if errors:
        return (
            f"agami's own code raised {errors[-1]} while running the statement. Only the error's "
            "type is kept, because its text can carry a path or a value."
        )
    return (
        "The database failed the statement with an error agami could not classify. Its text is not "
        "kept, because it can quote the statement."
    )


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
    _OWN_ERRORS.errors.clear()
    return execute_sql.execute_guarded(sql, profile, area, executor=execute_sql.BUILTIN_EXECUTOR)


def _stops(run: dict[str, Any]) -> bool:
    """Whether every later row would end the same way, so the whole run stops here."""
    return run["kind"] in _STOP_KINDS or run["rule"] in _STOP_RULES


def _clear(row_dir: Path) -> None:
    """Remove every file an earlier check of this row wrote. See `_OWN_FILES`."""
    for name in _OWN_FILES:
        (row_dir / name).unlink(missing_ok=True)
    for pattern in _OWN_PROBE_FILES:
        for path in row_dir.glob(pattern):
            path.unlink(missing_ok=True)


def _sm(out: Path, *argv: str) -> None:
    """One `sm` verb, in process, its stdout written to `out` as `sm … > out` wrote it.

    In process because the guard above already needs the semantic_model package in this interpreter,
    and `sm` runs this same `cli.main`. A verb that does not answer leaves an empty file, even when
    it printed something first: the ledger reads an empty file as a part that was not checked, and a
    half-written one, or a verb's own error payload, could read as checked. Three ways a verb does
    not answer, all treated alike:

      it raises           — the file would hold half an answer;
      it exits            — argparse ends a wrong call that way, and `SystemExit` would otherwise
                            carry past `except Exception` and end the row mid-check;
      it returns non-zero — several verbs print a JSON error payload and return 2, and `cli.main`
                            returns 3 for a root with no model.

    Neither channel of the verb is relayed: its own stdout can hold the payload, and argparse writes
    a wrong call's arguments to stderr, which would put a path in front of the person. Only the line
    below, which names the verb and how it ended and nothing else."""
    buf = io.StringIO()
    text = ""
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = sm_cli.main(list(argv))
    except (Exception, SystemExit) as exc:
        print(f"check_statement: `sm {argv[0]}` raised {type(exc).__name__}", file=sys.stderr)
    else:
        if code:
            print(f"check_statement: `sm {argv[0]}` returned {code}", file=sys.stderr)
        else:
            text = buf.getvalue()
    out.write_text(text, encoding="utf-8")


def _past_quoted(text: str, start: int) -> tuple[int, bool]:
    """One past the quoted value or identifier opening at `start`, and whether it closed cleanly.

    A doubled quote is an escaped one, as standard SQL writes it. A backslash inside makes the answer
    uncertain: MySQL reads it as escaping the next character and standard SQL does not, so where the
    value ends depends on the engine."""
    quote = text[start]
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            return i, False
        if text[i] == quote:
            if text[i + 1 : i + 2] == quote:
                i += 2
                continue
            return i + 1, True
        i += 1
    return i, False


def _wrap_body(statement: str) -> str:
    """The statement as the zero-row wrap must hold it: up to its last character of code.

    The terminating semicolon and any comment around it are dropped. Wrapped verbatim, a statement
    ending `; -- done` becomes two statements inside the wrap, and the guard refuses the wrap for a
    fault the statement does not have. Step 1 has already proved there is no second statement to
    lose. This is the wrap's copy; the statement itself still runs verbatim at step 4.

    Quoted values are walked over, never read, so a `;` or a `--` inside one is code and stays. When
    the scan cannot say where a value ends — a quote left open, or a backslash inside one — it drops
    the trailing semicolon and no more, as it always did: cutting mid-value would turn a sound
    statement into a syntax error, which reads as the person's own defect."""
    text = statement.strip()
    end = i = 0
    while i < len(text):
        ch = text[i]
        if ch in "'\"`":
            i, closed = _past_quoted(text, i)
            if not closed:
                return text.rstrip(";").rstrip()
            end = i
        elif text.startswith("--", i):
            newline = text.find("\n", i)
            i = len(text) if newline < 0 else newline + 1
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = len(text) if close < 0 else close + 2
        else:
            i += 1
            if not ch.isspace() and ch != ";":
                end = i
    return text[:end]


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
    _clear(row_dir)

    # 1. Read-only first, then no recon, by the guard's own gates in the chokepoint's own order.
    #    Nothing runs after a statement that is not one read-only SELECT, or that reads the server's
    #    own metadata, not even the zero-row wrap around it.
    refusal = sql_guard.check_read_only(statement) or sql_guard.check_no_recon(statement)
    if refusal is not None:
        run = _refused(refusal)
        _write_json(row_dir / "run.json", run)
        return 0, {**summary, "run": run}

    # 2. Does it run at all? See `_wrap_body` for what the wrap drops; the newlines still keep a
    #    comment it left in place from swallowing the wrapper. The wrap is agami's own statement,
    #    not the person's, so its outcome gets its own file rather than `run.json`: a wrap the guard
    #    refuses is this check not run, never a fault in the statement, and the statement is still
    #    checked on its own below. Written whatever happened, so nothing this script runs is
    #    unrecorded.
    body = _wrap_body(statement)
    zero_row = f"SELECT 1 FROM (\n{body}\n) AS _agami_check WHERE 1=0"
    (row_dir / "zero-row.sql").write_text(zero_row, encoding="utf-8")
    zero = _record(_guarded(zero_row, profile, area))
    _write_json(row_dir / "zero-row.run.json", zero)
    if zero["kind"] in _DEFECT_KINDS or _stops(zero):
        _write_json(row_dir / "run.json", zero)
        return (_STOP_RUN if _stops(zero) else 0), {**summary, "run": zero}

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
    if _stops(run):
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

    # The chokepoint logs to two places. Its raw log carries the driver's own words for an operator.
    # On this entry point stderr reaches the session, and those words can quote the statement, so
    # they are dropped exactly as `execute_sql.main` drops them. Its own log reports a break in
    # agami's code, and nothing else would show that break here, so it is kept, value-free.
    raw = logging.getLogger(execute_sql.__name__ + ".raw")
    raw.addHandler(logging.NullHandler())
    raw.propagate = False
    own = logging.getLogger(execute_sql.__name__)
    if _OWN_ERRORS not in own.handlers:
        own.addHandler(_OWN_ERRORS)
    own.propagate = False

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
