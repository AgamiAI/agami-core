# Running subagents in the background

> `<plugin_root>`: the plugin's absolute path, see [invocation-conventions.md](invocation-conventions.md).

agami-connect hands some reading and drafting to subagents so the person is never waiting on one
thing while another could run. This is the one place that says how. The schedule (what runs while
the person is on which cards) is in agami-connect's SKILL.md, "What runs at the same time".

## 1. Start

- **Stamp the batch first.** Run `bash "<plugin_root>/scripts/sm" batch-id` once per batch and put
  the `batch` it prints in every input and result file name of that batch, as the phase files show
  (`agami-enrich-<batch>-<chunk>.result.json`). A file from an earlier run (a reload, the sample
  rebuilt, another database with the same area name) then never passes for this run's.
- Prepare every input file, with the pre-approved `sm` call the phase file names: a subagent runs no
  commands of its own. Inputs and results live under `<artifacts_dir>/local/tmp/`.
- Launch the batch in one message: one Agent call each, with `run_in_background: true`, at most 4
  running at once. The rest wait in a queue (see 3). The one exception is the document readers
  (1.5): each starts as its document's path arrives, still at most 4 at once.
- Each gets its `input_path` (or document) and a `result_path`, and a description in the person's
  words: *"Describe the Sales tables (12)"*.
- Where the Agent tool doesn't list the agent by name (`agami-core:<agent>`), launch a
  general-purpose subagent told to *"Read `<plugin_root>/agents/<agent>.md` and follow it for this
  input: …"*.

## 2. Meanwhile

Go on with the cards and the main-chat work the schedule pairs with the batch. A card keeps the turn
open. **Do not end the turn while subagents are running**: a turn that ends loses this skill's
pre-approved commands, and every later command stops to ask for permission. The one exception is a
Pause the person asks for (review.md 4b): the turn ends and the writers are left to finish; nothing
they write is used, since a resume starts them again with a new stamp.

## 3. Collect

When the paired work is done, block on the results with the pre-approved call:

```bash
bash "<plugin_root>/scripts/sm" wait-files --file "<result 1>" --file "<result 2>" --wait 60
```

It prints `{"ready", "waiting", "all_ready"}`. Repeat it until `all_ready` is true. After each pass:

- post one line naming what is still running, from its `waiting` list (*"Still describing: Sales 2,
  Billing 1"*); never a guess at how long is left;
- apply each result as soon as its file is ready, one write at a time, with the command the phase
  file names;
- **start one queued subagent for each one that finished**, never more than 4 running. Do the same
  whenever the person answers a card while a batch runs: check with `wait-files --wait 0`, then start
  the queued ones. A notification can't be acted on during a card, so this is what moves the queue.

## 4. Give up cleanly

The give-up limit is per subagent: 10 minutes of `--wait 60` passes spent waiting on that subagent,
counted from the first pass after it started (a reader started long before the first pass gets the
full 10). Quick `--wait 0` checks after a card don't count. A result still missing after that, or a
file that is not what it was asked for (not JSON, the wrong shape, another area's), is given up on. Say so in one line and do that piece
yourself, inline, as the phase file describes it for a run with no subagents. The rest of the batch
is applied as usual.

## 5. Fallbacks, in order

1. **The Agent tool has no `run_in_background`:** launch the batch in one message in the foreground
   (the turn waits for them, at most 4 per message), then ask the paired cards after.
2. **With no Agent tool at all** (`claude -p`, older clients), do the same work inline, in the
   order the schedule lists it.

The result must be the same model either way.

## 6. Never

- Never a subagent that writes the model, runs a command, or asks the person anything. Subagents
  can't show cards and don't get this skill's pre-approvals: they only read and write their
  `result_path`.
- Never two writes at once. The main chat applies results one at a time, through `sm`.
- Never a second batch for the same tables while the first is running.
- Never a number shown to the person that no `sm` command printed.
