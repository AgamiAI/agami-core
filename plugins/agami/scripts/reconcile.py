#!/usr/bin/env python3
"""
Reconciliation helper for the agami-reconcile skill.

The skill drives the LLM (question generation, query execution, narration);
this helper handles the deterministic parts:

- CSV parsing (header / no-header, common dialects)
- Number-string parsing ($4.2M, ₹2.16Cr, "47,238,221.00", "42%", etc.)
- Diff logic with tolerance
- The tolerance band around an observed number, in a golden item's own bounds keys

Stdlib only.

Usage:

    # Parse a CSV and emit a normalized JSON list of {label, expected_value}:
    python3 reconcile.py parse --csv /path/to/dashboard-export.csv

    # Diff two numbers (expected vs actual) with optional tolerance:
    python3 reconcile.py diff --expected 47238221 --actual 47200000 --tolerance 0.01
    #   (--tolerance also takes a percentage: `--tolerance 1%` is the same band)

    # Band an observed number, ready to paste as a golden item's `bounds`:
    python3 reconcile.py band --value 47238221 --tolerance 0.01

    # Read any of the four input shapes a person brings into evidence rows:
    #   (a) questions, (b) questions with the SQL they trust, (c) labels with numbers,
    #   (d) labels with numbers and the SQL behind each. Several files merge by label.
    #   A second file merges by label, so it needs a label column: `label,sql` reads as the SQL
    #   behind each tile; a bare .sql file has no labels and stands as its own rows.
    python3 reconcile.py intake --file tiles.csv --file sql.csv --source "the finance dashboard"
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

# --- Number parsing -------------------------------------------------------

# Currency symbols and their codes that we strip from the front of a number.
CURRENCY_SYMBOLS = ("$", "€", "£", "¥", "₹", "₩", "₽", "₿")

# Magnitude suffixes. Indian numbering (Lakh / Crore) is included because
# dashboards from Indian deployments commonly use it.
SUFFIXES: dict[str, float] = {
    "k": 1_000,
    "K": 1_000,
    "m": 1_000_000,
    "M": 1_000_000,
    "b": 1_000_000_000,
    "B": 1_000_000_000,
    "bn": 1_000_000_000,
    "Bn": 1_000_000_000,
    "BN": 1_000_000_000,
    "L": 100_000,  # Lakh
    "l": 100_000,
    "Cr": 10_000_000,  # Crore
    "cr": 10_000_000,
    "CR": 10_000_000,
}


def parse_value(s: Any) -> float | None:
    """Parse a string-or-number into a float. Returns None if uninterpretable.

    Handles:
      "47238221"        -> 47238221.0
      "47,238,221.00"   -> 47238221.0
      "$4.2M"           -> 4200000.0
      "₹2.16Cr"         -> 21600000.0
      "42%"             -> 0.42  (percent → fraction)
      "12.4%"           -> 0.124
      "  148.95 "       -> 148.95
      "(123.45)"        -> -123.45  (accounting-style negative)
      "n/a", "—", ""    -> None
      None              -> None
    """
    if s is None:
        return None
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return float(s)
    if not isinstance(s, str):
        return None

    raw = s.strip()
    if not raw:
        return None

    # Common null sentinels in dashboards.
    if raw.lower() in {"n/a", "na", "—", "-", "null", "none", "nil"}:
        return None

    is_percent = raw.endswith("%")
    if is_percent:
        raw = raw[:-1].strip()

    # Accounting parens for negatives: (123.45) → -123.45
    is_negative = False
    if raw.startswith("(") and raw.endswith(")"):
        is_negative = True
        raw = raw[1:-1].strip()

    # Strip leading currency symbols + ISO codes (USD / INR / EUR / etc.).
    for sym in CURRENCY_SYMBOLS:
        if raw.startswith(sym):
            raw = raw[len(sym) :].strip()
            break
    iso = re.match(r"^[A-Z]{3}\s+", raw)
    if iso:
        raw = raw[iso.end() :].strip()

    # Look for a magnitude suffix (longest-match: handle "Cr" before "C").
    suffix_multiplier = 1.0
    sorted_suffixes = sorted(SUFFIXES.keys(), key=len, reverse=True)
    for suf in sorted_suffixes:
        if raw.endswith(suf) and len(raw) > len(suf):
            head = raw[: -len(suf)].strip()
            # Only treat as a suffix if what's before is a clean number.
            if (
                re.fullmatch(r"-?\d+(\.\d+)?(\s*[,\s]\s*\d{3})*", head)
                or re.fullmatch(r"-?\d+(?:[,_]\d{3})*(?:\.\d+)?", head)
                or re.fullmatch(r"-?\d+(?:\.\d+)?", head)
            ):
                raw = head
                suffix_multiplier = SUFFIXES[suf]
                break

    # Strip thousands separators (commas, underscores, NBSP, regular spaces).
    raw = re.sub(r"[,_ ]", "", raw)
    raw = raw.replace(" ", "")

    try:
        n = float(raw)
    except ValueError:
        return None

    n *= suffix_multiplier
    if is_negative:
        n = -n
    if is_percent:
        n /= 100.0

    return n


# --- CSV parsing ----------------------------------------------------------


# Heuristic header detection. The first cell is the label and is *always*
# non-numeric (the metric name). The second cell is the value: if it parses
# as a number, the row is data; if not, the row is a header (with column names
# like "Value" or "Amount").
def _looks_like_header(row: list[str]) -> bool:
    if not row or len(row) < 2:
        return False
    return parse_value(row[1]) is None


def parse_csv(path: str) -> list[dict]:
    """Parse a reconciliation CSV. Returns list of {label, expected_value, raw_value}.

    Accepts:
      - 2 columns (label, value) — most common
      - 3+ columns: first is label, second is value, the rest are appended to
        label as `(extra1, extra2)` for context.
      - With or without a header row (auto-detected).

    Skips rows where the value can't be parsed as a number; emits them with
    `expected_value: null` so the SKILL can surface them to the user.
    """
    rows: list[dict] = []
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"CSV not found: {p}")

    with p.open(newline="") as f:
        reader = csv.reader(f)
        all_rows = [r for r in reader if r and any(c.strip() for c in r)]

    if not all_rows:
        return rows

    # Header detection on the first non-empty row.
    has_header = _looks_like_header(all_rows[0])
    data_rows = all_rows[1:] if has_header else all_rows

    for r in data_rows:
        if len(r) < 2:
            continue
        label = r[0].strip()
        raw_value = r[1].strip()
        extras = [c.strip() for c in r[2:] if c.strip()]
        if extras:
            label = f"{label} ({', '.join(extras)})"
        rows.append(
            {
                "label": label,
                "expected_value": parse_value(raw_value),
                "raw_value": raw_value,
            }
        )

    return rows


# --- Diff -----------------------------------------------------------------


def diff(
    expected: float | None,
    actual: float | None,
    *,
    tolerance: float = 0.01,
) -> dict:
    """Compare expected vs actual. `tolerance` is fractional (0.01 = ±1%).

    Returns:
      {
        "match":      bool,
        "delta":      float (actual - expected) or None,
        "delta_pct":  float ((actual - expected) / expected) or None,
        "reason":     "match" | "mismatch" | "missing_expected" | "missing_actual"
      }
    """
    if expected is None:
        return {"match": False, "delta": None, "delta_pct": None, "reason": "missing_expected"}
    if actual is None:
        return {"match": False, "delta": None, "delta_pct": None, "reason": "missing_actual"}

    delta = actual - expected
    delta_pct: float | None = None
    if expected != 0:
        delta_pct = delta / expected
        match = abs(delta_pct) <= tolerance
    else:
        # Expected is exactly 0 — exact match required (no relative tolerance possible).
        match = actual == 0

    return {
        "match": match,
        "delta": delta,
        "delta_pct": delta_pct,
        "reason": "match" if match else "mismatch",
    }


# --- Band -----------------------------------------------------------------


def band(value: float, *, tolerance: float = 0.01) -> dict:
    """The band a single observed number is allowed to land in, as `GoldenBounds` keys.

    `tolerance` is fractional (0.01 = ±1%), the same shape and default `diff` uses. The four
    keys are exactly the ones `semantic_model.golden.GoldenBounds` accepts, so a caller pastes
    the output into an item and does no arithmetic of its own.

    Returns:
      {"min_rows": 1, "max_rows": 1, "min_value": <float>, "max_value": <float>}
    """
    low, high = value * (1 - tolerance), value * (1 + tolerance)
    # A negative observation inverts the two — and GoldenBounds refuses a floor above its
    # ceiling, so the band would be rejected rather than merely read oddly. A zero value falls
    # out of this as a zero band, which matches diff's rule that a zero expected is only ever
    # matched exactly: there is no relative tolerance around nothing.
    return {
        "min_rows": 1,
        "max_rows": 1,
        "min_value": min(low, high),
        "max_value": max(low, high),
    }


# --- Intake ---------------------------------------------------------------
#
# Any input a person brings reduces to rows of three optional fields: the question, the
# statement, the expected number. The four shapes the skill names are subsets of that row:
#   (a) questions only            (b) questions with the SQL the person trusts
#   (c) labels with numbers       (d) labels with numbers and the SQL behind each tile
# `intake` reads all four and says which it saw. The number path is untouched: a two-column
# CSV still goes through `parse_csv`, and a third column that is not SQL is still glued onto
# the label as context, exactly as `parse` always did.

_STATEMENT_RE = re.compile(r"^\s*(with|select)\b", re.IGNORECASE)

# Header names a person is likely to type, folded to the field each stands for. A header is
# recognised by NAME rather than by position so a `question,sql` file and a `label,value,sql` file
# both read the way they were written.
_HEADER_FIELDS: dict[str, str] = {
    "label": "label", "metric": "label", "tile": "label", "name": "label", "kpi": "label",
    "value": "value", "expected": "value", "expected_value": "value", "number": "value",
    "amount": "value", "actual": "value",
    "sql": "statement", "statement": "statement", "query": "statement",
    "question": "question", "prompt": "question",
}

def _fold(text: str) -> str:
    """Case and whitespace fold, the only normalization a label match is allowed."""
    return re.sub(r"\s+", " ", text.strip()).lower()


def _is_statement(cell: str | None) -> bool:
    return bool(cell) and _STATEMENT_RE.match(cell) is not None


def _row_shape(row: dict) -> str:
    has_statement = row["statement"] is not None
    has_expected = row["expected"] is not None
    if has_statement and has_expected:
        return "d"
    if has_statement:
        return "b"
    if has_expected:
        return "c"
    return "a"


def _new_row(*, file: str, line: int, source: str | None, label: str | None = None,
             question: str | None = None, statement: str | None = None,
             raw_value: str | None = None) -> dict:
    row = {
        "label": label or None,
        "question": question or None,
        "statement": statement.strip().rstrip(";").strip() if statement else None,
        "expected": parse_value(raw_value) if raw_value is not None else None,
        "raw_value": raw_value if raw_value not in (None, "") else None,
        "provenance": {"shape": None, "source": source, "file": file, "line": line, "graded": None},
    }
    row["provenance"]["shape"] = _row_shape(row)
    return row


def _header_map(first: list[str], rest: list[list[str]]) -> dict[int, str] | None:
    """Which field each column holds, when the first row is a header; None when it is data.

    Two ways a row is a header. Every cell names a field this module knows, which is how a
    `question,sql` or `label,value,sql` file declares itself. Or, the legacy two-column case
    `parse_csv` has always handled: a second cell that is neither a number nor a statement, over a
    file whose later rows do carry numbers there.
    """
    cells = [c.strip() for c in first]
    if cells and all(_fold(c) in _HEADER_FIELDS for c in cells if c):
        return {i: _HEADER_FIELDS[_fold(c)] for i, c in enumerate(cells) if c}
    if (len(cells) >= 2 and parse_value(cells[1]) is None and not _is_statement(cells[1])
            and any(len(r) >= 2 and parse_value(r[1]) is not None for r in rest)):
        fields = {0: "label", 1: "value"}
        for i in range(2, len(cells)):
            fields[i] = "statement" if _fold(cells[i]) in ("sql", "statement", "query") else "extra"
        return fields
    return None


def _row_from_named(cells: list[str], fields: dict[int, str], *, file: str, line: int,
                    source: str | None) -> tuple[dict | None, str | None]:
    got: dict[str, str] = {}
    extras: list[str] = []
    for i, cell in enumerate(cells):
        cell = cell.strip()
        if not cell:
            continue
        field = fields.get(i, "extra")
        if field == "extra":
            extras.append(cell)
        elif field == "statement" and not _is_statement(cell):
            # A `sql` column holding something that is not a statement is context, not SQL.
            extras.append(cell)
        else:
            got[field] = cell
    label = got.get("label")
    if label and extras:
        label = f"{label} ({', '.join(extras)})"
    raw = got.get("value")
    if raw is not None and parse_value(raw) is None:
        return None, f"the value {raw!r} could not be read as a number"
    if not any(k in got for k in ("label", "question", "statement", "value")):
        return None, "no question, statement or number in the row"
    return _new_row(file=file, line=line, source=source, label=label,
                    question=got.get("question"), statement=got.get("statement"),
                    raw_value=raw), None


def _row_from_positional(cells: list[str], *, file: str, line: int,
                         source: str | None) -> tuple[dict | None, str | None]:
    """A data row with no header to name its columns, read by shape."""
    cells = [c.strip() for c in cells]
    if len(cells) == 1:
        text = cells[0]
        if _is_statement(text):
            return _new_row(file=file, line=line, source=source, statement=text), None
        return _new_row(file=file, line=line, source=source, question=text), None
    first, second, rest = cells[0], cells[1], cells[2:]
    if _is_statement(second):
        return _new_row(file=file, line=line, source=source, question=first, statement=second), None
    if parse_value(second) is None:
        return None, f"the second column {second!r} is neither a number nor a statement"
    statement = None
    extras = []
    for cell in rest:
        if _is_statement(cell) and statement is None:
            statement = cell
        elif cell:
            extras.append(cell)
    label = f"{first} ({', '.join(extras)})" if extras else first
    return _new_row(file=file, line=line, source=source, label=label, statement=statement,
                    raw_value=second), None


def _rows_from_json(items: Any, *, file: str, source: str | None) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    skipped: list[dict] = []
    if not isinstance(items, list):
        return rows, [{"file": file, "line": 1, "reason": "a JSON input must be a list"}]
    for n, item in enumerate(items, 1):
        if isinstance(item, str):
            row, why = _row_from_positional([item], file=file, line=n, source=source)
        elif isinstance(item, dict):
            cells: list[str] = []
            fields: dict[int, str] = {}
            for key, value in item.items():
                field = _HEADER_FIELDS.get(_fold(str(key)))
                if field is None or value is None:
                    continue
                fields[len(cells)] = field
                cells.append(str(value))
            row, why = _row_from_named(cells, fields, file=file, line=n, source=source)
        else:
            row, why = None, "an item must be a string or an object"
        if row is None:
            skipped.append({"file": file, "line": n, "reason": why})
        else:
            rows.append(row)
    return rows, skipped


def _rows_from_file(path: Path, source: str | None) -> tuple[list[dict], list[dict]]:
    """One file's rows and the lines it could not use. The extension decides how lines are cut:
    `.json` is a list, `.sql` is statements split on `;`, `.txt` and `.md` are one question per
    line, and everything else is CSV."""
    file = path.name
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _rows_from_json(json.loads(text), file=file, source=source)
    if suffix == ".sql":
        rows, skipped = [], []
        for n, stmt in enumerate((s for s in text.split(";") if s.strip()), 1):
            # The same test a CSV cell gets: anything that is not a SELECT or a WITH is context or
            # a mistake, and never reaches the tier as a statement the person supplied.
            if _is_statement(stmt):
                rows.append(_new_row(file=file, line=n, source=source, statement=stmt.strip()))
            else:
                skipped.append({"file": file, "line": n, "text": stmt.strip()[:80],
                                "reason": "not a SELECT or WITH statement"})
        return rows, skipped
    if suffix in (".txt", ".md") or ("," not in text and "\t" not in text):
        rows = []
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip():
                rows.append(_row_from_positional([line], file=file, line=n, source=source)[0])
        return rows, []
    with path.open(newline="", encoding="utf-8") as fh:
        numbered = [(n, r) for n, r in enumerate(csv.reader(fh), 1) if r and any(c.strip() for c in r)]
    if not numbered:
        return [], []
    fields = _header_map(numbered[0][1], [r for _n, r in numbered[1:]])
    data = numbered[1:] if fields is not None else numbered
    rows, skipped = [], []
    for n, cells in data:
        if fields is not None:
            row, why = _row_from_named(cells, fields, file=file, line=n, source=source)
        else:
            row, why = _row_from_positional(cells, file=file, line=n, source=source)
        if row is None:
            skipped.append({"file": file, "line": n, "reason": why})
        else:
            rows.append(row)
    return rows, skipped


def _merge_by_label(rows: list[dict]) -> list[dict]:
    """A statement whose label matches a tile's label joins that tile's row; anything unmatched
    keeps its own row. Matching is the fold only, so `q3 revenue` meets `Q3 Revenue` and nothing
    looser does."""
    tiles: dict[str, dict] = {}
    for row in rows:
        if row["expected"] is not None and row["statement"] is None and row["label"]:
            tiles.setdefault(_fold(row["label"]), row)
    merged: list[dict] = []
    for row in rows:
        key = _fold(row["label"] or row["question"] or "")
        if (row["statement"] is not None and row["expected"] is None and key in tiles
                and tiles[key]["statement"] is None):
            tile = tiles[key]
            tile["statement"] = row["statement"]
            tile["provenance"]["shape"] = _row_shape(tile)
            tile["provenance"]["merged_from"] = {"file": row["provenance"]["file"],
                                                 "line": row["provenance"]["line"]}
            continue
        merged.append(row)
    return merged


def intake(paths: list[Path], *, source: str | None = None) -> dict:
    """Every file's rows, merged by label across files, with the shape that was seen.

    `shape` is one letter when every row has the same shape and `mixed` otherwise; each row also
    carries its own under `provenance.shape`, which is what the skill reads row by row.
    """
    rows: list[dict] = []
    skipped: list[dict] = []
    for path in paths:
        got, missed = _rows_from_file(Path(path).expanduser(), source)
        rows.extend(got)
        skipped.extend(missed)
    rows = _merge_by_label(rows)
    shapes = {row["provenance"]["shape"] for row in rows}
    shape = next(iter(shapes)) if len(shapes) == 1 else ("mixed" if shapes else None)
    return {"shape": shape, "rows": rows, "skipped": skipped}


# --- Ledger ---------------------------------------------------------------
#
# One grade per part of a statement the person supplied, read from fixed filenames in the row's
# directory: what happened when it ran (`run.json`), what `sm prepare` and `sm receipt` said about
# it, what `sm join-probes` and `sm filter-values judge` reported, and the probe CSVs the execution
# tier returned. Four grades, and only measurement can earn `model_gap`:
#   confirmed     the statement and the semantic model agree, and the data backs it
#   model_gap     the data proves the statement right where the semantic model is missing or wrong
#   query_defect  the data proves the statement wrong
#   unresolved    the part could not be checked, and the note says why
# The rules have a dependency in them, and it is applied rather than assumed: a join that could not
# be graded leaves the fan-out check on its aggregate `unresolved`, said out loud, never clean.

CONFIRMED = "confirmed"
MODEL_GAP = "model_gap"
QUERY_DEFECT = "query_defect"
UNRESOLVED = "unresolved"
# A fifth word that is not a grade: a fact the run states and never judges (rows an inner join
# dropped, a wide column nobody would list). Ranked below `confirmed` so it never decides a row's
# verdict, never blocks an example, and is rendered in its own block.
NOTED = "noted"
_VERDICT_RANK = {QUERY_DEFECT: 3, UNRESOLVED: 2, MODEL_GAP: 1, CONFIRMED: 0, NOTED: -1}

# Error-classifier kinds that mean the statement itself is wrong, as opposed to the connection.
_STATEMENT_DEFECT_KINDS = {"column_not_found", "table_not_found", "syntax"}
# Guard rules that mean the statement wanted something the semantic model does not expose.
_SCOPE_RULES = {"table_scope", "column_scope"}
# Pre-flight risks that describe how the aggregate itself was written, not how a join fanned it.
_AGGREGATION_RISKS = {"bad_aggregation", "semi_additive"}


def _part(part: str, verdict: str, *, kind: str | None = None, depends_on=(),
          evidence: dict | None = None, note: str = "") -> dict:
    return {"part": part, "verdict": verdict, "kind": kind, "depends_on": list(depends_on),
            "evidence": evidence or {}, "note": note}


def _load_json(path: Path) -> Any:
    """The JSON in `path`; None when the file is absent; `{"error": ...}` when it is empty or is not
    JSON. A verb that crashed leaves a zero-byte redirect behind, and that must read as "this input
    is unusable", never as "checked and clean"."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {"error": "empty_file"}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        return {"error": "unreadable_json", "detail": str(exc).splitlines()[0]}


