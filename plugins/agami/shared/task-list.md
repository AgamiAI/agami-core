# The onboarding task list

Run `sm steps` at the start of onboarding, when a step starts or ends, and after every `sm progress --wait`. Replace the session's task list with its output: one item per step, in order, using each step's `text` and `status`. Never write a step or a number `sm steps` did not print.

> `<plugin_root>`: the plugin's absolute path, see [invocation-conventions.md](invocation-conventions.md).
>
> `$ROOT` is the database's folder, `<artifacts_dir>/<profile>`.

```bash
bash "<plugin_root>/scripts/sm" steps "$ROOT"
bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark <id>=in_progress
bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark <id>=done
bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark <id>=skipped
```

A mark is written first, then the whole list is printed, so one call both marks the step and gives
the list to show. Each phase file says which mark to run where.

**Statuses.** `pending` is not started, `in_progress` is in progress, `done` is completed. A
`skipped` step is left out of the list.

**Where the list goes.** Use the task-list tool the host gives the session (`TodoWrite`,
`TaskCreate`, or the app's own task list, such as the one shown under the message in the Desktop
app). Where there is none, a short numbered list in the first message, posted again with the
current step marked whenever a step finishes.

**Before the database's folder exists** (the first cards of a first run), show the nine steps from
agami-connect's SKILL.md, all not started except the first. Switch to `sm steps` output as soon as
`$ROOT` exists.

**Resuming** (after a hand-off, a password page or a file wait): run `sm steps "$ROOT"` first and
show what it prints. Don't seed the list again from memory.

**A `problem`.** When `sm steps` prints a `problem` that is not null, the model on disk couldn't be
read or the last build didn't finish: say it to the person in one sentence before going on, and
don't start the next step until it is dealt with.

**The step ids**, for the marks:

| id | label |
|---|---|
| `connect` | Connect your database |
| `context` | Tell agami about your data |
| `tables` | Pick the tables |
| `build` | Read the tables and joins |
| `describe` | Describe columns and add metrics |
| `signoff` | Sign off definitions and hide personal data |
| `examples` | Write and check example questions |
| `reports` | Check your reports |
| `first_question` | Summary and first question |
