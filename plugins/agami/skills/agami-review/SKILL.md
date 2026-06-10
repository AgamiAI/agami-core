---
name: agami-review
description: "Opens the trust-layer review dashboard for the active profile's semantic model. Lists every entry needing review (Rule 1 metrics + named filters, plus Rule 2 entries below the confidence threshold) as cards with the source-signal block that produced each entry. The user replies in chat with structured commands (approve / reject / edit / threshold / done) to mark entries reviewed. Each approval writes back to the canonical YAML files in <artifacts_dir>/<profile>/ and runs the validator before promotion."
when_to_use: "Use when the user says 'open the review dashboard', 'review my model', 'show me what needs review', '/agami-review', 'walk through the review queue', or after agami-connect's Phase 7 summary box prompts to open the dashboard. Also use when the user replies to a previously-rendered dashboard with one of the chat back-channel commands (approve N / reject N / edit N / threshold X / approve all below X / done)."
argument-hint: "[threshold N.NN | rule1 | preseed | done]"
---

# agami review

You are running the trust-layer review surface. Goal: take the items in the user's semantic model that don't meet the trust threshold (or are Rule 1 entries needing sign-off) and let the user walk through them — see *why* each was inferred, and approve / reject / edit each. Every approval is a structured edit to a YAML file under `<artifacts_dir>/<profile>/`, gated by the validator.

This skill orchestrates:

1. **Load** — read the model, build the in-memory review queue.
2. **Render** — produce the HTML dashboard via `render_review.py`.
3. **Listen** — wait for the user's chat reply with structured commands.
4. **Apply** — for each command, read+edit the relevant YAML file, run the validator, commit if it passes, otherwise revert and report.
5. **Re-render** — after each batch of changes, regenerate the dashboard with updated counts. The user keeps walking the queue until they type `done`.

The trust block (`confidence` ∈ confirmed/inferred/proposed, `review_state`, `signed_off_*`) lives on each entry in the semantic model (`scripts/semantic_model/models.py`). The review queue + the apply path are the `curate` engine (`scripts/semantic_model/curate.py`), driven via `semantic_model.cli`.

## Conversation style

- **Tight loops.** This skill is a tool, not a conversation. Short surfaces between renders. The dashboard is the surface; the chat is the input channel.
- **Don't restate the dashboard in chat.** The HTML is the user's reading surface. The chat reply is "✓ Applied: approved 3, rejected 1. <N> items remain. Re-rendered."
- **Numbered references only in chat.** When you describe an item, use `#N` (matching the dashboard). Don't paste the full card text.

---

## Phase 0: Preflight

Run the same plan-mode + credentials checks as `agami-query-database`:
- Plan-mode: this skill needs Read + Bash + Write. **If plan mode is active: refuse and end the turn. DO NOT write a plan file. DO NOT call `ExitPlanMode`.** Refusal text: *"I can't apply review edits in plan mode — switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke. (You can still inspect a previously-rendered dashboard at `~/.agami/review/<profile>/<ts>.html`.)"*
- Resolve `<profile>` and `<artifacts_dir>` per the standard chain (`AGAMI_PROFILE` env → `~/.agami/.config.active_profile` → `default`; `AGAMI_ARTIFACTS_DIR` env → `~/.agami/.config.artifacts_dir` → `~/agami-artifacts`).
- If `<artifacts_dir>/<profile>/org.yaml` doesn't exist, invoke `agami-connect` and stop.
- Probe the validator is runnable: `python3 -c 'import yaml, jsonschema'`. If not, surface the install hint and stop — we won't write YAML edits without the validator gate.

**Scope:** if `$ARGUMENTS` contains `preseed` (the agami-connect Phase 4 "curate before examples" gate invokes `/agami-review preseed`), build with `review-items --scope preseed` — metrics + named-filters + entities needing review (relationships excluded). If it contains `rule1`, use `--scope rule1` (metrics + named-filters only). Otherwise build with no `--scope` (all four tabs).

Resolve the active threshold:
- If `$ARGUMENTS` starts with `threshold`, parse the number and use it for this session, AND persist it to `<artifacts_dir>/<profile>/agami.config.yaml` under `review.threshold`.
- Else read `<artifacts_dir>/<profile>/agami.config.yaml` → `review.threshold`. Default `0.7`.