def _usable(payload: Any, key: str) -> "tuple[dict | None, str | None]":
    """The payload when it carries `key`, else None and why: absent, empty, an error object from a verb
    that exited non-zero, or JSON of another shape."""
    if payload is None:
        return None, "was not written"
    if not isinstance(payload, dict):
        return None, "is not a JSON object"
    if payload.get("error"):
        return None, f"carries an error ({payload['error']})"
    if key not in payload:
        return None, f"has no `{key}` key"
    return payload, None


def _probe_csv(path: Path) -> "list[dict] | str | None":
    """A probe's CSV as rows; None when the file is absent; the string `failed` when it is empty.

    The execution tier writes CSV to stdout only on success. A probe that was refused or failed
    leaves a zero-byte file behind, and reading that as "the column holds no values" would turn a
    failed probe into a definite grade. A header-only file is the legitimately empty result.
    """
    if not path.exists():
        return None
    if path.stat().st_size == 0:
        return "failed"
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _first_number(rows, key: str) -> float | None:
    """The first row's `key` column as a number. Headers are matched without regard to case, because
    one tier upper-cases them; the fall-back to the only column is for a one-column result and never
    for a wider one, where it would read the wrong column."""
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    raw = next((v for k, v in row.items() if k and k.strip().lower() == key.lower()), None)
    if raw is None and len(row) == 1:
        raw = next(iter(row.values()))
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _grade_question_fit(fit: Any, ran: bool) -> list[dict]:
    """The one part graded by reading rather than measuring: the skill's Phase 1.5g judgment of
    whether the statement answers the question it came with, written to `question_fit.json`. It can
    withhold a row from the keep-offer and never proves anything about the semantic model. Expected
    for every statement row after a run that succeeded, so a check that was never made is an open
    part and not a silent pass."""
    got, why = _usable(fit, "fit")
    if got is None:
        if not ran:
            return []
        return [_part("question_fit", UNRESOLVED, evidence={"file": "question_fit.json", "problem": why},
                      note=f"question_fit.json {why}, so the fit of the statement to its question was not checked")]
    word, reason = got.get("fit"), got.get("reason")
    if word == "no_question":
        return []
    if word == "plausible":
        return [_part("question_fit", CONFIRMED, evidence={"fit": word, "reason": reason},
                      note="the statement plausibly answers the question, by reading; a judgment, not a measurement")]
    if word == "doubtful":
        return [_part("question_fit", UNRESOLVED, evidence={"fit": word, "reason": reason},
                      note=f"the statement may not answer the question: {reason or 'no reason was given'}; "
                           "reword the question or the statement and re-run this row")]
    return [_part("question_fit", UNRESOLVED, evidence={"fit": word},
                  note=f"question_fit.json carries an unknown fit {word!r}, so the fit was not checked")]


