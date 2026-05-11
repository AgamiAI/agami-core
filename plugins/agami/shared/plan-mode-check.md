# Plan-mode preflight — read this before invoking any agami skill

Claude Code's **Plan mode** restricts the assistant to read-only tools — no `Edit`, no `Write`, and Bash is locked down to commands the host considers safe. Every agami skill except trivial reopen-chart flows needs at least one of those: `agami-init` writes credentials.example (legacy `setup` mode), `agami-connect` writes the semantic model, `agami-query-database` writes charts and runs `psql`/`mysql`/`snowsql` via Bash, `agami-save-correction` writes corrections.

If a skill starts in plan mode and barrels ahead, the failure happens partway through (a Bash or Write call gets blocked), the partial state is confusing, and the user has to start over. The fix: every skill detects plan mode at entry and asks the user to switch **before** doing any work.

This doc is the single source of truth for the detection + ask logic. Each SKILL.md references it from a `Phase −1` section at the top.

## Hard rule — when refusing because of plan mode

**DO NOT write a plan file. DO NOT call `ExitPlanMode`.** These skills are not planning tools — they are *executing* tools that the user invoked while plan mode happened to be active (almost always by accident). The user did not ask for "a written plan of what would happen"; they asked the skill to do its job. Surface the one-line refusal and stop the turn. Let the user switch modes via Shift+Tab and re-invoke.

The generic plan-mode behavior in Claude Code (write a plan + call ExitPlanMode) is appropriate for *user-driven planning* of new work. When an executing skill is invoked in plan mode, that behavior is wrong — it produces a noisy plan file the user doesn't want and didn't ask for. Every Phase −1 below explicitly says "refuse without writing a plan."

## Detection

Two signals — use them in order, stop at the first positive:

1. **System-reminder context.** When plan mode is active, the host injects a `<system-reminder>` saying so into the conversation. If the latest such reminder in scope indicates plan mode, treat it as confirmed.
2. **Optional Bash probe.** If signal 1 is absent or ambiguous, attempt one no-op: `echo agami-plan-probe`. If it succeeds, the skill can proceed. If it fails because plan mode is blocking it, that failure IS the signal.

Don't run the probe just to be sure when signal 1 already says plan mode is active — it's wasteful.

## The ask

If plan mode is active, **stop the skill** and ask via `AskUserQuestion`:

> agami needs to write files (credentials, semantic model, chart HTML) and run database queries. **Plan mode is active**, which blocks both. Switch modes?

Options (place exactly one `(Recommended)` first):

| label | description |
|---|---|
| `Default mode (Recommended)` | Switch to default mode — agami will ask for permission per command. The host caches "always allow" choices. |
| `Auto-accept edits` | Switch to auto-accept-edits mode — agami runs without per-command prompts. Use if you trust the skill. |
| `Stay in plan mode` | Don't run. Behavior depends on which skill — see "Stay-in-plan-mode behavior" below. |

After the user picks `Default mode` or `Auto-accept edits`:

1. Surface a one-line reminder of the keystroke: `Press Shift+Tab to cycle modes — keep tapping until you see "Default mode" (or "Auto-accept edits") in the bottom bar.`
2. **Do NOT try to flip the mode programmatically.** Claude Code doesn't expose a mode-toggle tool. Only the user can press Shift+Tab.
3. Wait for the user to confirm "I've switched, continue" (or just send the next message). On the next turn, re-run the detection (step 1 above). If plan mode is now off, proceed to Phase 0 of the skill. If it's still on, ask once more — they may have missed the keystroke.

## Stay-in-plan-mode behavior — varies per skill

Plan mode CAN proceed for read-only flows. Each SKILL declares what's possible:

### `agami-init`

Stay-in-plan-mode → **refuse to proceed. Do not write a plan file. Do not call ExitPlanMode.** Earlier versions of this doc had agami-init emit a written plan as a fallback; that turned out to be noise — users in plan mode are almost always there by accident, and what they actually want is to switch and proceed, not read a description of what would have happened. Surface ONLY this:

> I can't run setup in plan mode. Press Shift+Tab to switch to Default or Auto-accept, then send any message to continue.

Then end the turn. No plan, no modal, no follow-up.

### `agami-connect`

Stay-in-plan-mode → **refuse to proceed. Do not write a plan file. Do not call ExitPlanMode.** Introspection requires Bash (`psql -c`, etc.) and writes (the per-schema yaml files). Credential setup is handled by `agami-init` (a separate skill) — agami-connect itself doesn't write credentials. Surface ONLY this:

> I can't introspect in plan mode — switch to Default or Auto-accept (Shift+Tab) and re-invoke me. The schema picker, description generation, and demo query all need write access to `<artifacts_dir>/<profile>/`.

Then end the turn. No plan file describing what would happen — that's noise the user didn't ask for.

### `agami-query-database`

Stay-in-plan-mode → **refuse, with one exception. Do not write a plan file. Do not call ExitPlanMode.** SQL execution requires Bash; chart rendering requires Write. Both are blocked. The exception is the **reopen-last-chart intent** (Phase 2a.1) — re-displaying an existing HTML report only needs the `Read` tool plus an `open <path>` command (which most hosts allow even in plan mode for files under `$HOME`). If the user's intent is reopen-last-chart, run that flow and stop. For anything else, surface ONLY this:

> I can't run SQL in plan mode. Switch to Default or Auto-accept (Shift+Tab) and re-invoke.

### `agami-save-correction`

Stay-in-plan-mode → **refuse to proceed. Do not write a plan file. Do not call ExitPlanMode.** Saving requires Write (examples + model edits) and Bash (EXPLAIN-validation). Surface ONLY this:

> I can't save corrections in plan mode — switch to Default or Auto-accept (Shift+Tab) and re-invoke. The correction won't persist otherwise.

### `agami-review`

Stay-in-plan-mode → **refuse to proceed. Do not write a plan file. Do not call ExitPlanMode.** Approving / rejecting items writes back to YAML files. Surface ONLY this:

> I can't apply review edits in plan mode — switch to Default or Auto-accept (Shift+Tab) and re-invoke. (You can still inspect the dashboard HTML if it was rendered in a prior session — open `~/.agami/review/<ts>.html` directly.)

### `agami-reconcile`

Stay-in-plan-mode → **refuse to proceed. Do not write a plan file. Do not call ExitPlanMode.** Per-row queries require Bash; chart receipts require Write. Surface ONLY this:

> I can't reconcile in plan mode — each row runs a live query and writes a receipt. Switch to Default or Auto-accept (Shift+Tab) and re-invoke me with the CSV path.

## When the system context is silent

If neither signal 1 nor signal 2 confirms plan mode is active, **skip this phase silently** and go to Phase 0 of the skill. Don't pop a modal asking "are you in plan mode?" — that's noise for the 95% of users who aren't. The probe-on-Bash-failure path catches the rest naturally.

## Recovery from mid-skill plan-mode failures

If the skill is past Phase −1 and a Bash or Write call fails with a plan-mode block:

1. Surface: "Looks like plan mode kicked in. Switch via Shift+Tab and re-invoke me — I'll pick up where I left off (the last successful step is recorded in `~/.agami/.config.last_phase` if you want to inspect)."
2. Do NOT retry. The state is cleanly suspended; another retry won't help.

This shouldn't happen often (mode-switching mid-conversation is rare), but the recovery path is documented so you don't paper over the failure.
