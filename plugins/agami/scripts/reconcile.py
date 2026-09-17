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
import hashlib
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
        "provenance": {"shape": None, "source": source, "file": file, "line": line},
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


_MAX_INTAKE_BYTES = 20 * 1024 * 1024


def _rows_from_file(path: Path, source: str | None) -> tuple[list[dict], list[dict]]:
    """One file's rows and the lines it could not use. The extension decides how lines are cut:
    `.json` is a list, `.sql` is statements split on `;`, `.txt` and `.md` are one question per
    line, and everything else is CSV."""
    file = path.name
    if path.stat().st_size > _MAX_INTAKE_BYTES:
        raise ValueError(f"{file} is {path.stat().st_size // (1024 * 1024)} MB; the intake reads files up to {_MAX_INTAKE_BYTES // (1024 * 1024)} MB. Export fewer rows, or split the file.")
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
        # A line whose first cell starts with `#` is guidance, the way the template the skill
        # writes for the person carries it; it is never a row.
        numbered = []
        guidance: list[dict] = []
        seen_row = False
        for n, r in enumerate(csv.reader(fh), 1):
            if not r or not any(c.strip() for c in r):
                continue
            if not seen_row and r[0].lstrip().startswith("#"):
                # The template's guidance lines sit above the header; below it, "# of orders" is a
                # label. Each skipped line is recorded, so a label swallowed here is at least visible.
                guidance.append({"file": file, "line": n, "reason": "guidance line (starts with #)"})
                continue
            seen_row = True
            numbered.append((n, r))
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
    skipped.extend(guidance)
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
    # The row number is given once, here. The intake page shows it, its block names it, and the run
    # directory and the report page use it; a row dropped on the page never renumbers the others.
    for n, row in enumerate(rows, 1):
        row.setdefault("row", n)
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
                              note="the join is not declared, and no key on one side is found on the other"))
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
        # The probe file keys this map with the semantic model's own spelling; the pair carries the
        # statement's, lowercased. Folded on both sides, so an uppercase-introspected model
        # (`customers.ID`) still says its key is unique.
        unique = {_fold(str(k)): v for k, v in ((probes or {}).get("unique_by_model") or {}).items()}
        for table, column in pairs[0]:
            if _fold(str(table)) == right_key and unique.get(_fold(f"{table}.{column}")):
                return True
    return False


def _dropped_rows(join: dict, label: str, jid: str, row_dir: Path) -> list[dict]:
    """The rows an inner join left behind, said and never judged. No probe planned → nothing to say."""
    probe = join.get("dropped_rows_probe")
    if not probe:
        return []
    left_t, right_t = probe.get("left"), probe.get("right")
    unexamined = probe.get("unexamined")
    got = _probe_csv(row_dir / f"{jid}.dropped_rows.csv")
    # Both numbers by their own header, never the one-column fall-back: a result with one column
    # would otherwise read as N of N dropped and be stated as a fact.
    total = _named_number(got, "total")
    dropped = _named_number(got, "dropped")
    if total is None or dropped is None:
        return [_part(f"dropped_rows:{label}", NOTED, evidence={"left": left_t, "right": right_t},
                      note="the dropped-rows probe was not run or failed; nothing is claimed")]
    t, d = int(total), int(dropped)
    said = (f"no {left_t} row is dropped by this join" if d == 0
            else f"{d} of {t} {left_t} rows have no {right_t} partner and are dropped by this inner join")
    said += "; counted over the whole table, before the statement's own filters"
    if unexamined:
        said += f"; rows of {unexamined} with no {left_t} partner were not counted"
    return [_part(f"dropped_rows:{label}", NOTED,
                  evidence={"total": t, "dropped": d, "left": left_t, "right": right_t, "unexamined": unexamined},
                  note=said)]


def _named_number(rows, key: str) -> float | None:
    """The first row's `key` column as a number, by header only, whatever the header's case."""
    if not isinstance(rows, list) or not rows:
        return None
    raw = next((v for k, v in rows[0].items() if k and k.strip().lower() == key.lower()), None)
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


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
    prepare_read = isinstance(prepare, dict) and "aggregates" in prepare
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
        elif item.get("aggregate") is False or (item.get("aggregate") is None and prepare_read and not aggregates and status == "unmatched"):
            # A plain column in a list query computes nothing a metric could define: `number`,
            # `type` or `opened` matching no metric is not a gap, and saying so for every column of
            # a twelve-column list would bury the one row that matters. The receipt says whether
            # the output aggregates; an older receipt without the key is read through the
            # pre-flight, which lists the statement's aggregates.
            continue
        elif only_bare_counts:
            rows.append(_part(f"metric:{column}", CONFIRMED,
                              note="a bare count matches no metric by design"))
        elif status == "unmatched" and (item.get("aggregate") is True or prepare_read):
            rows.append(_part(f"metric:{column}", MODEL_GAP, kind="metric", evidence={"column": column},
                              note="the output matches no metric the semantic model defines"))
        elif status == "unmatched":
            rows.append(_part(f"metric:{column}", UNRESOLVED, evidence={"column": column},
                              note="the output matches no metric, and without the pre-flight nothing says whether it aggregates; not judged"))
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
            rows.append(_part(part, CONFIRMED, evidence=evidence, note="both statements say the same"))
        elif claim.get("status") == "differs":
            # Two queries written differently is a fact about the pair, never a grade on yours: the
            # answer decides whether they agree, and the report page carries "different query" as a tag.
            rows.append(_part(part, NOTED, kind="different_query", evidence=evidence,
                              note="the two queries differ here; the answers decide, not the spelling"))
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


# Words an expression can open with that are never the column it filters.
_SQL_LEADING_WORDS = frozenset({"not", "case", "exists", "any", "all", "distinct", "null", "true", "false"})


def _part_subjects(part: str) -> list[str]:
    """The table or `table.column` a part is about, as the mentions verb keys them; empty for a part
    that names neither (a run, an aggregate, a claim)."""
    for prefix in ("literal:", "values_declared:"):
        if part.startswith(prefix):
            return [part[len(prefix):].split("=", 1)[0].lower()]
    if part.startswith("default_filter:"):
        # The receipt spells the table as the statement wrote it, schema and all; the mentions verb
        # keys tables bare, so `main.orders` must read as `orders` or its caveats never attach. The
        # filter's own column is the subject worth quoting: a declared filter is about one column,
        # and the table's description is what got quoted instead.
        table, _, expr = part[len("default_filter:"):].partition(":")
        table = table.lower().split(".")[-1]
        # The first identifier is the column only when the expression opens with a plain column
        # reference. `NOT is_test` opens with a keyword and `lower(status) = 'x'` with a function,
        # and taking either as the column produced a subject (`orders.not`, `orders.lower`) that
        # matches nothing at all, so the part lost its quoted prose AND the flag note that two
        # descriptions disagree. Fall back to the table rather than invent a column.
        column = re.match(r"\s*(?:[a-z_][a-z0-9_]*\.)?([a-z_][a-z0-9_]*)\s*(?![\w(])", expr.lower())
        if not column or column.group(1) in _SQL_LEADING_WORDS:
            return [table]
        return [f"{table}.{column.group(1)}"]
    for prefix in ("join:", "join_key:", "cardinality:", "dropped_rows:"):
        if part.startswith(prefix):
            label = part[len(prefix):].split("#", 1)[0]
            return [name for name in label.split("-", 1) if name and name != "*"]
    return []


def _prose_status(mentions: Any) -> list[dict]:
    """One `noted` part when the semantic model's words were asked for and could not be read, in
    whole or in part, so "no prose exists" and "the verb failed" stay tellable apart. Nothing when
    the file is absent (the step was not run) or clean."""
    if mentions is None:
        return []
    if not isinstance(mentions, dict) or mentions.get("error") or mentions.get("unreadable"):
        why = (mentions.get("error") or mentions.get("unreadable")) if isinstance(mentions, dict) else "not a JSON object"
        return [_part("prose:*", NOTED, evidence={"problem": why},
                      note=f"the semantic model's words could not be read ({why}); nothing about them is claimed")]
    skipped = mentions.get("skipped") or []
    if skipped:
        return [_part("prose:*", NOTED, evidence={"skipped": skipped[:20]},
                      note=f"{len(skipped)} prose source(s) could not be read and are not quoted: "
                           + ", ".join(sorted({str(s.get('where')) for s in skipped}))[:300])]
    return []


def _attach_prose(rows: list[dict], mentions: dict | None) -> None:
    """Put the semantic model's own words beside every part that fell short: the descriptions,
    caveats, glossary lines and examples that mention its table or column, from `sm mentions`.
    Never a grade; a person reads them. A part that is confirmed or noted gets none, so a clean
    row's ledger does not grow a copy of the semantic model's prose."""
    if not isinstance(mentions, dict) or not isinstance(mentions.get("mentions"), list) or not mentions["mentions"]:
        return
    by_about: dict[str, list[dict]] = {}
    for mention in mentions["mentions"]:
        if isinstance(mention, dict) and mention.get("about"):
            by_about.setdefault(mention["about"], []).append(mention)
    flags = {flag.get("about"): flag for flag in (mentions.get("flags") or []) if isinstance(flag, dict)}
    for row in rows:
        if row["verdict"] in (CONFIRMED, NOTED):
            continue
        subjects = _part_subjects(row["part"])
        if not subjects:
            continue
        # The words about the part's OWN subject, and no substitute. A part about a column used to
        # fall back to its table, so a check on one missing filter was answered with three
        # paragraphs describing the dataset: on-subject, and evidence about nothing. A part with no
        # words of its own now shows none, and the section does not render for that row.
        prose: list[dict] = []
        for subject in subjects:
            prose.extend(by_about.get(subject, []))
        if prose:
            # The page shows three. Keeping twenty here only grows an "and 34 more" line that tells a
            # reader the row has evidence it is not being shown.
            row["evidence"]["prose"] = prose[:3]
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

    rows.extend(_prose_status(mentions))
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
        # An example is offered only when the person's statement held on EVERY part of its own. A
        # part left open, or a row that was never graded at all, is not a statement that held. The
        # two claim parts compare the person's statement with agami's; they describe the difference
        # an example records, so they are its evidence and not its bar. A noted part is a fact, not a
        # grade, and bars nothing.
        own = [p for p in parts if p["part"] not in _CLAIM_PARTS]
        clean = bool(own) and all(p["verdict"] in (CONFIRMED, NOTED) for p in own)
        # A `no_question` fit removes the `question_fit` part; against a row that carries a
        # question, that is a contradiction and not a pass. The cross-check lives here because
        # this is where the row record and the ledger meet.
        if record.get("question") and record.get("statement") and graded is not None \
                and not any(p["part"] == "question_fit" for p in parts):
            clean = False
        if record.get("status") == "mismatch" and clean and record.get("question"):
            key = f"example:{_fold(record['question'])}"
            entry = grouped.setdefault(key, {"key": key, "kind": "example", "evidence": []})
            entry["evidence"].append({**evidence_base, "part": None,
                                      "note": "every check on the statement passed and agami's answer differed",
                                      "ledger": {}})
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
# A question with no statement and no number behind it. agami answered; whether the answer is right
# is a person's call, and until they make it the row is not an error, not a match and not a mismatch.
# It used to be written as `error`, which said the run broke when nothing had.
UNGRADED = "ungraded"

# The words a status takes wherever a person reads one. The token stays on the wire, because
# `rows.jsonl`, the keep gate and the `status` verb's choices are pinned to it and churning that
# would be risk without a reader benefit; what a person sees is always the sentence. A status is two
# facts, so the five read as a 2x2 plus the case where nothing could be compared: did the two answers
# agree, and did the person's own query check out. `expected_doubtful` is the reason this table
# exists: it is the row where the analyst's own query is the thing in doubt, the most delicate claim
# the tool makes, and a reader who has to look the word up will not trust it.
_STATUS_WORDS = {
    MATCH: "same answer",
    MATCH_UNVERIFIED: "same answer, but part of your query could not be checked",
    MISMATCH: "different answer",
    EXPECTED_DOUBTFUL: "different answer, and your query has a problem",
    ERROR: "could not compare",
    UNGRADED: "not graded yet",
}