def _grade_run(run: dict | None) -> list[dict]:
    if run is None:
        return [_part("runs", UNRESOLVED, note="no run record was found for the statement")]
    status, rule, kind = run.get("status"), run.get("rule"), run.get("kind")
    if status == "ok":
        return [_part("runs", CONFIRMED, note="the statement ran"),
                _part("scope", CONFIRMED, note="every table and column it named is in the semantic model")]
    if status == "refused":
        if rule in _SCOPE_RULES:
            return [
                _part("runs", UNRESOLVED,
                      note=f"the statement was refused before it ran ({rule}); see the scope part"),
                _part("scope", MODEL_GAP, kind="scope",
                      evidence={"rule": rule, "detail": run.get("detail")},
                      note="the statement names a table or column the semantic model does not expose"),
            ]
        if rule == "select_star":
            return [_part("runs", QUERY_DEFECT, evidence={"rule": rule},
                          note="SELECT * is refused; name the columns")]
        return [_part("runs", UNRESOLVED, evidence={"rule": rule},
                      note=f"the statement was refused before it ran ({rule})")]
    if status == "failed":
        if kind in _STATEMENT_DEFECT_KINDS:
            return [_part("runs", QUERY_DEFECT, evidence={"kind": kind, "remediation": run.get("remediation")},
                          note=f"the database rejected the statement ({kind})")]
        return [_part("runs", UNRESOLVED, evidence={"kind": kind},
                      note=f"the run failed with {kind}; the statement could not be checked")]
    return [_part("runs", UNRESOLVED, note="the statement was not run")]


