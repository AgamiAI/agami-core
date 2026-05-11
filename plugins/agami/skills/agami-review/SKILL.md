---
name: agami-review
description: "Opens the trust-layer review dashboard for the active profile's semantic model. Lists every entry needing review (Rule 1 metrics + named filters, plus Rule 2 entries below the confidence threshold) as cards with the source-signal block that produced each entry. The user replies in chat with structured commands (approve / reject / edit / threshold / done) to mark entries reviewed. Each approval writes back to the canonical YAML files in <artifacts_dir>/<profile>/ and runs the validator before promotion."
when_to_use: "Use when the user says 'open the review dashboard', 'review my model', 'show me what needs review', '/agami-review', 'walk through the review queue', or after agami-connect's Phase 5.5 summary box prompts to open the dashboard. Also use when the user replies to a previously-rendered dashboard with one of the chat back-channel commands (approve N / reject N / edit N / threshold X / approve all below X / done)."
argument-hint: "[threshold N.NN | done]"
---

# agami review

You are running the trust-layer review surface. Goal: take the items in the user's semantic model that don't meet the trust threshold (or are Rule 1 entries needing sign-off) and let the user walk through them — see *why* each was inferred, and approve / reject / edit each. Every approval is a structured edit to a YAML file under `<artifacts_dir>/<profile>/`, gated by the validator.

This skill orchestrates:

1. **Load** — read the model, build the in-memory review queue.
2. **Render** — produce the HTML dashboard via `render_review.py`.
3. **Listen** — wait for the user's chat reply with structured commands.
4. **Apply** — for each command, read+edit the relevant YAML file, run the validator, commit if it passes, otherwise revert and report.
5. **Re-render** — after each batch of changes, regenerate the dashboard with updated counts. The user keeps walking the queue until they type `done`.

Trust-layer spec lives in [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md) (the canonical contract). Confidence formulas live in [`scripts/compute_confidence.py`](../../scripts/compute_confidence.py).

## Conversation style

- **Tight loops.** This skill is a tool, not a conversation. Short surfaces between renders. The dashboard is the surface; the chat is the input channel.
- **Don't restate the dashboard in chat.** The HTML is the user's reading surface. The chat reply is "✓ Applied: approved 3, rejected 1. <N> items remain. Re-rendered."
- **Numbered references only in chat.** When you describe an item, use `#N` (matching the dashboard). Don't paste the full card text.

---

## Phase 0: Preflight

Run the same plan-mode + credentials checks as `agami-query-database`:
- Plan-mode: this skill needs Read + Bash + Write. **If plan mode is active: refuse and end the turn. DO NOT write a plan file. DO NOT call `ExitPlanMode`.** Refusal text: *"I can't apply review edits in plan mode — switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke. (You can still inspect a previously-rendered dashboard at `~/.agami/review/<ts>.html`.)"*
- Resolve `<profile>` and `<artifacts_dir>` per the standard chain (`AGAMI_PROFILE` env → `~/.agami/.config.active_profile` → `default`; `AGAMI_ARTIFACTS_DIR` env → `~/.agami/.config.artifacts_dir` → `~/agami-artifacts`).
- If `<artifacts_dir>/<profile>/index.yaml` doesn't exist, invoke `agami-connect` and stop.
- Probe the validator is runnable: `python3 -c 'import yaml, jsonschema'`. If not, surface the install hint and stop — we won't write YAML edits without the validator gate.

Resolve the active threshold:
- If `$ARGUMENTS` starts with `threshold`, parse the number and use it for this session, AND persist it to `<artifacts_dir>/<profile>/agami.config.yaml` under `review.threshold`.
- Else read `<artifacts_dir>/<profile>/agami.config.yaml` → `review.threshold`. Default `0.7`.

---

## Phase 1: Walk every entity and classify into tabs

