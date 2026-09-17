# RED baseline: agami-reconcile before ACE-116

Date: 2026-09-11. Commit: `7185577` (worktree ACE-115). File walked: `plugins/agami/skills/agami-reconcile/SKILL.md`. Read-only; no database was run.

Note first: `scripts/reconcile.py` at this commit already has `intake`, `ledger` and `findings` subcommands. SKILL.md never names them. The prose knows only `parse`, `diff` and `band`.

## Input A: CSV with a SQL column

**Phase 0.4.** "a path in `$ARGUMENTS`, or pasted inline" then "Go to Phase 1's **CSV branch**". Detected as a CSV. Fine so far.

**Phase 1, CSV branch.** "2-column or 3+ column inputs (3rd onward are appended to the label as context)". Ran `python3 plugins/agami/scripts/reconcile.py parse --csv /tmp/red-a.csv`:

```json
[{"label": "Total orders (SELECT COUNT(*) AS order_count FROM orders)", "expected_value": 4000.0, "raw_value": "4000"},
 {"label": "Delivered orders (SELECT COUNT(*) FROM orders WHERE status = 'Delivered')", "expected_value": 812.0, "raw_value": "812"},
 {"label": "Paid revenue (SELECT SUM(o.total_amount) FROM payments pay JOIN orders o ON o.id = pay.id)", "expected_value": 183220.5, "raw_value": "183220.50"}]
```

Each statement was glued onto the label as a parenthetical. The header name `SQL` only marked row one as a header, then was dropped. There is no `sql` key in the output.

**Phase 2a.** "Use the LLM to translate `label` (+ context if present) into the most natural English question". I would read the person's SQL as a wording hint ("How many orders are there?") and nothing more.

**Phase 2b.** "Invoke the same SQL-generation + execution path agami-query uses"; the capture list begins "The generated SQL". Agami's own statement runs. The person's never does. The row record's `sql` holds agami's statement; the person's survives only inside `label`.

**Phase 2c.** `diff --expected --actual`. Numbers only. Row 3's join `ON o.id = pay.id` (an order id matched to a payment id) is never looked at. Row 1 omits the default filter `status != 'cancelled'`; agami-query applies it, so agami likely returns under 4000, Phase 3b calls it a "mismatch", and the prose has me write an interpretation in the shape "If your dashboard nets refunds, that's the gap". No sentence exists for "your statement is the one that is wrong". Row 2's literal `'Delivered'` (case, and whether the value exists) is never checked.

**Phase 3e.** "Only rows whose `status` is `match` are offered." Had row 3's bad join landed within 1% of agami's number, it would be offered as a `status: "confirmed"` example on that number alone.

## Input B: a trusted statement pasted alone

**Phase 0.4** lists three shapes: "A screenshot / image", "A CSV", "Numbers pasted inline as a list/table". A bare SELECT is none of them. Two readings:

- Literal: it is not numbers, so "If they gave nothing (or just asked "can you check my dashboard?"), ask once". The statement is discarded.
- Generous: "treat as inline CSV". Wrote it to a file and ran `parse`: output `[]`. The commas inside `IN ('pending','paid','Hold')` split the line into three cells, `'paid'` is not a number, so `_looks_like_header` calls the only row a header. The cheat sheet then says: "No rows with parseable numeric values. Common cause: the value column has formatting like `$1,234.56 (USD)`". Wrong advice for a SQL statement.

**Phase 1** has nothing to extract either way. No phase runs the person's statement: Phase 2b runs only what agami generates from a question, and there is no question. B has no join; its other parts (the `IN` list with `'Hold'`, the missing `status != 'cancelled'` default filter) are never examined, because nothing in the skill reads SQL as SQL.

## Input C: three bare questions

**Phase 0.4.** Not a screenshot, not a CSV, not numbers. "ask once" applies; the cheat sheet adds "don't proceed without the expected numbers". The skill stops and asks for a dashboard. It does not hand off to agami-query; no sentence names that route.

Treated as inline CSV, `parse` returns `[]` (one cell per row; `len(r) < 2` skips it), again producing the "parseable numeric values" refusal.

No phase grades an answer without an expected number. `diff` with an empty `--expected` returns `{"match": false, "reason": "missing_expected"}`, and Phase 3e says such rows "never reached `match` either". The Phase 2d record requires `"expected": <number>`. There is no other grade.

## Yes/no

1. **Does the skill ever grade a part of a statement the person supplied? No.** The helper is "CSV parser + number normalization + diff with tolerance"; Phase 2c: "Tolerance applies to numeric comparisons"; extra columns are "appended to the label as context". `ledger` exists in the script; the skill never calls it.

2. **Is a matching number sufficient to offer the row in 3e? Yes.** "Only rows whose `status` is `match` are offered." and "it is the only notion of agreement this skill has, so nothing here re-judges a number." The only other gates: "Only a single-cell result is offered" and "A row with no statement is never offered". The line "a row accepted on its number alone is a row nobody checked the meaning of" puts that check on the person, not the skill.

3. **Does hard rule 3 forbid writing findings about the semantic model to disk? No.** It forbids mutation: "Reconcile reads + diffs; it never mutates a metric, a join, a column". Hard rule 4 already lists non-3e writes (`/tmp/agami-reconcile-results-*.jsonl`). One loose sentence to tighten when editing: "The writes this skill can make are Phase 3e's".

4. **Does the plan-mode refusal assume a CSV path? Yes.** `plugins/agami/shared/plan-mode-check.md`: "re-invoke me with the CSV path." SKILL.md Phase 0.1 repeats it verbatim.