def _join_tables(join: dict) -> tuple[str, str]:
    """The two tables a join is between, sorted: from its one written pair when it has one, and
    from its endpoint labels otherwise."""
    pairs = join.get("pairs") or []
    if len(pairs) == 1 and len(pairs[0]) == 2:
        a, b = pairs[0][0][0], pairs[0][1][0]
    else:
        a, b = (join.get("endpoints") or ["", ""])[:2]
        a, b = _fold(a), _fold(b)
    first, second = sorted((a, b))
    return first, second


def _join_status(join: dict) -> str:
    """The status `sm join-probes` gave the join; one it did not label stays open."""
    return join.get("status") or "undetermined"


def _cardinality_result(probes: dict | None, key: str, row_dir: Path) -> dict | None:
    """One endpoint's uniqueness: from the semantic model when it declares the column a key, else
    from the column's cardinality CSV, which is shared by every join that reads that column."""
    if ((probes or {}).get("unique_by_model") or {}).get(key):
        return {"unique": True, "source": "the semantic model declares the column a key"}
    got = _probe_csv(row_dir / f"cardinality.{key}.csv")
    if not isinstance(got, list) or not got:
        return None
    total = _first_number(got, "total")
    distinct = _first_number(got, "distinct_count")
    nulls = _first_number(got, "null_count") or 0.0
    if total is None or distinct is None:
        return None
    return {"total": total, "distinct": distinct, "nulls": nulls,
            "unique": distinct == total - nulls, "source": "probe"}


def _grade_joins(probes: dict | None, row_dir: Path) -> list[dict]:
    rows: list[dict] = []
    if probes is not None and probes.get("unreadable"):
        return [_part("join:*", UNRESOLVED, evidence={"unreadable": probes["unreadable"]},
                      note="the join verb could not read the statement, so no join was checked")]
    seen: dict[str, int] = {}
    for join in (probes or {}).get("joins", []):
        a, b = _join_tables(join)
        label = f"{a}-{b}"
        # Two joins between the same two tables in one statement are two parts, not one: keyed by
        # the same label, the second would silently overwrite the first's grade.
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = f"{label}#{seen[label]}"
        jid = join.get("id", "join")
        status = _join_status(join)
        planned = join.get("probes") or {}
        declared_pairs = join.get("declared_pairs", [])
        written = {"pairs": join.get("pairs"), "predicate": join.get("predicate")}

        overlaps: list[float | None] = []
        overlap_failed = False
        for i, _probe in enumerate(planned.get("overlap", [])):
            got = _probe_csv(row_dir / f"{jid}.overlap.{i}.csv")
            if got == "failed":
                overlap_failed = True
            elif got is not None:
                overlaps.append(_first_number(got, "matched"))
        card = {key: result for key in planned.get("cardinality", [])
                if (result := _cardinality_result(probes, key, row_dir)) is not None}
        hits = [m for m in overlaps if m is not None]
        any_overlap = any(m > 0 for m in hits)
        one_row_on_right = _one_row_on_right(join, probes)

        if status in ("undeclarable", "undetermined"):
            rows.append(_part(f"join:{label}", UNRESOLVED, evidence=written,
                              note=join.get("not_probed_because")
                              or "the join could not be resolved to two declared tables"))
            continue
        if status == "declared":
            rows.append(_part(f"join:{label}", CONFIRMED, evidence={"declared_pairs": declared_pairs},
                              note="the join is on the key the semantic model declares"))
        elif status == "wrong_key":
            rows.append(_part(f"join:{label}", QUERY_DEFECT,
                              evidence={"declared_pairs": declared_pairs, **written},
                              note="the join is on a different key than the one the semantic model declares"))
        elif join.get("too_big_to_probe") or not planned.get("overlap"):
            rows.append(_part(f"join:{label}", UNRESOLVED, evidence=written,
                              note="the join is not declared and no probe could be run: "
                                   + (join.get("not_probed_because") or "no probe was planned")))
        elif any_overlap:
            rows.append(_part(f"join:{label}", MODEL_GAP, kind="relationship",
                              evidence={"overlap": hits, **written},
                              note="the join is not declared, and its keys resolve in the data"))
        elif hits and not overlap_failed:
            rows.append(_part(f"join:{label}", QUERY_DEFECT, evidence={"overlap": hits, **written},
                              note="the join is not declared, and its keys never meet in the data"))
        else:
            # No hit, or a hit beside a probe that failed: half the evidence is not evidence.
            rows.append(_part(f"join:{label}", UNRESOLVED, evidence={"overlap": hits, **written},
                              note="the join is not declared and "
                                   + ("a probe file is empty, so that probe likely failed; the rest is not enough to decide"
                                      if overlap_failed else "no probe result was supplied")))
        # Whether this join brings in one row at most per row of the table it joins to, by the
        # semantic model's own word. The aggregate grader reads it; nothing is re-derived there.
        rows[-1]["evidence"]["one_row_on_right"] = one_row_on_right

        # The probe rows: whenever probes were planned or answered. A declared join plans none.
        if hits or planned.get("overlap"):
            if any_overlap:
                rows.append(_part(f"join_key:{label}", CONFIRMED, evidence={"overlap": hits},
                                  note="sampled keys from one side exist on the other"))
            elif hits and not overlap_failed:
                rows.append(_part(f"join_key:{label}", QUERY_DEFECT, evidence={"overlap": hits},
                                  note="no sampled key from either side exists on the other"))
            else:
                rows.append(_part(f"join_key:{label}", UNRESOLVED, evidence={"overlap": hits},
                                  note="an overlap probe result is missing or its file is empty"))
        if card or planned.get("cardinality"):
            uniques = sorted(k for k, v in card.items() if v["unique"])
            if len(card) >= 2 and uniques:
                rows.append(_part(f"cardinality:{label}", CONFIRMED,
                                  evidence={"one_side": uniques[0], "sides": card},
                                  note=f"{uniques[0]} is unique, so the join does not multiply rows"))
            elif len(card) >= 2:
                rows.append(_part(f"cardinality:{label}", QUERY_DEFECT, evidence={"sides": card},
                                  note="both sides repeat, so the join multiplies rows"))
            else:
                rows.append(_part(f"cardinality:{label}", UNRESOLVED, evidence={"sides": card},
                                  note="no cardinality result for both sides"))
        rows.extend(_dropped_rows(join, label, jid, row_dir))
    return rows


