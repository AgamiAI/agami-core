---
name: agami-model
description: "Opens the model-explorer dashboard for the active profile's semantic model. Lets the user browse every subject area, table, and field with live search, and queue Exclude / Include actions on tables and columns they don't want the runtime to use. Each action flips the entry's `review_state` to `rejected` (exclude) or `unreviewed` (include) via the curation engine, gated by the validator and committed to the profile's git repo."
when_to_use: "Use when the user says 'open the model explorer', 'show me the model', 'browse my tables', 'exclude a table', 'remove this column', 'take out the X table', 'I don't want PII columns', '/agami-model', or after agami-connect's Phase 7 trust-layer summary prompts the user to inspect the model. Also use when the user replies to a previously-rendered model-explorer artifact with one of the chat back-channel commands (exclude tables: ... / include columns: ... / done)."
argument-hint: "(no args — opens the explorer for the active profile)"
---

# agami model

You are running the model-explorer surface. Goal: let the user see every dataset and field in the live semantic model, search for things by name or description, and **remove tables or columns they don't want the runtime to use** — without re-introspecting.

This skill orchestrates:

1. **Render** — invoke `render_model_explorer.py` to walk every YAML and write a self-contained HTML artifact at `~/.agami/model/<profile>/<ts>.html`. The Python script does the YAML reading — **no LLM tokens spent on the walk**.
2. **Open + wait** — auto-open the file, end the turn, wait for the user to come back with exclude / include commands from the dashboard's "Generate feedback for Claude" button.
3. **Apply** — for each batch of commands, run `semantic_model.cli curate` with an ops JSON. The engine flips review_state, runs the validator, reverts via git on failure, appends to `curation_log.jsonl`, and commits to the profile's git repo.
4. **Re-render** — render to a new timestamped file and re-open. Wait for the next batch.

Trust-spine semantics: "exclude" flips `review_state` to `rejected`. The model loader drops `rejected` tables, columns, and relationships entirely (it loads with `include_rejected=False` for the runtime) — they never appear in prompts, never get joined to, never get aggregated. "Include" flips back to `unreviewed`; the user can re-approve via `/agami-review` if they want a sign-off badge.

## Conversation style

- **Tight loops.** The dashboard is the surface; the chat is just the input channel.
- **Don't restate the dashboard in chat.** A successful apply gets a one-line ack with the count and the new file path.
- **Qualified names everywhere.** Tables are `<area>.<table>`. Columns are `<area>.<table>.<column>` (area = subject area). No bare names.

---

## Phase 0: Preflight

Same shape as `agami-review`:

- **Plan-mode check** — this skill writes YAMLs. If plan mode is active, refuse: *"I can't apply model edits in plan mode — switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke. (You can still inspect a previously-rendered model-explorer artifact at `~/.agami/model/<profile>/<ts>.html`.)"* **Do NOT write a plan file. Do NOT call `ExitPlanMode`.**
- **Resolve `<profile>` and `<artifacts_dir>`** via the standard chain (`AGAMI_PROFILE` → `~/.agami/.config.active_profile` → `default`; `AGAMI_ARTIFACTS_DIR` → `.config.artifacts_dir` → `~/agami-artifacts`).
- **If `<artifacts_dir>/<profile>/org.yaml` doesn't exist**, invoke `agami-connect` and stop — there's no model to explore yet.
- **Verify Python + PyYAML are importable** (the renderer + applier both depend on PyYAML): `python3 -c 'import yaml'`. If not, surface the install hint and stop.

