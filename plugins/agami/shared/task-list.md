# The onboarding task list

Refresh the session's task list from the `list` in `sm steps` output, or in `sm progress --wait` output during the build. Run `sm steps` at the start of onboarding and when a step starts or ends. Replace the task list with that `list`: one item per line, in order, each line's mark giving the item's state. Never write a step or a number `sm steps` did not print.

> `<plugin_root>`: the plugin's absolute path, see [invocation-conventions.md](invocation-conventions.md).
>
> `<root>` is the database's folder, `<artifacts_dir>/<profile>`, written out in full.

The list also moves during describe and sign-off: after each applied chunk, `apply-enrichment`'s `then` says to refresh it, and after each sign-off card is answered, run `sm steps "<root>"` and do what its `then` says.

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

**After a `sm steps` or `sm progress` call, update the task list from `list` when its `then` says
to, before anything else; otherwise there is nothing to do.** The output's
`say` and `then` are followed as [engine-says.md](engine-says.md) says. Never filter or shorten the
output (no `grep`, `head`, `sed` or `jq`): copy the whole `list`.

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
read or the last build didn't finish. The first time, its `say` is the problem line: post it (that is
how you say it to the person before going on), then do what its `then` says; wait until it is fixed.

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