# The colour family a status takes on the page, and the words the filter chips use. The chips group
# by family, so two statuses share one: a row whose query has a proven problem and a row a check
# could not reach both land on "your query needs a look", which is the honest thing they have in
# common. A coarser vocabulary than _STATUS_WORDS, and it lives here for the same reason: the page
# renders what it is given and keeps no glossary, so the card, the chips and the chat cannot drift
# into calling one row three things.
_STATUS_CLASS = {MATCH: "held", MATCH_UNVERIFIED: "defect", MISMATCH: "gap",
                 EXPECTED_DOUBTFUL: "defect", ERROR: "noted", UNGRADED: "ask"}
# `ask` takes the same grey as `noted` and keeps its own name and words: nothing is claimed about
# the row either way, but "could not compare" and "your call" are not the same thing to read.
_STATUS_CHIP_WORDS = {"held": "same answer", "defect": "your query needs a look", "gap": "different answer",
                      "open": "could not check", "noted": "could not compare", "ask": "your call"}


def status_words(status: str | None) -> str:
    """The sentence for a status. Unknown statuses read as the plainest true thing, never the token."""
    return _STATUS_WORDS.get(status or "", "could not compare")


def status_legend() -> dict:
    """What a page needs to show a status: its colour family and the chip words. Shipped with the
    page so nothing is written into a template twice."""
    return {"cls": dict(_STATUS_CLASS), "chips": dict(_STATUS_CHIP_WORDS)}


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



# --------------------------------------------------------------------------------------------
# next-chunk: the run works five rows at a time, and rows.jsonl is its checkpoint.
# --------------------------------------------------------------------------------------------

CHUNK_SIZE = 5


def _done_rows(run_dir: Path) -> tuple[list[dict], list[str]]:
    """The row records already written to rows.jsonl, and the lines that could not be read."""
    path = run_dir / "rows.jsonl"
    if not path.exists():
        return [], []
    done: list[dict] = []
    bad: list[str] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            bad.append(f"line {n}")
            continue
        if not isinstance(rec, dict) or "row" not in rec:
            bad.append(f"line {n}")
            continue
        if isinstance(rec["row"], str) and rec["row"].strip().isdigit():
            rec["row"] = int(rec["row"])
        done.append(rec)
    # A row run twice (the skill says "re-run this row") is the last record written: one row, one
    # record, whether the chunk arithmetic counts it or the report page shows it.
    last: dict = {}
    for rec in done:
        last[rec["row"]] = rec
    return list(last.values()), bad


def next_chunk(run_dir: Path, size: int = CHUNK_SIZE) -> dict:
    """The next `size` rows of `<run_dir>/intake.json` that are not yet in `<run_dir>/rows.jsonl`.

    `rows.jsonl` is the run's checkpoint: Phase 2d appends one record per finished row, so a run
    interrupted anywhere resumes with this call and no row is ever run twice. The counts by status
    over the finished rows travel too, so the progress line the skill says is read, never tallied by
    hand. A checkpoint line that cannot be read is refused rather than skipped: skipping it would
    run its row again and write it twice.
    """
    intake_path = run_dir / "intake.json"
    data = json.loads(intake_path.read_text(encoding="utf-8"))
    rows = data.get("rows", []) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("intake.json holds no list of rows")
    for n, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"intake.json row {n} is not an object")
        row.setdefault("row", n)
    done, bad = _done_rows(run_dir)
    if bad:
        raise ValueError(f"rows.jsonl has a line that cannot be read ({', '.join(bad)}); fix or remove it "
                         "before continuing, or the row it holds would be run twice")
    done_ids = {rec["row"] for rec in done}
    remaining = [row for row in rows if row["row"] not in done_ids]
    chunk = remaining[:size]
    progress: dict[str, int] = {}
    intake_ids = {r["row"] for r in rows}
    for rec in done:
        if rec["row"] not in intake_ids:
            continue  # a stale record from another row list is not this run's progress
        status = rec.get("status") or "unknown"
        progress[status] = progress.get(status, 0) + 1
    finished = len(rows) - len(remaining)
    return {
        "size": size, "total": len(rows), "done": sorted(done_ids & {r["row"] for r in rows}),
        "finished": finished, "remaining": len(remaining), "complete": not remaining,
        "chunk": chunk, "chunk_rows": [r["row"] for r in chunk],
        "chunk_index": (finished // size) + 1 if chunk else None,
        "chunks_total": -(-len(rows) // size) if size else None,
        "progress": progress,
    }


# --------------------------------------------------------------------------------------------
# report-items: the report page's items, templated from what the run wrote, never written by hand.
# --------------------------------------------------------------------------------------------

_STATE = {CONFIRMED: "held", QUERY_DEFECT: "defect", MODEL_GAP: "gap", UNRESOLVED: "open", NOTED: "noted"}
_STATE_WORDS = {"held": "passed", "defect": "a mistake in your query", "gap": "a gap in the semantic model",
                "open": "could not check", "noted": "noticed", "differs": "the two queries differ"}
# The same words when the query the ledger graded is agami's. Only the two that name a writer change:
# a gap in the semantic model is a gap whoever tripped on it.
_STATE_WORDS_AGAMI = dict(_STATE_WORDS, defect="a mistake in agami's query")


def _one_query(rec: dict) -> bool:
    """Whether one query was written for this row, or two.

    The run grades the query the row carries: the person's statement when there is one, agami's own
    when there is not. So a row with no statement of the person's has one query, whether or not its
    ledger has been run yet, and nothing on the card may name a second side: no "yours" column, no
    answer-against-answer row, and a failing check names agami rather than the person, who wrote
    nothing here to be wrong about.
    """
    return not rec.get("statement") and bool(rec.get("ledger") or rec.get("status") == UNGRADED)
_CLAIM_KEYS = {"tables": "tables read", "outputs": "selects", "filter_predicates": "filters", "date_window": "date window",
               "group_keys": "grouped by", "join_keys": "join keys", "ordering": "ordered by", "limit": "limit"}
# The words a ledger part's grade takes on the page, by part family and grade. Every cell on the
# page comes from this table, the run's files, or the receipt; none is written by hand.
_PART_WORDS = {
    "join": {"held": "declared in the semantic model", "defect": "wrong key", "gap": "not declared, but the keys match up", "open": "could not check"},
    "join_key": {"held": "keys match", "defect": "keys do not match", "open": "not probed"},
    "cardinality": {"held": "one row per key", "defect": "many rows per key on both sides", "open": "unknown"},
    "fan_out": {"held": "no row is counted twice", "defect": "a join repeats rows, so some are counted more than once", "open": "could not tell which table it counts"},
    "aggregation": {"held": "allowed", "defect": "may be wrong for this column"},
    "default_filter": {"held": "applied", "gap": "omitted", "open": "unclear"},
    "metric": {"held": "matched", "gap": "matches no metric", "open": "matched a metric defined on another table"},
    "literal": {"held": "exists", "defect": "matches no rows", "gap": "not in the declared list", "open": "not checked"},
    "values_declared": {"held": "listed", "gap": "no value list", "noted": "too many values to list", "open": "not checked"},
    "dropped_rows": {"noted": "noticed"},
    "question_fit": {"held": "yes", "open": "doubtful"},
    "prose": {"noted": "read", "open": "could not read"},
    "scope": {"held": "in scope", "gap": "refused: outside the semantic model"},
    "runs": {"held": "ran", "defect": "failed", "gap": "refused", "open": "not run"},
}
_PART_KEYS = {"join": "join {a} to {b}", "join_key": "join key {a} to {b}", "cardinality": "one row per key, {a} to {b}",
              "fan_out": "double counting in {x}", "aggregation": "aggregation {x}", "default_filter": "default filter on {t}",
              "metric": "metric {x}", "literal": "value {x}", "values_declared": "value list for {x}",
              "dropped_rows": "rows dropped by join {a} to {b}", "question_fit": "answers the question",
              "prose": "caveats read", "scope": "scope", "runs": "ran"}
_OWNER_CHANGE = {
    "keep": ([], ["Keep as a worked example, if you say yes."]),
    "you": (["Fix your query where the marks are red, then run this row again."], ["Your query: fix the red rows, then re-run."]),
    "model": (["Decide which definition your team means. A change to the semantic model goes through /agami-save-correction."],
              ["The semantic model: decide the definition."]),
    "question": (["Reword the question, or change your query, so they ask the same thing."], ["The question: reword it and re-run."]),
    "agami": (["Ask the question again in other words; agami's query failed."], ["agami: ask again."]),
    "nothing": (["Nothing to change. Some checks could not run against the database, so this row is not offered as an example."], ["Nothing to do; not offered as an example."]),
}


def _fmt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            text = f"{value:.3g}" if abs(value) < 0.01 else f"{value:,.2f}".rstrip("0").rstrip(".")
            return "0" if text in ("-0", "-0.0", "0.0") else text
        return f"{int(value):,}"
    return str(value)


def _fit_reason(note: Any) -> str | None:
    """The fit note as one sentence: the ledger's lead-in and its trailing instruction stripped."""
    if not isinstance(note, str) or not note.strip():
        return None
    text = note.split(":", 1)[1].strip() if note.startswith("the statement may not answer the question:") else note.strip()
    text = text.split("; reword the question")[0].strip().rstrip(".;")
    return (text[0].upper() + text[1:] + ".") if text else None


def _rows_word(n: int) -> str:
    """`1 row`, `5 rows`. One place, so no card says "1 rows"."""
    return f"{n:,} {'row' if n == 1 else 'rows'}"


def _in_our_words(reason: Any) -> str | None:
    """The comparator's reason in this page's vocabulary.

    `comparator.py` serves the golden run first, where the two sides are an answer key and a
    generated result. On a reconcile card they are the analyst's query and agami's, and a reader who
    never wrote an answer key cannot place the words. Same rule as the status words: the machinery
    keeps its terms, the page does not show them.
    """
    if not isinstance(reason, str) or not reason:
        return None
    for their, ours in (("the answer key's", "your query's"), ("the answer key", "your query"),
                        ("the generated result", "agami's result"), ("the generated statement", "agami's query"),
                        ("generated", "agami's")):
        reason = reason.replace(their, ours)
    return reason


def _recorded_display(recorded: Any, row_count: int | None = None) -> tuple[str | None, bool]:
    """What a recorded result looks like on the page: one cell as text, or 'N rows'. Never a row."""
    if not isinstance(recorded, dict):
        return None, False
    rows = recorded.get("rows")
    if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], list) and len(rows[0]) == 1:
        return _fmt(rows[0][0]), True
    n = recorded.get("row_count", row_count)
    if n is None and isinstance(rows, list) and rows:
        n = len(rows)
    if n is None:
        return None, False
    return f"{n:,} {'row' if n == 1 else 'rows'}", False


def _part_family(part: str) -> str:
    return part.split(":", 1)[0]


def _part_key(part: str) -> str:
    fam = _part_family(part)
    rest = part.split(":", 1)[1] if ":" in part else ""
    tpl = _PART_KEYS.get(fam, part)
    if rest == "*":
        return {"join": "joins", "literal": "values", "fan_out": "fan-out", "receipt": "receipt"}.get(fam, fam)
    if "{a}" in tpl:
        a, _, b = rest.partition("-")
        return tpl.format(a=a, b=b or "?")
    if "{t}" in tpl:
        return tpl.format(t=rest.split(":", 1)[0])
    return tpl.format(x=rest) if "{x}" in tpl else tpl


_KEY_OPS = {"eq": "=", "neq": "≠", "gte": "≥", "gt": ">", "lte": "≤", "lt": "<", "add": "+", "sub": "-", "mul": "×", "div": "/",
            "and": "and", "or": "or", "like": "like", "ilike": "ilike", "in": "in", "is": "is", "not": "not", "between": "between"}
_KEY_FUNCS = {"currentdate": "current_date", "currenttimestamp": "now", "timestamptrunc": "date_trunc", "datetrunc": "date_trunc"}


