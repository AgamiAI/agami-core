# The onboarding task list

Run `sm steps` at the start of onboarding, when a step starts or ends, and after every `sm progress --wait`. Replace the session's task list with its `list`: one item per line, in order, each line's mark giving the item's state. Never write a step or a number `sm steps` did not print.

> `<plugin_root>`: the plugin's absolute path, see [invocation-conventions.md](invocation-conventions.md).
>
> `<root>` is the database's folder, `<artifacts_dir>/<profile>`, written out in full.

Also refresh the task list after each applied chunk in describe, and after each sign-off card is answered.

```bash
bash "<plugin_root>/scripts/sm" steps "<root>"
bash "<plugin_root>/scripts/sm" steps "<root>" --mark <id>=in_progress
bash "<plugin_root>/scripts/sm" steps "<root>" --mark <id>=done
bash "<plugin_root>/scripts/sm" steps "<root>" --mark <id>=skipped
```

Each line above is its own Bash call. A mark is written first, then the list is printed, so one
call both marks the step and gives the list to show. Each phase file says which mark to run where.
`sm progress --wait` prints the same `list`, so during the build its output refreshes the list with
no `sm steps` call of its own.

**After every `sm steps` call and every `sm progress --wait` call, the very next thing is the
task-list update.** Never filter or shorten the output (no `grep`, `head`, `sed` or `jq`): copy the
whole `list`.

**Marks.** `✓` is done, `▶` is in progress, `·` is not started. Drop the mark from the item's text
and set the item's state from it. A skipped step is not in the list.

**Where the list goes.** Use the task-list tool the host gives the session (`TodoWrite`,
`TaskCreate`, or the app's own task list, such as the one shown under the message in the Desktop
app). Where there is none, a short numbered list in the first message, posted again with the
current step marked whenever a step finishes.

**Before the database's folder exists** (the first cards of a first run), show the nine steps from
agami-connect's SKILL.md, all not started except the first. Switch to `sm steps` output as soon as
`<root>` exists.

**Resuming** (after a hand-off, a password page or a file wait): run `sm steps "<root>"` first and
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