---

## Phase 1: Build the review items from the model

The dashboard has **four tabs**: For Review · Approved Automatically · Manually Approved · Rejected. One command produces every entry, already tab-classified — run it from `plugins/agami/scripts/` with `ROOT="<artifacts_dir>/<profile>"`:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" review-items "$ROOT" > /tmp/agami-review-items-$ts.json
# scoped (used by agami-connect's Phase 4 gate): only Rule-1 items needing sign-off
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" review-items "$ROOT" --scope rule1 > /tmp/agami-review-items-$ts.json
```

Each item carries: `n` (stable global index — the number chat commands reference), `entity_type` (`metric` | `join` | `entity`), `rule` (1 = metrics, sign-off required; 2 = joins/entities, lazy), `title`, `source_signal` (the prose `calculation` for metrics, the `from.col → to.col` join + cardinality for relationships), `confidence` (`confirmed`/`inferred`/`proposed`), `review_state`, `signed_off_*`, and `tab`:

```
tab = "rejected"  if review_state == "rejected"
      "review"    if review_state in (unreviewed, stale)        # actionable
      "auto"      if approved by a system signer (agami_introspect / role=system)
      "manual"    if approved by a human signer
```

The template renders: **For Review** → action buttons (Approve / Reject / Edit / Skip), grouped by entity type, Rule 1 (metrics) in a primary "must-do-to-ship" section + Rule 2 collapsed below; **Auto** / **Manual** → read-only with the approval phrase; **Rejected** → a "Move to For Review" button (`unreject N`).

**Scope filter — `review-items --scope rule1`** (used by `/agami-connect` Phase 4's upfront gate, and when this skill is invoked with a `rule1` argument): the CLI returns **only** the Rule-1 items needing sign-off (metrics + named filters in the review tab). Pass that file straight to the renderer — **don't hand-filter and don't look for a scope flag inside `render_review.py`; the renderer renders exactly the items it's given.** The rendered "(N items)" then equals the sign-off count exactly. Default (no `--scope`) returns all four tabs.

**Summary counts** for the summary card: count items by `tab` and `rule` (e.g. `review` Rule 1 = metrics needing sign-off; `review` Rule 2 = inferred joins; `auto`/`manual`/`rejected` totals). Write to `--summary-file`.

There's no per-tab cap — the template groups by entity type with collapsible sections. The numbering is stable across re-renders because `review-items` sorts deterministically (Rule 1 first, then by tab, then by name).

---

## Phase 2: Render the dashboard

Build `/tmp/agami-review-items-<ts>.json` and `/tmp/agami-review-summary-<ts>.json`. Then:

```bash
ts=$(date +%Y%m%d-%H%M%S)
# Per-profile subdir so multi-profile users can tell renders apart and
# clean up per-profile (dev/reset-yamls.sh --clean-renders scopes to the
# named profile).
mkdir -p ~/.agami/review/"$profile"
python3 "$AGAMI_PLUGIN_ROOT/scripts/render_review.py" \
  --title "Review queue · $profile · threshold $threshold" \
  --threshold "$threshold" \
  --model-version "$model_version" \
  --items-file "/tmp/agami-review-items-$ts.json" \
  --summary-file "/tmp/agami-review-summary-$ts.json" \
  --out "$HOME/.agami/review/$profile/$ts.html"

rm -f "/tmp/agami-review-items-$ts.json" "/tmp/agami-review-summary-$ts.json"
```

Surface in chat (single block, no padding):

```
Review queue rendered — <N> items at threshold <X>.
~/.agami/review/<profile>/<ts>.html

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

**Auto-open the file on EVERY render**, not just the first. Earlier versions of this SKILL tried "first render only" to avoid browser-tab sprawl, but in practice that produced worse UX — users kept looking at the old stale tab. Every re-render targets a NEW timestamped file (Phase 5), so opening every time gets the user the fresh state without ambiguity. Tab sprawl is the lesser cost; tell the user once in the closing chat that the previous tab can be closed if they want.

Use the same multi-command fallback chain as agami-query-database Phase 4e.vi:

```bash
out="$HOME/.agami/review/$profile/<ts>.html"
( command -v open    >/dev/null 2>&1 && open "$out" ) || \
( command -v xdg-open >/dev/null 2>&1 && xdg-open "$out" ) || \
( command -v start    >/dev/null 2>&1 && start "$out" ) || \
( command -v cmd      >/dev/null 2>&1 && cmd /c start "" "$out" ) || \
echo "agami: couldn't auto-open the dashboard — open manually: $out"
```

Treat as best-effort — never block on the open call. If it fails, the printed path is the contract.

End the turn here. Wait for the user.

---

## Phase 3: Chat back-channel grammar

The user replies with one or more commands. Commands can come from the dashboard's "Generate feedback for Claude" button (newline-separated block) or be typed directly. Same grammar either way.

```
approve <num-list> [by <email>] [role=<role>]
reject  <num-list>
unreject <num-list>
edit    <num>
edit    <num> <kind>>>>\n<new text>\n<<<
approve all below <number>
threshold <number>
done
```

`edit N <kind>>>>\n...\n<<<` is the **inline-edit form** the dashboard generates when the user fills in the per-card Edit textarea. `<kind>` ∈ `{description, calculation}`:

- `description` — set the entry's `description` field → an `edit` op with `field: "description"`.
- `calculation` — set a metric's prose `calculation` (the Rule 1 intent) → an `edit` op with `field: "calculation"`. The edit alone doesn't re-trigger sign-off; queue an `approve` op too if the user wants it approved.

Parser: see a line matching `edit N <kind>>>>` (case-sensitive, exact closing token `<<<` on its own line); read every line between as the new value. Emit it as an `edit` op (Phase 3b) and apply via `cli curate` (Phase 4) — the engine validates + reverts on failure.

The bare `edit N` form (no `>>>...<<<` block) is the **chat-side form** — Claude reads the current entry, surfaces it as a fenced code block, accepts the new content conversationally, and writes back. Use this for entity types without an inline form (joins, dataset trust blocks).

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
  3. **Otherwise, ask the user exactly once per install**: surface a single inline prompt asking for both at once. **Do NOT infer the email from any source** — not `git config`, environment, credentials, **and not the Claude Code login / session email** (which the host exposes to the model as context). Inferring it produces a silently-wrong audit trail; the sign-off identity must be typed by the user. Don't even pre-fill the domain. Use this exact prompt:
     > To sign off the Rule 1 items in this batch, I need your email and role. Reply like: `you@company.com / data_lead`
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

### 3b — translate commands to an ops array

Map the parsed commands (by item `n` → its `{entity_type, area, name}` from the rendered items) to curation ops, one object per change:

```json
[
  {"op": "approve", "kind": "metric",       "area": "sales",  "name": "total_revenue", "at": "<UTC ISO>"},
  {"op": "reject",  "kind": "relationship", "area": "sales",  "name": "orders->tickets"},
  {"op": "include", "kind": "metric",       "area": "sales",  "name": "old_metric"}
]
```

`kind` is the item's `entity_type` mapped to the model kind: `metric`→`metric`, `join`→`relationship`, `entity`→`entity` (and `table`/`column` come from the model explorer, not here). `unreject` → `op: include`. For **edit**, surface the entry's editable block in chat, apply the user's plain-English change as `{"op":"edit","kind":...,"area":...,"name":...,"field":"<field>","value":<new>}` (e.g. set a metric's `calculation`, or a relationship's `on:` for the "approve with fix" flow), then ask "Approve this entry too?" and append an `approve` op if yes.

**Rule 1 sign-off requires a non-empty `calculation`.** Before emitting an `approve` op for a metric, check its `source_signal` (the prose calculation) in the rendered items. If empty, refuse upfront — don't let the validator catch it after:
> Item #N is a metric and Rule 1 needs a non-empty `calculation` before approval. Reply `edit N` to add it, then approve.

---

## Phase 4: Apply via the curation engine

One call applies the whole batch atomically — it flips `review_state`/sign-off on the canonical YAMLs, **validates the model, commits to the profile git repo, appends to `curation_log.jsonl`, and reverts every change if validation fails**. You don't hand-edit YAML or run the validator yourself.