def _one_row_on_right(join: dict, probes: dict | None) -> bool:
    """True when the table this join introduces (its right endpoint) contributes one row at most per
    row already there: it is the one side of the declared relationship the statement actually wrote,
    or its written column is unique by the semantic model. False for a self-join and for anything
    the model did not say. Sound for a chain, because each such join leaves the row count alone."""
    endpoints = join.get("endpoints") or ["", ""]
    left_key, right_key = _fold(str(endpoints[0])), _fold(str(endpoints[-1]))
    if not right_key or left_key == right_key:
        return False
    matched_one_sides = {_fold(str(side)) for edge in (join.get("declared_cardinality") or [])
                         if edge.get("matched") for side in (edge.get("one_side") or [])}
    if right_key in matched_one_sides:
        return True
    pairs = join.get("pairs") or []
    if len(pairs) == 1 and len(pairs[0]) == 2:
        unique = (probes or {}).get("unique_by_model") or {}
        for table, column in pairs[0]:
            if _fold(str(table)) == right_key and unique.get(f"{table}.{column}"):
                return True
    return False


def _dropped_rows(join: dict, label: str, jid: str, row_dir: Path) -> list[dict]:
    """The rows an inner join left behind, said and never judged. No probe planned → nothing to say."""
    probe = join.get("dropped_rows_probe")
    if not probe:
        return []
    left_t, right_t = probe.get("left"), probe.get("right")
    got = _probe_csv(row_dir / f"{jid}.dropped_rows.csv")
    total = _first_number(got, "total") if isinstance(got, list) and got else None
    dropped = _first_number(got, "dropped") if isinstance(got, list) and got else None
    if total is None or dropped is None:
        return [_part(f"dropped_rows:{label}", NOTED, evidence={"left": left_t, "right": right_t},
                      note="the dropped-rows probe was not run or failed; nothing is claimed")]
    t, d = int(total), int(dropped)
    said = (f"no {left_t} row is dropped by this join" if d == 0
            else f"{d} of {t} {left_t} rows have no {right_t} partner and are dropped by this inner join")
    return [_part(f"dropped_rows:{label}", NOTED,
                  evidence={"total": t, "dropped": d, "left": left_t, "right": right_t},
                  note=said + "; counted over the whole table, before the statement's own filters")]


def _joins_named(label: str, join_rows: list[dict]) -> list[str]:
    """The `join:` parts whose two tables both appear in a pre-flight join label."""
    words = set(re.findall(r"[a-z0-9_]+", _fold(label)))
    out = []
    for row in join_rows:
        if not row["part"].startswith("join:"):
            continue
        a, b = row["part"][len("join:"):].split("#", 1)[0].split("-", 1)
        if a in words and b in words:
            out.append(row["part"])
    return out


def _grade_aggregates(prepare: dict | None, join_rows: list[dict], probes: dict | None = None,
                      no_joins_written: bool = False) -> list[dict]:
    """`no_joins_written` is settled by `sm join-probes` having read the statement and counted zero
    joins written: a statement that writes no join has nothing that can multiply its aggregates,
    however the pre-flight labelled them, so an `undetermined` there is confirmed rather than left
    open. A verb that could not read the statement settles nothing. `probes` is the same file, read
    for the sibling rule: every join written brings in one row at most."""
    if prepare is None:
        return []
    if prepare.get("unchecked"):
        return [_part("fan_out:*", UNRESOLVED, evidence={"unchecked": prepare["unchecked"]},
                      note=f"the pre-flight did not run: {prepare['unchecked']}")]
    rows: list[dict] = []
    by_part = {row["part"]: row for row in join_rows}
    # Every join the statement wrote, not the aggregate's own `joins` list: the pre-flight fills
    # that list from multiplying findings only, so it is empty for exactly the aggregate this rule
    # is for. The rule needs every written join listed (none dropped at the cap, the verb having
    # read the statement), every one confirmed, and every one bringing in one row at most.
    join_parts = [row for row in join_rows if row["part"].startswith("join:") and row["part"] != "join:*"]
    one_row_joins = (
        isinstance(probes, dict) and probes.get("unreadable") is None
        and probes.get("dropped") == 0 and probes.get("joins_written") == len(probes.get("joins") or [])
        and bool(join_parts)
        and all(row["verdict"] == CONFIRMED and row["evidence"].get("one_row_on_right") for row in join_parts)
    )
    for agg in prepare.get("aggregates", []):
        text = agg.get("aggregate", "?")
        risks = {f.get("risk") for f in agg.get("findings", [])}
        deps = sorted({p for label in agg.get("joins", []) for p in _joins_named(label, join_rows)})
        weak = [p for p in deps if by_part[p]["verdict"] != CONFIRMED]
        if weak:
            rows.append(_part(f"fan_out:{text}", UNRESOLVED, depends_on=deps,
                              note=f"the join {weak[0][len('join:'):]} this total depends on is "
                                   f"{by_part[weak[0]]['verdict']}, so the fan-out check has no "
                                   "cardinality to reason from"))
        elif agg.get("status") == "multiplied":
            if risks and risks <= {"fan_out_invariant"}:
                rows.append(_part(f"fan_out:{text}", CONFIRMED, depends_on=deps,
                                  note="a join multiplies the rows, but this aggregate cannot move"))
            else:
                named = sorted(risks - {"fan_out_invariant"}) or ["multiplied"]
                rows.append(_part(f"fan_out:{text}", QUERY_DEFECT, depends_on=deps,
                                  evidence={"risks": named, "joins": agg.get("joins", [])},
                                  note=f"a join multiplies the rows this total is computed from "
                                       f"({', '.join(named)})"))
        elif agg.get("status") == "not_multiplied":
            rows.append(_part(f"fan_out:{text}", CONFIRMED, depends_on=deps,
                              note="no join multiplies the rows behind this aggregate"))
        elif no_joins_written:
            rows.append(_part(f"fan_out:{text}", CONFIRMED, depends_on=deps,
                              note="the statement writes no join, so nothing multiplies this aggregate"))
        elif one_row_joins:
            rows.append(_part(f"fan_out:{text}", CONFIRMED, depends_on=[row["part"] for row in join_parts],
                              evidence={"joins": [row["part"] for row in join_parts]},
                              note="every join the statement writes brings in one row at most, so no join "
                                   "multiplies this aggregate"))
        else:
            # The pre-flight names the blindness it hit; the note repeats it rather than blaming a join.
            reason = agg.get("reason") or "no reason was given"
            rows.append(_part(f"fan_out:{text}", UNRESOLVED, depends_on=deps, evidence={"reason": agg.get("reason")},
                              note=f"the pre-flight could not bind this aggregate to one table: {reason}"))
        bad = sorted(risks & _AGGREGATION_RISKS)
        if bad:
            rows.append(_part(f"aggregation:{text}", QUERY_DEFECT, evidence={"risks": bad},
                              note=f"the aggregate is not legal over this column ({', '.join(bad)})"))
        else:
            rows.append(_part(f"aggregation:{text}", CONFIRMED, note="the aggregate is legal over its column"))
    return rows


def _grade_filters(receipt: dict | None) -> list[dict]:
    rows: list[dict] = []
    for item in ((receipt or {}).get("tables") or {}).get("items", []):
        table = item.get("ref") or item.get("qname") or "?"
        for flt in item.get("filters", []) or []:
            part = f"default_filter:{table}:{flt.get('expr')}"
            status = flt.get("status")
            if status == "applied":
                rows.append(_part(part, CONFIRMED, note="the declared filter is applied"))
            elif status == "omitted":
                rows.append(_part(part, MODEL_GAP, kind="filter", evidence={"table": table, "expr": flt.get("expr")},
                                  note="the semantic model declares this filter and the statement does not apply it"))
            else:
                rows.append(_part(part, UNRESOLVED, note="whether the declared filter is applied could not be read"))
    return rows


