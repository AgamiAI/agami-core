# Describing and writing examples in parallel (more than 12 tables)

> `<plugin_root>`: the plugin's absolute path, see [invocation-conventions.md](invocation-conventions.md). Each command here is its own Bash call, every path written out in full.

On a model of more than 12 tables, agami-connect hands two pieces of work to subagents so they run
side by side instead of one table after another: describing the tables (Phase 2a and 2b, one
**agami-enricher** per chunk of at most 12 tables) and drafting example questions (Phase 5a, one
**agami-example-writer** per subject area). This is the one place that says how. Everything else in
those phases (the progress lines, the coverage gate, metrics, caveats, the cards) stays with the main
chat and runs as the phase describes it.

## 0. When, and when not

Run `bash "<plugin_root>/scripts/sm" areas "<root>"` and add up `table_count`. Work in parallel only
when **all** of these hold:

- the model has **more than 12 tables**;
- the session has an **Agent tool** (Claude Code has one; `claude -p` without it, and Claude Desktop
  without it, do not);
- the work splits into **at least two** subagents (two chunks for 2a, two areas for 5a).

Otherwise do the phase **inline, exactly as written**, one table after another: that path is
unchanged and gives the same model. Never ask the person which way to go; it is plumbing.

## 1. Chunks (2a only)

Cut each subject area's tables, in the order its bundle lists them, into the fewest runs of at most
12, sized evenly: 13 tables are 7 and 6, 20 are 10 and 10, 25 are 9, 8 and 8. An area of 12 or fewer
is one chunk. Name chunks `<area>-1`, `<area>-2`, … Hidden tables are not in the bundle and are not
described.

## 2. Start

- **Stamp the batch first.** Run `bash "<plugin_root>/scripts/sm" batch-id` once per batch and put
  the `batch` it prints in every input and result file name of that batch. A file from an earlier run
  (a reload, another database with the same area name) then never passes for this run's.
- **Prepare every input with agami's own commands**; a subagent runs no commands. For each area:
  ```bash
  bash "<plugin_root>/scripts/sm" bundle "<root>" --area <area> --out "<artifacts_dir>/local/tmp/agami-enrich-<batch>-<area>.bundle.json"
  ```
  Read the bundle file for the area's table names. For 2a, also each chunk's profile:
  ```bash
  bash "<plugin_root>/scripts/sm" profile "<root>" --table <t1> <t2> --out "<artifacts_dir>/local/tmp/agami-enrich-<batch>-<chunk>.profile.json"
  ```
- **Launch at most 4 subagents in one message**, one Agent call each, with no `run_in_background`, so
  each call returns only when its subagent has finished. More than 4: launch the next 4 once those
  calls have returned. Give each a description in the person's words: *"Describe the Sales tables
  (1 of 2)"*, *"Draft Sales example questions"*.
- Use the agent by name, `agami-core:agami-enricher` or `agami-core:agami-example-writer`. Where the
  Agent tool doesn't list it, launch a general-purpose subagent told to *"Read
  `<plugin_root>/agents/<agent>.md` and follow it for this input: …"*.
- **The enricher's prompt** names `area`, `tables` (the chunk's tables, comma-separated), `bundle_path`,
  `profile_path`, `plugin_root`, `docs` (`$DATA_MODEL_DOC_TEXT` from 1.5, or "none"), `datasource_md`
  (`<root>/datasource.md` when it exists, or "none") and `result_stem`
  (`<artifacts_dir>/local/tmp/agami-enrich-<batch>-<chunk>`).
- **The example writer's prompt** names `area`, `bundle_path`, `plugin_root`, `dialect` (the database
  type), `asked_questions` (`<artifacts_dir>/local/tmp/agami-asked-questions.txt` when 1.5 saved it,
  or "none") and `result_path` (`<artifacts_dir>/local/tmp/agami-seeds-<batch>-<area>.json`).

## 3. Collect: the returned calls are the wait

Each Agent call returns when its subagent is done, so there is nothing to poll. When a message's calls
have returned, post one line (*"✓ Described Sales 1 and 2, Billing 1"*), then for each subagent:

- **Read its result file** with the Read tool (an enricher's `<result_stem>.json`, written last; a
  writer's `result_path`). Present, valid JSON, the shape its agent file names and this run's area,
  and an enricher's `.json` names exactly the `area` and `tables` you launched it with: apply it
  (below). A subagent may write any `agami-` file in the folder, so this is what keeps one chunk's
  from standing in for another's.
- **Missing, not JSON, the wrong shape, another area's or another chunk's, or the call came back without finishing**
  (it says the agent was launched or is still running): give it up in one line and do that piece
  yourself, inline, as the phase describes it. Never wait on it, and never use a file it writes later.

Apply **one write at a time**, never two at once, each with the file as the subagent wrote it (never
edited, merged or split by you):

- **Enricher, 2a:** its edits, held to its own chunk by the engine:
  ```bash
  AGAMI_SCRATCH_ONLY=1 bash "<plugin_root>/scripts/sm" curate "<root>" --ops-file "<artifacts_dir>/local/tmp/agami-enrich-<batch>-<chunk>.ops.json" --allow description,description_source,choice_field,column_group_descriptions --area <area> --tables <t1,t2>
  ```
  A `refused` list means the file did something an enricher may not: nothing was applied. Describe
  that chunk inline instead. A validation failure is handled as 2a handles one.
- **Enricher, 2b:** its entities, when the list isn't empty:
  ```bash
  AGAMI_SCRATCH_ONLY=1 bash "<plugin_root>/scripts/sm" add "<root>" --kind entity --area <area> --from-subagent --file "<artifacts_dir>/local/tmp/agami-enrich-<batch>-<chunk>.entities.json"
  ```
  `--from-subagent` lands every entity unreviewed, whatever the file says, and never replaces one
  already in the model: an entity skipped because its name is taken stays as it was, and goes on 2g's
  cards as a note to the person if the two differ.
  An entity skipped for any other reason is not fixed in the file: draft it
  yourself as 2b says, and add it with your own entities.
- **Enricher, the rest:** its `terms` go into 2a's one `set-terminology` file with your own; its
  `metric_ideas` into 2c; its `cross_area` notes into the entities, joins and metrics you draft with
  the whole model in view; its `questions` into 2g's cards. A subagent never asks; you do.
- **Example writer, 5a:** each area's file goes through 5b+5c's own command, one call per file:
  ```bash
  AGAMI_SCRATCH_ONLY=1 bash "<plugin_root>/scripts/sm" seed-examples "<root>" --profile <profile> --file "<artifacts_dir>/local/tmp/agami-seeds-<batch>-<area>.json"
  ```
  Then write the 2–4 cross-schema seeds yourself, as 5a describes, and run them the same way.

Then carry on with the phase: the coverage gate, the progress line from the command's own numbers,
and the rest of 2 or 5, exactly as in the inline path.

## 4. Never

- Never a subagent that writes the model, runs a command, or asks the person anything. Subagents
  can't show cards: they only read and write their own `agami-` files under
  `<artifacts_dir>/local/tmp/`.
- Never two writes at once. The main chat applies results one at a time, through `sm`.
- Never a second batch for the same tables while the first is running.
- Never end the turn while subagents are running: the person would be left with half a phase.
- Never a number shown to the person that no `sm` command printed.