def _split_args(text: str) -> list[str]:
    """Top-level comma split of `a, f(b, c), 'd, e'`."""
    out, depth, quote, cur = [], 0, False, []
    for ch in text:
        if ch == "'" :
            quote = not quote
        if not quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                out.append("".join(cur).strip()); cur = []
                continue
        cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def _readable(key: Any) -> Any:
    """A claim key re-spelled for a person: `eq(orders.region, 'EU')` reads `orders.region = 'EU'`,
    `gte(o.d, add(timestamptrunc(currentdate(), var(year)), interval('7', var(months))))` reads
    `o.d ≥ date_trunc(year, current_date) + interval 7 months`. Words and symbols, never SQL: the key
    is the claims reader's structural form and this only unfolds it for reading."""
    if isinstance(key, list):
        return [_readable(k) for k in key]
    if not isinstance(key, str):
        return key
    m = re.fullmatch(r"\s*([a-z_]+)\((.*)\)\s*", key, flags=re.S)
    if not m:
        return key
    name, inner = m.group(1), m.group(2)
    args = [_readable(a) for a in _split_args(inner)]
    if not args and name in _KEY_FUNCS:
        return _KEY_FUNCS[name]  # current_date, now: a clock reading, written without brackets
    if name == "var" and len(args) == 1:
        return args[0]
    if name == "interval" and len(args) == 2:
        return f"interval {args[0].strip(chr(39))} {args[1]}"
    if name == "paren" and len(args) == 1:
        return f"({args[0]})"
    if name in _KEY_OPS and len(args) == 2 and name not in ("not",):
        return f"{args[0]} {_KEY_OPS[name]} {args[1]}"
    if name in ("and", "or") and len(args) > 2:
        return f" {name} ".join(args)
    if name == "not" and len(args) == 1:
        return f"not {args[0]}"
    if name == "in" and len(args) >= 2:
        return f"{args[0]} in ({', '.join(args[1:])})"
    if name == "between" and len(args) == 3:
        return f"{args[0]} between {args[1]} and {args[2]}"
    if name == "cast" and len(args) == 2:
        return f"{args[0]} as {args[1]}"
    return f"{_KEY_FUNCS.get(name, name)}({', '.join(args)})"


def _claim_text(value: Any) -> str | list[str] | None:
    if value is None or value == [] or value == {}:
        return None
    if isinstance(value, dict):  # a date window
        col = _readable(value.get("column") or "")
        start = value.get("start"); end = value.get("end")
        lo = ("≥ " if value.get("start_inclusive", True) else "> ") + str(start) if start else ""
        hi = ("≤ " if value.get("end_inclusive") else "< ") + str(end) if end else ""
        return " ".join(p for p in (col, lo, hi) if p) or None
    if isinstance(value, list):
        out = []
        for v in value:
            if isinstance(v, list) and v and all(isinstance(x, list) for x in v):
                out.append(" = ".join(".".join(map(str, x)) for x in v))
            elif isinstance(v, list):
                out.append(" ".join(map(str, v)))
            else:
                out.append(str(v))
        return out
    return str(value)


def _only(a: Any, b: Any) -> list[str]:
    la = a if isinstance(a, list) else ([a] if a else [])
    lb = b if isinstance(b, list) else ([b] if b else [])
    return [x for x in la if x not in lb]


def _receipt_filters(receipt: Any) -> dict[str, str]:
    """`table:expr` → applied | omitted | undetermined, from a receipt's table items."""
    out: dict[str, str] = {}
    if not isinstance(receipt, dict):
        return out
    for item in ((receipt.get("tables") or {}).get("items") or []):
        table = str(item.get("qname") or item.get("ref") or "").lower().split(".")[-1]
        for flt in item.get("filters") or []:
            if isinstance(flt, dict):
                out[f"{table}:{str(flt.get('expr') or '').lower()}"] = str(flt.get("status") or "")
    return out


def _receipt_metrics(receipt: Any) -> dict[str, str]:
    """output column → the metric it matched, from a receipt's column items."""
    out: dict[str, str] = {}
    if not isinstance(receipt, dict):
        return out
    for item in ((receipt.get("columns") or {}).get("items") or []):
        if item.get("kind") == "output" and item.get("status") == "matched" and item.get("name"):
            out[str(item.get("column") or "").lower()] = str(item["name"])
    return out


def _agami_steps(rec: dict) -> list[str]:
    """Every statement agami wrote for this row when there was more than one, in order, the last
    being the one run and compared; empty for the usual single statement."""
    steps = [s for s in (rec.get("agami_statements") or []) if isinstance(s, str) and s.strip()]
    return steps if len(steps) > 1 else []


def _diff_rows(rec: dict, agami_receipt: Any) -> tuple[list[dict], list[str]]:
    # Whether one query was written or two, decided once. With one there is no second side to name
    # anywhere: no answer-against-answer row, no "agami" column beside it, and a failing check names
    # agami rather than the person, who wrote nothing here to be wrong about.
    one_query = _one_query(rec)
    state_words = _STATE_WORDS_AGAMI if one_query else _STATE_WORDS
    rows: list[dict] = []
    words: list[str] = []
    # Which of the card's three sections a row belongs to. A reader asks three questions in order:
    # did we get the same answer, how did the two queries differ, and is my query sound against the
    # semantic model. The page groups by this rather than guessing from the label, because "value
    # list for x" and "values" are one prefix apart and mean different things. Reassigned before each
    # block below; `add` reads it at call time.
    section = "data"

    def add(key, state, yours=None, agami=None, note=None, yours_hi=None, agami_hi=None, family=None):
        row = {"key": key, "state": state, "yours": yours, "agami": agami, "note": note or None,
               "section": section}
        if family:
            row["family"] = family
        if yours_hi:
            row["yours_hi"] = yours_hi
        if agami_hi:
            row["agami_hi"] = agami_hi
        rows.append(row)

    ledger_rows = ((rec.get("ledger") or {}).get("rows") or []) if isinstance(rec.get("ledger"), dict) else []
    parts = {r["part"]: r for r in ledger_rows if isinstance(r, dict) and "part" in r}
    comparison = rec.get("comparison") or {}
    has_statement = bool(rec.get("statement"))
    result_set = comparison.get("result_set") if isinstance(comparison, dict) else None
    scalar = comparison.get("scalar") if isinstance(comparison, dict) else None

    # 1 · the answers. One row for a number; rows, columns and values for a table.
    agami_text, _single = _recorded_display(rec.get("recorded"), (result_set or {}).get("generated_row_count"))
    if rec.get("status") == "error" or rec.get("error"):
        agami_text = "failed"
    yours_text, _ = _recorded_display(rec.get("statement_recorded"), (result_set or {}).get("golden_row_count"))
    if yours_text is None and rec.get("expected") is not None:
        yours_text = _fmt(rec.get("expected"))
    if agami_text is None and rec.get("actual") is not None:
        agami_text = _fmt(rec.get("actual"))
    runs = parts.get("runs")
    if runs and runs["verdict"] != CONFIRMED:
        yours_text = _PART_WORDS["runs"].get(_STATE[runs["verdict"]], yours_text)
    steps = _agami_steps(rec)
    # "the last one's result is compared" is true only where a comparison happened, and an error row
    # can carry statements now. The answer row below never showed this on such a row (the row's own
    # error text precedes it), but the rows row takes it unguarded, so the claim is dropped here
    # rather than at each use. Same correction the card's step marker needed.
    steps_note = (
        None if not steps
        else f"agami ran {len(steps)} queries"
        + ("; the last one's result is compared" if rec.get("status") != ERROR else "")
    )
    if result_set:
        same_rows = result_set.get("golden_row_count") == result_set.get("generated_row_count")
        add("rows", "held" if same_rows else "defect", yours_text, agami_text, note=steps_note)
        yc = list(((rec.get("statement_recorded") or {}).get("columns")) or [])
        ac = list(((rec.get("recorded") or {}).get("columns")) or [])
        pairs = [tuple(p) for p in (result_set.get("column_pairs") or []) if isinstance(p, (list, tuple)) and len(p) == 2]
        if yc or ac:
            values_compared = same_rows and (bool(pairs) or bool(result_set.get("unmatched_golden_columns")))
            if values_compared:
                # Columns are compared by the values they carry, never by name: the comparator says
                # which of yours paired with which of agami's, and a renamed column is the same column.
                only_yours = [c for c in (result_set.get("unmatched_golden_columns") or []) if c in yc]
                only_agami = list(result_set.get("unmatched_generated_columns") or [])
                renamed = [f"{a} → {b}" for a, b in pairs if a != b]
            else:
                # No values comparison ran (the row counts differ, or an older score file): names are
                # all there is. Identical names are one column set; the rows check carries the counts.
                only_yours, only_agami, renamed = _only(yc, ac), _only(ac, yc), []
            if not only_yours and not only_agami:
                col_state = "held"
            elif only_yours:
                col_state = "defect"
            else:
                col_state = "noted"  # agami returned more than asked; nothing of yours is missing
            # No sentence: the tokens carry the difference the way a diff does. A column only yours
            # has reads as removed, one only agami's as added, and a pair with two names is marked in
            # place so the reader sees they hold the same values.
            add("columns", col_state, yc, ac, yours_hi=only_yours, agami_hi=only_agami)
            if renamed:
                rows[-1]["renamed"] = [[a, b] for a, b in pairs if a != b]
        acc = result_set.get("accuracy")
        share = result_set.get("paired_row_share")
        agreement = list(result_set.get("column_agreement") or [])
        n_rows = result_set.get("golden_row_count")
        if acc is not None:
            same = float(acc) >= 1.0
            paired_word = "the columns both queries return"
            if same:
                add("values", "held", "identical", None)
            elif pairs and share is not None:
                # The comparator says how many rows agree over the paired columns, and which pair
                # disagrees on how many rows; the card reads those numbers rather than a 0.
                if float(share) >= 1.0:
                    add("values", "held", "The values match.", None,
                        note=f"Compared {paired_word}. Your query returns others that agami's doesn't.")
                else:
                    if isinstance(n_rows, int) and n_rows > 0:
                        agree = round(float(share) * n_rows)
                        text = f"{agree} of {n_rows} rows match."
                        weak = [f"{a} on {n_rows - round(g * n_rows)} of {n_rows} rows"
                                for (a, _b), g in zip(pairs, agreement) if isinstance(g, (int, float)) and g < 1.0]
                    else:
                        text, weak = f"{float(share):.0%} of the rows match.", []
                    add("values", "defect", text, None, note=("differs in " + ", ".join(weak)) if weak else None)
            elif pairs and result_set.get("unmatched_golden_columns"):
                # An older score file without the share: every pair it reports agreed by construction.
                add("values", "held", "The values match.", None,
                    note=f"Compared {paired_word}. Your query returns others that agami's doesn't.")
            else:
                gc, ac_n = result_set.get("golden_row_count"), result_set.get("generated_row_count")
                if isinstance(gc, int) and isinstance(ac_n, int) and gc != ac_n:
                    # The comparator decides on the row counts BEFORE it pairs anything, so no value
                    # was compared. Printing its 0.0 as "0% of the values match" reports an artefact
                    # of the method as a fact about the data, on a row where a school can sit in both
                    # results. Say what actually happened instead.
                    add("values", "open", "The results weren't compared.", None,
                        note=f"Your query returned {_rows_word(gc)} and agami's returned {_rows_word(ac_n)}, "
                             "so they couldn't be lined up row by row.")
                else:
                    add("values", "defect", f"{float(acc):.0%} of the values match.", None,
                        note=_in_our_words(result_set.get("reason")))
    else:
        match = rec.get("match") if rec.get("match") is not None else (scalar or {}).get("match")
        if rec.get("status") == "error":
            state = "open"
        elif runs and runs["verdict"] == QUERY_DEFECT:
            state = "defect"
        elif match is None:
            state = "open"
        else:
            state = "held" if match else "defect"
        delta = rec.get("delta_pct")
        note = None
        if state == "defect" and isinstance(delta, (int, float)) and not isinstance(delta, bool):
            note = f"agami is {delta * 100:+.1f}% from your number"  # `delta_pct` is a signed fraction (2d)
        # An "answer" row compares two answers. With one query there is no comparison to report, and
        # the section already says what agami returned and shows five of the rows: a third telling,
        # marked "could not check", reads as a failure where nothing failed.
        if not one_query:
            add("answer", state, yours_text, agami_text, note=note or rec.get("error") or steps_note,
                yours_hi=[yours_text] if state == "defect" and yours_text else None,
                agami_hi=[agami_text] if state == "defect" and agami_text else None)

    section = "sql"
    # 2 · what the two statements claim, side by side. The person's statement is the golden side.
    claims = rec.get("claims") or {}
    claim_list = (claims.get("claims") or []) if isinstance(claims, dict) else []
    if len(claim_list) >= 7 and all(c.get("status") == "unknown" and c.get("generated") is None and c.get("golden") is None for c in claim_list):
        add("claims", "open", "could not read", "could not read", note="one of the two statements could not be read, so nothing was compared")
        claim_list = []
    for claim in claim_list:
        name = claim.get("name")
        yours, agami = _readable(_claim_text(claim.get("golden"))), _readable(_claim_text(claim.get("generated")))
        status = claim.get("status")
        if yours is None and agami is None and status in ("agrees", "same"):
            continue
        state = "held" if status in ("agrees", "same") else "differs" if status == "differs" else "open"
        note = None
        graded = parts.get({"date_window": "date_window", "filter_predicates": "predicates"}.get(name, ""))
        if state == "open" and graded and graded.get("verdict") == CONFIRMED:
            # The ledger read this claim with more context (no date filter anywhere, say) and confirmed it.
            state, note = "held", None
            if name == "date_window":
                yours, agami = yours or "no date filter", agami or "no date filter"
        elif name == "date_window" and state == "open":
            note = (graded or {}).get("note") or "the window could not be read from one of the two queries"
            yours = yours or "could not read"
            agami = agami or "could not read"
        add(_CLAIM_KEYS.get(name, name), state, yours, agami, note=note,
            yours_hi=_only(yours, agami) if state == "differs" else None, agami_hi=_only(agami, yours) if state == "differs" else None)

    section = "checks"
    # 3 · every part of the statement the ledger graded, with agami's side where a receipt says. When
    # the statement graded IS agami's, reading its own receipt back into the agami column prints the
    # same word twice and reads as two sides agreeing.
    filters, metrics = _receipt_filters(agami_receipt), _receipt_metrics(agami_receipt)
    for part in ledger_rows:
        pid = part.get("part", "")
        fam = _part_family(pid)
        if fam in ("runs", "predicates", "date_window") or pid == "receipt:*":
            continue
        if fam == "scope" and part.get("verdict") == CONFIRMED:
            continue
        state = _STATE.get(part.get("verdict"), "open")
        ev = part.get("evidence") or {}
        yours = _PART_WORDS.get(fam, {}).get(state, state_words[state])
        agami = None
        if fam == "default_filter":
            t, _, expr = pid.split(":", 1)[1].partition(":")
            agami = filters.get(f"{t.lower().split('.')[-1]}:{expr.lower()}")
        elif fam == "metric":
            if ev.get("metric"):
                yours = f"matched {ev['metric']}" if state == "held" else f"matched {ev['metric']}, defined elsewhere"
            agami = metrics.get(pid.split(":", 1)[1].lower())
            if agami:
                agami = f"matched {agami}"
        elif fam == "cardinality" and ev.get("one_side"):
            yours = f"one row per key on {ev['one_side']}"
        elif fam == "dropped_rows" and ev.get("dropped") is not None:
            yours = f"{_fmt(ev.get('dropped'))} of {_fmt(ev.get('total'))} {ev.get('left')} rows"
        elif fam == "question_fit" and ev.get("fit"):
            yours = {"plausible": "yes", "doubtful": "doubtful", "no_question": "no question given"}.get(ev["fit"], ev["fit"])
        elif fam == "literal" and ev.get("near_miss") and state == "defect":
            yours = f"matches no rows; the data spells it {ev['near_miss']}"
        for mention in ev.get("prose") or []:
            if isinstance(mention, dict) and mention.get("text"):
                words.append(f"{mention.get('about') or mention.get('source') or 'the semantic model'}: \"{mention['text']}\"")
        # The fit judgment is about the statement against its question, which is the SQL section's
        # subject, not a check of the statement against the semantic model.
        section = "sql" if fam == "question_fit" else "checks"
        add(_part_key(pid), state, yours, None if one_query else agami,
            note=part.get("note") if state != "held" else None, family=fam)
    return rows, words