def _grade_metrics(receipt: dict | None, prepare: dict | None) -> list[dict]:
    rows: list[dict] = []
    aggregates = [_fold(a.get("aggregate", "")) for a in (prepare or {}).get("aggregates", [])]
    only_bare_counts = bool(aggregates) and all(a == "count(*)" for a in aggregates)
    for item in ((receipt or {}).get("columns") or {}).get("items", []):
        if item.get("kind") != "output":
            continue
        column = item.get("column", "?")
        status = item.get("status")
        if status == "matched":
            sources = [str(t) for t in (item.get("source_tables") or [])]
            read = _tables_read(receipt)
            if sources and not (set(sources) & read):
                # The receipt matched by shape: the qualifiers were stripped to compare, so a
                # `SUM(amount)` on one table equals a metric's `SUM(amount)` on another.
                rows.append(_part(f"metric:{column}", UNRESOLVED,
                                  evidence={"metric": item.get("name"), "source_tables": sources,
                                            "tables_read": sorted(read)},
                                  note=f"matched the metric {item.get('name')!r}, defined on "
                                       f"{', '.join(sources)}, which this statement does not read; "
                                       "the expression matches by shape only"))
            else:
                rows.append(_part(f"metric:{column}", CONFIRMED, evidence={"metric": item.get("name")},
                                  note="the output matches a defined metric"))
        elif only_bare_counts:
            rows.append(_part(f"metric:{column}", CONFIRMED,
                              note="a bare count matches no metric by design"))
        elif status == "unmatched":
            rows.append(_part(f"metric:{column}", MODEL_GAP, kind="metric", evidence={"column": column},
                              note="the output matches no metric the semantic model defines"))
        else:
            # `undetermined` is the receipt saying it could not tell (an ambiguous binding, a column
            # behind a CTE, a declaration it could not read). A failure to read is never a gap.
            rows.append(_part(f"metric:{column}", UNRESOLVED, evidence={"column": column, "status": status},
                              note="whether the output matches a metric could not be read"))
    return rows


def _tables_read(receipt: dict | None) -> set[str]:
    """The bare, folded names of every table the statement read, from the receipt's `tables` items."""
    out: set[str] = set()
    for item in ((receipt or {}).get("tables") or {}).get("items", []):
        for name in (item.get("qname"), item.get("ref")):
            if name:
                out.add(_fold(str(name)).split(".")[-1])
    return out


def _grade_literals(judge: dict | None) -> list[dict]:
    rows: list[dict] = []
    if judge is not None and judge.get("unreadable"):
        return [_part("literal:*", UNRESOLVED, evidence={"unreadable": judge["unreadable"]},
                      note="the filter-values verb could not read the statement, so no value was checked")]
    for lit in (judge or {}).get("literals", []):
        part = f"literal:{lit.get('table')}.{lit.get('column')}={lit.get('literal')}"
        verdict = lit.get("verdict", UNRESOLVED)
        rows.append(_part(part, verdict, kind="description" if verdict == MODEL_GAP else None,
                          evidence={"tier": lit.get("tier"), "op": lit.get("op"),
                                    "near_miss": lit.get("near_miss"), "observed": lit.get("observed"),
                                    "rows_with_value": lit.get("rows_with_value")},
                          note=lit.get("note", "")))
    # One part per filtered COLUMN: did the semantic model declare its values at all? Said once,
    # not once per literal, and only a column that holds a short list of values is a gap; a wide
    # column is noted, because nobody would list it.
    for _key, fact in sorted(((judge or {}).get("columns") or {}).items()):
        part = f"values_declared:{fact.get('table')}.{fact.get('column')}"
        declared, distinct, n = fact.get("declared"), fact.get("distinct"), fact.get("observed_count")
        evidence = {"declared": declared, "distinct": distinct, "observed_count": n}
        if declared == "populated":
            rows.append(_part(part, CONFIRMED, evidence=evidence, note="the semantic model lists this column's values"))
        elif fact.get("sensitive"):
            rows.append(_part(part, NOTED, evidence=evidence,
                              note="sensitive column: its values are never listed, so no list is expected"))
        elif distinct == "listed":
            note = ("introspection found a low-cardinality column and nobody decoded its values"
                    if declared == "empty" else
                    f"the column holds {n} distinct values and the semantic model lists none of them")
            rows.append(_part(part, MODEL_GAP, kind="description", evidence=evidence, note=note))
        elif distinct == "overflow":
            rows.append(_part(part, NOTED, evidence=evidence,
                              note="the column holds more than 25 distinct values, so no value list is expected"))
        elif distinct == "empty":
            rows.append(_part(part, NOTED, evidence=evidence,
                              note="the column returned no values at all, so nothing says whether a list is expected"))
        else:
            rows.append(_part(part, UNRESOLVED, evidence=evidence,
                              note="whether the semantic model should list this column's values could not be "
                                   f"checked: the distinct probe was {distinct or 'not run'}"))
    return rows


_CLAIM_PARTS = frozenset({"predicates", "date_window"})


def _grade_claims(claims: dict | None) -> list[dict]:
    rows: list[dict] = []
    wanted = {"filter_predicates": "predicates", "date_window": "date_window"}
    unreadable = (claims or {}).get("unreadable")
    both_readable = isinstance(unreadable, dict) and not any(unreadable.values())
    # `sm claims` counts, per side, the conjuncts that speak of time. Two zeros beside a window that
    # reads `unknown` mean neither statement filtered on a date. A window written in a shape the
    # resolver does not fold also reads `unknown`, with a count above zero, and stays open.
    temporal = (claims or {}).get("temporal_predicates") or {}
    no_date_filter_anywhere = (both_readable and temporal.get("sql_file") == 0
                               and temporal.get("against_sql_file") == 0)
    for claim in (claims or {}).get("claims", []):
        part = wanted.get(claim.get("name"))
        if part is None:
            continue
        evidence = {"status": claim.get("status"), "generated": claim.get("generated"),
                    "golden": claim.get("golden")}
        if claim.get("status") == "agrees":
            rows.append(_part(part, CONFIRMED, evidence=evidence, note="both statements agree"))
        elif claim.get("status") == "differs":
            rows.append(_part(part, UNRESOLVED, evidence=evidence,
                              note="the two statements differ here; which is right is not decided by this comparison"))
        elif (part == "date_window" and no_date_filter_anywhere
              and claim.get("generated") is None and claim.get("golden") is None):
            rows.append(_part(part, CONFIRMED, evidence={**evidence, "temporal_predicates": temporal},
                              note="neither statement writes a date filter"))
        elif part == "date_window" and both_readable:
            rows.append(_part(part, UNRESOLVED, evidence={**evidence, "temporal_predicates": temporal},
                              note="a date filter was written in a shape the claims reader does not fold, "
                                   "so the two windows could not be compared"))
        else:
            rows.append(_part(part, UNRESOLVED, evidence=evidence,
                              note="this claim could not be read on one side"))
    return rows


def _part_subjects(part: str) -> list[str]:
    """The table or `table.column` a part is about, as the mentions verb keys them; empty for a part
    that names neither (a run, an aggregate, a claim)."""
    for prefix in ("literal:", "values_declared:"):
        if part.startswith(prefix):
            return [part[len(prefix):].split("=", 1)[0].lower()]
    if part.startswith("default_filter:"):
        return [part[len("default_filter:"):].split(":", 1)[0].lower()]
    for prefix in ("join:", "join_key:", "cardinality:", "dropped_rows:"):
        if part.startswith(prefix):
            label = part[len(prefix):].split("#", 1)[0]
            return [name for name in label.split("-", 1) if name and name != "*"]
    return []