```bash
printf '%s' "$OPS_JSON" > /tmp/agami-curate-ops.json
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" curate "$ROOT" \
  --ops-file /tmp/agami-curate-ops.json \
  --signer "${reviewer_email}" --role "${reviewer_role}"
rm -f /tmp/agami-curate-ops.json
```

`--signer`/`--role` come from the user's approve command (`approve N by you@x.com role=cfo`) or `~/.agami/.config` (`reviewer_email`/`reviewer_role`); they stamp `signed_off_*` on approved entries. The command prints JSON: `{applied, skipped, errors, validated, committed}`.

- `validated: true` → the batch stuck. Surface the applied count, continue to Phase 5 (re-render).
- `validated: false` → **nothing stuck** (the engine reverted via git). Surface `errors` verbatim and tell the user which op likely caused it; do not re-apply blindly.
- `skipped[]` → ops that couldn't be located (bad item number / already-changed); surface them.

The validator gate is non-bypassable — a model that fails validation is never persisted. The curation log + git history are the audit trail (the engine records the rejecter there; rejected entries carry no sign-off attribution on the entry itself).

---

## Phase 5: Re-render or close

After a batch of edits applies cleanly, **always re-render the dashboard** with a **new timestamped filename** at `~/.agami/review/<profile>/<new-ts>.html`. Recompute Phase 1 from scratch (re-run `cli review-items`, re-classify into tabs, re-count the summary). The numbering shifts as approved/rejected items leave the For Review tab — that's expected.

**Delete the previous timestamped file from the same profile dir before writing the new one** (`rm -f "$HOME/.agami/review/$profile/$prev_ts.html"`). Earlier versions kept old files around so the user would "notice the refresh," but real testing showed the stale files accumulate, confuse the user about which tab is current, and clutter the directory. The auto-open of the new file is the refresh signal; the previous file is dead. Track `$prev_ts` in session state across re-renders so you always know which file to delete. If the user already had the old file open in a browser tab, the new auto-open opens a fresh tab — they can close the stale one (we mention that in the chat ack).

**Surface BOTH the ack AND the new file path in one chat block** so the user can't miss it:

```
✓ Applied: approved 2 (#1, #3), rejected 1 (#7). 38 items remain.

Re-rendered: ~/.agami/review/<profile>/<new-ts>.html
(Open the new file — the previous one is stale.)
```

The `<new-ts>` is a fresh `date +%Y%m%d-%H%M%S` timestamp. Surfacing it inline (not just internally) is mandatory: the alternative — writing to a new path without telling the user — is what produced the "page summary says 237 but chat says 187" confusion in real testing. The user's expectation is "if you say the dashboard re-rendered, give me the URL to open."

Best-effort auto-open on every re-render — same multi-command fallback chain as Phase 2 above (`open` → `xdg-open` → `start` → `cmd /c start` → echo the path). The new file gets a new tab in the user's browser; the previous tab now points at stale content and can be closed.

Then end the turn. The user replies with the next batch of commands against the NEW dashboard's numbering.

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
| `org.yaml` missing | Invoke `agami-connect`. Stop. |
| `agami.config.yaml` missing | Use defaults (threshold 0.7). Don't error. |
| Validator missing | Refuse to run. Tell the user `pip install pyyaml jsonschema`. |
| User replies with a number that's out of range | Surface: *"Item #N doesn't exist — current queue has 1..K. Re-render to see numbering."* |
| Approve on a Rule 1 item without `by`/`role` | Refuse with the exact required form. Don't apply. |
| Validator fails after an edit | Revert via `git checkout`. Surface errors. Stop applying further edits in this turn. |
| `.git` missing in profile dir | Surface: *"This profile predates the trust-layer launch — initialize with `cd <artifacts_dir>/<profile> && git init && git add -A && git commit -m 'baseline'`, then re-run."* (Newer agami-connects do this automatically — Phase 3e.) |
| The user typed prose, not a command | Try to interpret intent. If unclear, surface: *"I expected `approve N` / `reject N` / `edit N` / `threshold X` / `done`. What did you want to do?"* |