Resolve the curator's identity for `curation_log.jsonl` + git commits:
1. `~/.agami/.config.reviewer_email` — read once-and-persist; see [`agami-review/SKILL.md → Phase 3a`](../agami-review/SKILL.md#3a--validate-the-command).
2. If absent, ask once: *"What's your email? I'll save it to `~/.agami/.config.reviewer_email` so future review + model actions don't re-ask."* Validate the shape, persist. **Do NOT infer it from any source** — not git config / env / credentials, **and not the Claude Code login / session email** (the host exposes it to the model, but using it produces a silently-wrong audit trail). The sign-off identity must be typed by the user; don't even pre-fill the domain.

---

## Phase 1: Render the explorer

```bash
ts=$(date -u +%Y%m%d-%H%M%S)
# Per-profile subdir so multi-profile users can tell renders apart.
mkdir -p ~/.agami/model/"$profile"
out="$HOME/.agami/model/$profile/$ts.html"
python3 "$AGAMI_PLUGIN_ROOT/scripts/render_model_explorer.py" \
  --profile "$profile" \
  --artifacts-dir "$artifacts_dir" \
  --out "$out"
```

`$AGAMI_PLUGIN_ROOT` resolves the same way as in agami-review (the plugin dir under `~/.claude/plugins/cache/<marketplace>/agami/`). If the env var isn't set, fall back to the conventional install paths described in [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md).

**Surface in chat (single block, no padding):**

```
Model explorer rendered — <N> schemas · <M> tables · <K> fields.
~/.agami/model/<profile>/<ts>.html

The dashboard has live search, filter chips (All / Active / Excluded /
Unreviewed / Queued), and per-table + per-column Exclude / Include
buttons. When you exclude a column, it offers **"exclude all N named
<col>"** — one click drops that column across every table (PII like
`aadhaar`/`ssn`, audit columns, etc.). Click your way through, hit
"Generate feedback for Claude" at the bottom, paste back here.

You can also type commands directly:
  exclude tables:  <area>.<table>, <area>.<table>
  include tables:  <area>.<table>
  exclude columns: <area>.<table>.<column>, <area>.<table>.<column>
  include columns: <area>.<table>.<column>
  done
```

**Auto-open with the same multi-command fallback chain as agami-review:**

```bash
( command -v open    >/dev/null 2>&1 && open "$out" ) || \
( command -v xdg-open >/dev/null 2>&1 && xdg-open "$out" ) || \
( command -v start    >/dev/null 2>&1 && start "$out" ) || \
( command -v cmd      >/dev/null 2>&1 && cmd /c start "" "$out" ) || \
echo "agami: couldn't auto-open the explorer — open manually: $out"
```

End the turn. Wait for the user.

---

## Phase 2: Parse the chat back-channel

The user replies with a block like:

```
exclude tables: BUREAU_DATA.NOHIT_DATA, BUREAU_DATA.UNSCRUBBED_DATA
exclude columns: BUREAU_DATA.GENERAL_INFO.PAN, BUREAU_DATA.GENERAL_INFO.AADHAAR
include tables: BUREAU_DATA.LOAN_REPAYMENT
done
```

Grammar:

```
exclude tables:  <qname-list>
include tables:  <qname-list>
exclude columns: <qname-list>
include columns: <qname-list>
curate-ops:
[{"op":"exclude","kind":"metric","area":"...","name":"..."}, {"op":"edit","kind":"table","area":"...","name":"orders","column":"amount","field":"description","value":"..."}, ...]
example-edits:
[{"area":"sales","question":"...","sql":"...","source":"correction","status":"confirmed"}]
organization-md: "<full new ORGANIZATION.md text, JSON-encoded>"
done
```

Where `<qname-list>` is comma-separated, whitespace-tolerant:
- Table qname: `<area>.<table>` (e.g., `bureau_data.NOHIT_DATA`)
- Column qname: `<area>.<table>.<column>` (e.g., `bureau_data.PII.AADHAAR`)

Three optional blocks may follow, each a single JSON line (the dashboard emits whichever the user touched):
- **`curate-ops:`** — exclude/include on metrics/entities/relationships AND field **edits** (`op:"edit"` with `field`/`value` — table/column/metric/entity descriptions, metric `calculation`/`unit`). Already a valid curate ops array; merge it verbatim with the table/column ops below and apply via one `sm curate` call.
- **`example-edits:`** — edited prompt examples `[{area, question, sql, source, status}]`. Group by `area` and apply each group with `sm add-example "$ROOT" --area <area> --file <json>` (it dedups by `question`, so an edit replaces the prior example). Write the per-area JSON with the **Write tool**.
- **`organization-md:`** — a JSON-encoded string of the full new `ORGANIZATION.md`. JSON-decode it and **Write** it to `<artifacts_dir>/<profile>/ORGANIZATION.md` (overwrite; free-form Markdown, no validator).

Show the user a one-line summary of what each block changed before applying.

Tolerate trailing commas, mixed-case schema/table/column names (the YAMLs typically preserve the DB's casing — pass them through verbatim).

**Reject malformed targets in chat, not by silently dropping them.** If a user types `exclude tables: NOHIT_DATA` without the schema prefix, surface: *"Tables need a schema prefix — `bureau_data.NOHIT_DATA` not just `NOHIT_DATA`. Did you mean that?"* and stop.

---

## Phase 3: Apply via the curation engine

Translate the parsed exclude/include commands into a curation ops array, **then merge in the `curate-ops:` JSON array** (metric/entity/relationship actions) verbatim. Table targets are `<area>.<table>`, column targets `<area>.<table>.<column>`. `exclude` → `op: exclude` (the engine treats it as reject), `include` → `op: include`:

```json
[
  {"op": "exclude", "kind": "table", "area": "bureau_data", "name": "NOHIT_DATA"},
  {"op": "include", "kind": "table", "area": "bureau_data", "name": "LOAN_REPAYMENT"},
  {"op": "exclude", "kind": "table", "area": "bureau_data", "name": "PII", "column": "AADHAAR"},
  {"op": "exclude", "kind": "metric", "area": "bureau_data", "name": "avg_score"},
  {"op": "exclude", "kind": "relationship", "area": "bureau_data", "name": "loans->customers"}
]
```

(Column exclusions use `kind: table` + a `column` field — the engine flips the column inside that table's YAML.) **Write the ops array to the file with the Write tool — never a heredoc, a shell variable (`printf "$OPS"`), or `python3 -c` (JSON quotes/`null` break those).** Then apply the whole batch atomically — the engine flips `review_state`, **validates the model, commits, logs to `curation_log.jsonl`, and reverts everything on validation failure**:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" curate "$ROOT" \
  --ops-file /tmp/agami-model-ops.json \
  --signer "${reviewer_email}" --role "${reviewer_role}"
```

`ROOT="<artifacts_dir>/<profile>"`. Stdout is JSON: `{applied, skipped, errors, validated, committed}`.

**Success** (`validated: true`):
- Ack: *"✓ Applied: <X> tables excluded, <Y> columns excluded, <Z> re-included. Re-rendering…"*
- List any `skipped[]` (bad target / not found) on their own lines.
- Continue to Phase 4 (re-render).

**Failure** (`validated: false`):
- The engine already reverted via git. Surface: *"⚠ Validator rejected the batch — all changes reverted. No files were modified."* Show `errors` verbatim. Stop; don't re-render.

Excluded entries vanish from the runtime model (the loader drops `review_state: rejected`) but stay visible in the explorer (which loads with `include_rejected=True`) so the user can re-include them.

---

## Phase 4: Re-render

Every successful apply produces a new explorer file at a new timestamp.

**Delete the previous timestamped file from this profile's dir before writing the new one** (`rm -f "$HOME/.agami/model/$profile/$prev_ts.html"`). Track `$prev_ts` across re-renders. The auto-open of the new file is the refresh signal; old files just accumulate and confuse the user about which tab is current.

```bash
new_ts=$(date -u +%Y%m%d-%H%M%S)
new_out="$HOME/.agami/model/$profile/$new_ts.html"
python3 "$AGAMI_PLUGIN_ROOT/scripts/render_model_explorer.py" \
  --profile "$profile" --artifacts-dir "$artifacts_dir" --out "$new_out"
```

Auto-open the new path. Surface the new file path in the chat ack:

```
✓ Applied: 2 tables excluded, 2 columns excluded. Re-rendered.
~/.agami/model/<profile>/<new-ts>.html
(Previous tab is stale and can be closed.)
```

End the turn. The user comes back with the next batch.

---

## Phase 5: Closing

When the user types `done` AND no further commands are in the message, surface:

```
✓ Model exploration session ended. Final state:
  <X> tables excluded · <Y> columns excluded across the profile.

The runtime semantic model now skips these entries — they will not
appear in any prompt or join. To restore them later, run /agami-model
and queue `include tables: ...` / `include columns: ...`.
```

Then end the turn. The skill is one-shot per invocation — re-enter via the slash command or natural-language phrase to start another session.

---

## Edge cases

| Case | Behavior |
|---|---|
| User types `exclude tables: schema.table` for a table that's already excluded | The applier still runs (idempotent flip to rejected with the curator's name on the curation log). No-op for the YAML, but the audit trail records the re-confirm. |
| User types `include tables: schema.table` for a table that's already unreviewed | Same — idempotent flip; entry stays unreviewed; curator name recorded. |
| User types `exclude columns: schema.table.col` and the column doesn't exist | The applier returns it in `skipped[]` with reason "field not found on dataset". Surface the skip; continue with the rest. |
| User types `exclude tables: schema.table.col` (3 segments — looks like a column) | The grammar is positional; agami treats the `exclude tables:` prefix as authoritative. The applier will fail to find a table with that 3-segment name and return `skipped` with reason "table yaml not found". Surface clearly. |
| Validator fails on a partially-applied batch | The applier reverts ALL YAML changes via `git checkout -- .` before returning. `applied` counts are zeroed in the response (defense against partial-apply confusion). The user sees `validator_output` verbatim and can fix + retry. |
| User has the profile dir but no `.git/` (legacy from before Phase 3e) | The applier skips the git commit + revert steps silently. Curation log still gets appended. The user loses revert-on-validator-fail safety — surface a one-liner suggesting `git init` in the profile dir. |
| The model has 5000+ fields | The renderer handles it (JSON manifest is ~hundreds of KB at the high end). Client-side search is instant up to the millions of DOM nodes mark. If it gets sluggish, file an issue. |

---

## What the runtime does with `rejected` entries

The trust spine has always read `agami.review_state`. The model loader in [`plugins/agami/skills/agami-query-database/SKILL.md → Phase 1c`](../agami-query-database/SKILL.md#1c--index-the-model-for-fast-access) filters entries with `review_state: rejected` out of `datasets_by_name`, `datasets_by_qname`, `fields_by_qname`, and `relationships_by_endpoints`. Rejected entries:

- **Never appear in the schema context** the SQL generator sees (Phase 2b).
- **Are not joinable** — the join-path picker skips relationships whose endpoints reference a rejected dataset.
- **Don't trigger the strict gate** — a rejected metric or named filter is excluded from runtime; queries that would have referenced them get a "metric not found" path-not-an-error.
- **Stay in the YAML** for audit. The user can `include` them later without re-introspect.

This is the same mechanism `/agami-review`'s Reject button uses on individual entries. `/agami-model` is the bulk-and-search interface for the same operation, with tables-as-units instead of metric-by-metric.

---

## Examples

### Excluding PII columns

```
You: open the model explorer

[skill renders ~/.agami/model/finbud/20260512-101500.html and opens it]

You (in dashboard): search "pan" → toggle Exclude on every PAN/AADHAAR
                    field → click Generate feedback → paste back in chat:

exclude columns: BUREAU_DATA.GENERAL_INFO.PAN,
                 BUREAU_DATA.GENERAL_INFO.AADHAAR,
                 BUREAU_DATA.NOHIT_DATA.PAN
done

[skill applies, validator passes, commits, re-renders]

agami: ✓ Applied: 3 columns excluded. Re-rendered.
       ~/.agami/model/finbud/20260512-101830.html
```

### Removing staging tables

```
You: I never want agami to use the staging tables

[skill checks ORGANIZATION.md / table names for `_stg` suffix; if
 unsure, asks: "I see GENERAL_INFO_SCORE_STG — anything else? Click
 Exclude on the table cards in the dashboard."]

[user excludes via dashboard, pastes back]
exclude tables: BUREAU_DATA.GENERAL_INFO_SCORE_STG
done

agami: ✓ Applied: 1 table excluded.
```