def _condense(rows: list[dict]) -> list[dict]:
    """Drop what a reader cannot act on, and count what repeats.

    Three rules, each removing a line that is true and useless. A wide column having no declared
    value list is the DEFAULT state of every free-text column, not a finding: the probe reads the
    whole column (`SELECT DISTINCT city ... LIMIT 26`) and ignores the statement's own filters, so a
    query pinned to one city still reported "more than 25 distinct values". Twelve
    `value x = y · exists` rows say one thing twelve times. And a dropped-rows count earns no line at
    all: it is counted over the whole table BEFORE the statement's own filters, so a query pinned to
    one city and one year reports that most of the table has no partner, which is arithmetic about
    the warehouse rather than anything about this query. It stays in `ledger.json` as a fact the run
    states; it is not something a person can act on, so it is not on the card.

    Anything that did not pass is kept, whole and in place: the point is to make the exceptions
    visible, not to hide them under a count.
    """
    out: list[dict] = []
    for family, group in _by_family(rows):
        if family == "values_declared":
            kept = [r for r in group if r["state"] != "noted"]
            held = [r for r in kept if r["state"] == "held"]
            out.extend(r for r in kept if r["state"] != "held")
            if held:
                out.append(_rollup(held, f"{len(held)} value list{'s' if len(held) > 1 else ''} declared"))
        elif family == "dropped_rows":
            continue
        elif family == "literal" and group and all(r["state"] == "held" for r in group) and len(group) > 1:
            out.append(_rollup(group, f"{len(group)} values checked, all exist"))
        else:
            out.extend(group)
    return out


def _by_family(rows: list[dict]) -> list[tuple[str | None, list[dict]]]:
    """The rows in their original order, with each family's members gathered at its first sighting."""
    order: list[str | None] = []
    groups: dict[str | None, list[dict]] = {}
    for row in rows:
        fam = row.get("family")
        key = fam if fam in _CONDENSED else id(row)  # an uncondensed row is its own group
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(row)
    return [(k if isinstance(k, str) else None, groups[k]) for k in order]


_CONDENSED = {"values_declared", "dropped_rows", "literal"}


def _rollup(group: list[dict], words: str) -> dict:
    """One row standing for several that all passed. It carries the count and nothing else: a reader
    who wants the members opens the section, where they are listed."""
    first = dict(group[0])
    first.update(key=words, state="held", yours=None, agami=None, note=None, rolled=len(group))
    first.pop("yours_hi", None)
    first.pop("agami_hi", None)
    return first


def result_set_for_sample(rec: dict) -> dict:
    """The table comparison from a row record, or an empty dict for a scalar row."""
    comparison = rec.get("comparison")
    if isinstance(comparison, dict) and isinstance(comparison.get("result_set"), dict):
        return comparison["result_set"]
    return {}


def _sample(row_dir: Path, score: Any, limit: int = 5, *, one_sided: bool = False) -> dict | None:
    """Up to five rows of the two results, side by side, read from the run's own CSVs.

    The CSVs are on disk already, so nothing here travels through `rows.jsonl` or the row record:
    the checkpoint stays free of result rows and only the page shows them. Columns are paired by the
    comparator, which pairs on VALUES, so a renamed column is the same column. Rows are aligned by
    position, which is honest only when both sides returned the same count and is reported as
    unaligned when they did not; a real row identity is the grain work, not this.

    Differing rows come first. Five arbitrary rows answer nothing; five rows chosen because they
    disagree are the sample a reader wants.
    """
    yours, agami = _read_csv(row_dir / "statement.csv"), _read_csv(row_dir / "actual.csv")
    if not agami:
        return None
    if one_sided:
        # The statement under test is agami's own, so `statement.csv` is agami's result under
        # another name. Two columns of the same numbers is not a comparison, and showing one as
        # yours would credit the person with a query they did not write.
        yours = []
    elif not yours:
        # The person wrote a statement and it produced no result: a failed run, a refusal. That is
        # not a one-sided answer, and listing agami's rows alone under a heading the person will
        # read as theirs is worse than showing no grid.
        return None
    if not yours:
        # A question on its own: only agami answered, and its rows are the whole of what there is to
        # look at. The section exists to show the answer, so one side is not a reason to show nothing.
        return {"pairs": [[c, c] for c in agami[0]], "rows": [], "shown": 0, "total": 0,
                "agami_rows": [r for r in agami[1:limit + 1]], "agami_total": len(agami) - 1,
                "aligned": False, "by_name": False, "only_yours": [], "only_agami": []}
    pairs = [p for p in ((score or {}).get("column_pairs") or []) if isinstance(p, (list, tuple)) and len(p) == 2]
    if not pairs:
        # The comparator returns no pairs at all when the row counts differ: it decides that before
        # pairing anything. That is exactly the case a reader wants to look at, so fall back to
        # identical names. Named pairing is weaker than pairing on values and the page says so.
        pairs = [(c, c) for c in yours[0] if c in set(agami[0])]
        if not pairs:
            return None
        by_name = True
    else:
        by_name = False
    yi = {c: i for i, c in enumerate(yours[0])}
    ai = {c: i for i, c in enumerate(agami[0])}
    pairs = [(a, b) for a, b in pairs if a in yi and b in ai]
    if not pairs:
        return None
    ybody, abody = yours[1:], agami[1:]
    aligned = len(ybody) == len(abody)
    out = []
    for n in range(min(len(ybody), len(abody)) if aligned else min(len(ybody), limit)):
        y = [ybody[n][yi[a]] if yi[a] < len(ybody[n]) else "" for a, _ in pairs]
        g = [abody[n][ai[b]] if ai[b] < len(abody[n]) else "" for _, b in pairs] if aligned else None
        out.append({"yours": y, "agami": g, "same": g is not None and y == g})
    out.sort(key=lambda r: r["same"])  # the rows that disagree first; False sorts before True
    # When the counts differ the rows cannot sit beside each other, but agami still answered, and a
    # grid showing five rows all labelled "yours" reads as though it did not. Carry its rows too.
    agami_rows = None if aligned else [[r[ai[b]] if ai[b] < len(r) else "" for _, b in pairs] for r in abody[:limit]]
    return {"pairs": [list(p) for p in pairs], "rows": out[:limit], "shown": min(len(out), limit),
            "agami_rows": agami_rows, "agami_total": len(abody),
            "total": len(ybody), "aligned": aligned, "by_name": by_name,
            "only_yours": [c for c in yours[0] if c not in {a for a, _ in pairs}],
            "only_agami": [c for c in agami[0] if c not in {b for _, b in pairs}]}