def _attach_prose(rows: list[dict], mentions: dict | None) -> None:
    """Put the semantic model's own words beside every part that fell short: the descriptions,
    caveats, glossary lines and examples that mention its table or column, from `sm mentions`.
    Never a grade; a person reads them. A part that is confirmed or noted gets none, so a clean
    row's ledger does not grow a copy of the model's prose."""
    if not isinstance(mentions, dict) or not mentions.get("mentions"):
        return
    by_about: dict[str, list[dict]] = {}
    for mention in mentions["mentions"]:
        by_about.setdefault(mention.get("about", ""), []).append(mention)
    flags = {flag.get("about"): flag for flag in mentions.get("flags") or []}
    for row in rows:
        if row["verdict"] in (CONFIRMED, NOTED):
            continue
        subjects = _part_subjects(row["part"])
        if not subjects:
            continue
        prose: list[dict] = []
        for subject in subjects:
            prose.extend(by_about.get(subject, []))
            if "." in subject:
                prose.extend(by_about.get(subject.split(".", 1)[0], []))
        if prose:
            row["evidence"]["prose"] = prose[:20]
        flagged = [flags[subject] for subject in subjects if subject in flags]
        if flagged:
            row["evidence"]["prose_flags"] = flagged
            row["note"] += "; two descriptions in the semantic model name different values for this column, read both"


def ledger(row_dir: Path, *, with_claims: bool = False) -> dict:
    """Every part of the statement in `row_dir`, graded, and the verdict the weakest part decides."""
    row_dir = Path(row_dir)
    run = _load_json(row_dir / "run.json")
    prepare = _load_json(row_dir / "statement-prepare.json")
    receipt = _load_json(row_dir / "statement-receipt.json")
    probes = _load_json(row_dir / "join-probes.json")
    judge = _load_json(row_dir / "filter-values.judge.json")
    claims = _load_json(row_dir / "claims.json") if with_claims else None
    # Optional: the semantic model's own words about what the statement reads. Absent, the ledger
    # grades exactly as it would have; present, they ride on the parts that fell short.
    mentions = _load_json(row_dir / "mentions.json")
    fit = _load_json(row_dir / "question_fit.json")

    rows = _grade_run(run)
    rows.extend(_grade_question_fit(fit, isinstance(run, dict) and run.get("status") == "ok"))
    # After a run that succeeded, every input the later steps write is expected. One that is absent,
    # empty, or an error object is a part of the statement that was NOT checked, said as such: the
    # alternative, grading only what is there, makes a crashed verb read as a clean statement.
    ran = isinstance(run, dict) and run.get("status") == "ok"
    expected = (("statement-prepare.json", prepare, "aggregates", "fan_out:*"),
                ("statement-receipt.json", receipt, "tables", "receipt:*"),
                ("join-probes.json", probes, "joins", "join:*"),
                ("filter-values.judge.json", judge, "literals", "literal:*"))
    checked: dict[str, dict | None] = {}
    for name, payload, key, part in expected:
        got, why = _usable(payload, key)
        checked[name] = got
        if ran and got is None:
            rows.append(_part(part, UNRESOLVED, evidence={"file": name, "problem": why},
                              note=f"{name} {why}, so this part of the statement was not checked"))
    prepare, receipt = checked["statement-prepare.json"], checked["statement-receipt.json"]
    probes, judge = checked["join-probes.json"], checked["filter-values.judge.json"]
    if with_claims:
        claims, _why = _usable(claims, "claims")
    join_rows = _grade_joins(probes, row_dir)
    rows.extend(join_rows)
    rows.extend(_grade_aggregates(prepare, join_rows, probes,
                                  no_joins_written=(probes is not None and probes.get("unreadable") is None
                                                    and probes.get("joins_written") == 0)))
    rows.extend(_grade_filters(receipt))
    rows.extend(_grade_metrics(receipt, prepare))
    rows.extend(_grade_literals(judge))
    rows.extend(_grade_claims(claims))

    _attach_prose(rows, mentions if isinstance(mentions, dict) and not mentions.get("error") else None)

    counts = {v: 0 for v in _VERDICT_RANK}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    verdict = max((row["verdict"] for row in rows), key=lambda v: _VERDICT_RANK.get(v, 2))
    return {"rows": rows, "verdict": verdict, "counts": counts}


# --- Findings -------------------------------------------------------------


# A table or alias qualifier in front of a column name, for folding `o.status` and `orders.status`.
_QUALIFIER_RE = re.compile(r"\b[a-z_][a-z0-9_]*\.(?=[a-z_])")


def _finding_key(row: dict) -> str | None:
    """One key per problem, so the same gap seen from two statements counts once."""
    part, kind = row["part"], row.get("kind")
    if kind == "relationship" and part.startswith("join:"):
        return f"relationship:{part[len('join:'):]}"
    if kind == "filter" and part.startswith("default_filter:"):
        table, _sep, expr = part[len("default_filter:"):].partition(":")
        # The receipt spells the filter with the statement's own alias (`o.status`), so the same
        # declared filter seen through two aliases would be two findings. The qualifier is dropped.
        return f"filter:{table}:{_QUALIFIER_RE.sub('', _fold(expr))}"
    if kind == "metric" and part.startswith("metric:"):
        return f"metric:{_fold(part[len('metric:'):])}"
    if kind == "scope":
        return f"scope:{row.get('evidence', {}).get('rule') or 'scope'}"
    if kind == "description" and part.startswith("literal:"):
        return f"description:{_fold(part[len('literal:'):].split('=', 1)[0])}"
    if kind == "description" and part.startswith("values_declared:"):
        # The same family as a stale list: one column whose declared values are missing or wrong.
        return f"description:{_fold(part[len('values_declared:'):])}"
    return f"{kind or 'other'}:{_fold(part)}"


