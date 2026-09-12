# The evidence row

Shared by `agami-reconcile` (which reads every input through it) and `agami-save-correction` (which
reads a pasted statement the same way). Read this before writing code or prose that consumes what a
person hands over: a screenshot, a spreadsheet, a query they trust, a list of questions, or a mix.

**The person's query is evidence, never the answer.** Every row here is something to check. Nothing in
a row is trusted until the checks in [`part-ledger.md`](part-ledger.md) have run.

## The four shapes, and the one row they all become

Any input reduces to rows of three optional fields plus where it came from:

```json
{"label": "Q3 Revenue", "question": null, "statement": "SELECT SUM(total) FROM orders WHERE q = 3",
 "expected": 4200000.0, "raw_value": "$4.2M",
 "provenance": {"shape": "d", "source": "the finance dashboard", "file": "tiles.csv", "line": 2,
                "graded": null}}
```

| Shape | What the person handed over | `question` | `statement` | `expected` |
|---|---|---|---|---|
| **a** | a list of questions | given | none | none, until the person grades the AI's answer |
| **b** | questions with the SQL they trust | given, or derived from the statement and confirmed | given | produced by running the statement |
| **c** | a dashboard screenshot, or a label-and-number table | a label, turned into a question and confirmed | none | read by code, confirmed by the person |
| **d** | a screenshot or table with the SQL behind each tile | a label | given | read by code, and checked against the statement's own result |

`provenance.shape` is per row. The input's own `shape` is one letter when every row agrees and
`mixed` otherwise.

## How `reconcile.py intake` reads an input

Run it once per input, with every file the person gave:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" intake --file <path> [--file <path> ...] \
  [--source "<the person's own words for where this came from>"] > /tmp/agami-reconcile-rows-<ts>.json
```

Detection is by content, never by asking:

- **A cell that starts with `SELECT` or `WITH`** is a statement. A `.sql` file is statements split on
  `;`, and every chunk gets the same test: one that is not a `SELECT` or a `WITH` is skipped with a
  reason, never a statement row.
- **A `.txt` or `.md` file, or a file with no commas or tabs,** is one question per line; a line that
  starts with `SELECT` or `WITH` is a statement.
- **A `.json` file** is a list of strings (questions) or objects with any of the keys `label`,
  `question`, `value`, `sql`, or their common spellings (`metric`, `tile`, `expected`, `statement`,
  `query`, `prompt`, and the like).
- **Anything else is CSV.** A header row is recognised by name (`label`, `metric`, `value`,
  `expected`, `sql`, `statement`, `question`, and their common spellings), or by the shape
  `parse_csv` always recognised: a non-numeric second cell over rows whose second cells are numbers.
  Without a header: one cell is a question; two cells are a question and a statement when the second
  is SQL, or a label and a number when it parses; three or more cells are a label, a number, and a
  statement when one of the rest is SQL. A third cell that is not SQL is glued onto the label as
  context, exactly as `parse` has always done.
- **Numbers are parsed by code**, through `parse_value`, never by the AI eyeballing them. The
  vision branch of the skill writes what it read to a CSV first, so it goes through the same reader.

**Mixed input merges by label.** A statement whose label matches a tile's label, under a case and
whitespace fold, joins that tile's row and the row's shape becomes `d`. An unmatched statement gets
its own row (shape `b`); an unmatched tile stays a number-only row (shape `c`).

**What is skipped, and what is refused.** A row whose second column is neither a number nor a
statement is skipped with a reason, in `skipped`. A file that does not exist exits `2`. An input with
no question, statement or number anywhere exits `4`. Nothing is guessed.

## The row record the skill keeps

Phase 2d of `agami-reconcile` writes one record per row to `rows.jsonl`. Every key that record has
always had stays, with the same meaning: `label`, `question`, `expected`, `actual`, `delta_pct`,
`match`, `status`, `report_path`, `sql`, `recorded`, `error`. These keys are appended after `error`:

| Key | Holds |
|---|---|
| `provenance` | the block above: `shape`, `source`, `file`, `line`, `graded` (`null`, `right`, `wrong` or `unsure` once the person has graded a shape-a row), and `merged_from` (the file and line of a statement that joined a tile's row by label) |
| `statement` | the person's SQL, verbatim; `null` when they gave none |
| `statement_recorded` | what the person's SQL returned: one cell as `{"columns": [...], "rows": [[v]]}`, or `{"columns": [...], "row_count": n}` for a table. Never the rows of a table |
| `statement_receipt_path` | the receipt of the person's SQL, in the row directory |
| `receipt_path` | the receipt of the AI's SQL |
| `ledger`, `ledger_verdict` | the graded parts and the weakest grade, from `reconcile.py ledger` |
| `comparison` | `{"scalar": <diff>}` or `{"result_set": <compare-results>}` |
| `claims` | the `sm claims` diff between the two statements, when both exist |
| `finding_keys` | the keys of the findings this row contributed to |
| `words` | what the person wrote beside a `wrong` grade when they gave no SQL; `null` otherwise |

## The status a row gets

`status` keeps its three old values and gains two:

| Status | When |
|---|---|
| `match` | the numbers agree within tolerance, and every graded part is `confirmed` (or there was no statement to grade) |
| `match_unverified` | the numbers agree, but a part of the person's statement is not `confirmed`. Never offered in Phase 3e: a match nobody could verify may be luck. A doubtful `question_fit` (Phase 1.5g: the statement may not answer its question) is such a part, so a sound statement paired with the wrong question is never kept as an example |
| `mismatch` | the numbers differ and the person's statement has no `query_defect`, so the AI is the likelier culprit |
| `expected_doubtful` | the numbers differ and the person's statement has a `query_defect`, so the expected value itself is in doubt. Kept out of the mismatch tally |
| `error` | the row could not run; `sql` and `recorded` are `null`, as they always were |

`match` (the boolean) stays `reconcile.diff`'s verdict on the numbers alone.