def _read_csv(path: Path) -> list[list[str]]:
    """A result CSV as rows of text. Empty when the file is absent or the statement never ran."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [row for row in csv.reader(fh)]


_COUNT_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def _count_word(n: int) -> str:
    """Words for nine and under, numerals above, per the Microsoft style guide. Row counts and
    measured values keep their numerals wherever they are: those are data, not prose."""
    return _COUNT_WORDS[n] if 0 <= n < len(_COUNT_WORDS) else f"{n:,}"


def _and_list(items: list[str]) -> str:
    """`a`, `a and b`, `a, b, and c`. The serial comma, per the same guide."""
    if len(items) < 3:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _summaries(rows: list[dict], result: dict, rec: dict) -> dict:
    """One line per section, read while the section is closed.

    The rule every one of them follows: name the exception when there is one, a count when there is
    not. "20 passed" tells a reader to move on; "1 problem in your query" tells them where to click.
    """
    def of(section):
        return [r for r in rows if r.get("section") == section]

    # The verdict above already gives the label; this line adds the measurement behind it, because
    # "9 of 10 rows match" is what turns a verdict into something a reader can weigh.
    data_rows = of("data")
    values = next((r for r in data_rows if r["key"] in ("values", "answer")), None)
    counts = next((r for r in data_rows if r["key"] == "rows"), None)
    not_graded = result.get("data") == "not_graded"
    if not_graded:
        # What agami returned, read from the record rather than from a diff row: with one query
        # there is no row comparing two answers, and this line is where the count belongs.
        answered = next((r for r in data_rows if r["key"] in ("answer", "rows")), None)
        said = (answered or {}).get("agami")
        if not said:
            said, _single = _recorded_display(rec.get("recorded"), None)
        data = f"agami answered: {said}" if said else "agami answered this one"
    elif result.get("data") == "could_not_compare":
        # The label is a chip's worth of words; a summary is a sentence.
        data = {"same query, answer not compared": "The two queries match, but the answers weren't compared.",
                "different query, answer not compared": "The two queries differ, and the answers weren't compared.",
                "could not compare": "The answers weren't compared."}.get(
            result.get("label") or "", "The answers weren't compared.")
    else:
        data = (values or {}).get("yours") or (counts or {}).get("yours") or result.get("label") or "not compared"
    data = str(data).rstrip(".") + "."
    extra = next((r for r in data_rows if r["key"] == "columns" and r["state"] != "held"), None)
    if extra and extra.get("yours_hi") and not not_graded:
        n = len(extra["yours_hi"])
        data += f" Your query returns {_count_word(n)} more column{'s' if n > 1 else ''}."

    sql_rows = of("sql")
    differs = [r["key"] for r in sql_rows if r["state"] == "differs"]
    fit = next((r for r in sql_rows if r.get("family") == "question_fit"), None)
    # The product's name stays lowercase at the head of a sentence, so this lead-in is written with
    # its own capitalisation rather than upper-cased from a variable.
    whose = "agami's query" if not_graded else "Your query"
    if not_graded:
        sql = "Only agami wrote a query for this row."
    else:
        sql = ("The two queries differ in " + _and_list(differs) + ".") if differs else "The two queries ask for the same things."
    if fit and fit["state"] != "held":
        sql = f"{whose} might not answer the question. " + sql

    model_rows = of("checks")
    # A rolled-up line stands for the checks it replaced, so the count is of CHECKS, not of lines.
    passed = sum(r.get("rolled", 1) for r in model_rows if r["state"] == "held")
    failed = [r for r in model_rows if r["state"] not in ("held", "noted")]
    if not failed:
        # Zero checks on a row nobody graded is not a clean row: nothing was checked, and the line
        # says which of the two it is rather than letting a silence read as a pass.
        nothing = "agami's query wasn't checked." if not_graded else "Nothing to check."
        model = {0: nothing, 1: "The check passed.", 2: "Both checks passed."}.get(
            passed, f"All {passed} checks passed.")
    else:
        model = f"{passed} check{'' if passed == 1 else 's'} passed."
    if failed:
        model += f" {_count_word(len(failed)).capitalize()} didn't: {failed[0]['key']}."
    unrun = int(result.get("unchecked") or 0)
    if unrun:
        model += f" {_count_word(unrun).capitalize()} couldn't be run."

    return {"data": data, "sql": sql, "checks": model}


_DEFINITIONAL = {"tables read", "selects", "filters", "date window", "join keys", "grouped by"}
_RESULT_LABEL = {
    ("matches", "same"): "match", ("matches", "different"): "same answer, different query",
    ("matches", "not_comparable"): "match",
    ("partly", "same"): "same rows, different columns", ("partly", "different"): "same rows, different columns",
    ("partly", "not_comparable"): "same rows, different columns",
    ("differs", "same"): "same query, different answer", ("differs", "different"): "different answer",
    ("differs", "not_comparable"): "different answer",
}
# The action's name, never the verdict's. "not graded yet" as both put the same three words on the
# card twice, once at 20px and once beside the very sentence explaining them.
_FIX_WORDS = {"ungraded": "your call", "query": "fix your query", "semantic_model": "fix the semantic model", "examples": "add an example",
              "question": "reword the question", "ask_again": "ask agami again", "none": "nothing to fix"}
_FIX_OWNER = {"ungraded": "nothing", "query": "you", "semantic_model": "model", "examples": "agami", "question": "question", "ask_again": "agami", "none": "nothing"}


_NOT_GRADED = {"data": "not_graded", "query": "not_comparable", "label": "not graded yet",
               "unchecked": 0, "differs_in": []}

_ERROR_LEAD = {
    "agami_failed": "agami's query failed",
    "yours_failed": "your query did not run",
    "nothing_to_compare": "both queries returned no rows, so there is nothing to compare",
    "unknown": "the run's files do not say why",
}


def _first_line(text: Any) -> str:
    """The first line of an error, the only line a card shows."""
    return str(text).strip().splitlines()[0].strip() if text else ""


def _error_cause(rec: dict) -> str | None:
    """Why an `error` row could not be compared, read from the row's files and never assumed: the
    person's statement did not run (or was refused), agami wrote no statement or its run failed,
    both results were empty, the row has nothing to compare against, or the files do not say."""
    if (rec.get("status") or "error") != "error":
        return None
    parts = ((rec.get("ledger") or {}).get("rows") or []) if isinstance(rec.get("ledger"), dict) else []
    by_part = {p.get("part"): p for p in parts}
    for part in ("runs", "scope"):
        if part in by_part and by_part[part].get("verdict") != CONFIRMED:
            return "yours_failed"
    # Read before the statement: an error row carries `recorded` as null whatever the cause (the 2d
    # rule), so a comparison that declined to score is the fact that says both ran and were empty.
    # `sql` is no longer that signal, being kept on an error row now so the card can show what failed.
    score = (rec.get("comparison") or {}).get("result_set") if isinstance(rec.get("comparison"), dict) else None
    if score and score.get("status") == "unscored":
        return "nothing_to_compare"
    if not rec.get("sql") or (rec.get("recorded") is None and rec.get("actual") is None and rec.get("error")):
        return "agami_failed"
    return "unknown"


def _result(rec: dict, diff: list[dict]) -> dict:
    """The result in two facts read by code: whether the data matches, and whether the two queries are
    the same. `label` is the plain-word pill; `unchecked` counts the checks on your query that could
    not run, shown as a tag rather than a status of their own."""
    if rec.get("status") == UNGRADED:
        return dict(_NOT_GRADED)   # nothing was compared, so there is no verdict until a person gives one

    rows = {r["key"]: r for r in diff}
    status = rec.get("status") or "error"
    if status == "error" or (rows.get("answer") or {}).get("state") == "open" and "rows" not in rows:
        data = "could_not_compare"
    elif "rows" in rows:
        cols = rows.get("columns"); values = rows.get("values")
        same_rows = rows["rows"]["state"] == "held"
        values_ok = values is None or values["state"] == "held"
        if same_rows and values_ok and (cols is None or cols["state"] in ("held", "noted")):
            data = "matches"  # a column only agami returned is noticed, not a difference in the answer
        elif same_rows and cols is not None and cols["state"] != "held" and _values_agree_on_shared_columns(rec):
            data = "partly"
        else:
            data = "differs"
    else:
        answer = rows.get("answer") or {}
        data = "matches" if answer.get("state") == "held" else "differs"
    claims = ((rec.get("claims") or {}).get("claims") or []) if isinstance(rec.get("claims"), dict) else []
    statuses = [c.get("status") for c in claims]
    columns_differ = "columns" in rows and rows["columns"]["state"] != "held"
    by_name = {c.get("name"): c.get("status") for c in claims}
    parts = ((rec.get("ledger") or {}).get("rows") or []) if isinstance(rec.get("ledger"), dict) else []
    part_verdicts = {p.get("part"): p.get("verdict") for p in parts}
    # A claim the ledger graded confirmed (no date filter anywhere, say) is not unknown for this purpose.
    for name, part in (("date_window", "date_window"), ("filter_predicates", "predicates")):
        if by_name.get(name) == "unknown" and part_verdicts.get(part) == CONFIRMED:
            by_name[name] = "agrees"
    statuses = list(by_name.values())
    definitional_unknown = any(by_name.get(n) == "unknown" for n in ("tables", "outputs", "filter_predicates", "date_window", "join_keys", "group_keys"))
    if (not claims or all(st == "unknown" for st in statuses)) and not columns_differ:
        query = "not_comparable"
    elif any(st == "differs" for st in statuses) or columns_differ:
        # The seven claims do not cover the projection; two queries that return different columns
        # are different queries even when every claim agrees.
        query = "different"
    elif definitional_unknown:
        # Nothing differs, but a claim that decides sameness could not be read: sameness is not established.
        query = "not_comparable"
    else:
        query = "same"
    # A check counts once: a claim the ledger carries as a part is counted as that part.
    graded_claims = {"date_window", "filter_predicates"} if part_verdicts else set()
    unchecked = sum(1 for p in parts if p.get("verdict") == UNRESOLVED) + sum(1 for n, st in by_name.items() if st == "unknown" and n not in graded_claims)
    if data == "could_not_compare":
        # The data could not be compared; the two statements still say what they are.
        label = {"same": "same query, answer not compared", "different": "different query, answer not compared"}.get(query, "could not compare")
    else:
        label = _RESULT_LABEL[(data, query)]
    differing = sorted(r["key"] for r in diff
                       if (r["state"] in ("defect", "differs") or (r["key"] == "columns" and r["state"] == "noted"))
                       and r["key"] in _DEFINITIONAL | {"ordered by", "limit", "columns"})
    return {"data": data, "query": query, "label": label, "unchecked": unchecked, "differs_in": differing}


def _values_agree_on_shared_columns(rec: dict) -> bool:
    """A table compare whose only difference is the column set. The comparator pairs columns by their
    values, so every pair it reports agrees by construction; the answer is partly the same when at
    least one pair exists beside a column of yours with no partner or a column of agami's with none.
    The score itself is 0.0 whenever any column of yours is unpaired, so it cannot be the test."""
    score = (rec.get("comparison") or {}).get("result_set") if isinstance(rec.get("comparison"), dict) else None
    if not score:
        return False
    acc = score.get("accuracy")
    if acc is not None and float(acc) >= 1.0:
        return True  # agami returned everything you did, and more
    pairs = score.get("column_pairs") or []
    share = score.get("paired_row_share")
    if pairs and share is not None:
        # The comparator says whether the paired columns agree on every row; before it did, every
        # pair it reported agreed by construction.
        return float(share) >= 1.0 and bool(score.get("unmatched_golden_columns") or score.get("unmatched_generated_columns"))
    if pairs:
        return bool(score.get("unmatched_golden_columns") or score.get("unmatched_generated_columns"))
    # an older score file without pairs: fall back to the matched share of the columns
    cols = list(((rec.get("statement_recorded") or {}).get("columns")) or [])
    unmatched = list(score.get("unmatched_golden_columns") or [])
    if acc is None or not cols:
        return False
    matched = len(cols) - len(unmatched)
    return matched > 0 and abs(float(acc) - matched / len(cols)) < 1e-6


def _fix(rec: dict, diff: list[dict], result: dict) -> str:
    """What to change, in this order: the query the ledger proved wrong, the gap the ledger measured,
    the examples when agami wrote a different query with nothing wrong behind it, the question when it
    was read differently, agami again when it failed, nothing when both facts match."""
    if rec.get("status") == UNGRADED:
        # The same vocabulary as every other row, supplied by the person instead of computed. Until
        # they choose, the card says so rather than suggesting an action nothing has established.
        return "ungraded"

    parts = ((rec.get("ledger") or {}).get("rows") or []) if isinstance(rec.get("ledger"), dict) else []
    fit = next((p for p in parts if p.get("part") == "question_fit"), None)
    cols = next((r for r in diff if r["key"] == "columns"), None)
    if (rec.get("status") or "error") == "error":
        cause = _error_cause(rec)
        if cause == "yours_failed":
            return "semantic_model" if any(p.get("verdict") == MODEL_GAP for p in parts) else "query"
        if cause == "nothing_to_compare":
            return "none"
        return "ask_again"
    if rec.get("status") == "expected_doubtful" or any(p.get("verdict") == QUERY_DEFECT for p in parts):
        return "query"
    if result["data"] == "partly" and cols and cols.get("yours_hi"):
        return "query"
    if any(p.get("verdict") == MODEL_GAP for p in parts):
        return "semantic_model"
    definitional = bool(set(result["differs_in"]) & _DEFINITIONAL)
    if fit and fit.get("verdict") != CONFIRMED:
        # A query that may not answer its question is never the example to teach, however the data fell.
        return "question"
    if result["data"] == "matches":
        return "examples" if result["query"] == "different" and definitional else "none"
    if result["data"] == "partly":
        return "none" if not definitional else "question"
    if result["data"] == "differs" and result["query"] == "same":
        return "ask_again"
    if result["data"] == "differs" and result["query"] == "different":
        return "examples"
    return "question"


_FIX_CHANGE = {
    "examples": (["Add your query as a prompt example for this question through /agami-save-correction: agami wrote a different query, and nothing in the semantic model explains why."],
                 ["The examples: add your query for this question."]),
    "ask_again": (["Ask the question again; the same query gave a different answer, so the data moved or a run failed."], ["agami: ask again."]),
}


def _change_for_fix(fix: str, rec: dict, diff: list[dict]) -> tuple[list[str], list[str], dict]:
    """The card's change text, its to-do and the words each decision box starts with, all from the one
    fix. The three used to come from three places and could disagree on one card."""
    parts = ((rec.get("ledger") or {}).get("rows") or []) if isinstance(rec.get("ledger"), dict) else []
    fit = next((p for p in parts if p.get("part") == "question_fit"), None)
    gaps = [r["key"] for r in diff if r["state"] == "gap"]
    cols = next((r for r in diff if r["key"] == "columns"), None)
    extra = list((cols or {}).get("yours_hi") or [])
    mistakes = _measured_mistakes(rec)
    prefill = {"change": "", "fix": "", "reword": rec.get("question") or "", "example": ""}
    cause = _error_cause(rec)
    if fix == "query" and cause == "yours_failed":
        error = _first_line(rec.get("error"))
        change = [f"Your query did not run{': ' + error if error else ''}. Fix it, then run this row again."]
        todo = ["Your query: fix it so it runs, then re-run."]
    elif fix == "query":
        if mistakes:
            change = [f"Fix your query: {', '.join(mistakes)}. Then run this row again."]
            prefill["fix"] = "; ".join(mistakes)
        elif extra:
            change = [f"Your query returns columns the question did not ask for: {', '.join(extra)}. Remove them, or name them in the question."]
            prefill["fix"] = "remove " + ", ".join(extra)
            prefill["reword"] = (rec.get("question") or "").rstrip(".?") + f", with {', '.join(extra)}?"
        else:
            change = ["Fix your query where the marks are red, then run this row again."]
        todo = ["Your query: fix the red rows, then re-run."]
    elif fix == "semantic_model":
        change = [f"The semantic model is missing: {', '.join(gaps)}. Add them through /agami-save-correction." if gaps
                  else "Decide which definition your team means. A change to the semantic model goes through /agami-save-correction."]
        todo = [f"The semantic model: {', '.join(gaps)}." if gaps else "The semantic model: decide the definition."]
        prefill["change"] = ("add " + ", ".join(gaps)) if gaps else ""
    elif fix == "examples":
        change, todo = list(_FIX_CHANGE["examples"][0]), list(_FIX_CHANGE["examples"][1])
    elif fix == "question":
        # The fit note already says what does not line up, in a full sentence. Prefixing a generic
        # instruction and suffixing the todo produced three fragments joined by a semicolon, starting
        # mid-sentence in lower case. Say the finding, then the one instruction.
        reason = _fit_reason((fit or {}).get("note"))
        change = [reason or "The question and your query do not ask the same thing.",
                  "Reword the question, or change your query, so they ask the same thing."]
        todo = ["The question: reword it and re-run."]
    elif fix == "ask_again":
        if cause == "agami_failed":
            change, todo = list(_OWNER_CHANGE["agami"][0]), list(_OWNER_CHANGE["agami"][1])
        elif cause == "unknown":
            change = ["Run this row again; it could not be compared and the run's files do not say why."]
            todo = ["Run the row again."]
        else:
            change, todo = list(_FIX_CHANGE["ask_again"][0]), list(_FIX_CHANGE["ask_again"][1])
    elif fix == "ungraded":
        # The action on this row is the person's decision, so the text points at it rather than
        # naming a change nothing measured. What the checks did find is already in the verdict.
        change = ["Read agami's answer and the query behind it, then say what to do below.",
                  "If the answer is right, add an example so agami writes it this way next time."]
        todo = ["Decide whether agami's answer is right."]
    elif fix == "none" and cause == "nothing_to_compare":
        change = ["Both queries returned no rows, so there is nothing to compare. Widen the date window or the filters in your query, then run this row again."]
        todo = ["Your query: widen the window or the filters, then re-run."]
    else:
        change, todo = list(_OWNER_CHANGE["keep"][0]), list(_OWNER_CHANGE["keep"][1])
    if fit and fit.get("verdict") != CONFIRMED and fit.get("note") and fix != "question":
        change.append(f"Also: {fit['note']}")
    return change, todo, prefill


def _owner(rec: dict, diff: list[dict]) -> str:
    """Who acts, from the evidence, in this order: a mistake in the query is the person's; a gap the
    ledger measured is the semantic model's; a question read differently is the question's."""
    status = rec.get("status")
    parts = ((rec.get("ledger") or {}).get("rows") or []) if isinstance(rec.get("ledger"), dict) else []
    fit = next((p for p in parts if p.get("part") == "question_fit"), None)
    differing = {r["key"] for r in diff if r["state"] in ("defect", "differs")}
    if status == "match":
        # The keep-offer (Phase 3e, and the page's keep gate) is made only for a one-cell answer whose
        # statement, if any, answers its question; a matched table or a doubtful fit is not offered.
        one_cell = _recorded_display(rec.get("recorded"))[1] or (rec.get("recorded") is None and rec.get("actual") is not None)
        # The parser's keep gate: a row with both a question and a statement needs a confirmed
        # question_fit part; a fit that is missing is as good as doubtful there.
        needs_fit = bool(rec.get("question") and rec.get("statement"))
        fit_ok = (fit is not None and fit.get("verdict") == CONFIRMED) if needs_fit else (fit is None or fit.get("verdict") == CONFIRMED)
        return "keep" if one_cell and fit_ok else "nothing"
    if status == "error":
        cause = _error_cause(rec)
        if cause == "yours_failed":
            return "model" if any(p.get("verdict") == MODEL_GAP for p in parts) else "you"
        return "nothing" if cause == "nothing_to_compare" else "agami"
    if status == "expected_doubtful" or any(p.get("verdict") == QUERY_DEFECT for p in parts):
        return "you"
    if any(p.get("verdict") == MODEL_GAP for p in parts):
        return "model"
    cols = next((r for r in diff if r["key"] == "columns"), None)
    extra_yours = bool(cols and cols.get("yours_hi"))
    if (extra_yours or differing & {"ordered by", "limit"}) and not (differing & {"tables read", "selects", "filters", "join keys", "grouped by", "values", "rows"}):
        # The two answers hold the same rows and differ in what the person's query returns or how
        # it orders them: that is the query to change, not a definition and not the question.
        return "you"
    if (fit and fit.get("verdict") != CONFIRMED) or (differing & {"tables read", "filters", "date window", "join keys", "grouped by"}):
        return "question"
    if status == "mismatch":
        return "question"
    return "nothing"


def _change(owner: str, rec: dict, diff: list[dict]) -> tuple[list[str], list[str]]:
    """Beat 4, templated from what the rows say: the gaps by name, the extra columns by name, the
    reason the fit is doubtful. The AI may rewrite these words; it never adds a fact they lack."""
    parts = ((rec.get("ledger") or {}).get("rows") or []) if isinstance(rec.get("ledger"), dict) else []
    change, todo = _OWNER_CHANGE[owner]
    change, todo = list(change), list(todo)
    if owner == "nothing" and rec.get("status") == "match":
        table = not _recorded_display(rec.get("recorded"))[1]
        change = ["The two answers match. A table is not kept as an example; nothing to change." if table
                  else "The numbers match, but the statement may not answer its question, so this row is not kept as an example."]
        todo = ["Nothing to do."]
    if owner == "model":
        gaps = [r["key"] for r in diff if r["state"] == "gap"]
        if gaps:
            change = [f"The semantic model is missing: {', '.join(gaps)}. Add them through /agami-save-correction."]
            todo = [f"The semantic model: {', '.join(gaps)}."]
    if owner == "you":
        cols = next((r for r in diff if r["key"] == "columns" and r["state"] == "defect"), None)
        red = _measured_mistakes(rec)
        if cols and cols.get("yours_hi") and not red:
            extra = ", ".join(cols["yours_hi"])
            change = [f"Your query returns columns the question did not ask for: {extra}. Remove them, or name them in the question."]
            todo = [f"Your query: remove {extra}, or name them in the question."]
        elif red:
            change = [f"Fix your query: {', '.join(red)}. Then run this row again."]
            todo = [f"Your query: {', '.join(red)}; then re-run."]
    fit = next((p for p in parts if p.get("part") == "question_fit"), None)
    if fit and fit.get("verdict") != CONFIRMED and fit.get("note"):
        line = f"Also: {fit['note']}"
        if owner == "question":
            change = [f"Reword the question, or change your query, so they ask the same thing. {fit['note']}"]
        elif line not in change:
            change.append(line)
    return change, todo


def _measured_mistakes(rec: dict) -> list[str]:
    """The parts the ledger proved wrong, in the page's words. A claim that differs between the two
    statements is a difference, never a mistake, so it is not in this list."""
    parts = ((rec.get("ledger") or {}).get("rows") or []) if isinstance(rec.get("ledger"), dict) else []
    return [_part_key(p["part"]) for p in parts
            if p.get("verdict") == QUERY_DEFECT and _part_family(p.get("part", "")) not in ("runs", "predicates", "date_window")]


def _one_sentence(text: Any, cap: bool = True) -> str:
    """Text as one sentence: capitalised unless it opens with the product's name, and stopped once."""
    said = str(text).strip().rstrip(".")
    if cap and said:
        said = said[0].upper() + said[1:]
    return said + "."