def findings(run_dir: Path) -> dict:
    """The run's findings, its defects, and every row's ledger, written beside `rows.jsonl`.

    A finding is one place the semantic model was shown to be missing or wrong, with every row that
    showed it. A defect is one part of a person's statement the data proved wrong, listed apart so
    nothing about the semantic model is proposed from it. A statement the AI got wrong while every
    part of the person's statement held is a finding of kind `example`: the fix is a worked example,
    not a change to a definition.
    """
    run_dir = Path(run_dir)
    records: list[dict] = []
    rows_path = run_dir / "rows.jsonl"
    if rows_path.exists():
        for n, line in enumerate(rows_path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                record = json.loads(line)
                record.setdefault("row", n)
                records.append(record)
    ledgers: dict[str, dict] = {}
    grouped: dict[str, dict] = {}
    defects: list[dict] = []
    for record in records:
        n = record["row"]
        row_dir = run_dir / "rows" / str(n)
        graded = ledger(row_dir, with_claims=True) if row_dir.exists() else None
        if graded is not None:
            ledgers[str(n)] = graded
        parts = graded["rows"] if graded else []
        evidence_base = {"row": n, "question": record.get("question"),
                         "statement": record.get("statement"), "expected": record.get("expected"),
                         "claims": record.get("claims")}
        for part in parts:
            if part["verdict"] == QUERY_DEFECT:
                defects.append({"row": n, "part": part["part"], "note": part["note"]})
            elif part["verdict"] == MODEL_GAP:
                key = _finding_key(part)
                entry = grouped.setdefault(key, {"key": key, "kind": part.get("kind"), "evidence": []})
                entry["evidence"].append({**evidence_base, "part": part["part"],
                                          "note": part["note"], "ledger": part["evidence"]})
                if record.get("words"):
                    entry["words"] = record["words"]
        # An example is offered only when the person's statement held on EVERY part of its own. A
        # part left open, or a row that was never graded at all, is not a statement that held. The
        # two claim parts compare the person's statement with agami's; they describe the difference
        # an example records, so they are its evidence and not its bar. A noted part is a fact, not a
        # grade, and bars nothing.
        own = [p for p in parts if p["part"] not in _CLAIM_PARTS]
        clean = bool(own) and all(p["verdict"] in (CONFIRMED, NOTED) for p in own)
        if record.get("status") == "mismatch" and clean and record.get("question"):
            key = f"example:{_fold(record['question'])}"
            entry = grouped.setdefault(key, {"key": key, "kind": "example", "evidence": []})
            entry["evidence"].append({**evidence_base, "part": None,
                                      "note": "the statement held on every part and the AI's answer differed",
                                      "ledger": {}})
        # A person graded the answer wrong and said why, with no statement to grade. The receipt
        # could not say what was wrong, so the finding carries their words and nothing else.
        person_grade = (record.get("provenance") or {}).get("graded")
        if (person_grade == "wrong" and record.get("words") and not record.get("statement")
                and record.get("question")):
            key = f"description:{_fold(record['question'])}"
            entry = grouped.setdefault(key, {"key": key, "kind": "description", "evidence": []})
            entry["evidence"].append({**evidence_base, "part": None,
                                      "note": "the person graded the answer wrong; their words say why",
                                      "ledger": {}})
            entry["words"] = record["words"]
    result = {
        "findings": sorted(grouped.values(), key=lambda f: f["key"]),
        "query_defects": sorted(defects, key=lambda d: (d["row"], d["part"])),
        "ledger": ledgers,
    }
    for entry in result["findings"]:
        entry["evidence"].sort(key=lambda e: e["row"])
    (run_dir / "findings.json").write_text(json.dumps({"findings": result["findings"]}, indent=2), encoding="utf-8")
    (run_dir / "query_defects.json").write_text(json.dumps(result["query_defects"], indent=2), encoding="utf-8")
    (run_dir / "ledger.json").write_text(json.dumps(ledgers, indent=2), encoding="utf-8")
    return result


# --- Row status -----------------------------------------------------------

MATCH = "match"
MATCH_UNVERIFIED = "match_unverified"
MISMATCH = "mismatch"
EXPECTED_DOUBTFUL = "expected_doubtful"
ERROR = "error"


def row_status(match: bool | None, ledger_verdict: str | None) -> str:
    """The status a row gets, from the number comparison and the weakest grade on the person's
    statement. Applied by code so the skill never decides it by feel.

    A match with a part not confirmed is `match_unverified`: two wrong statements agree easily, and
    Phase 3e must never see it. A difference beside a defect in the person's statement is
    `expected_doubtful`: the expected value itself is in doubt, so the row is kept out of the
    mismatch tally rather than counted against the AI.
    """
    if match is None:
        return ERROR
    if match:
        return MATCH if ledger_verdict in (None, CONFIRMED) else MATCH_UNVERIFIED
    return EXPECTED_DOUBTFUL if ledger_verdict == QUERY_DEFECT else MISMATCH


# --- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Reconciliation helper for agami-reconcile.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_parse = sub.add_parser("parse", help="Parse a reconciliation CSV.")
    p_parse.add_argument("--csv", required=True)

    # `--tolerance` is read the way every other number on this CLI is read, rather than by
    # argparse's float: the skill's own prose says a tolerance out loud as `±1%` in a dozen places
    # and then tells the caller to pass the run's tolerance, so `1%` is the form that actually
    # arrives. `parse_value` folds it to 0.01 and leaves 0.01 alone, so the decimal form every
    # existing caller passes is untouched.
    p_diff = sub.add_parser("diff", help="Diff two numbers.")
    p_diff.add_argument("--expected", required=True)
    p_diff.add_argument("--actual", required=True)
    p_diff.add_argument("--tolerance", default="0.01")

    p_band = sub.add_parser("band", help="Band an observed number for a golden item's bounds.")
    p_band.add_argument("--value", required=True)
    p_band.add_argument("--tolerance", default="0.01")

    p_intake = sub.add_parser("intake", help="Read any input shape a person brings into evidence rows.")
    p_intake.add_argument("--file", action="append", required=True, dest="files")
    p_intake.add_argument("--source", default=None, help="the person's own words for where this came from")

    p_ledger = sub.add_parser("ledger", help="Grade every part of a supplied statement from the files in its row directory.")
    p_ledger.add_argument("--row-dir", required=True, dest="row_dir")
    p_ledger.add_argument("--with-claims", action="store_true", dest="with_claims",
                          help="also read claims.json, the diff against the AI's own statement")

    p_findings = sub.add_parser("findings", help="Write a run's findings, defects and ledgers beside its rows.jsonl.")
    p_findings.add_argument("--run-dir", required=True, dest="run_dir")

    p_status = sub.add_parser("status", help="The status a row gets, from the number comparison and the ledger's verdict.")
    p_status.add_argument("--match", required=True, choices=["true", "false", "none"],
                          help="reconcile.py diff's match, or none when the row could not run")
    p_status.add_argument("--ledger-verdict", default="none", dest="ledger_verdict",
                          choices=[CONFIRMED, MODEL_GAP, QUERY_DEFECT, UNRESOLVED, "none"],
                          help="the ledger's verdict, or none for a row with no statement")

    args = p.parse_args(argv)

    if args.cmd == "status":
        match = None if args.match == "none" else args.match == "true"
        verdict = None if args.ledger_verdict == "none" else args.ledger_verdict
        print(json.dumps({"status": row_status(match, verdict)}))
        return 0

    if args.cmd == "ledger":
        row_dir = Path(args.row_dir).expanduser()
        if not row_dir.is_dir():
            print(f"reconcile ledger: row directory not found: {row_dir}", file=sys.stderr)
            return 2
        result = ledger(row_dir, with_claims=args.with_claims)
        (row_dir / "ledger.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "findings":
        run_dir = Path(args.run_dir).expanduser()
        if not run_dir.is_dir():
            print(f"reconcile findings: run directory not found: {run_dir}", file=sys.stderr)
            return 2
        if not (run_dir / "rows.jsonl").exists():
            print("reconcile findings: no rows to read; the run wrote no rows.jsonl", file=sys.stderr)
            return 4
        print(json.dumps(findings(run_dir), indent=2))
        return 0

    if args.cmd == "intake":
        paths = [Path(f).expanduser() for f in args.files]
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            print(f"reconcile intake: file not found: {', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            result = intake(paths, source=args.source)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"reconcile intake: could not read the input: {exc}", file=sys.stderr)
            return 2
        if not result["rows"]:
            # Exit 4, "nothing to do", kept apart from 2 so the skill can say which happened:
            # a file it could not open, or a file with no question, statement or number in it.
            # The skipped lines and their reasons still go to stdout, so the person hears why.
            print(json.dumps(result, indent=2))
            print("reconcile intake: nothing usable in the input; no question, statement or number "
                  "was found", file=sys.stderr)
            return 4
        print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "parse":
        rows = parse_csv(args.csv)
        print(json.dumps(rows, indent=2))
        return 0

    if args.cmd in ("diff", "band"):
        # Refused rather than defaulted. A tolerance nobody can read is not a reason to fall back
        # to ±1%: the caller asked for a band of some other width, and silently returning the
        # default one would answer a question they did not ask.
        tolerance = parse_value(args.tolerance)
        if tolerance is None or tolerance < 0:
            print(
                f"reconcile {args.cmd}: could not read a tolerance from --tolerance "
                f"{args.tolerance!r}; pass a fraction (0.01) or a percentage (1%).",
                file=sys.stderr,
            )
            return 2

    if args.cmd == "diff":
        e = parse_value(args.expected)
        a = parse_value(args.actual)
        result = diff(e, a, tolerance=tolerance)
        print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "band":
        value = parse_value(args.value)
        if value is None:
            # Refused rather than crashed, and specifically not with exit 1: the band feeds a
            # golden item, and 1 on that path is the save door's "needs confirmation" — a
            # traceback landing on it would read as a stop somebody could say yes to.
            print(
                f"reconcile band: could not read a number from --value {args.value!r}; "
                "pass the observed value as it was recorded.",
                file=sys.stderr,
            )
            return 2
        print(json.dumps(band(value, tolerance=tolerance), indent=2))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
