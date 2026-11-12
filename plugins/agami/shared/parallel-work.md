# Running subagents in the background

agami-connect hands some reading and drafting to subagents so the person is never waiting on one
thing while another could run. This is the one place that says how. The schedule (what runs while
the person is on which cards) is in agami-connect's SKILL.md, "What runs at the same time".

> `<plugin_root>`: the plugin's absolute path, see [invocation-conventions.md](invocation-conventions.md).

## 1. Start

- Launch every subagent of a batch in one message: one Agent call each, with
  `run_in_background: true`. At most 4 run at once; the rest of the batch starts as one finishes.
- Each gets an input file path (`input_path`) and a `result_path` to write its result to, both under
  `<artifacts_dir>/local/tmp/`. Prepare every input file first, with the pre-approved `sm` call the
  phase file names: a subagent runs no commands of its own.
- Each call's description is the work in the person's words: *"Describe the Sales tables (12)"*.
- Where the Agent tool doesn't list the agent by name (`agami-core:<agent>`), launch a
  general-purpose subagent told to *"Read `<plugin_root>/agents/<agent>.md` and follow it for this
  input: …"*.

## 2. Meanwhile

Go on with the cards and the main-chat work the schedule pairs with the batch. A card keeps the turn
open. **Do not end the turn while subagents are running**: a turn that ends loses this skill's
pre-approved commands, and every later command stops to ask for permission.

## 3. Collect

When the paired work is done, block on the results with the pre-approved call:

```bash
bash "<plugin_root>/scripts/sm" wait-files --file "<result 1>" --file "<result 2>" --wait 60
```

It prints `{"ready", "waiting", "all_ready"}`. Repeat it until `all_ready` is true. After each pass,
post one line naming what is still running, from its `waiting` list (*"Still describing: Sales 2,
Billing 1"*); never a guess at how long is left. Apply each result as soon as its file is ready, one
write at a time, with the command the phase file names.

A doc reader (`agami-doc-reader`) has no Write tool: it returns its result in its reply, which is
collected when its notification arrives, not with `wait-files`.

## 4. Give up cleanly

A result still missing after 10 passes (10 minutes), or a file that is not what the agent was asked
for (not JSON, the wrong shape, another area's): say so in one line and do that piece yourself,
inline, as the phase file describes it for a model with no subagents. The rest of the batch is
applied as usual.

## 5. Fallbacks, in order

1. **The Agent tool has no `run_in_background`:** launch the batch in one message in the foreground
   (the turn waits for them, at most 4 per message), then ask the paired cards after.
2. **No Agent tool at all** (`claude -p`, older clients): do the same work inline, in the order the
   schedule lists it.

The result must be the same model either way.

## 6. Never

- A subagent that writes the model, runs a command, or asks the person anything. Subagents can't
  show cards and don't get this skill's pre-approvals: they only read and write their `result_path`.
- Two writes at once. The main chat applies results one at a time, through `sm`.
- A second batch for the same tables while the first is running.
- A number shown to the person that no `sm` command printed.