def _sentence(rec: dict, diff: list[dict]) -> str:
    status = rec.get("status")
    if status == UNGRADED:
        # The person decides what measurement cannot: whether this answer is right. What measurement
        # CAN say about the query behind it belongs in the same breath, so the call is an informed one.
        said = "agami answered this one. Whether the answer is right is your call."
        checks = [r for r in diff if r.get("section") == "checks"]
        short = [r["key"] for r in checks if r["state"] not in ("held", "noted")]
        if short:
            return said + f" The checks on agami's query didn't all pass: {_and_list(short)}."
        if checks:
            return said + " Every check on agami's query passed."
        return said
    red = [r["key"] for r in diff if r["state"] in ("defect", "differs") and r["key"] not in ("answer", "rows", "values")]
    mistakes = _measured_mistakes(rec)
    open_ = [r["key"] for r in diff if r["state"] == "open"]
    gaps = [r["key"] for r in diff if r["state"] == "gap"]
    if status == "match":
        return "The numbers match and every check passed." if not any(r["key"] == "rows" for r in diff) else "The two answers match row for row, and every check passed."
    if status == "match_unverified":
        return "The numbers match, but " + (f"these checks could not be confirmed: {', '.join(open_ + red + gaps)}." if (open_ or red or gaps) else "one check on your query could not be confirmed.")
    if status == "expected_doubtful":
        return f"Your query has a mistake ({', '.join(mistakes or red) or 'see the red rows'}), so the number you expected is doubtful."
    if status == "error":
        cause = _error_cause(rec) or "unknown"
        error = _first_line(rec.get("error"))
        with_error = cause in ("agami_failed", "yours_failed", "unknown") and error
        return f"This row could not be compared: {_ERROR_LEAD[cause]}{': ' + error if with_error else ''}."
    # What the two QUERIES differ in is the SQL section's summary; repeating it here made the card
    # say one thing twice and, on a row whose paired columns all agreed, contradict itself. This
    # sentence says what happened to the ANSWER, and leaves the queries to their own section.
    values = next((r for r in diff if r["key"] == "values"), None)
    scalar = next((r for r in diff if r["key"] == "answer"), None)
    measured = (values or {}).get("yours")          # the comparator's own phrasing, "9 of 10 rows match"
    if measured and " of " in str(measured):
        return _one_sentence(measured)
    if values is not None and values.get("state") == "open" and values.get("note"):
        # Nothing was compared. Saying which columns differ here would imply the values were looked
        # at and found wanting, which is the thing that made this card unreadable.
        return _one_sentence(values["note"])
    if scalar and scalar.get("note"):               # a number: how far apart the two are
        return _one_sentence(scalar["note"], cap=False)   # the product's name is lowercase
    where = [k for k in red + gaps if k in ("columns", "rows")]
    if where:
        return "The two answers differ in " + ", ".join(where) + "."
    return "The two answers do not match, and no check explains why."