The dashboard now has **four tabs**: For Review · Approved Automatically · Manually Approved · Rejected. So this phase loads EVERY entity (not just review-needing) and tags each with a `tab` field. The template filters by tab.

Load every yaml under `<artifacts_dir>/<profile>/` — `index.yaml`, every `<schema>/_schema.yaml`, every `<schema>/<table>.yaml`. Walk the structures.

For each entity (dataset, field, relationship, metric, named_filter), parse its `agami` extension payload (one entry per `custom_extensions[]` whose `vendor_name=COMMON` and JSON has an `agami` top-level key — see [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md)). Then classify:

```
needs_review(entry) =
  (entity_type ∈ {metric, named_filter} AND review_state != approved)         # Rule 1
  OR
  (review_state == unreviewed AND confidence < threshold)                      # Rule 2
  OR
  (review_state == stale)                                                      # drift

tab(entry) =
  "rejected"  if review_state == "rejected"
  "review"    if needs_review(entry)
  "auto"      if review_state == "approved" AND (signed_off_by == "agami_introspect_v1"
                                                  OR signed_off_role == "system")
  "manual"    otherwise (review_state == "approved" with a human signer)
```

Set `item.tab` on each item. The template:
- **For Review** tab: shows action buttons (Approve / Reject / Edit / Skip), groups by entity type, sorts by ascending confidence within each group.
- **Approved Automatically** tab: read-only, shows the approval phrase (e.g., "auto-approved (FK declared in DB)").
- **Manually Approved** tab: read-only, shows "approved by jane@example.com (cfo), Mar 15".
- **Rejected** tab: shows a single "Move to For Review" button per card, which generates `unreject N`.

**No per-tab item cap.** Earlier versions of this SKILL capped the For Review tab at 50 items because the old flat layout became a wall past that. The new dashboard groups by entity type with collapsible sections — 237 items split across 4 groups (Metrics / Named Filters / Joins / Field Descriptions) is navigable. The user expands the group they want to work on; the others stay collapsed.

