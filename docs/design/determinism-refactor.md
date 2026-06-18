# Determinism refactor — move mechanical orchestration out of LLM prose

**Status:** in progress · **Owner:** agami core · anchor doc for a multi-PR effort.

## Why

agami's skills are LLM prose that interleaves *judgment* (what does the user
mean? what does this column mean? is this number real?) with *mechanical
plumbing* (resolve a path, parse a pasted block, format a number, write a
config). When the LLM has to faithfully execute the plumbing, it sometimes
doesn't — the fidelity failures we keep hitting are almost all in the plumbing,
not the judgment: a wrong interpreter written to `.config`, a `/sample` vs
`/agami-example` mis-wire, a `sqlite3` query missing `-header`, the model
**re-typing result numbers** into a chart (a silent wrong-number risk).

The deterministic *core* is already heavily scripted (13 scripts + ~30 `sm`
subcommands). The gap is the **orchestration** around it, still in prose.

## Principle — thick deterministic core, thin LLM orchestration, judgment escape hatch

1. **Every deterministic step is a script that emits structured JSON** —
   `{status, data, anomalies: [...], needs_judgment?: {...}}`. The skill prose
   shrinks to: *call script → read JSON → branch* (continue / ask the user at a
   seam / hand an anomaly to the LLM).
2. **The LLM is reserved for exactly four things:**
   - **Seams** — interactive choices (`AskUserQuestion`): which DB, profile name,
     which schemas, copy-vs-rebuild, etc. These are the natural boundaries
     between deterministic script segments. *The flow is `script → ask → script`,
     never one monolith.*
   - **Authoring** — prose and SQL: table/column descriptions, entity names,
     metric definitions, NL→SQL, the answer insight. Genuine creativity.
   - **Decide-what, script-does** (the **Hybrid** pattern, already used by
     `sm curate` / `sm add` / `sm seed-examples`): the LLM emits a structured
     decision (ops JSON, a presentation spec) and a *tested* command applies it.
   - **Anomalies / unforeseen errors** — the case that justifies an agent at all:
     it samples data, finds a sentinel value, and figures out what to do.

3. **Scripts must surface anomalies, never swallow them.** A deterministic script
   that hits something ambiguous (a column it can't classify, a result that
   can't be parsed, an unexpected DB error) returns `needs_judgment` with
   context — it does **not** guess and does **not** hard-fail silently. That is
   the mechanism that keeps the LLM in the loop *exactly* where it adds value.

## The lens — classify every step

- **D (Deterministic)** — pure I/O / mechanical → script.
- **J (Judgment)** — genuine LLM call → keep.
- **H (Hybrid)** — LLM emits a spec, a script executes it → keep both, with a
  tested apply-command.
- **Seam** — an `AskUserQuestion` decision point → segment boundary.

### What stays LLM (do NOT script)
Pick DB type / name profile / choose schemas (seams); subject-area boundaries;
**all enrichment authoring** (descriptions, entity names, metric definitions);
NL→SQL; insight/approach prose; chart-type choice; the bad-number / anomaly
diagnosis. These are judgment.

## The three gaps (everything else is already scripted)

### Gap 1 — Render pipeline (highest value; correctness/trust)
`agami-query` Phase 4e.iii: the LLM hand-builds `agami-sections.json`,
**transcribing result numbers** into `table_rows` *and* `datasets[].data`.
`render_chart.py` never reads the result CSV — it renders whatever the model
typed. A miscopy shows a wrong number in the chart while the table is right;
nothing catches it. Phase 4e.iii.5 similarly hand-builds the trust receipt.
- **Fix:** `csv_to_sections.py` reads the result CSV + the units map (reusing the
  same `units.py` logic `sm format-table` uses) and emits
  `labels` / `table_rows` / `datasets[].data`. The LLM supplies only the
  **presentation spec** (title, insight, chart_type, label column, SQL verbatim).
  `build_receipt.py` parses the executed SQL (sqlglot) to populate `tables_used`
  + the relationships/metrics it touches + `model_version` + warnings (from
  `review_state`), instead of the LLM hand-extracting them. Numbers and
  provenance leave the model's hands.

### Gap 2 — agami-connect bootstrap spine (the fidelity bugs)
Phase 0/0a is ~48 deterministic steps in prose; the bug-prone ones cluster here:
interpreter resolution (the "wrong Python in `.config`" trap), path / credential
/ config wiring, the zsh word-split in prune handling, gate re-counting.
- **Fix:** small **segment** scripts between the seams —
  `connect_resolve.py` (profile + artifacts_dir + credentials + interpreter → one
  JSON state, with **scored** interpreter selection so it can't pick a Python
  missing a dep), `parse_prune_block.py`, `parse_validation_batch.py`,
  `check_curate_gate.py`. The 16 `AskUserQuestion` seams stay as boundaries.

### Gap 3 — near-deterministic skills still prose-driven
- **agami-serve** (~95% mechanical) — collapse to a thin wrapper over
  `setup_desktop_mcp.py`.
- **agami-save-correction** — extract its **rules-based** classification tree to
  `classify_correction.py` (it's a decision tree, not an opinion).
- **agami-model** — back-channel block parsing → a parser script (render + apply
  are already scripts).

## Phasing (each its own PR off `main`)

| Phase | Scope | Risk |
|---|---|---|
| **A — Render piping** (Gap 1) | `csv_to_sections.py` + `build_receipt.py`; rewrite query Phase 4d/4e.iii/iii.5 | Touches every render → golden-fixture tests |
| **B — Connect spine** (Gap 2) | `connect_resolve.py` + 3 parsers; rewrite Phase 0/0a/0s prose to call them | Medium — interactive, segment-by-segment |
| **C — Collapse skills** (Gap 3) | serve wrapper, `classify_correction.py`, `parse_model_feedback.py` | Low — mechanical |

**Cross-cutting (settled in A):** the JSON contract + the `needs_judgment`
channel convention, which B and C then follow.

## The JSON contract (settled in Phase A)

Every refactor script prints one JSON object on stdout:

```json
{
  "ok": true,
  "data": { "...": "step-specific result" },
  "anomalies": [ { "kind": "...", "detail": "...", "where": "..." } ],
  "needs_judgment": null
}
```

- `ok: false` → a hard error the caller surfaces (with `error`).
- `anomalies` → non-fatal things the LLM *may* want to mention/act on.
- `needs_judgment` → the script deliberately stopped short of guessing; the LLM
  must decide (with the provided context) and usually re-invoke with a choice.

Exit code mirrors `ok` (0/non-0) so a bash caller can branch without parsing.

## Non-goals
- Not scripting judgment (enrichment authoring, NL→SQL, anomaly diagnosis).
- Not one monolithic orchestrator — segmentation at the seams is mandatory.
- Not removing the LLM from the loop — moving it to where it's load-bearing.