def resume(reconcile_dir: Path) -> dict | None:
    """The newest run directory under `<artifacts_dir>/local/reconcile/` that still has rows to run,
    with its counts, or None. "Resume the reconcile" on a later day is this, then `next-chunk`."""
    candidates = sorted((p for p in reconcile_dir.iterdir() if p.is_dir() and (p / "intake.json").exists()),
                        key=lambda p: p.name, reverse=True)
    for run_dir in candidates:
        try:
            state = next_chunk(run_dir)
        except json.JSONDecodeError:
            continue  # an intake.json that is not JSON is not a run to resume
        except ValueError as exc:
            # A corrupt checkpoint is refused, never skipped: skipping would resume an older run and
            # leave this one's row to be run twice later.
            raise ValueError(f"{run_dir}: {exc}") from exc
        if not state["complete"]:
            return {"run_dir": str(run_dir), "finished": state["finished"], "remaining": state["remaining"],
                    "chunk_rows": state["chunk_rows"], "progress": state["progress"]}
    return None


def report_items(run_dir: Path) -> list[dict]:
    """One report item per row of rows.jsonl, every field templated from the run's own files."""
    records, bad = _done_rows(run_dir)
    if bad:
        raise ValueError(f"rows.jsonl has a line that cannot be read ({', '.join(bad)})")
    # In the order the person gave the rows, not the order the checkpoint happened to write them.
    # `record` replaces a row by dropping its old line and appending the new one, which is right for
    # an append log and wrong for a page: a row re-run after a fix would jump to the bottom, and two
    # renders of one run would not match.
    records = sorted(records, key=lambda r: (not isinstance(r.get("row"), int), r.get("row") or 0))
    items = []
    for rec in records:
        rec = dict(rec, status=rec.get("status") or "error")
        if isinstance(rec.get("error"), str):
            # The page shows the classifier's one line, never a driver's message with a host or a table in it.
            rec["error"] = rec["error"].strip().splitlines()[0][:200] if rec["error"].strip() else None
        n = rec.get("row")
        row_dir = run_dir / "rows" / str(n)
        agami_receipt = None
        for cand in (row_dir / "agami-receipt.json", row_dir / "receipt.json"):
            if cand.exists():
                agami_receipt = _load_json(cand)
                break
        if agami_receipt is None and str(rec.get("receipt_path") or "").endswith(".json") and Path(rec["receipt_path"]).exists():
            agami_receipt = _load_json(Path(rec["receipt_path"]))
        one_query = _one_query(rec)
        diff, words = _diff_rows(rec, agami_receipt)
        result = _result(rec, diff)
        fix = _fix(rec, diff, result)
        legacy_owner = _owner(rec, diff)
        # keep is the fix "nothing" on a row the keep gate accepts; the page's keep offer is that
        keep_ok = legacy_owner == "keep"
        owner = "keep" if (keep_ok and fix in ("none", "examples")) else _FIX_OWNER[fix]
        change, todo, prefill = _change_for_fix(fix, rec, diff)
        if fix == "none" and not keep_ok and (rec.get("status") or "error") != "error":
            # nothing to fix, and not kept either: say why in the words the status gives. An error
            # row with nothing to fix already says its cause (nothing to compare, or no ground truth).
            change, todo = _change("nothing", rec, diff)
        if result["data"] == "matches" and result["query"] == "different":
            clause = ("The two queries differ in: " + ", ".join(result["differs_in"]) + ("; the match may not hold on other data." if set(result["differs_in"]) & _DEFINITIONAL else "; a cosmetic difference."))
        else:
            clause = None
        cols_row = next((r for r in diff if r["key"] == "columns"), None)
        if cols_row and cols_row.get("agami_hi") and not cols_row.get("yours_hi"):
            clause = ((clause + " ") if clause else "") + f"agami also returned: {', '.join(cols_row['agami_hi'])}."
        # Every verdict above read the full diff; what follows is display only. Condensing first
        # would let a rolled-up line change a result, which is the one thing it must never do.
        diff = _condense(diff)
        summaries = _summaries(diff, result, rec)
        sample = _sample(run_dir / "rows" / str(n), result_set_for_sample(rec), one_sided=one_query)
        prov = rec.get("provenance") or {}
        shape_words = {"a": "a question", "b": "a question with your SQL", "c": "a number from your dashboard", "d": "a number with the SQL behind it"}
        source = ", ".join(p for p in (prov.get("source"), f"{prov['file']}:{prov['line']}" if prov.get("file") and prov.get("line") else prov.get("file"),
                                       shape_words.get(prov.get("shape"))) if p)
        result_set = (rec.get("comparison") or {}).get("result_set") if isinstance(rec.get("comparison"), dict) else None
        answer, single = _recorded_display(rec.get("recorded"), (result_set or {}).get("generated_row_count"))
        if answer is None and rec.get("actual") is not None:
            answer, single = _fmt(rec.get("actual")), True
        expected, _ = _recorded_display(rec.get("statement_recorded"), (result_set or {}).get("golden_row_count"))
        if expected is None and rec.get("expected") is not None:
            expected = _fmt(rec.get("expected"))
        delta = rec.get("delta_pct")
        items.append({
            "row": n, "label": rec.get("label"), "question": rec.get("question") or rec.get("label") or f"row {n}",
            "source": source or None, "status": rec.get("status") or "error",
            "status_words": status_words(rec.get("status")),
            "expected": expected, "answer": answer,
            "delta_pct": (round(delta * 100, 1) if isinstance(delta, (int, float)) and not isinstance(delta, bool) else None),
            "single_cell": bool(single), "owner": owner, "keep_allowed": keep_ok, "diff": diff,
            "result": result, "fix": fix, "fix_words": _FIX_WORDS[fix], "prefill": prefill,
            # Whether one query was written or two. The page's grid is two value columns, "yours"
            # beside "agami"; with one query there is no second side to put anywhere, and no "yours".
            "one_query": one_query,
            "sentence": _sentence(rec, diff) + (" " + clause if clause else ""),
            "words": words, "disagreement": None, "change": list(change), "todo": list(todo),
            "sql_yours": rec.get("statement") or None, "sql_agami": rec.get("sql") or None,
            "sql_agami_steps": _agami_steps(rec),
            "summaries": summaries, "sample": sample,
            "report_path": rec.get("report_path"),
        })
    return items

class RecordError(Exception):
    """A row record could not be built from the row directory: the message names what is missing."""


STAMP_NAME = "agami-reconcile-report"
_STAMP_RE = re.compile(r'<meta name="' + STAMP_NAME + r'" content="render_reconcile_report\.py (?P<run>\S+) (?P<digest>[0-9a-f]{12})">')


def items_digest(items: list[dict]) -> str:
    """Twelve hex characters over the items a page was rendered from, so the page can say which
    items it shows and `check-run` can tell a stale page from a current one."""
    return hashlib.sha256(json.dumps(items, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]


def stamp_for(run: str, items: list[dict]) -> str:
    return f'<meta name="{STAMP_NAME}" content="render_reconcile_report.py {run} {items_digest(items)}">'


def _csv_shape(path: Path) -> tuple[dict | None, Any]:
    """A result CSV as the record carries it: one cell as `{"columns", "rows": [[cell]]}`, anything
    else as `{"columns", "row_count"}`. Never result rows beyond one cell. The second value is that one
    cell, as a number when it reads as one. Nothing when the file is absent or names no column."""
    if not path.exists():
        return None, None
    with path.open(newline="", encoding="utf-8") as fh:
        rows = [row for row in csv.reader(fh)]
    if not rows or not any(cell.strip() for cell in rows[0]):
        # A zero-byte file is a run that returned nothing, never a result of no rows: the execution
        # tier writes CSV only on success, and a real result always has a header row that names its
        # columns. A blank line, or a header of nothing but spaces, names none, so it is no result either.
        return None, None
    columns, data = rows[0], [r for r in rows[1:] if any(cell.strip() for cell in r)]
    if len(data) == 1 and len(data[0]) == 1:
        cell = data[0][0]
        value = parse_value(cell)
        kept = value if value is not None else cell
        return {"columns": columns, "rows": [[kept]]}, kept
    return {"columns": columns, "row_count": len(data)}, None


def _run_result(path: Path, run: Any) -> tuple[dict | None, Any]:
    """`_csv_shape` of the CSV a run wrote, or nothing when the run record beside it is there and does
    not say `ok`: a refused or failed run can still leave its CSV behind. No run record says nothing
    either way, so the CSV decides alone."""
    if run is not None and not (isinstance(run, dict) and run.get("status") == "ok"):
        return None, None
    return _csv_shape(path)


def _run_record(path: Path) -> Any:
    """The run record at `path`, or None when it is absent or cannot be read. An empty or broken file
    is `_load_json`'s error object, with `error` and no `status`. It says nothing about how the run
    went, so it counts as no record: the CSV beside it decides, and no parser message becomes the
    reason a statement did not run."""
    run = _optional_json(path)
    return None if isinstance(run, dict) and run.get("error") and "status" not in run else run


def _optional_json(path: Path) -> Any:
    return _load_json(path) if path.exists() else None


def record(run_dir: Path, row: int, *, tolerance: float = 0.01, report_path: str | None = None) -> dict:
    """The row record Phase 2d writes, built from the row directory's files and appended to
    `rows.jsonl` (replacing an earlier record for the same row). Nothing in it is typed by the
    session: the question and the statement come from `intake.json`, agami's statement from
    `agami-answer.json`, the two results from their CSVs (one cell, or the shape), the comparison from
    `diff.json` or `comparison.json`, the grades from `ledger.json`, the claims from `claims.json`.

    A question-only row is recorded `ungraded`: agami answered and nothing here can say whether the
    answer is right, so the row waits for a person on the report page (Phase 2.5) and is never
    written as `error`. Its expected value never comes from the run: a value the run supplied would
    be agami's own answer coming back as its own answer key.
    """
    intake = _load_json(run_dir / "intake.json")
    rows = intake.get("rows") if isinstance(intake, dict) else intake
    if not isinstance(rows, list):
        raise RecordError(f"{run_dir / 'intake.json'} is missing or holds no list of rows")
    base = None
    for n, candidate in enumerate(rows, 1):
        if isinstance(candidate, dict) and int(candidate.get("row", n)) == row:
            base = candidate
            break
    if base is None:
        raise RecordError(f"row {row} is not in intake.json")
    row_dir = run_dir / "rows" / str(row)
    answer = _optional_json(row_dir / "agami-answer.json")
    if not isinstance(answer, dict) or answer.get("error") in ("empty_file", "unreadable_json"):
        raise RecordError(f"rows/{row}/agami-answer.json is missing or unreadable; ask agami (Phase 2b) first")
    sql = answer.get("sql") if isinstance(answer.get("sql"), str) and answer["sql"].strip() else None
    statements = [st for st in (answer.get("statements") or []) if isinstance(st, str) and st.strip()]
    agami_run = _run_record(row_dir / "agami-run.json")
    recorded, actual_cell = _run_result(row_dir / "actual.csv", agami_run)
    statement = base.get("statement") or None
    statement_recorded, statement_cell = (
        _run_result(row_dir / "statement.csv", _run_record(row_dir / "run.json")) if statement else (None, None))
    exp = base.get("expected")
    if exp is None and statement and isinstance(statement_cell, (int, float)) and not isinstance(statement_cell, bool):
        exp = float(statement_cell)  # Phase 1.5f: the statement's own result is the expected value
    ledger = _optional_json(row_dir / "ledger.json")
    ledger = ledger if isinstance(ledger, dict) and not ledger.get("error") else None
    ledger_verdict = ledger.get("verdict") if ledger else None
    claims = _optional_json(row_dir / "claims.json")
    claims = claims if isinstance(claims, dict) and not claims.get("error") else None
    score = _optional_json(row_dir / "comparison.json")
    diff_file = _optional_json(row_dir / "diff.json")
    actual = actual_cell if isinstance(actual_cell, (int, float)) and not isinstance(actual_cell, bool) else None

    error: str | None = None
    comparison: dict | None = None
    match: bool | None = None
    delta = delta_pct = None
    if sql is None:
        error = answer.get("error") or "agami wrote no statement"
    elif recorded is None:
        detail = agami_run if isinstance(agami_run, dict) else {}
        error = detail.get("detail") or detail.get("kind") or detail.get("status") or "agami's statement was not run, or its result was not recorded"
        error = f"agami's statement did not run: {error}" if detail else error
    elif exp is None and statement is None:
        # A question on its own: the person supplied no statement and no number, so there is nothing
        # of theirs to compare against and NO file on disk can change that. `comparison.json` and
        # `diff.json` are already loaded above; what this branch's position decides is that neither
        # is ever allowed to set `match` here. Phase 2.5 writes agami's own query as `statement.sql`
        # so the ledger can grade it, which means `statement.csv` holds agami's own result, so a
        # score built from it compared agami against agami and returned accuracy 1.0. With this
        # branch below them the row recorded `match`, and the card said "the two answers match row
        # for row" about one answer.
        pass
    elif isinstance(score, dict) and score.get("status") in ("scored", "unscored", "error"):
        comparison = {"result_set": score}
        match = (score.get("accuracy") == 1.0) if score.get("status") == "scored" else None
    elif isinstance(diff_file, dict) and "match" in diff_file:
        comparison = {"scalar": diff_file}
        match, delta, delta_pct = diff_file.get("match"), diff_file.get("delta"), diff_file.get("delta_pct")
    elif exp is not None and actual is not None and isinstance(exp, (int, float)) and not isinstance(exp, bool):
        scalar = diff(float(exp), float(actual), tolerance=tolerance)
        comparison = {"scalar": scalar}
        match, delta, delta_pct = scalar["match"], scalar["delta"], scalar["delta_pct"]
    else:
        error = ("the two results are tables, and they were not compared"
                 if exp is None or actual is None else "the two values could not be compared")
    status = UNGRADED if (match is None and error is None and exp is None and statement is None) \
        else row_status(match, ledger_verdict)
    is_error = status == ERROR
    provenance = dict(base.get("provenance") or {})
    rec = {
        "row": row, "label": base.get("label"), "question": base.get("question"),
        "expected": exp, "actual": None if is_error else actual, "delta_pct": None if is_error else delta_pct,
        "match": match, "status": status, "report_path": report_path,
        # The 2d rule: an error row carries no RESULT anyone could mistake for a verified answer.
        # The statement is kept, and the difference between the two is the whole point. A result
        # implies the query ran; a statement that failed cannot be mistaken for one that answered,
        # because the row's own error sentence says it did not. Reading it is how a person tells a
        # semantic model declaring a column the warehouse does not have from a query agami wrote
        # wrong, and dropping it left the SQL section of a failed row empty, which is exactly where
        # that reading would have happened.
        "sql": sql, "recorded": None if is_error else recorded,
        "error": error,
        "provenance": provenance, "statement": statement, "statement_recorded": statement_recorded,
        "statement_receipt_path": str(row_dir / "statement-receipt.json") if (row_dir / "statement-receipt.json").exists() else None,
        "receipt_path": str(row_dir / "receipt.json") if (row_dir / "receipt.json").exists() else None,
        "ledger": ledger, "ledger_verdict": ledger_verdict, "comparison": comparison, "claims": claims,
        "finding_keys": [],
        "agami_statements": [] if not sql or len(statements) < 2 else statements,
    }
    if delta is not None:
        rec["delta"] = delta
    path = run_dir / "rows.jsonl"
    kept: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                old = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)  # next-chunk refuses it; not this verb's to drop
                continue
            old_row = old.get("row") if isinstance(old, dict) else None
            if isinstance(old_row, str) and old_row.strip().isdigit():
                old_row = int(old_row)
            if old_row != row:
                kept.append(line)
    path.write_text("".join(line + "\n" for line in kept) + json.dumps(rec) + "\n", encoding="utf-8")
    return rec