(If a user reports the page is sluggish on a model with, say, 5000+ unreviewed field descriptions, *then* introduce a per-group cap with "Show more" pagination. Don't optimize speculatively.)

Number items 1, 2, 3… **globally across all tabs**, in a stable order — Rule 1 first (metrics, then named_filters), then Rule 2 by ascending confidence, then approved entries grouped by entity type, then rejected. The numbering corresponds to chat commands, so it must stay stable across re-renders.

### 1a — count the auto-approved entries (for the summary card)

Walk the same yamls and count, per category:
- `auto_approved.datasets` — datasets with `review_state: approved` (typically all, since the dataset itself is mechanical)
- `auto_approved.fields` — fields with `review_state: approved`
- `auto_approved.fk_relationships` — relationships with `review_state: approved` AND `origin: fk`
- `auto_approved.field_descriptions_from_comments` — fields with `review_state: approved` AND `origin: column_comment`
- `needs_review.inferred_relationships` — relationships with `review_state: unreviewed` AND `origin: introspect_heuristic`
- `needs_review.low_confidence_field_descriptions` — fields with `review_state: unreviewed` AND `confidence < threshold`
- `needs_review.metric_proposals` — metrics with `review_state: unreviewed`
- `needs_review.named_filter_proposals` — named_filters with `review_state: unreviewed`
- `needs_review.stale` — any entry with `review_state: stale`

These feed the summary card via `--summary-file`.

### 1b — build per-item card data

For each item, build the item object per [`shared/review-dashboard-template.html` → `ITEMS_JSON`](../../shared/review-dashboard-template.html). Specifically:

- **`signals`** — translate the entry's `agami.signal_breakdown` into a list of `{ok: bool, text: string}`. The text should be human-readable — not just `fk_declared: false` but `✗ No FK declared in DB metadata`. See examples below.
- **`inferred`** — the SQL fragment / definition / mapping the system proposed:
  - For joins: `<from>.<from_col> = <to>.<to_col>`
  - For metrics: the metric's `expression.dialects[0].expression`
  - For field descriptions: the description text
  - For named filters: the predicate
- **`extra_lines`** — for metrics, include `Definition` (from `agami.definition_prose`) and `Assumptions` (from `agami.assumptions`). For field descriptions, include `Choices` (formatted from `agami.choice_field`).
- **`reply_hint`** — for Rule 1 items: `approve N by you@example.com role=cfo`. For Rule 2 items: `approve N`.

Signal-text translation table (used to render the ✓/✗ list):

| signal_breakdown key | When `true` (✓ text) | When `false` (✗ text) |
|---|---|---|
| `fk_declared` | `FK declared in DB metadata` | `No FK declared in DB metadata` |
| `pk_overlap` | `Both endpoints are primary keys` | `No primary key on the source side` |
| `unique_index_match` | `Target column has a unique index` | `Target column has no unique index` |
| `column_type_match` | `Column types match exactly` | `Column types do not match` |
| `column_name_similarity` | (number — show only if ≥ 0.7: `Column-name similarity: <X>`) | (omit) |
| `plural_pattern_match` | `Plural-of-table-name pattern matches` | (omit) |
| `dba_column_comment` | `DBA-authored column comment present` | `No DBA column comment` |
| `well_known_measure_pattern` | `Column name matches a known measure pattern` | (omit) |
| `numeric_type` | `Source column is numeric` | `Source column is not numeric` |
| `aggregate_friendly_distribution` | `Distribution looks aggregate-friendly` | (omit) |
| `business_term_match` | `Column name matches a known business term` | (omit) |
| `enum_like_distribution` | `Distinct values look enum-like` | (omit) |
| `synonym_match` | `Synonym matches an already-approved entry` | (omit) |
| `llm_inferred` | `LLM proposed this with no DB-side signal` | (omit) |

Skip a signal entirely if its truthful rendering would be empty. Negative signals are interesting only when their absence is meaningful — `column_type_match=False` is a red flag; `plural_pattern_match=False` is just noise.

---

## Phase 2: Render the dashboard

Build `/tmp/agami-review-items-<ts>.json` and `/tmp/agami-review-summary-<ts>.json`. Then:

```bash
ts=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/.agami/review
python3 "$AGAMI_PLUGIN_ROOT/scripts/render_review.py" \
  --title "Review queue · $profile · threshold $threshold" \
  --threshold "$threshold" \
  --model-version "$model_version" \
  --items-file "/tmp/agami-review-items-$ts.json" \
  --summary-file "/tmp/agami-review-summary-$ts.json" \
  --out "$HOME/.agami/review/$ts.html"

rm -f "/tmp/agami-review-items-$ts.json" "/tmp/agami-review-summary-$ts.json"
```

Surface in chat (single block, no padding):

```
Review queue rendered — <N> items at threshold <X>.
~/.agami/review/<ts>.html

The dashboard has 4 tabs (For Review · Approved Automatically · Manually
Approved · Rejected) and click-to-approve buttons on each card. Click
the actions you want, hit "Generate feedback for Claude" at the bottom,
then paste the result back here.

You can also type commands directly:
  approve N         (or `approve 1, 3, 7`)
  approve all below 0.95
  reject N
  edit N
  unreject N
  threshold 0.5
  done
```

**Don't auto-open the file** in this skill — let the user open it themselves. The dashboard re-renders every time the user replies; opening repeatedly creates browser-tab sprawl. The first render of a session can include `open ~/.agami/review/<ts>.html` (best-effort, same fallback chain as agami-query-database Phase 4e.vi). Subsequent re-renders surface only the path.

End the turn here. Wait for the user.

---

## Phase 3: Chat back-channel grammar

The user replies with one or more commands. Commands can come from the dashboard's "Generate feedback for Claude" button (newline-separated block) or be typed directly. Same grammar either way.

```
approve <num-list> [by <email>] [role=<role>]
reject  <num-list>
unreject <num-list>
edit    <num>
approve all below <number>
threshold <number>
done
```

Where:
- `<num-list>` = `1` | `1, 3, 5` | `1-5` (range)
- `<email>` = anything matching `\S+@\S+\.\S+`
- `<role>` ∈ `{cfo, cto, data_lead, engineer, analyst, other}` (no `system` — that's auto-only)
- `<number>` = float in `[0, 1]`

Multiple commands on one line are allowed, comma-separated. Newline-separated blocks (as the dashboard generates) are also fine:

```
approve 1, 3, 7 by you@x.com role=cfo
approve 2, 4
reject 5
unreject 12
done
```

**`unreject N`** flips a rejected entry back to `unreviewed` — clears `signed_off_by` / `signed_off_at` / `signed_off_role`. The item appears on the For Review tab on the next re-render. Used when the curator wants a second look at something they previously rejected.

### 3a — validate the command

Per command:

- **approve N** — N must be a valid item number in the current queue. **For Rule 1 items (metric / named_filter), the sign-off email + role are required.** Source them in this order, **stop at the first hit**:
  1. **The chat command** itself: `approve N by <email> role=<role>` — use these verbatim.
  2. **`~/.agami/.config`'s `reviewer_email` and `reviewer_role`** fields, if present.
  3. **Otherwise, ask the user exactly once per install**: surface a single inline prompt asking for both at once (do NOT infer from `git config`, environment, or credentials — that path produces silent inconsistency). Use this exact prompt:
     > To sign off the Rule 1 items in this batch, I need your email and role. Reply like: `ashwin@agami.ai / data_lead`
     >
     > Valid roles: `cfo`, `cto`, `data_lead`, `engineer`, `analyst`, `other`.
     >
     > I'll save these to `~/.agami/.config` so I don't ask again.

  Parse the user's response: split on `/`, trim each half. Validate the email against `\S+@\S+\.\S+` and the role against the enum above. If either fails, re-prompt with the same format.

  **Persist on success** — merge into `~/.agami/.config` (preserve any existing fields like `tier`, `host`, `tool_paths`):
  ```bash
  python3 - <<PY
  import json, pathlib
  p = pathlib.Path.home() / ".agami" / ".config"
  cfg = json.loads(p.read_text()) if p.exists() else {"schema_version": 1}
  cfg["reviewer_email"] = "<email>"
  cfg["reviewer_role"]  = "<role>"
  p.write_text(json.dumps(cfg, indent=2))
  PY
  chmod 600 ~/.agami/.config
  ```

  If the user later passes a different email/role in an explicit `by`/`role=` clause, use those for that command only — don't overwrite the stored defaults unless they say "remember this" or edit `.config` directly.

  Refusal text only fires when sources 1, 2, and the asked response all fail: *"Item #N is a metric and requires sign-off and I couldn't get an email + role. Reply: `approve N by <email> role=<role>` or update `~/.agami/.config`."*
- **reject N** — same numbering check.
- **unreject N** — N must be an item with `review_state: rejected`. If not, surface: *"Item #N isn't currently rejected — no-op."*
- **edit N** — open YAML for review (see Phase 4d).
- **approve all below X** — bulk-approve every Rule 2 item where `confidence < X` AND it's not Rule 1. Skip Rule 1 items. Surface what would be skipped: *"Skipping 8 Rule 1 items (metrics + named filters) — those need explicit sign-off."*
- **threshold X** — change the threshold for this render. Persist to `agami.config.yaml`. Re-render.
- **done** — close the session. Surface: *"Closed. Run `/agami-review` anytime to reopen."* and stop.

### 3b — group edits by yaml path

Multiple commands often touch the same YAML file. Group commands by `yaml_path` so we read each file once, apply all relevant edits, validate, then move to the next file. Don't read+write per command — too many round-trips and harder to revert atomically.

---

## Phase 4: Apply edits to YAML

For each `(yaml_path, list_of_edits)` group:

### 4a — read

Use the Read tool on `<artifacts_dir>/<profile>/<yaml_path>`. Parse mentally — locate the entity by its qualified ID (e.g., `relationships.orders_to_customers` ↔ the relationship array entry whose `name == "orders_to_customers"`).

### 4b — locate the agami JSON payload

Each entity has a `custom_extensions[]` array. Find the entry with `vendor_name: COMMON` AND a `data:` JSON string whose top-level key is `agami`. That's the trust block.

### 4c — apply the edit

For **approve** (Rule 2 item):
- Update the JSON: `review_state: approved`, `signed_off_by: "<email>"` (the user's email from credentials or default to a placeholder string the user provides), `signed_off_at: "<UTC ISO>"`, `signed_off_role: "<role>"` (if provided; else omit).

For **approve** (Rule 1 item — metric / named_filter):
- Same as above, but `signed_off_role` is REQUIRED. If missing in the command, surface a refusal as in Phase 3a.
- Verify `definition_prose` is non-empty in the entry. If empty, surface: *"Item #N is a metric/named_filter; `definition_prose` is empty. Use `edit N` first to add the definition, then approve."*

For **reject**:
- `review_state: rejected`. Per Hard Rule #10, set `signed_off_by: null`, `signed_off_at: null`, `signed_off_role: null` (rejected entries don't carry sign-off attribution; the rejecter is recorded in the curation log instead).

For **unreject**:
- `review_state: unreviewed`. Set `signed_off_by: null`, `signed_off_at: null`, `signed_off_role: null`. The entry's `confidence` and `signal_breakdown` are preserved — only the review state flips. Item reappears on the For Review tab on the next re-render. Append a `unreject` event to `curation_log.jsonl` with the curator's identity so the audit trail captures it.

For **edit**:
- Read the current entity.
- Surface the editable block in chat as a fenced code block.
- The user replies with the edit instructions in plain English. You apply them — typically updating `description`, `expression`, `definition_prose`, or `choice_field`.
- After the edit, ask: *"Approve this entry as well? (yes/no)"*. If yes, fall through to the approve flow.

### 4d — write back via the Edit tool

Use the Edit tool with `old_string` = the original JSON-string line for that entity, `new_string` = the updated JSON-string line. Preserve indentation (the JSON sits inside a YAML `data: '...'` value — the YAML quoting is what you preserve; the JSON inside the quotes is what you change).

For convenience, build the new JSON string with `json.dumps(...)` ordering keys consistently — `agami` outermost, then the existing keys in the order they appeared (so diffs stay clean).

### 4e — validate

After all edits in a single `(yaml_path)` group are applied, run:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/validate_semantic_model.py" --directory "$artifacts_dir/$profile"
```

- **Exit 0**: edits stick. Continue to the next yaml file.
- **Exit 1**: revert the file using `git checkout <yaml_path>` (the `<artifacts_dir>/<profile>/.git` repo is initialized by agami-connect Phase 3e). Surface the validator errors verbatim. Tell the user which command caused the failure (best-effort — usually the most recent one). Do NOT continue applying further edits.
- **Exit 2**: tooling error. Stop. The validator gate is non-bypassable.

### 4f — append to curation log

For every applied edit (approve / reject / edit), append one line to `<artifacts_dir>/<profile>/curation_log.jsonl`:

```bash
jq -nc \
  --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg actor "$user_email_or_placeholder" \
  --arg action "approve" \
  --arg entity_type "relationship" \
  --arg entity_qname "orders_to_customers" \
  --arg from_state "unreviewed" \
  --arg to_state "approved" \
  --argjson confidence 0.62 \
  '{ts:$ts, actor:$actor, action:$action, entity_type:$entity_type,
    entity_qname:$entity_qname, from_state:$from_state, to_state:$to_state,
    confidence:$confidence}' \
>> "$artifacts_dir/$profile/curation_log.jsonl"
```

The curation log is the audit trail — append-only, chmod 600. It captures rejecter identity (which we don't keep on the entity itself, per Hard Rule #10).

### 4g — git commit (best-effort)

After all edits in this turn are applied and validated, commit to the repo at `<artifacts_dir>/<profile>/.git`:

```bash
( cd "$artifacts_dir/$profile" \
  && git add -A \
  && git -c user.name="${USER_NAME:-curator}" \
       -c user.email="${USER_EMAIL:-curator@local}" \
       commit -q -m "review: <N> entries approved/rejected by ${USER_EMAIL:-curator}" \
  ) || true
```

Best-effort — never block on git failure. The YAML files + curation log are the source of truth.

---

## Phase 5: Re-render or close

After a batch of edits applies cleanly, re-render the dashboard (Phase 2) with the updated counts. The numbering shifts (some items are now approved/rejected and dropped from the queue) — recompute from scratch.

Surface a one-line ack before the re-render:

```
✓ Applied: approved 2 (#1, #3), rejected 1 (#7). 38 items remain. Re-rendering.
```

Then end the turn. The user replies with the next batch of commands.

If the queue is empty after edits, surface:

```
✓ Review queue is empty at threshold <X>. The model is fully reviewed.
Run `/agami-review threshold 0.5` to inspect borderline entries, or `done` to close.
```

If the user types `done`, close. Don't re-render.

---

## Hard rules

1. **Never bypass the validator.** Every yaml edit MUST be re-validated before the next group. A failed validation reverts the file via `git checkout` (assumes Phase 3e of agami-connect ran and the dir is a git repo). If `.git` is absent, take a backup copy of the file before edit and restore on failure.
2. **Never auto-approve a metric or named_filter.** Rule 1 entries always require an explicit `by + role` from a human in the chat command. Auto-approve is reserved for the introspect step (Phase 2c of agami-connect), and even there only joins / fields / descriptions can auto-approve.
3. **Stale entries never auto-reset.** A `stale` entry remains stale until the user explicitly re-reviews it with `approve N` or `reject N`. The skill surfaces stale entries first because they're the most surprising — a previously-trusted entry that's no longer trustable.
4. **Threshold persists.** When the user types `threshold X`, the new value is written to `<artifacts_dir>/<profile>/agami.config.yaml`. Don't keep it session-only — the next time the user runs `/agami-review`, they get their preferred threshold.
5. **Don't paste full YAMLs in chat.** The dashboard is the surface. The chat is short ack lines. Reading a YAML block to apply an edit is fine; pasting it verbatim into the chat is noise.

---

## Error handling cheat sheet

| Symptom | Action |
|---|---|
| `index.yaml` missing | Invoke `agami-connect`. Stop. |
| `agami.config.yaml` missing | Use defaults (threshold 0.7). Don't error. |
| Validator missing | Refuse to run. Tell the user `pip install pyyaml jsonschema`. |
| User replies with a number that's out of range | Surface: *"Item #N doesn't exist — current queue has 1..K. Re-render to see numbering."* |
| Approve on a Rule 1 item without `by`/`role` | Refuse with the exact required form. Don't apply. |
| Validator fails after an edit | Revert via `git checkout`. Surface errors. Stop applying further edits in this turn. |
| `.git` missing in profile dir | Surface: *"This profile predates the trust-layer launch — initialize with `cd <artifacts_dir>/<profile> && git init && git add -A && git commit -m 'baseline'`, then re-run."* (Newer agami-connects do this automatically — Phase 3e.) |
| The user typed prose, not a command | Try to interpret intent. If unclear, surface: *"I expected `approve N` / `reject N` / `edit N` / `threshold X` / `done`. What did you want to do?"* |