def check_run(run_dir: Path) -> dict:
    """Whether the run directory and its page agree: every row in the checkpoint is a row of the
    intake and has the files its record names, `report.html` exists, and its stamp is the digest of
    the items the run's files build now. Exit 4 with the list when anything is off."""
    problems: list[str] = []
    intake = _optional_json(run_dir / "intake.json")
    rows = intake.get("rows") if isinstance(intake, dict) else intake
    if not isinstance(rows, list):
        problems.append("intake.json is missing or holds no list of rows")
        rows = []
    intake_ids = {int(r.get("row", n)) for n, r in enumerate(rows, 1) if isinstance(r, dict)}
    records, bad = _done_rows(run_dir)
    problems.extend(f"rows.jsonl: {line} cannot be read" for line in bad)
    for rec in records:
        n = rec.get("row")
        if n not in intake_ids:
            problems.append(f"rows.jsonl: row {n} is not in intake.json")
        row_dir = run_dir / "rows" / str(n)
        if not (row_dir / "agami-answer.json").exists():
            problems.append(f"rows/{n}/agami-answer.json is missing")
        if rec.get("statement") and not (row_dir / "ledger.json").exists():
            problems.append(f"rows/{n}/ledger.json is missing for a statement row")
        # An error row keeps agami's statement now, and a statement that did not run has no result
        # file to point at. The row is complete without one, so the demand is on rows that answered.
        if rec.get("sql") and rec.get("status") != ERROR and not (row_dir / "actual.csv").exists():
            problems.append(f"rows/{n}/actual.csv is missing although the record carries agami's statement")
    page = run_dir / "report.html"
    if not page.exists():
        problems.append("report.html is missing; render it")
    elif records:
        m = _STAMP_RE.search(page.read_text(encoding="utf-8", errors="replace"))
        current = items_digest(report_items(run_dir))
        if not m:
            problems.append("report.html carries no render stamp; it was not written by render_reconcile_report.py")
        elif m.group("digest") != current:
            problems.append(f"report.html was rendered from other items (stamp {m.group('digest')}, current {current}); render it again")
    if records and not (run_dir / "report-items.json").exists():
        problems.append("report-items.json is missing; render the page from the run directory")
    return {"ok": not problems, "rows": len(records), "problems": problems}


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

    p_chunk = sub.add_parser("next-chunk", help="The next rows to run: those in the run's intake.json not yet in its rows.jsonl.")
    p_chunk.add_argument("--run-dir", required=True, dest="run_dir")
    p_chunk.add_argument("--rows-file", default=None, dest="rows_file",
                         help="the applied intake rows; copied to <run-dir>/intake.json when that file does not exist yet")
    p_chunk.add_argument("--size", type=int, default=CHUNK_SIZE, help=f"rows per chunk (default {CHUNK_SIZE})")

    p_resume = sub.add_parser("resume", help="The newest run under a reconcile directory that still has rows to run.")
    p_resume.add_argument("--reconcile-dir", required=True, dest="reconcile_dir", help="<artifacts_dir>/local/reconcile")

    p_record = sub.add_parser("record", help="Build one row's record (Phase 2d) from its row directory and append it to rows.jsonl.")
    p_record.add_argument("--run-dir", required=True, dest="run_dir")
    p_record.add_argument("--row", required=True, type=int)
    p_record.add_argument("--tolerance", default="0.01", help="the scalar diff's tolerance when no diff.json was written")
    p_record.add_argument("--report-path", default=None, dest="report_path", help="the chart report for agami's statement, when there is one")

    p_check = sub.add_parser("check-run", help="Whether the run directory and its report page agree; exit 4 with the list when not.")
    p_check.add_argument("--run-dir", required=True, dest="run_dir")

    p_items = sub.add_parser("report-items", help="Build the report page's items from a run's rows.jsonl, ledgers, comparisons and receipts.")
    p_items.add_argument("--run-dir", required=True, dest="run_dir")
    p_items.add_argument("--out", default=None, help="where the items file goes (default <run-dir>/report-items.json)")

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

    if args.cmd == "resume":
        root = Path(args.reconcile_dir).expanduser()
        if not root.is_dir():
            print(f"reconcile resume: directory not found: {root}", file=sys.stderr)
            return 2
        try:
            found = resume(root)
        except ValueError as exc:
            print(f"reconcile resume: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(found, indent=2))
        # Exit 4, "nothing to do": every run under the directory is complete, or there is none.
        return 0 if found else 4

    if args.cmd == "record":
        run_dir = Path(args.run_dir).expanduser()
        try:
            rec = record(run_dir, args.row, tolerance=parse_value(args.tolerance) or 0.01,
                         report_path=args.report_path)
        except RecordError as exc:
            print(f"reconcile record: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(rec, indent=2))
        return 0
    if args.cmd == "check-run":
        run_dir = Path(args.run_dir).expanduser()
        if not run_dir.is_dir():
            print(f"reconcile check-run: run directory not found: {run_dir}", file=sys.stderr)
            return 2
        result = check_run(run_dir)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 4
    if args.cmd == "report-items":
        run_dir = Path(args.run_dir).expanduser()
        if not run_dir.is_dir():
            print(f"reconcile report-items: run directory not found: {run_dir}", file=sys.stderr)
            return 2
        if not (run_dir / "rows.jsonl").exists():
            print("reconcile report-items: no rows to read; the run wrote no rows.jsonl", file=sys.stderr)
            return 4
        try:
            items = report_items(run_dir)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"reconcile report-items: {exc}", file=sys.stderr)
            return 2
        out = Path(args.out).expanduser() if args.out else run_dir / "report-items.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(items, indent=2), encoding="utf-8")
        by_status: dict[str, int] = {}
        for item in items:
            by_status[item["status"]] = by_status.get(item["status"], 0) + 1
        print(json.dumps({"items": len(items), "out": str(out), "by_status": by_status}, indent=2))
        return 0

    if args.cmd == "next-chunk":
        run_dir = Path(args.run_dir).expanduser()
        if not run_dir.is_dir():
            print(f"reconcile next-chunk: run directory not found: {run_dir}", file=sys.stderr)
            return 2
        if args.size < 1:
            print("reconcile next-chunk: --size must be at least 1", file=sys.stderr)
            return 2
        intake_path = run_dir / "intake.json"
        if args.rows_file and not intake_path.exists():
            src = Path(args.rows_file).expanduser()
            if not src.exists():
                print(f"reconcile next-chunk: rows file not found: {src}", file=sys.stderr)
                return 2
            try:
                seed = json.loads(src.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                print(f"reconcile next-chunk: the rows file is not JSON: {exc}", file=sys.stderr)
                return 2
            seed_rows = seed.get("rows") if isinstance(seed, dict) else seed
            if not isinstance(seed_rows, list) or not all(isinstance(r, dict) for r in seed_rows):
                print("reconcile next-chunk: the rows file holds no list of row objects; nothing seeded", file=sys.stderr)
                return 2
            intake_path.write_text(json.dumps(seed, indent=2), encoding="utf-8")
        if not intake_path.exists():
            print(f"reconcile next-chunk: no intake.json in {run_dir}; pass --rows-file once to seed it", file=sys.stderr)
            return 2
        try:
            result = next_chunk(run_dir, size=args.size)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"reconcile next-chunk: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2))
        # Exit 4, "nothing to do", when every row is in the checkpoint: the skill reads it as
        # "go to Phase 3", with the JSON above still carrying the counts for the summary.
        return 4 if result["complete"] else 0

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
        csv.field_size_limit(sys.maxsize)  # a long SQL cell is a row, not an error
        try:
            result = intake(paths, source=args.source)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, csv.Error, RecursionError) as exc:
            print(f"reconcile intake: could not read the input: {str(exc).splitlines()[0][:300]}", file=sys.stderr)
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
