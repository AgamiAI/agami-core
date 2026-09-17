# Changelog

All notable changes to **agami** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The `version` in `.claude-plugin/marketplace.json` and `plugins/agami/.claude-plugin/plugin.json`
is the source of truth a host installs against — bumping it is what invalidates a
user's plugin cache (see [CONTRIBUTING.md](CONTRIBUTING.md)). Each released section
below corresponds to one such version.

## [Unreleased]

### Fixed

- **A served deployment no longer names a datasource the organization does not have.** With no
  `datasource` named and `AGAMI_PROFILE` unset, the server resolved a profile from
  `.config.active_profile` — a setting the local CLI writes — before looking at what the deployment
  actually serves. A leftover `.config` on a dev box or a mounted artifacts directory therefore won:
  on an organization with one datasource, an omitted call ran against a name from someone's
  machine, and `list_datasources` reported that name as active. A served deployment now ignores
  `.config`, and `list_datasources` reports `active_datasource` as `null` rather than `default`
  when several datasources are served and none is named. The local CLI is unchanged. (#253)
- **`AGAMI_REQUIRE_THREAD_ID` no longer accepts a blank `thread_id`.** A required field only has to
  be present, so `""` or whitespace satisfied it without naming a conversation. With the flag on, a
  blank id is now rejected as an input validation error — a deployment turning the flag on should
  confirm its clients send a real id. Deployments with the flag off are unchanged. (#257)
- **An identical answer no longer scores as different when two of its columns hold the same values
  on different rows.** When row order is not compared (reconcile always, and a golden run whose
  answer key has no ORDER BY), each column's values are sorted before columns are paired. Two
  flags that are each true on half the rows then look the same, and the comparator paired
  whichever came first. Pairing each flag with the other's partner misaligned every row, so a
  right answer scored below 1.0. A golden column now takes a generated column of its own name
  first. A name can mislead too, when a statement swaps two labels or aliases one of two columns
  that share a label, so that pairing is checked against the old one and the one that lines up
  more rows is kept.
- **A column that mostly repeats one value no longer pairs with a different column.** A flag that
  is N on every row agreed with any other mostly-N column on nine rows of ten. So a report said
  the rows differed and blamed a column that was right, when the real finding was a missing
  column. A column with no same-named partner now pairs on its values only when those values can
  tell it apart. When row order is compared, no one value may fill more than half the rows. When
  it is not, more than half the values must be distinct, because two flags with similar counts
  overlap on most rows as multisets whatever they hold. Otherwise the column is reported missing.
  Two costs are accepted. A renamed column of that kind that is wrong on one row is now reported
  missing too, rather than as a column that differs on one row. And a score without row order can
  change for such items. A different column that holds exactly the same values still pairs.
- **A right answer now scores 1.0 when its equal columns are renamed or have their labels swapped.**
  When row order is not compared, columns that hold the same values look the same. The comparator
  pairs them by name first, or in order when that lines up more rows. When every column is renamed,
  or labels are swapped among three or more such columns, neither lines the rows up, and a right
  answer scored below 1.0. The comparator now tries the ways to pair these columns and keeps the one
  that lines up the most rows. A tie goes to the way that pairs more same-named columns, then to the
  old pairing. The search is bounded in two ways. It tries at most 720 ways, every order of six
  equal columns. A group past that keeps the old pairing, so seven or more such columns can still
  score below 1.0. And once it has paired every column one way, taking the choice that lines up the
  most rows each time, it reads at most two million more rows, about a fifth of a second. Past that
  it keeps the best pairing it has found. That pairing never lines up fewer rows than the old one,
  but it can fall short of the best. A wrong answer's score can also drop: when one result has more
  such columns than the other, the search can leave a different column over, and that column may
  find no partner.

## [0.9.2] — 2026-09-16

### Added

- **A `sign_in_required` failure kind, for an executor that connects as the person asking.** An
  injected executor that exchanges the signed-in person's own credential for a warehouse one had
  only exit code 4 to report a missing or unrenewable credential. That reached the caller as `auth`
  and "The database rejected the connection's credentials.", and a client relaying it told the
  person their warehouse was broken when signing in again was the whole fix. Exit code 11 now
  arrives as `sign_in_required` on every transport, with a sentence telling the person to sign in
  again (reconnect the connector) and start a new conversation. The `execute_sql` description tells
  the agent not to retry and not to describe the database as failing. The executor's own text is
  never relayed and cannot reclassify the code. The built-in executor never raises it. An executor
  should use `FAILURE_KIND_TO_EXIT.get("sign_in_required", 4)` rather than the literal, so it falls
  back to `auth` on an older core.

## [0.9.1] — 2026-09-16

### Added

- **A resumed conversation can no longer query a model that has since changed (#364).** A client
  keeps `get_datasource_schema` and `get_prompt_examples` output as text in its context, so a user
  who returned to an old conversation after the model was edited, or after an older version was put
  back, got SQL written against the model as it used to be. Both tools now return `model_version`,
  and on a hosted deployment `execute_sql` takes it back: a missing or different version is refused
  on the new rule `stale_model`, whose remediation names the live version and says to fetch the
  schema and examples again. The comparison is equality, so putting an older version back is caught
  the same way as deploying a new one. The property is optional in the tool's input schema on
  purpose — a required one fails validation before the handler runs, with no fix named. A
  deployment with no recorded version, and the local path, never refuse on it.

- **The instructions say which columns may be queried, and what a scope refusal means.** Neither
  rule was stated anywhere a client reads before writing SQL. "Only columns declared on the model's
  tables may be queried" existed solely inside the refusal a statement received for breaking it, so
  an agent learned the rule by being refused: on one deployment, five refusals in a row were
  plausible names that the model simply does not declare. And the two failure channels taught
  opposite lessons for one mistake. A column the DATABASE lacks is documented as repairable
  ("correct the statement and retry SILENTLY"); a column the MODEL does not declare said only to
  relay the refusal's `remediation`, which reads as a dead end and hands the user an instruction to
  go and edit the semantic model. Both now say the same thing, and say which fix belongs to which
  rule rather than stating one default and hedging it: on a scope refusal the schema the agent
  already holds is usually enough, so rewrite with declared names and retry, and on every other
  rule the remediation is the fix. Two cases a rewrite does not repair are named, because getting
  either wrong is worse than the refusal: a table name matching more than one schema is declared
  twice and is repaired by qualifying it, not renaming it; and a column left out of the model ON
  PURPOSE is how an author makes one unreadable, and arrives as the same refusal as a typo, so
  answering with the nearest declared column instead answers a different question than the one
  asked. The columns rule is keyed to the table's own entry rather than to the response as a
  whole, since a response sized down to `summary` or `index` carries no columns at all and a rule
  keyed on what was read would tell that agent every column it needs is out of scope.

### Fixed

- **A self-hosted server could report an old model version as the live one (#364).** Each deploy
  added a version row and never removed the previous ones, wrote them without a date, and the
  lookup picked the "newest" by that date. With every date blank, the live version was whichever
  hash sorted last; on Postgres, which sorts blanks first, a single undated row also outranked every
  dated one. The trust receipt's `model_version` came from that lookup. The table now holds one
  row per datasource, replaced and dated on every write (a restore included), and the lookup sorts
  any undated row left from before last, on both engines. A deploy also records the hash of the
  files it deploys rather than the newest snapshot's name, so an edit without a new snapshot is a
  new version, and a model that was never snapshotted no longer deploys as the constant `deployed`.

- **A reconcile card showed nothing in its SQL section when agami's query failed.** The row record
  dropped agami's statement whenever the row's status was `error`, so the one row where reading the
  statement is the entire diagnosis was the one row that did not show it: a semantic model declaring
  a column the warehouse does not have looks identical, from the card, to agami writing a name that
  was never there. The rule it came from is kept and narrowed to what it was actually protecting.
  An error row still carries no RESULT anyone could mistake for a verified answer (`actual`,
  `recorded` and `delta_pct` stay null), and now carries the statement, which cannot be mistaken for
  one that answered because the row's own error sentence says it did not. A multi-statement answer
  keeps all of its statements for the same reason. `check-run`'s demand for a result file is now on
  rows that answered rather than on rows that hold a statement, since a query that never ran has no
  result file to point at. Two smaller consequences, both deliberate: a card showing several
  statements for an error row no longer marks the last one "compared", because on that row nothing
  was; and a row whose comparison failed while agami's query ran now reads "the run's files do not
  say why" rather than "agami's query failed", which is what the files actually support.

### Changed

- **`get_prompt_examples` no longer tells a client to leave `area` out.** The steer landed in 0.9.0
  so that an `area` call could not hide a better match sitting in another area, and it overshot: on
  a served deployment no client passed an `area` at all, so a question that plainly belongs to one
  subject area was ranked against every area's library. All three copies of the sentence are gone
  (the server instructions, the tool description, and the reminder stamped onto a schema response).
  What a call does is unchanged: an `area` still returns that area's examples, plus the cross-area
  bucket on a served deployment, and omitting it still searches the whole library.
- **The `area` parameter now describes both serving paths.** It said "cross-area examples still
  included", which is true of a served deployment and false of the local file path, where no
  cross-area bucket exists on disk and an `area` returns that area alone. One schema described one
  path, so a local caller was promised examples that path cannot return.

## [0.9.0] — 2026-09-16

### Added

- **`python -m execute_sql --batch` runs a plan of statements in one process.** Every statement paid
  an interpreter start, a full semantic-model load (twice) and a connect, about sixteen seconds for
  a query of a tenth of a second, and reconcile's statement check issues about ten per row. The
  batch door takes a JSON list of `{id, sql | sql_file, out, area?}`, resolves the semantic model once (a
  per-process memo keyed by the model files' count and newest mtime, never caching an absent
  model; the hosted path is unchanged), keeps the connection open across the items on Postgres,
  Redshift, Supabase and SQLite (dropped after a statement that broke it), and still runs every
  item through the guard on its own; each CSV, each `<out>.run.json` and a manifest are written by
  the door. Reconcile's statement check runs a row's probes this way on the `execute_sql` tier.
  (ACE-137)

- **The reconcile report is built by code from the run directory, never typed.** `reconcile.py
  record --run-dir --row` assembles the row record from the row's files and appends it to the
  checkpoint (replacing an earlier record for the row; a question-only row nobody graded is
  refused, since it waits for the grading page). `render_reconcile_report.py --run-dir` builds the
  items itself, takes from the session a words file with `sentence` and `change` only (any other
  field is refused), writes `report-items.json` beside the page, stamps the page with the digest
  of the items it rendered, and prints the three lines the skill says. `reconcile.py check-run`
  refuses to let Phase 3 speak while a row's files are missing or the page's stamp is not the
  current items' digest. The skill's 2c writes `diff.json`, 2b writes `agami-run.json`, and 2d and
  3a.5 name the verbs. (ACE-136)

- **Agami's answer may be several statements, and the reconcile page shows every one.** The cold
  client's reply was read as one string under `sql`; a list, or several statements in one string,
  lost everything but the first object or read as unreadable. The generator now keeps every
  statement in order (`statements`), answers with the last, and the prompt says so; the ask door
  writes `statements` beside `sql`; the row record carries `agami_statements`; the report card's
  SQL block lists them numbered with the last marked "compared", and the rows check notes that
  agami ran N queries. Only the last is run and graded. (ACE-135)

- **An eighth claim, `outputs`, says what a statement selects.** `sm claims` and the golden run
  compared tables, filters, date window, group keys, join keys, ordering and limit, and never the
  projection, so two statements selecting different expressions could still read as the same
  query. The new claim reads each output expression with its alias peeled (`total` and
  `o.amount_total` over one `SUM(orders.amount)` agree; `SUM` against `AVG` differs; `SELECT *` is the
  one key `*`). It reports and never gates. Every reader that counted seven now counts eight.
  (ACE-131)

- **`sm compare-results --unordered`** compares the rows as a set whatever ORDER BY either statement
  wrote, for a caller whose ordering is a claim of its own (reconcile's 2e, in ACE-134). The
  comparator's `compare_result_sets` takes the same as `ordered=False`; the golden run is unchanged.
  (ACE-131)

- **Four `sm` verbs that grade a statement a person supplied, part by part.** `agami-reconcile` is
  learning to take a trusted query as evidence rather than as the answer, and these are the
  deterministic checks it will lean on. `sm claims` reports where two statements differ, in the seven
  claims the golden runner already compares. `sm compare-results` says whether two result CSVs say the
  same thing, through the golden comparator, so a table-shaped answer is judged the way an answer key
  is. `sm join-probes` names, for every join a statement wrote, whether the semantic model declares a
  relationship between those tables and whether the written key matches the declared one, and emits
  the overlap and cardinality probes that would show whether the keys really resolve. `sm filter-values
  plan` names, for every value typed into a filter, the column it binds to, whether the semantic
  model's list of values holds it, and the probes that would settle it; `sm filter-values judge` reads
  the probe results back and grades each value `confirmed`, `model_gap`, `query_defect` or
  `unresolved`. None of the four runs SQL: the skill runs every probe through the same execution tier
  a question takes. Joins are classified with the receipt's own flags, so a CTE that shadows a
  declared table, a `USING`, a comma join, or a declared `on:` this layer cannot read all come back
  as open states and never as a settled claim about a key nobody read. A probe that came back empty
  is graded as a probe that failed, never as a column that holds nothing. A near miss is a case and
  whitespace fold only, an empty list of values reads as not yet decoded rather than as no legal
  values, a column marked sensitive is never probed for a value, and a value carrying a backslash or
  a control character is never sent to the warehouse, because engines quote it differently. The
  overlap probe is the one introspection already trusts, now shared as `introspect.overlap_sql` and
  bounded to its 50-row sample on every engine through the new `Dialect.limited`, where it used to
  be bounded only where the row-limit keyword was `LIMIT`. (ACE-114)

- **`reconcile.py` reads any input a person brings, and grades a supplied statement part by part.**
  Three new verbs beside `parse`, `diff` and `band`, which are unchanged. `intake` reads the four
  shapes the reconcile skill will accept, a list of questions, questions with the SQL the person
  trusts, labels with numbers, and labels with numbers and the SQL behind each tile, into one row
  shape with the question, the statement and the expected value, any of which may be missing. A CSV
  whose third column is SQL now yields a statement instead of a label with SQL glued onto it; a
  statement whose label matches a tile joins that tile's row. `ledger` grades every part of a
  supplied statement from the files the skill wrote beside it: what happened when it ran, what
  `sm prepare` and `sm receipt` said, what `sm join-probes` and `sm filter-values judge` reported, and
  the probe CSVs the execution tier returned. Four grades, and only measurement earns `model_gap`; a
  join that could not be graded leaves the fan-out check on its aggregate `unresolved`, said out
  loud. `findings` writes a run's findings, the person's own defects listed apart, and every row's
  ledger. Three shared references describe the row, the ledger and how a supplied statement is run
  the way the AI's own SQL runs. Nothing here runs SQL or writes to the semantic model. (ACE-115)

- **`agami-reconcile` takes the evidence a person brings and grades their SQL part by part.** Four
  input shapes, detected and never asked for: a dashboard screenshot, a table of numbers, SQL the
  person trusts (alone, beside a question, or as the third column behind each tile), and a list of
  questions with no answers. A supplied statement runs on the road agami's own SQL runs, with the
  same guards, and every part of it is graded before anything is compared: a scope refusal is a
  finding about the semantic model rather than a crash, a miscased value or a join on the wrong key
  is the person's defect, and a part nobody could check says so. Two new row statuses keep a lucky
  match out of the keep-offer (`match_unverified`) and keep a doubtful expected value out of the
  mismatch count (`expected_doubtful`); `reconcile.py status` applies the rules. Tables are compared
  through the golden comparator and the two statements' differences are named by claim. The run
  writes its findings and the person's defects under `local/reconcile/<ts>/`. The skill still never
  writes to the semantic model; a single fix goes through `agami-save-correction`, which now grades a
  pasted statement with the same ledger. The plan-mode refusal no longer assumes a CSV path.
  (ACE-116)

- **A grading page for a list of questions.** When a person brings questions and no answers, agami
  answers each one and there is nothing to compare against, so the person grades. One page lists
  every question with the AI's answer as one cell or a shape, the receipt's signals, and right,
  wrong or unsure, with a box for the right SQL or for words; one block comes back. A right answer
  becomes the expected value. A wrong answer with SQL is graded like any statement the person
  supplies. A wrong answer with words becomes a finding carrying them. Unsure changes nothing.
  `render_reconcile_grades.py` and `parse_reconcile_grades.py` follow the model explorer's
  paste-back pattern; the page never renders a result row, and a grade decides nothing on its own.
  An end-to-end test walks the flawed-inputs chain over the sample store: the wrong join key, the
  miscased value, the omitted required filter, the clean count. (ACE-117)
- `sm mentions` quotes every description, caveat, glossary line, narrative paragraph and prompt
  example that mentions a table or column a statement reads, and the reconcile ledger puts those
  words beside every part that fell short, with a flag when two of them name different values for
  one column. Quoted, never graded. (ACE-119)
- Reconcile Phase 1.5g reads a person's question beside their statement and writes
  `question_fit.json`; the ledger's `question_fit` part withholds a doubtful row from the keep-offer,
  and a fit that was never checked is an open part rather than a silent pass. (ACE-120)
- `shared/plain-language.md` says how reconcile talks to the person in Phase 3: four actors and no
  fifth, the thing and never the mechanism, one idea per sentence, the semantic model quoted when it
  decided something. Phase 3 points at it. (ACE-121)
- Reconcile's Phase 3 tells every row in four beats, in the reader's order: how we read and checked
  the input, what agami did with the question and answered, how it got there, and what to change or
  keep. The tables and the offer keep their text. The ledger runs once per row, after the
  comparison. (ACE-122)
- One reconcile report page per run, on the plugin's theme, tells every row in the four beats and
  takes the person's decisions back as one pasted block; a keep is offered only where the run said
  match, and every decision goes through a door that already exists. (ACE-123)
- Reconcile takes input the way connect does: an options prompt for the four shapes, a template CSV
  the person fills with a hand-off, and an intake page that shows what was read before anything
  runs, with one block back applied by a parser. The grading, intake and report pages share one
  stylesheet and the same four beats. (ACE-124)
- Reconcile works five rows at a time: `reconcile.py next-chunk` reads the run's checkpoint
  (rows.jsonl) and hands back the next rows, so a long run resumes and no row runs twice; the
  report, intake and grading pages filter by status, owner, shape or a word. (ACE-125)
- The reconcile report card is a diff grid built by code: `reconcile.py report-items` templates one row
  per check from the run's files, the check named first, yours and agami as values with the differing
  tokens highlighted, the sides aligned by construction; one sentence and one action per card. (ACE-126)
- The reconcile report card carries two facts read by code: the result (does the data match; are the two
  queries the same) as the pill, and the fix (your query, the semantic model, the examples, the question,
  agami again, nothing) as the action. A query written differently is a noted fact, no longer a blocker
  on a matching answer. (ACE-127)
- The reconcile card after a second read: the question is the title, columns are compared by the data
  they carry (the compare-results score names `column_pairs` and `unmatched_generated_columns`), a bare
  column reads as its table's column in the claims reader, the change text and the decision boxes derive
  from the one fix, an `example` decision joins the block, the checks panel folds. (ACE-128)
- Reconcile asks agami cold: `run_golden_eval.py --ask` answers one question and `--ask-file` a chunk of
  them with the golden run's own generator and context, fetching the context once and spawning the
  client per question several at a time; Phase 2b never writes agami's SQL in the reconcile session, and
  a missing client is an error row, never the session's own statement. (ACE-129)
- From the first test of the grid: a plain column in a list query is no longer graded as a missing
  metric (the receipt's output items say whether they aggregate); a date window written against the
  clock (`date_trunc('year', current_date) + interval`) resolves and compares against another such
  window; the claim keys read as words on the page; the owner order is your query, then the semantic
  model, then the question, with the change text naming the gaps or the extra columns; the two SQL
  statements sit collapsed under the grid. (ACE-126)

### Changed

- **The reconcile card shows a verdict and what to do, and nothing else until asked.** Five
  statements run against a real warehouse found the card burying the answer: the verdict sat as plain
  text between two panels, the same conclusion was written four times in four registers, and a check
  saying "the column holds more than 25 distinct values" had the same weight as one that found a
  mistake. A card is now the question, one verdict carrying the measurement behind it ("9 of 10 rows
  match"), the one thing to do about it, and three closed sections: the answer, the two queries, and
  the model checks. Each section carries a one-line summary that names the exception when there is
  one and a count when there is not, so most cards need no opening and every card is the same height
  at rest. Opening the answer shows up to five rows of the two results side by side, differing rows
  first, read from the run's own CSVs at render time so the checkpoint still carries no result row
  and the renderer enforces the five-row cap rather than trusting its producer. Three kinds of line
  that were true and useless are gone: a wide column having no declared value list (the default state
  of every free-text column, and reported even for a query pinned to one city, because the probe
  reads the whole column), a join that dropped nothing, and twelve near-identical value checks that
  now roll into one counted line. Anything that did not pass is kept whole and in place. The pages
  also went from eleven font sizes, including 12.5 and 13.5, to five plus one for data, where
  monospace now means "this came from the warehouse, or it is a statement".

- **A verdict a person reads is a sentence, not a code word.** A row could read `expected_doubtful`,
  which is the row where the analyst's own query is the thing in doubt, and a check could read
  `fan_out: SUM(total)`, the one word the skill's own plain-language rule forbids. The tokens stay
  on the wire, where `rows.jsonl`, the keep gate and the `status` verb are pinned to them; what a
  person sees now comes from one table in `reconcile.py` and nowhere else. A row reads "same
  answer", "same answer, but part of your query could not be checked", "different answer",
  "different answer, and your query has a problem", or "could not compare", so the status says both
  of the facts it carries: whether the two answers agreed, and whether the analyst's own query
  checked out. The report page is handed the colour family and the filter-chip words with its items
  and keeps no glossary of its own, so the card, the chips and the chat cannot drift into calling
  one row three things. `shared/plain-language.md` points at the table rather than restating it, and
  a test refuses a status token in any line the skill puts in front of a person.

- **The result comparator pairs a column that mostly agrees instead of calling it missing.** One
  differing cell used to unpair a column: the score fell to 0 with "no generated column carries the
  values of: total" for a column agreeing on nine rows of ten, and every reader keyed on the pairs
  saw an empty list. Columns still pair on whole-vector equality first; what is left pairs by name
  when the two sides spell one (the qualifier and case dropped), or by the highest share of agreeing
  rows when more than half agree. The score then counts rows ("9 of the answer key's 10 rows
  matched") and carries `column_agreement` beside `column_pairs` and `paired_row_share` over the
  paired columns. An item still passes at exactly 1.0; a golden column with no partner at all still
  scores 0 with its name. Three pins moved with it: a same-named column of another type, a null
  against an empty string, and one differing row now read as a pair that disagrees, not a column
  that is absent. (ACE-131)

### Fixed

- **Text past plain ASCII survives `sm` on Windows.** Five `sm` commands — `set-terminology`,
  `curate`, `add`, `add-example` and `seed-examples` — read their JSON file in the platform's
  default encoding, which on Windows is not UTF-8. An em dash became `â€”`, and was saved as valid
  UTF-8, so nothing flagged it. Worse, that garbled text holds a byte Windows' encoding cannot read
  back, so the next read of the file failed — and because `get_prompt_examples` reads every subject
  area into one response, one such file dropped the curated examples for every area. Both reads now
  use UTF-8, and a file that still cannot be read is skipped on its own rather than failing the
  rest. Text already garbled by an earlier `sm` run is not repaired by this; re-save it. (#236)

- **The reconcile card reads how many rows agree, names the side that failed, and still says what
  the two queries are when the data could not be compared.** One differing cell used to print "0% of
  the values match" for a column that was there; the values row now reads the comparator's share
  ("9 of 10 rows match, differs in total on 1 of 10 rows"), and "same rows, different columns" needs
  the paired columns to agree on every row. Every error row said "agami's query failed" although the
  cause was often the person's statement, two empty results or a bare question; the card now reads
  the cause from the row's files ("This row could not be compared: your query did not run: …"),
  offers agami again only when agami failed, and sends a question-only row to the grading page. The
  result pill reads "could not compare", or "same query, answer not compared" and "different query,
  answer not compared" when the claims could be read, the new `outputs` claim ("selects") included.
  Phase 2e passes `--unordered`, so a different sort is a different query and never a different
  answer. (ACE-134)

- **A join on the key the model declares one-to-one is no longer reported as a fan trap because a
  second edge exists between the same two tables.** The fan and chasm pre-flight matched a declared
  edge to a join by table pair and never read the columns the join wrote, so a subclass view joined
  to its base table on its id fanned whenever the model also declared a many-to-one between the pair
  on another key, and the same receipt's joins section, which does read the key, called the join
  one-to-one. The pre-flight now keeps only the edges whose declared columns the written join
  matches; a join on a key the model does not declare, or two tables in scope with no join between
  them, keep every edge and today's verdict. (ACE-133)

- **A database failure nobody could read is no longer reported as a syntax error.** Every engine
  raises its execution failure with the same exit code, and the classifier read that code back as
  `syntax` whenever none of its rules matched the message, so a connection dropping mid-statement
  ("server closed the connection unexpectedly", "SSL SYSCALL error: EOF detected") told agami-query to
  regenerate a correct statement twice and told reconcile that the person's statement was wrong. The
  wire-drop messages now read `network` (stop, no retry), and an execution failure no rule reads is
  `other`, never `syntax`. The connection reference's exit-code line now matches the code. (ACE-132)

- **A filter value the warehouse spells differently is no longer graded as a mistake in your query.**
  The value grader compared the literal your statement wrote, as text, against the column's distinct
  values, as the CSV rendered them, and returned a defect on a miss before it read the existence
  count it had already run. `WHERE is_active = 1` over a column whose values render as `True` and
  `False` graded "not a value the column holds" while its own probe had counted thousands of rows
  with it, and the reconcile report told the person their working query was wrong. The count now
  decides: a value missing from the list but matched by the statement's own predicate is confirmed,
  with a note that the spelling check did not decide and, when the list spells it another way, what
  that spelling is. The grade stays a defect when the count was zero, or when the probe did not run
  (the note says which). (ACE-130)

## [0.8.8] — 2026-09-15

### Fixed

- **A same-named table in another schema no longer passes table or column scope** (#332). With
  `sales_data.orders` declared, `SELECT … FROM staging.orders` used to pass because only the bare
  name was compared. A schema-qualified reference must now match a table declared with that schema
  (a table declared without one is reachable only unqualified), and an unqualified name the model
  declares under two or more schemas is refused as ambiguous, asking for `schema.table`. Column
  scope checks a column against the schema actually read, not every same-named table.
- **A CTE name no longer hides a physical table its `WITH` does not enclose.** CTE references are
  resolved per reference: a body sees earlier siblings and enclosing `WITH`s, its own name only in
  the last arm of a `WITH RECURSIVE … UNION` (its recursive term), a schema-qualified name is never a CTE, and a
  quoted name binds only a quoted reference spelled exactly the same.
- The validator warns when one table name is declared under two or more schemas: lookups and schema
  serving still treat such names by bare name, so queries must use the qualified form.

## [0.8.7] — 2026-09-15

### Added

- **An organisation can have its own row cap and statement time limit** (#329, engine half). Core
  stores no such setting: an embedder registers a provider, `(org_id) -> {"max_rows", "timeout_s"}`,
  through `Adapters.statement_limits` or `tools.set_statement_limits_provider`. A missing, `None` or
  unusable value (not a positive whole number, or a provider that raises) falls back to
  `AGAMI_SQL_MAX_ROWS` / `AGAMI_SQL_TIMEOUT_S`, which stay the deployment default, with a warning in
  the log. There is no policy ceiling, only what the engines can represent: a time limit whose native
  setting — the limit plus the executor's 5-second skew — would pass seven days (604,800 seconds,
  Snowflake's own maximum and the smallest among the supported engines; so 604,795 is the largest
  usable limit) and a
  row cap of 2,147,483,647 or more (the drivers fetch one row past the cap, in a 32-bit count) are
  treated as unusable, from the provider and from `AGAMI_SQL_TIMEOUT_S` / `AGAMI_SQL_MAX_ROWS` alike.
  - An evaluation run scores both statements of each case under the named organisation's limits.
  - `tools.statement_limit_is_usable(key, value)` is the rule a provider's values are held to (a
    positive whole number within those two bounds), public so a settings screen can refuse at save
    time what the executor would otherwise decline on every statement.
  - The limits are resolved once per `execute_sql` call and held for the whole call, and the forked
    child is handed the same two numbers in its environment, so the watchdog, the native bound, the
    outer bound and the supervisor still derive from one budget on both sides of the fork.
  - `tools.statement_limits(org_id=None)` reports the limits in force for the current (or a named)
    organisation; `tools.statement_limit_defaults()` reports the deployment values and the
    recommended ones (1000 rows, 30 seconds). `tools.pinned_statement_limits(org_id)` lets a direct
    caller of `execute_guarded` apply an organisation's limits.
  - The `execute_sql` description states the caller's organisation's numbers, built when tools are
    listed rather than once at start-up. A client keeps the list for its session, so a changed limit
    reaches new sessions; an existing session meets it in the refusal, which names the number per call.
- **`sm set-description`, and onboarding asks for a datasource description** (#327). The one line
  `list_datasources` shows an agent to route a question by could only be hand-edited into
  `datasource.yaml`. `sm set-description <root> --description "…"` writes it (validated, committed),
  `agami-connect` asks for it on every onboard — with an option to generate it from the enriched
  model, as it does for the database narrative — and `model_deploy` warns when a datasource is
  deployed without one. A generated line is passed to the command through a quoted heredoc, never
  pasted into a shell argument, since database metadata can contain quotes or `$(…)`; a re-onboard
  can keep an existing description.

### Fixed

- **Follow-ups to per-organisation statement limits** (#334, #338):
  - A row cap too large for the drivers to fetch (2,147,483,647 or more) and a time limit over seven
    days now fall back to the deployment value, instead of failing every statement with an
    `OverflowError` or a native timeout the engine rejects before the query runs.
  - A provider mapping that raises when read falls back like a provider that raises, instead of
    escaping the resolver.
  - With a provider registered, the HTTP server's tool-visibility predicate runs in the request task
    again, as `build_server` documents; only the descriptions are computed off the event loop.
  - The `execute_sql` description no longer names "the deployment row ceiling" before stating the
    caller's own row limit.
- **A table outside the connection's default schema resolves when the client names it without its
  schema** (#258). A large model is served at the `summary` tier, whose area table lists carried a
  bare name, so the client wrote `FROM orders` and a warehouse keeping it in `sales_data` answered
  `relation "orders" does not exist` — a failed statement and a retry on every such question. Two
  changes:
  - `get_datasource_schema`'s summary table list now carries each table's `schema`, as the full
    and table-scoped tiers already did.
  - On Postgres, Redshift and Supabase, when every table in the model declares the same schema and it
    is not `public`, a statement runs with `SET LOCAL search_path` set to that schema (plus `public`),
    so a bare name still resolves. Any other model — two or more schemas, a table in `public`, or a
    table with no schema — gets no path: the path would outrank the schema those tables resolve
    through, and a bare name meant for one could silently read a same-named table the model does not
    declare. It uses the model the semantic-model pass already loaded, so it applies only when that
    pass is on — which is off by default on a server (see `SECURITY.md`); there, the summary tier's
    `schema` is the fix.
- **A query that names no datasource no longer runs against a guessed one** (#327). On an
  organization serving several datasources, an omitted `datasource` fell through to a fallback (an
  env var, an active profile) and the statement ran there — so SQL written for one datasource reached
  another and was refused as out of scope. `execute_sql`, `get_datasource_schema` and
  `get_prompt_examples` now refuse an omitted `datasource` when more than one is served, naming the
  choices (new `datasource_required` rule, decided before any model is consulted). One served
  datasource still resolves as before.
- **A table-scope refusal names the datasource that declares the table** (#327). It said "add the
  table to the model" when the table was already declared in another of the organization's
  datasources; it now says which one to run the query against — only a datasource that declares every
  table in the statement — or that the tables live in different datasources and one statement cannot
  join them. Only the same organization's datasources are named. Table-scope refusals come from the
  semantic-model pass, which is off by default on a server (see `SECURITY.md`), so there the hint has
  nothing to rewrite.

## [0.8.6] — 2026-09-14

### Added

- **A Redshift schema response tells the client what Redshift rejects** (#325). The client was told
  the engine and still wrote PostgreSQL — `FILTER` on aggregates, `SUBSTR`, date arithmetic in
  PostgreSQL argument order, boolean casts — and each statement failed at the warehouse and cost a
  retry. `get_datasource_schema` now carries `dialect_rules` for an engine with known gaps (Redshift
  today): each construct it rejects and what to write instead, read before the SQL is written. Rules
  are per engine and read-path only; an engine with no entry gets nothing.
- **`execute_sql`'s description states the row cap and statement deadline** (#326). It said a result
  over "the deployment row ceiling" was refused and never said what the ceiling was, so a client
  learned it by being refused. The numbers now come from the same settings the executor enforces
  (`AGAMI_SQL_MAX_ROWS`, `AGAMI_SQL_TIMEOUT_S`), with what to do about each. `tools.statement_limits()`
  returns both, so an administration screen can show the limits in force.

### Fixed

- **The activity log records the datasource a call ran against** (#328). `tool_calls.datasource`
  was the argument the client sent, so every call that omitted it — and the server resolved one —
  was logged with an empty datasource. It is now the resolved datasource, and the new
  `datasource_source` column (migration 025) says whether the client named it (`explicit`) or the
  server chose it (`resolved`).

## [0.8.5] — 2026-09-14

### Added

- **The import door reads an Excel workbook.** `golden_author.py parse --file bank.xlsx` reads one
  sheet of an `.xlsx` into the same parse a CSV goes through, with the standard library alone — a
  workbook is a zip of XML, so no dependency is added to a plugin that installs with none. A workbook
  with several sheets stops and names them, because which tab holds the questions is the person's
  call; `--sheet` names it. In a workbook the header no longer has to be the first row: the first row
  within the top 20 that names a question column is taken, so a title block above the table is fine,
  and every row above the header is reported rather than silently dropped. A CSV's header is still
  its first row. Rows keep Excel's own numbering, so a skipped row is reported at the number the
  person sees, and trailing rows Excel formats but never fills are dropped rather than reported as
  empty questions. Both the Transitional and Strict flavours of `.xlsx` are read. Every coordinate
  in the file is checked against Excel's own limits before it is used, and a part declaring a DTD is
  refused in any encoding, so a crafted workbook costs a refusal rather than memory. An `.xls` file
  — Excel's older binary format — is still refused, with how to get past it, and `--csv` keeps
  working. A column the import doesn't read, but that looks like a field it does — a header with
  `sql` or `question` in it, for a field the sheet hasn't supplied — is reported rather than
  silently ignored, the skill asks the person about it, and `--column sql="<header>"` reads it
  without renaming anything. (#261)

### Changed

- **A schema response says when a datasource has stored examples.** Clients often skipped
  `get_prompt_examples` (#301): the only instruction to call it lived in the server instructions,
  which a host may weight below its own, so curated corrections never reached the SQL.
  `get_datasource_schema` now carries `prompt_examples` — how many examples the datasource has, and
  a one-line reminder to fetch them — whenever it has any. It carries a count, never the examples:
  ranking and returning them stays `get_prompt_examples`' job. The count is datasource-wide even on
  an `area`-scoped call, and the instructions and both tool descriptions now say to pass the
  question as `query` and leave `area` out unless sure, because an `area` drops every other
  area's examples, however well they match.

- **A golden run pays for the model's description once, not once per question.** Every question
  starts its own client, and every one re-sent the whole model — about 35k tokens on a 22-area
  profile — in a prompt that could not be reused, because it began with the client's own system
  prompt naming a fresh temporary directory. So nothing was ever read from cache, and a dozen
  questions could spend a subscription's session limit. The part of the context that is the same
  for every question — the tool's description of the model, plus any glossary paragraph it lacks —
  now goes in the child's system prompt, which the client caches — fenced as reference data, so
  text people wrote into a narrative or a note cannot act as an instruction. Only what the question
  adds goes with it: metrics the cached description lacks, and its ranked examples. Measured: a second call sharing an 11k-token system
  prompt read all of it from cache and wrote 134 tokens. Two smaller savings come with it: the
  client's own ~6k-token default system prompt is replaced, and glossary paragraphs the tool already
  sends are no longer sent twice. What the generator is told, and how the answer is scored, are
  unchanged.

- **A golden run can set how hard the generator reasons.** `--effort low|medium|high|xhigh|max`
  passes the client's own reasoning level to every generation; unset keeps the client's default,
  as before. Once the model's description is cached, reasoning the answer never shows is most of
  what a question costs — measured at 90-97% of its output tokens, against a statement of 50-80 —
  so a lower level is the next large saving. The level is recorded in the run's JSON and artifact,
  because a score measured at one level says nothing about another.

### Fixed

- **A chain of joins is no longer reported as a chasm trap.** The aggregates section flagged two
  measures as inflating each other through a shared dimension whenever the model declared both of
  their tables many-to-one to it — even when the statement joined one table to the other and only
  then to the dimension, so no cross-product could occur. The receipt then named a join the SQL never
  wrote. Two measures now count as a chasm only when the statement did not join their tables to each
  other; a statement joining each of them to the dimension separately is still reported.

## [0.8.4] — 2026-09-12

### Fixed

- **A tool result now carries the verified caller's identity.** The hosted HTTP transport
  (`mcp_http.py`) stamps a `caller_identity` field onto every tool result that is a JSON object, and the shared
  instructions tell the model to resolve a self-referential term ("my", "me", "I", "mine") against
  it rather than against a stored example's SQL — so a self-referential question has a real
  identity to anchor on instead of inheriting whichever past example happens to match closely
  (ACE-118). `caller_identity` is reserved and server-injected: it always overwrites a same-named
  key a tool's own JSON body might already carry.

- **The eval's generator can start on Windows.** The client's Windows runtime (Bun) needs
  `SystemRoot` set to initialize WinSock before its first network call; without it the child exited
  immediately on a message `stderr=subprocess.DEVNULL` discarded, so every item in a Windows run
  errored identically with "the generator exited without answering" — indistinguishable from a real
  client or model problem. `SystemRoot` joins the child's environment allowlist, inert everywhere
  the key doesn't exist. (#280)

- **The save door stops committing a query's result rows to git.** Confirming a golden-dataset item
  wrote the confirmed query's actual result rows into a `recorded` block, into a path with no
  gitignore exclusion — any real data a golden question's query returned ended up in version
  control as a side effect of confirming an answer key. Nothing needed those rows: scoring
  re-executes `expected.sql` live and never reads `recorded`, and the explorer already refused to
  render row content for the same privacy reason. The save door now stamps only `recorded.at`. (#281)

- **The eval sources its context from the product's own `get_datasource_schema` tool, not a
  hand-rendered copy of it.** The script's own rendering of the whole semantic model measured ~60k
  tokens per question on a live 22-area profile — a 43-case run cost ~2.6M tokens, enough to
  exhaust a subscription twice. Worse than the cost: it scored a generator against a context no
  real session ever sees, since the shipped product always narrows through this same tool. Calling
  it in-process, with no `mode` forced so `auto` picks verbosity the way a real session does, cuts
  token cost by roughly half and — verified live against a real warehouse, twice — reproduces the
  correct join every time. Known gap: called this way, with no `dataset_names` narrowing, the tool
  does not return join cardinality or entity aliases; a session that narrows to specific tables
  sees more than this single unnarrowed call does. (#277)

### Changed

- **Importing a question bank confirms the rows that already carry an answer.** A row with no `sql`
  is still written unconfirmed — a question with nothing yet to check — but a row whose sheet
  already carried a statement now lands `sql_confirmed: true` on import, with `confirmed_by.method`
  naming the sheet as the source. Previously every imported row was written unconfirmed regardless,
  which meant a pre-validated question bank could never gate a run without re-confirming every
  answer a second time through the save door — reporting scores nobody's CI could act on. The
  statement is still carried through verbatim and never generated by the import itself.

## [0.8.3] — 2026-09-08

Three additive fields on the activity record, each answering a different half of "what produced this
answer?" — the database identity a statement ran as, the warehouse's own id for it, and the AI model
a client says it is running. All three are nullable, written by the insert that already writes the
row, and read by nothing that makes a decision.

### Added

- **An execution record can name the warehouse's own id for the statement.** Some engines mint a
  per-execution id, and it is the key to their own logs — the plan the statement got, how long it
  queued, how much it scanned. Recording it lets somebody follow a row from here into a system this
  database does not own and cannot replicate.

  The column is `warehouse_query_id`, and this side treats it as **opaque**: nothing joins on it,
  parses it or fails without it. That is a limit on purpose rather than an unfinished thought — the
  moment the value means something here it becomes a thing that can disagree with the warehouse, and
  a pointer that argues with what it points at is worse than one that says nothing.

  **Which engines can answer is not decided in this library.** No database type or engine list
  appears in the recorder or the seam: an executor that can name its statement reports one through
  `ExecResult.warehouse_query_id` or `report_warehouse_query_id()`, and one that cannot never does.
  Adding an engine is therefore a change where the connection lives, with no migration and no change
  to the record — and there is a test asserting the field carries no engine name near it.

  Named for the concept rather than for whichever warehouse was first, because a vendor-named column
  becomes a schema change the day a second engine has one. Null where the engine has none, where the
  executor did not ask, or where the ask failed — all four nulls, the pre-migration row included,
  are claims rather than gaps.

  Migration `023_query_executions_warehouse_query_id.sql`, one nullable TEXT column, portable across
  SQLite and Postgres. Nothing reads it yet; nothing updates it.

- **An execution record can name the database identity that ran it.** `query_executions` said what
  ran, when, for whom, against which model and with what verdict, and never said who the warehouse
  authenticated. Where every statement runs as one shared account that is worth little; where a
  deployment runs each statement as the person who asked, it is a different identity per row and
  the record could not say so.

  The column is `executing_identity`, and it holds **what the connection says it is** — not the
  signed-in principal, not the subject of the token that opened it, not a namespace configured on
  the profile. Those are statements of intent, and a row built from intent would report the account
  we meant to run as even on the day a misconfiguration ran everything as somebody else, which is
  the day anybody reads this column.

  An executor reports through one of two seams: `ExecResult.executing_identity` for the ordinary
  path, or `report_executing_identity()` at connect time, which is what lets a statement that
  **fails after its connection was opened** still be attributed — the case the column is most worth
  having for. Both are optional; an executor that reports nothing is not doing anything wrong.

  **The built-in executor reports nothing, deliberately.** Obtaining an identity would cost a round
  trip on every query in every self-hosted deployment, to record the same shared account each time —
  a price paid by the people who get the least from it. Those rows stay null, and null here is a
  claim (no executor reported one) rather than a gap, alongside the two other honest nulls: a row
  written before the column existed, and a driver that could not answer. On the forked stdio surface
  the column is null too, by the same construction that already leaves `error_detail` null there —
  the child holds the connection and the parent writes the row.

  Migration `022_query_executions_executing_identity.sql`, one nullable TEXT column, portable across
  SQLite and Postgres. Nothing reads it yet; nothing updates it — the row stays append-only and the
  value is written by the insert that already writes it.

- **A tool call can record the AI model the client says it is running.** The activity log recorded
  who called, which tool, the statement and the outcome — everything the server observed — and never
  what was driving. Over MCP the model belongs to the client and the server never sees it, so an
  operator reading a run of unusually poor statements could not tell whether the client had moved to
  a different model that week. That is the first question somebody asks, and the log had no answer.

  `client_model` is an optional property on all four tools and a nullable column beside the other
  self-reported ones. It is **evidence of a claim and never a fact**: models self-identify
  unreliably, nothing checks the value, and nothing branches on it — it is never a basis for an
  access decision, a bound, a bill or an audit conclusion. A client-controlled value that could
  change what the server does would be far worse than an empty column, so a test asserts against the
  source that no reader of it exists outside the record, the writer, the read list and the render.

  There is deliberately **no list of known models** to check it against. A closed set would refuse a
  model that shipped after the set was written, which is a certainty rather than a risk; storing what
  was said, unvalidated and marked as unvalidated, is the only account that stays true. The admin
  activity view shows it marked `· self-reported`, beside the agent's own framing.

  Bounded by the writer at 200 characters — many times the longest honest model id — and with no
  truncation flag, on the same reasoning the refusal detail already carries: nobody re-runs a model
  id, so a flag would be a column false on every row ever written. Null is the ordinary case; a
  client that reports nothing is behaving correctly, and so is one built before this existed.

  Migration `024_tool_calls_client_model.sql`, one nullable TEXT column, portable across SQLite and
  Postgres.

## [0.8.2] — 2026-09-07

Two reliability fixes to the eval, both found by running it for real: the client couldn't be found
on a non-interactive shell's `PATH`, and the test suite could be talked out of substituting a
scripted generator for the real one — silently, the way it already happened once.

### Fixed

- **The eval finds the client where it installs, and says so early when it cannot.** A run whose
  `claude` client was not on the shell's `PATH` spawned it once per case, failed identically every
  time, and presented a wall of "the generator command could not be started" — which reads as a
  catastrophic model regression and is a `PATH`. It cost a 43-case run in the field.

  Two changes. The client is now **resolved** rather than assumed: `AGAMI_CLIENT` if set, then
  `PATH`, then the handful of directories it actually installs to. That is `scripts/sm`'s idea
  applied to the other executable this package spawns, and for the same reason — a user-local
  install directory is not on every non-interactive shell's `PATH`.

  And when it still cannot be found, **one probe before the loop** turns N failed spawns and up to
  2N warehouse queries into a two-second refusal that names the cause and what to do about it. Only
  an unstartable client stops a run: a generator that starts and declines the probe is one answer
  short, which the loop already reports per item. `--skip-preflight` refuses the probe.

- **The eval names its generator once, so the test suite cannot be talked out of substituting it.**
  `run_golden_eval` constructed its generator in two places, and every test replaced the class by
  the name those places used. Swapping the default meant editing the two construction sites — which
  left the substitutions pointing at a class the command no longer named. Nothing failed: the suite
  went on passing while spawning a real client, once per case, against the operator's own account.

  The generator is now chosen at a single module-level name, `GENERATOR`, that both construction
  sites and every test go through, so a changed default carries the substitutions with it. Two
  tests hold that shut — one parses the command's own source and fails on any generator built
  outside that name, the other pins what the name is currently set to, so moving it is a decision
  someone makes rather than a diff that passes.

## [0.8.1] — 2026-09-07

Everything below came out of running the golden-dataset feature against a live warehouse for the
first time. One fix is the difference between the feature working and not working at all; the rest
is what a first real run and its review turned up.


### Added

- **Saving a golden item checks it against the profile's own examples first.** The curated example
  library is where a team's conventions live — which of two equally correct date columns they answer
  with, how they phrase a join — and it is what agami reads when it answers a question for real.
  Nothing in the write path had ever looked at it, so an answer key could contradict the convention
  and then fail every future run while blaming the model.

  That is measured rather than imagined. On the first real dataset authored against a live
  warehouse, nine of fifteen items failed and six were one mistake: the keys filtered on one
  timestamp column where that profile's examples used another, 24 times out of 36.

  The save door now ranks the nearest example for the question, reads both statements, and stops
  with exit `1` and a `needs_confirmation_convention` payload when they disagree — before writing,
  because a warning that arrives after the key is on disk is a warning about a file somebody now has
  to decide whether to undo. `--confirm-convention` writes it anyway: a golden item may legitimately
  depart from convention, and that is sometimes exactly why one is written.

  Only three claims can stop a save — the tables read, the filters written, the resolved date window
  — because those are the ones that change which rows are counted. A save only stops when the CLI
  itself reports `high_confidence`, so a library that does not cover the question has no opinion to
  hold anyone to. And the check is total: no model, no CLI, a profile mid-rebuild, all return no
  opinion rather than blocking a correct answer from being written down, and the whole check is
  bounded by one wall-clock budget rather than a timeout per subject area.

  `agami-reconcile` is taught the second meaning of exit `1` in the same change, since a promotion
  that met it and answered with `--confirm-replace` would loop on a payload with no before and
  after to render.

- **`/agami-eval` says what a pass rate is not.** A run scores a generator with every tool switched
  off and one attempt, because that isolation is what keeps it from reading the answer key. The
  agent that answers the same question in production has four tools and can iterate. A dataset is
  also deliberately not a random sample. So a run measures regression, and quoting its pass rate as
  agami's accuracy is wrong in both directions.

### Changed

- **A reconcile run now teaches as well as tests.** Its only write was promoting agreeing rows to a
  golden dataset. Nothing went to the curated example library — the one path there was the referral
  to `agami-save-correction` when a number *disagreed*. So a run that proved twelve statements
  correct against the customer's own dashboard taught agami nothing from any of them, while an
  argument taught it something.

  Agreeing rows are now split between the two. They cannot go to both: an item that is also an
  example ranks first for its own question, the model reproduces it, and the test can only ever
  pass. They came from one dashboard, so they are the same family of question, which is exactly what
  makes holding some back meaningful.

  **The product does the splitting.** Which rows should teach depends on what the example library
  already covers, and asking a user that is asking them to guess at a library they have never
  opened. `sm examples --query` already answers it: its `high_confidence` flag is the product's own
  judgement of "we have a close example for this", so a row it does not cover teaches and a row it
  does becomes a test. Below four agreeing rows nothing is split, and never more than half the
  batch is sent to the examples so onboarding runs still write tests.

  **The user still makes one decision, and it is not the split** — one yes for the batch, with the
  breakdown shown. Saying what went where is mandatory rather than decorative: adding examples
  changes how agami answers future questions, and that must not happen silently behind a button
  that says "keep these numbers".

  A promoted row passes `--confirm-convention` to the save door. The statement being saved is the
  one agami generated *from* those examples minutes earlier, so the convention check would ask the
  person to confirm a departure they never made.

- **The golden run report shows the difference it already worked out.** Reading a failure meant
  diffing two SQL statements by eye, from a page that had computed the answer and kept it in a file
  beside itself. Six things changed, all in the renderer and its template, none of them touching the
  comparator, the claim reader or the contract:

  - **A gated item is no longer described as failing to reproduce the answer key.** The verdict was
    derived from `passed`, which a gate turns false — so an item whose rows matched the answer key
    exactly was announced as not having reproduced it, next to a perfect score. Reproducing and
    passing are different questions, which is why structure gates separately from rows, and the page
    now says both: *"Same rows as the answer key, but not the same question."* The verdict is also
    phrased as what happened to the data rather than in the dataset's own vocabulary — the pill
    beside the question already says passed or failed, so the line's one job is whether the rows
    matched.
  - **The accuracy is gone from the page.** Over a real fifteen-case run every value was exactly
    1.0 or exactly 0.0, which is structural: a row-count difference, an unpaired column and an extra
    column each short-circuit before an overlap is computed, and columns pair only on equal value
    vectors. On the narrow occasion a value in between is reachable, the comparator already writes
    the same fact in words. So the number was redundant when it meant something and misleading
    otherwise. The score itself is unchanged and still decides the verdict.
  - **A gate names the column it fired on**, instead of one fixed sentence saying that *a* filter
    was missing. It also stops describing a differing date window as a missing filter: structure
    gates twice, and the page assumed one.
  - **Every claim that differs is drawn, not just the table set.** The page kept `claims[0]` and
    dropped the other six. In a structural failure the table set reliably *agrees* — both statements
    read the same table, which is why the comparison got far enough to gate on something else — so
    the page reliably showed the claim that said nothing while `filter_predicates`, which explains
    the failure, was written to the JSON artifact and never rendered.
  - **The run timestamp reads as a date** rather than `2026-09-06T11:47:38+00:00`.
  - **The summary shows three tiles, not five**, surfacing "not compared" and "couldn't run" only
    when they are non-zero and giving them a sentence when they appear. They are deliberately not
    folded into "failed": a run of nothing but errors would then read as green, which is exactly
    what the `1` / `2` exit codes exist to tell apart.

- **A claim reads the way a person writes it, in a table rather than a run of text.** A
  difference was one separator-joined line per claim, holding the claim reader's own field names
  (`filter_predicates`) and its functional rendering (`gte(created, '2024-01-01')`). Both are
  deliberate where they are written — a claim rides on tool output a model reads as
  server-authored, and something that looked like SQL would be read as SQL — but that argument is
  about a model's context and does not reach a local page a person opens.

  So the report now prints `created >= '2024-01-01'`, labels each claim in English ("Filters",
  "Grouped by", "Date range"), and lays the two sides out as a table so they line up under one
  another. Three shapes that rendered as nothing or as noise are handled: a join key
  (`customers.id = orders.customer_id`), an ordering with its direction, and a subquery, which
  arrives as its whole parse tree and now reads `customer_id IN (subquery)`. `GROUP BY 1` is
  labelled "column 1 of the select list" rather than shown as a bare digit — under `group_keys`
  only, since a `limit` of 100 is a row count.

- **A golden run report can be filtered by outcome.** A corpus-sized run is hundreds of cases and
  was a flat scroll. Display only — nothing is recounted or re-sorted, so the tiles and the section
  headings keep describing the whole run.

### Fixed

- **`/agami-eval` can generate again for an operator who signed in rather than exporting an API
  key.** The generator child is given an allowlisted environment, and `USER` was not on the list. On
  macOS the client resolves its stored credential through it, so the child started, reported that it
  was not logged in, and exited — every case in the run coming back "the generator exited without
  answering" and the run exiting `2`.

  Anyone with `ANTHROPIC_API_KEY` set was unaffected, which is why it shipped: that variable *is* on
  the list, and a machine with one set never reaches the failure. The tests could not catch it
  either, because they inject their own generator and never spawn the real client.

  `USER` names the account and not a path, so it opens nothing the no-tools invocation had already
  closed. The allowlist is still an allowlist, and a new test holds it to that.

## [0.8.0] — 2026-09-05

One change: the server decides which tool calls belong to one conversation, instead of asking the
model for an id it cannot supply reliably. `tool_calls` gains a column and the Activity view groups
by it; nothing existing is removed and no argument, return shape or refusal rule moves.

### Added

- **The server decides which tool calls are one conversation, instead of asking the model.**
  `tool_calls` gains `conversation_id`, worked out when the call is recorded from the authenticated
  actor, their organization and the clock: calls by one person continue a conversation until a pause
  longer than `CONVERSATION_IDLE_MINUTES` (30), and the next call after that pause starts a new one.
  Nothing a caller sends can influence it.

  `thread_id` was meant to be this and is minted by the model, which cannot be relied on. Measured on
  one deployment: asked in prose it arrived on 2 of 10 calls and then 0 of 8; made a required
  property (0.7.1) it arrived on 9 of 9 **and collided** — a model forced to invent an id it does not
  otherwise track emits the shortest string the schema accepts, so two conversations two days apart
  both arrived as `t1` and were shown as one, with their turns merged. Handing it a server-minted id
  to echo back was ignored as well; nothing carried in prose is honoured, wherever the prose sits.
  The transport cannot supply one either — MCP removed `MCP-Session-Id` in the 2026-07-28 revision,
  and this server already runs stateless for the reason the spec dropped it.

  The rule is a judgement, not a fact: two conversations a minute apart are one row. What it
  guarantees is that the answer is the SAME every time, computed rather than remembered, and that the
  mistake it can make is bounded to one person's own back-to-back conversations — it can never merge
  two people and never join two days.

  `list_sessions` groups by it, falling back to the old `(actor, thread_id)` pairing for rows written
  before the column existed — so the Activity view stops blending two conversations that reported the
  same self-reported id, without turning history into singletons.

  `thread_id` is unchanged and still recorded; this is an addition. Deriving the value costs one
  indexed lookup per recorded call, on the connection the recorder already holds.

## [0.7.1] — 2026-09-04

One opt-in change, off by default: a deployment can require the `thread_id` that links a
conversation's tool calls together. Nothing about the served tool surface moves unless the flag is
set.

### Added

- **A deployment can require `thread_id`, so conversations stop fragmenting in the activity log.**
  The id that groups a conversation's calls is self-reported — the activity-log directive asks for it
  on every call and ends "Best-effort; omit if unknown" — and models take the omission. Measured on
  one deployment across two consecutive conversations: 2 of 10 calls carried one, then 0 of 8, not
  one including the first. `list_sessions` degrades gracefully (each orphan becomes its own
  singleton, so the Activity view stays audit-complete), but the conversation those calls belonged to
  is gone. Setting `AGAMI_REQUIRE_THREAD_ID` promotes the property from optional to **required** on
  every tool declaring it, which the MCP SDK enforces before dispatch; the same client then sent it
  on 9 of 9 calls. Deriving it server-side is not available — the OAuth `sid` identifies one
  authorization and survives token rotation, so it would file every conversation under one thread,
  and the streamable-HTTP transport is stateless, so there is no `mcp-session-id`. **Default off**,
  because the change cannot fail softly: a call omitting a required property returns an input
  validation error rather than a degraded record, so it should be turned on somewhere observed before
  being rolled forward. Existing deployments are unaffected until they opt in.

## [0.7.0] — 2026-09-02

The release is one feature: **golden datasets** — a way to write down questions whose answers you
have already agreed on, and then find out, on demand or in CI, whether the model still answers them.
Everything below is additive. No argument, return shape, refusal rule, receipt key or CLI flag from
0.6.x moves.

### Added

- **A golden dataset: the questions you have agreed on, and the answer key for each.** A golden dataset
  is a file of questions where an author has written the question, the SQL they accept as the answer,
  and how strictly a run has to match it. Datasets live under the profile and are read by
  `semantic_model.golden`, whose whole job is to hand a runner records it can trust — and to say out
  loud which files and cases it dropped getting there.

  Two properties hold and are the reason it can be trusted. **One bad file does not cost you the
  run:** a malformed file, or one malformed case inside a good file, becomes a finding and the read
  continues. **Nothing returned carries a filesystem path** — not on a record, not in a finding — so
  a runner downstream cannot forward a dataset location into a subprocess.

  `plugins/agami/shared/golden-dataset-shape.md` is the authoring reference, and a test parses its
  own example through the real reader, so the document cannot drift from the parser that has to
  accept it. Its hard rule against reading a sibling profile to learn the shape binds harder here
  than anywhere else: a golden dataset holds the business definitions *and* the answer key in one
  file, so a glob across profiles reads another tenant's questions together with the SQL that
  answers them.

- **Scoring is deterministic, not a model's opinion (`semantic_model.comparator`).** Given the answer
  key's result set and the one the generated SQL produced, `compare_result_sets` returns a score. It
  executes nothing, writes nothing, opens no connection, and cannot make a model call. That is what
  makes a run reproducible rather than a second opinion.

  Three decisions in it are worth knowing, because they decide what counts as the same answer.
  **Columns match on their values, never on name or position** — a correct statement that renames and
  reorders its projection is still correct, and that is precisely the case a name-matched comparator
  fails. **Cells canonicalize to a `(type_tag, value)` key**, which is not stylistic: Python collapses
  `True`, `1`, `1.0` and `Decimal(1)` into one bucket, so without the tag a boolean column would
  compare equal to an integer column of zeroes and ones. **Ordering is read from the golden statement
  alone**, parsed rather than pattern-matched, so a generated statement that drops the `ORDER BY`
  still scores order-sensitively — and an `ORDER BY` inside a subquery, a CTE, an `OVER (…)` or an
  aggregate does not make a result ordered.

- **`/agami-eval` — run a dataset and read the verdicts, failures first.** The developer surface for
  all of the above: no admin, no server, no browser. `plugins/agami/scripts/run_golden_eval.py`
  takes `--profile`, `--dataset`, `--tag`, `--rerun-failures`, `--top-k` and `--timeout-s`, and
  `--list` answers "what is there" with no database, no generator and no credentials.

  **The generating context never holds the answer key.** A model grading itself against a key it can
  see reports a pass rate that means nothing, and no assertion over the scores would reveal it. The
  generation runs in a sandboxed child process, and this is the one path in the product that does not
  generate SQL in the caller's own context — the isolation is the point. The child's flags **are** the
  sandbox and are asserted present by a test, because in that client the absence of a flag is the
  permissive default.

  **Both statements execute through the guarded chokepoint**, with an injected executor rather than a
  raw connection — so a golden run is bound by the same read-only guard, row cap and statement
  timeout as any other query, and works on every engine rather than one.

- **An exit code CI can gate on.** `0` every confirmed case passed, `1` a confirmed case failed, `2`
  no verdict could be produced. `2` means *go look at the harness*, never *the model regressed*: it
  covers a preflight refusal, a run that stopped partway, a run where every generation errored, and a
  selector that matched nothing. A run that is both incomplete *and* carries failures reports `2`,
  because sending someone to debug a model change against a run that never happened is the expensive
  mistake.

  The verdict rests on **confirmed** cases only. An unconfirmed item's answer key is one nobody has
  reviewed, and gating CI on it would fail a build over a case no human has agreed to.

- **`/agami-save-golden` — two doors into a dataset, kept apart on purpose.** A dataset that has to be
  hand-written stays empty, and an empty dataset gates nothing. The **import** door turns a CSV
  question bank into items written unverified — volume arrives here and it gates nothing. The **save**
  door writes one answer a person just accepted, with the statement that produced it and the result
  as its receipt, marked verified.

  They are separate because a spreadsheet holds questions, not the statement that answers them. An
  import that marked its own rows verified would forge the gate: the suite would fail on its own
  errors and teach people to ignore it.

  Every write is **append-only** — a write that would change an existing item renders the before and
  the after and stops for an explicit yes — and every write funnels through one function, so the rule
  holds for both doors. A write proves itself by re-reading the file through the runner's own reader
  and rolling back if that read refuses.

- **A report for a run, and a page for a dataset.** When a golden question stops matching, the useful
  artifact is the confirmed statement and the generated one read against each other, which does not
  fit in a chat message. A run now writes a self-contained HTML report with both statements side by
  side — no rows, no network — beside its machine-readable artifact, sharing one filename stamp so the
  pair is findable by name.

  The **golden dataset explorer** answers the other question: not what broke, but what the dataset
  *is* — how many items exist, how many can actually gate a run, which have rotted. Four tabs (Items ·
  Coverage · Lint · Queued) with live search, and edits queued in the page and handed back as one
  block a deterministic parser applies. **The Coverage tab is the reason the page exists:** forty
  questions that never touch the revenue metric read as coverage they do not have, and the model
  change that breaks revenue passes clean. Coverage is computed from what the statements actually
  read, so that gap is a fact rather than a judgement.

### Changed

- **A reconcile run keeps the answers it already proved.** A run that matched nine of twelve numbers
  has produced nine verified answers — a person read each one and accepted it — and then threw them
  away: the statement that produced each number lived only inside the report HTML, and the row record
  never held it. The row record now keeps the statement and the result (additive; every existing key
  stays), and after the run's summary the rows that agreed are offered once for promotion into a
  golden dataset. It writes through the save door above and nothing else. Declining writes nothing.

  Only rows that actually matched at the run's own tolerance are eligible, and a promoted item is
  written as a **band around the observed value** rather than an exact match, so it passes its own
  next run by arithmetic rather than by hope. A promotion edits the question and never re-anchors the
  statement to `CURRENT_DATE`: the band was drawn around today's value of a window that slides, so
  anchoring it would drift the item out of its own band and fail later as a false alarm.

## [0.6.9] — 2026-08-27

### Added

- **An existing password account can move onto an identity provider, keeping everything attached to
  it.** A deployment running on usernames and passwords could not switch to single sign-on:
  `_resolve_oidc_user` refuses an account not already bound to the provider, so on the day it was
  turned on every person already using the product would be locked out, with their conversations
  orphaned behind an account nobody could reach. Verified against a real account rather than
  reasoned about — it came back `None`.

  `migrate_password_user_to_oidc` keeps the row, so the id, the address and everything keyed on it
  survive. **The password is cleared**, because left in place the directory would not actually be in
  charge: revoking somebody there would not stop them signing in with a password they chose months
  ago, and nothing would show it. Guarded so it can never take an account already bound to a
  different provider — the identity-confusion guard has to survive the migration that relaxes the
  password case.

  Deciding *whether* to adopt is deliberately not this function's job. It is safe only where the
  caller has already verified the token against a pinned tenant and checked the address against that
  company's own domains; core has neither of those facts, and its consumer does.

- **`set_user_names`** fills in a display name from whatever the provider now says, so a name
  corrected in a directory reaches the product without anybody retyping it. A blank never erases
  what is there: a provider that omits a name is silent rather than authoritative, and treating
  silence as an instruction to delete would wipe a name an administrator typed in.

### Changed

- **`http://localhost` is accepted for `PUBLIC_BASE_URL`.** TLS is still required everywhere else,
  for the reason it always was — a browser drops a `Secure` cookie over http. Browsers make a
  specific exception for loopback, which is a secure context by their own definition, so nothing
  that rule protects is lost. It is also the only thing that works: identity providers permit a
  plain-http redirect only on loopback, so a developer signing in against a real provider from their
  own machine had no https option to choose and could not run the server at all. The host is parsed
  rather than prefix-matched, because `http://localhost.example.com` is somebody else's domain.

- The sign-in consent line now reads **"Allow `<client>` to connect to your data"** rather than "to
  access your data". What is granted is a connection the person can see and remove, not a handover
  of the data itself, and on a screen read in two seconds before deciding that is the whole
  decision.

## [0.6.8] — 2026-08-27

### Fixed

- **A database connection the server closed is now reopened, instead of poisoning the process until
  it is replaced.** `Store` opened one Postgres connection at startup and held it for the life of the
  process. A deployment that sits idle for about an hour has that connection reaped from the server
  side, and nothing told the process: `execute` called `conn.cursor()` with no liveness check and
  there was no reconnect path anywhere in the package.

  The instance was then poisoned for as long as it lived — **every** request, not only the ones that
  touch data, answered in about three milliseconds having attempted no round trip at all. The three
  milliseconds are the tell. A downstream deployment hit this four times in four days, one to two
  hours each, across two revisions; thirty days of that service's error logs contained 123 tracebacks
  and every one was this.

  It fails closed on all traffic because the first thing a request does is resolve the caller's org,
  and that read goes down the dead connection — before authentication, before routing. Somebody who
  has just signed in successfully sees a connector error, which reads as a login failure and gets
  reported as one.

  `Store` now keeps the URL it connected with and retries once when the connection is gone. Three
  things bound that retry, and each of them was a real defect in an earlier draft rather than
  caution for its own sake:

  - **It refuses while an uncommitted write is outstanding.** Callers write two rows that must land
    together — a role change and the audit line naming who granted it. If the connection dies between
    them the first is already lost, and reconnecting silently would let the second commit **alone**:
    an audit line for a grant that never happened, which is exactly what that line exists to make
    impossible.
  - **A read does not count as an outstanding write**, and that distinction is the difference between
    fixing this and appearing to. The path this exists for reads five tables in a row and never
    commits, because there is nothing to commit. Counting reads would have reconnected once and then
    refused for the life of the process.
  - **`OperationalError` alone is not enough to act on.** It is a broad base class — a cancelled
    query, a statement timeout, a full disk. Retrying on any of them re-runs the statement, and for
    the first write of a transaction that lands the row twice. Only the driver marking the connection
    closed counts. `InterfaceError` needs no such test, since the driver raises it only on a
    connection it has already closed.

  It also refuses while a session-scoped advisory lock is held, so a reconnect part-way through a
  migration cannot continue without the lock and let a second instance apply the same files.

  `Store.rollback()` is new, and callers that reached past it into `store.conn.rollback()` now go
  through it — a rollback that bypassed it would leave the connection marked mid-transaction forever
  and unable to reconnect again.

## [0.6.7] — 2026-08-18

### Added

- **A curated example keeps the same id across deploys, and the caller can see it.** `prompt_example`
  has had an `id` column in its primary key since the table existed, and it was unusable three times
  over: an example with no `id` in its YAML got a minted uuid4, nothing wrote that back, so the next
  deploy minted a different one for the same example — and the read selected `question, doc`, never
  the `id`, so no identity reached a caller even where one existed. Across a real deployed model of
  16 subject areas and 46 curated examples, not one carried an id.

  So nothing could name an example, count one, or say whether the one returned yesterday is the one
  returned today. `agami-save-correction` cannot tell a correction that created an example from one
  that should have replaced it; ranking returns a subset and which subset is unrecordable; the library
  only grows, because no two observations of an example can be tied together.

  The id is now **derived from the example's own content** — `question` and `sql`, each NUL-terminated,
  SHA-256, twelve hex characters, the construction `compute_model_hash` already uses. Derived rather
  than minted, because a minted id changes every deploy; derived rather than authored, because
  authoring gives the id two homes that can disagree and needs a pass over every deployment. An
  authored `id` in the YAML still wins, which is the escape hatch for an example deliberately filed
  under two subject areas — those otherwise resolve to one id, and the first now wins rather than the
  second aborting the deploy.

  `select_examples` returns it, and `example_by_id` reads one back, scoped by org and datasource like
  every other read in the module. The derivation is one-way, so a consumer holding an id needs the
  lookup for the id to be worth having.

- **An agent can say what it based a query on, and why.** The activity log records what ran and two
  self-reported columns carrying the caller's framing of the question. What it never recorded is the
  reasoning in between: why that table, when the model declares three that look similar; why that
  join, when it declares no relationship between those two tables; which curated example this
  followed, or whether there was none. An operator could see that a query was wrong and not where the
  reasoning went wrong — and a query against the wrong table is indistinguishable, in the log, from
  one against the right table.

  `execute_sql` takes an optional `basis`: a list of `{kind, ref, why}`, recorded on `tool_calls` and
  rendered in the activity log beneath the statement it explains. Absent is the ordinary case, the
  field is not `required`, and a client that has never heard of it is unaffected.

  **The property is declared as a bare array, and that is a correctness constraint rather than a
  simplification.** The MCP SDK validates arguments against `inputSchema` before the handler runs, so
  a `maxLength`, `maxItems` or `enum` there does not trim a bad entry — it refuses the whole call and
  takes the caller's answer with it. The kinds are advertised in the description and enforced at the
  boundary, where an over-long `ref` or an unknown `kind` costs its own entry and nothing else. The
  stored value says when it was cut, so a reader never takes a truncated list for the whole claim.

  Self-reported, like its neighbours: core records the claim and adjudicates nothing. Whoever holds
  the receipt can check `ref` against the SQL themselves.

## [0.6.6] — 2026-08-14

### Added

- **The activity log can name the execution it ran.** `tool_calls` records that a tool ran and
  `query_executions` records what the warehouse was asked and what the guard decided; both are
  written for every `execute_sql`, they are 1:1, and nothing joined them. `018`'s own comment
  recorded the gap — *"there is no key between them (`Envelope.audit_id` is `query_executions.id`,
  which `tool_calls` does not carry)"* — and chose to duplicate a column rather than add one.

  That holds for a single sentence-shaped field. It stops holding once anything has to be attached to
  a **statement**: `correlation_id` names the turn, one turn runs several statements, and no column
  told them apart. A reader wanting per-statement facts had to guess, and the only material to guess
  with was the agent's own framing text — which repeats whenever a turn retries a sub-question, so it
  mis-attributes in silence.

  `tool_calls` now carries `audit_id`, and the value is one the caller already received: `_emit`
  stamps it onto every envelope, and `query_executions.id` is that same id. The column reconciles
  nothing and mints nothing.

  It is read off the body `record_tool_call` already parses, on `ok`, `refused` and `failed` alike —
  a blocked or broken call is the one an auditor most wants to trace. A caller that hands over no
  body can state it instead, like the outcome fields beside it. NULL is the ordinary case, not a gap:
  a schema read or a datasource list runs no statement and has none.

### Fixed

- **A client asking what this server looks like gets the icon, not a 401.** `/favicon.ico` was not
  merely absent, it was gated: the bearer middleware challenges anything off its public list before
  routing can 404 it, so the one unauthenticated request a client makes to learn what a server looks
  like came back as an auth prompt. An MCP client showing a server in a connector list has no
  credentials to offer for an icon fetch, and a deployment that redirects `/` elsewhere leaves this
  the only path left to ask on — so such a server drew a letter avatar beside servers that answered.

## [0.6.5] — 2026-08-13

### Fixed

- **A model whose subject area joins one pair of tables twice now loads.** It did not before: each
  relationship row was keyed by its two table names, and that key is the table's primary key — so the
  second join collided with the first and the INSERT raised part-way through the write. The failure
  was total rather than partial. Nothing commits, so the deployment ends up serving **no semantic
  model at all**, and the error names a column tuple rather than the join that collided.

  Self-referencing tables make this ordinary rather than exotic: an employee row pointing at another
  as its manager, and again as its mentor, is one pair of tables and two genuinely different joins.
  Two `on:`-expression joins between one pair, and same-named tables living in two schemas, failed
  the same way.

  The key now carries the relationship's position within its subject area, so a collision is
  impossible for any model that validates rather than impossible for the shapes somebody enumerated
  — which is what the two previous versions of this key each attempted, and each missed. Nothing
  reads the value (a relationship is rebuilt from its stored document), and a redeploy replaces a
  datasource's rows wholesale, so models written under the old key need no migration and no backfill.

## [0.6.4] — 2026-08-12

### Added

- **The claim page says whose account it is.** Somebody following a setup or reset link was asked to
  choose a password with no indication of which account it belonged to. A link is shared out of band,
  so the person holding it may have been sent the wrong one, or two of them — and now that a link can
  REPLACE a working password rather than only set a first one, following the wrong one silently locks
  somebody out of their own account with nothing on the page to warn them. It discloses nothing new:
  whoever holds the link already holds a token naming that account.
- **The MCP surface states what the server already does.** Four capabilities shipped working and
  undocumented, and a silent feature is worse than a broken one — a broken one gets reported.
  `execute_sql` returns three statuses and described two; the third carries the whole database-error
  channel with a `kind` from a declared enum, which tells an agent whether a failure is repairable
  from the schema it already holds or whether retrying is pointless. Documented, so it can be used.

### Fixed

- A password refused for being **too long** said "use at least 8 characters" — the same message as
  one refused for being too short, and advice that makes the problem worse the more carefully it is
  followed. Each bound now names itself.
- Seven store-step tests resolved the profile from the developer's own `~/agami-artifacts` rather
  than from the temporary directory they had set, so they compared against a value from a file the
  test never mentions. CI has no such file, so they passed there and failed only on a machine that
  had run the CLI.

## [0.6.3] — 2026-08-11

### Added

- **An administrator can give a colleague a new password.** The setup link works exactly once —
  claiming an account flips it out of `pending`, and every later use is refused — so an administrator
  could create somebody's account and then never help them back into it, and with no mail path there
  is no self-service reset either. `/claim` now carries a second purpose: `setup` still means a
  pending account choosing its first password, and `reset` means a claimed account being given a new
  one because an administrator asked. The two are matched against the state of the account rather
  than trusted from the link, so neither can do the other's job.
- A reset link is **single-use**, by a mechanism the setup link does not need: a reset leaves an
  account exactly as claimed as it was, so nothing about it would change and the link would work
  forever. The link is bound to the credential it was minted against, and that credential is a
  condition of the write — so the first successful reset retires it, a password change by any other
  route retires it, and two simultaneous uses cannot both succeed.
- A reset **ends the sessions running on the old password** by revoking that person's refresh
  tokens. Stated precisely because a security control believed to cover more than it does is worse
  than a narrow one: a session already holding an access token lasts until that token expires, and
  the `/admin` console cookie and any outstanding authorization code are untouched.
- A reset can never give a password to an identity that signs in through a provider, and can never
  revive a switched-off account.

### Changed

- The claim page is worded for what the link is for — somebody with an account is not told to
  "finish setting up" — and `setup_page_html` / `setup_done_html` are now `claim_page_html` /
  `claim_done_html`, which take the purpose.
- Password hashing on `/claim` runs off the event loop. It is deliberately slow and memory-hard, and
  this is a public endpoint, so hashing inline stalled every other request in flight.
- `get_datasource_schema` answers **within the scope the caller declares**, and the never-hide
  guarantee is now stated relative to that scope: within it, nothing is hidden — the scope's own
  metrics plus the cross-area bucket. A new `area` parameter narrows to one subject area
  (completing the set `execute_sql` and `get_prompt_examples` already had); `dataset_names`
  narrows to those tables. `query` and `metric_names` rank and select detail and do **not** scope,
  because narrowing on something the caller never declared is silent deprivation. The response
  echoes the `scope` it resolved.
- The appended domain context no longer repeats the subject-area listing that the same response
  already carries as structured `subject_areas`.

### Fixed

- A table-scoped `get_datasource_schema` call returned columns and **no way to join them**. It had
  resolved the relationships and metrics for those tables and discarded them, while the tool's own
  description promised both. It now returns them, with each metric carrying the binding for the
  deployment's engine — so one call carries the columns, the joins and the declared expression.
- **The instructions named a metric field the payload does not send.** Both instruction surfaces
  told the agent to reuse a metric's `calculation`/`bindings` VERBATIM; the payload key is
  `binding`, singular, already resolved to this deployment's engine. An agent told in capitals to
  reuse a field it never receives hand-rolls the SQL instead — and hand-rolled SQL does not reduce
  to the declared binding, so the receipt reads `unmatched` on a column that does compute the
  metric. The text and the payload are now pinned to each other by a test derived from the
  projection itself.

## [0.6.2] — 2026-08-09

### Fixed

- **An omitted `datasource` names the org's real datasources instead of one nobody has.** #216 (also in
  this release) removed the invented `'default'` for a deployment serving exactly ONE datasource;
  serving several, resolution still fell through to that literal and the caller heard
  `no such datasource: default` — so the one-datasource case and the several-datasource case are fixed
  together in this release, and nothing in 0.6.1 or earlier ever consulted the store to resolve a
  profile at all.

  Two audiences read that sentence and both were misled: an administrator sees a name no customer has
  and concludes their data has gone missing, and a model sees a failed lookup and invents another name,
  because the tool's own description told it a default existed. So the description is fixed as well as
  the refusal — all three tools now say what is true on each install and point at `list_datasources`.
  An omitted argument gets the catalogue back (`datasource_required`); a NAMED one that does not exist
  is still told so; an empty string counts as omitted, matching `resolve_profile`'s own
  `if explicit:`. (#218)

- **A CASE predicate is not what a SUM aggregates, and a shared expression names no metric.** (#215)

### Changed

- **The tool surface reports what it checked, and accepts what it advertises** — nine findings from a
  connector audit, all one shape: the server stated something it had not established, or named a field
  the agent could not reach. `model_present` dropped from the served listing (it was the literal `True`);
  `resolve_profile` consults the store; a hosted server no longer claims "All execution is local";
  `bindings` is actually sent; `area` reaches `get_prompt_examples`; `list_datasources` returns
  `description`; `for_questions_about` dropped from the payload. (#216)

  **Released as a patch deliberately, and it is the one judgement call in this version.** #216 REMOVES
  fields from emitted tool payloads, which a consumer reading them would notice — normally a minor bump
  under 0.x. It ships as 0.6.2 because every field removed was provably dead: `model_present` could only
  ever be `True` on that path, and `for_questions_about` had no writer anywhere in the product. A
  consumer reading either was reading a constant or an empty list. The field stays DECLARED on the model
  format, so no model on disk fails to load.

## [0.6.1] — 2026-08-08

### Fixed

- **A refusal's own sentence reaches the audit row again, so a console reader can act on it.**
  `tool_calls.refusal_detail` and `tool_calls.refusal_remediation` — the two columns added in 0.6.0 so
  that a row says what the decision was made against and what you were told — were **NULL on every
  refusal a real deployment recorded**. The rule was recorded; neither sentence was.

  The cause was the coherence rule that shipped beside them. It cleared both whenever a caller
  *stated* the outcome rather than leaving it to be parsed out of the response body, reasoning that
  such a caller is not offering these sentences. That is true of a caller who **contradicts** the
  body, and false of every real one: the served MCP transport states the outcome for every tool that
  speaks the Envelope, which `execute_sql` does, and a consumer's own sink states what it observed
  with no body to hand over. So the sentences survived on exactly one path — a caller that hands over
  a body and states nothing — and nothing in production takes it.

  They are now kept when the stated outcome **agrees** with the body, and dropped when it does not.
  What the rule guarded is unchanged: a stated success has no refusal to explain, and a stated kind
  naming a different failure is describing something these sentences are not about. No consumer
  change is needed — both surfaces already state the rule itself.

  The tests are worth naming, because they were the real defect. Every test that asserted these
  sentences were **present** drove the recorder the way no production caller does — handing over a
  body and stating nothing — so they went on passing while every path the world actually takes
  recorded nothing. A test can be demonstrably wired to the line it covers and still say nothing
  about whether that line is reached the way the world reaches it. The new test that states an
  outcome agreeing with the body is the one that fails against the shipped behaviour.

## [0.6.0] — 2026-08-08

### Added

- **A server can now run with the semantic-model pass off, and always says so
  (`AGAMI_GOVERNANCE_ENFORCED`, ACE-101).** The model-scoping checks refuse on facts about *our*
  parser and *our* model resolution rather than about your SQL, so a dialect drift or a model that
  will not resolve could refuse every query on a server until an operator intervened, and there was
  no way to bring that server up without them. There is now one switch, read per request so flipping
  it takes effect on the next query with no redeploy.

  **It is off by default,** which means a fresh server does not enforce table scope, column scope,
  the `SELECT *` ban, or the engine-mismatch check until you turn it on. With it off, a query may
  read anything the connecting role is granted, including columns you excluded from the model, and
  may enumerate your schema through catalog relations. Turn it on once the checks have been
  validated against your data; see the [self-hosting reference](docs/self-hosting.md) and
  [SECURITY.md](SECURITY.md).

  **What the switch cannot reach:** the read-only guard, the dangerous-function guard, the statement
  timeout and the row cap. Those are composed outside the pass and enforce in both postures, as does
  the read-only database role. And nothing ever claims the checks ran: every answer and every audit
  row carries "the semantic-model checks are turned off in this deployment", on all five receipt
  sections, with no findings attached.

  The server logs one warning at startup naming the variable and the exposure whenever it boots with
  the pass off.

- **The receipt now tells you which of a table's declared filters your statement actually applied
  (ACE-099).** Declaring `default_filters` on a table has never applied them to your SQL, and since
  the filter injector was removed nothing reported on them either — so `SELECT COUNT(*) FROM orders`
  returned every row where it once returned the undeleted ones, and nothing on the answer said the
  number meant something different from what the model says the table means.

  Each entry in the receipt's `tables` section now carries `filters`, one `{expr, status}` per
  declared filter, with `status` one of `applied`, `omitted` or `undetermined`, plus a `scope`
  naming where in the statement that reference sits.

  The determination is **per reference, not per table**, which is the part that makes it worth
  trusting. A filter satisfied inside a CTE and absent from the outer query is two different answers
  about the same table, and reporting one verdict for both is what made the old injection unsafe:

  ```
  WITH recent AS (SELECT id FROM orders o WHERE o.status != 'cancelled')
  SELECT o.id FROM orders o JOIN recent r ON o.id = r.id
  ```
  ```
  orders  scope=cte:recent  filters=[{expr: "o.status != 'cancelled'", status: applied}]
  orders  scope=main        filters=[{expr: "o.status != 'cancelled'", status: omitted}]
  ```

  `applied` means the declared predicate is one of the top-level `AND` conjuncts of that reference's
  own scope; extra conditions beside it do not weaken that. Anything the check cannot stand behind
  is `undetermined` rather than a verdict — a predicate on the same column that is not identical, one
  reachable only through an `OR`, one that only appears in an outer join's `ON`. Only an outright
  absence is `omitted`, because a confident "you left this out" that turns out to be wrong is worse
  than saying nothing. **An omitted filter is never a refusal**: whether it matters depends on the
  question, which only you have.

  The shipped sample declares one (`orders`: `status != 'cancelled'`), so this is visible the first
  time you run it.

### Changed

- **The receipt now reports on every number your query computes, not just the ones with a problem
  (ACE-060).** The `aggregates` section listed findings, so a total no join had multiplied produced
  no entry at all — and an entry that is absent looks exactly like a check that never ran. It also
  named the measure *table*, so a query computing two numbers over one table told you a join
  multiplied "orders" and left you to work out which of your two numbers it meant.

  There is now one entry per aggregate, saying the aggregate as parsed, whether a join multiplies
  the rows behind it, and which join does. A number nothing multiplied **says so**, which is the
  point: reading "not multiplied" beside a total is the difference between a clean answer and an
  unchecked one.

  An aggregate whose reads could not be resolved reports `undetermined` rather than clean.
  `COUNT(*)` is the case that matters: it names no column, so nothing tells us which table's rows it
  counts, and a fan-out around it is invisible to the check. Reporting that as clean would put a
  clean bill of health on the one number a join had multiplied.

  The section's `undetermined` line is now composed per query from what *that* query left open, so
  it can finally be empty. It used to carry a sentence on every answer, including "whether this is a
  problem depends on the question" — true of every answer forever, and the reason the section could
  never say "checked, and complete". A trap is still reported and still never refused.

- **The audit row now says what the decision was made against, and what you were told (ACE-098).**
  A row recorded the verdict — `ok` / `refused` / `failed`, and for a refusal which rule under which
  reason — but nothing about the basis for it, so nobody could take a row and check the decision
  again. Three columns close that: `detail` (the refusal's own sentence, which is where "which bound
  fired and what it was set to" lives, since the statement timeout and the row bound share one rule),
  `receipt` (everything the trust receipt reported, including its `undetermined` markers), and
  `model_version` as a column you can filter on.

  A test now takes those rows and **re-derives each refusal with no database connection at all**,
  matching what was recorded. That is the check that tells whether the fields are sufficient rather
  than merely present. The two runtime bounds are exempt and stay exempt: whether a statement
  outruns its budget is a property of the run, not of the SQL, so it is not reproducible offline by
  anyone.

  Also, the **tool-call log now reads the verdict rather than re-reading the answer**. It used to
  parse the response body to work out whether a call failed and why, which made the audit trail
  depend on the wire format; it now takes the classified outcome directly.

- **A self-hosted server that cannot record a query no longer runs it (ACE-097).** Recording was
  best-effort in three places, and two of them were silent, so a deployment could execute SQL
  against your database and keep no record of having done so with nothing anywhere saying the
  record was lost.

  On a **server** (one with `AGAMI_DB_URL` or `APP_DATABASE_URL` configured), the audit store is now
  checked before the statement runs. If it cannot be opened the call is refused, with
  `rule: audit_unavailable` and a remediation naming the operator action, and the statement never
  reaches your database. If the store was reachable at that check and the write fails afterwards,
  the call fails rather than returning an answer whose statement left no trace. The connection is
  read-only, so nothing was changed and re-running costs only the round trip.

  **Local single-player use is unchanged.** With no database configured there is no audit store to
  reach: the log is a local jsonl file, a write failure is still logged and never breaks your query,
  and a read-only artifacts directory cannot stop you asking questions.

  For operators this is an availability change, and a deliberate one: a briefly unreachable audit
  database now produces refusals rather than unrecorded answers.

- **A result too large to return is refused, not trimmed.** A query whose result exceeded the
  deployment ceiling (`AGAMI_SQL_MAX_ROWS`, default 1000, unchanged) used to come back cut down to
  that many rows with a flag saying so. It now comes back as a structured refusal carrying no rows.

  The trim was unsound before it was anything else. Without an `ORDER BY` a SQL result has no
  defined prefix, so what you got was whichever rows the engine happened to emit first — different
  between runs, different between engines, and presented as the answer. It was not a smaller version
  of your result; it was an arbitrary sample of it.

  The refusal tells you which fix applies to the statement you sent, because the wrong one is worse
  than none: a row listing should be bounded with a `LIMIT` and an `ORDER BY`, while an aggregate
  should have its grouping narrowed or a filter added — putting a `LIMIT` on a grouped result drops
  groups, and the breakdown you get back reads exactly like a complete one.

- **A `#` is no longer mistaken for the SQL it hides, and is now refused wherever it appears
  outside a string (ACE-096).** `SELECT a FROM t # DROP TABLE t` came back as *"keyword 'DROP' is
  not allowed"*, and `... # note; more` as *"multiple statements are not allowed"* — both refusals
  of valid MySQL, and both naming a fix that would not have helped, because neither the `DROP` nor
  the second statement was ever going to run.

  The guard reads every statement with one grammar and no engine, and `#` means four different
  things across the engines it speaks: a line comment in MySQL and MariaDB, the `#` / `#>` / `#>>`
  operators in PostgreSQL, a temp-table prefix in SQL Server (`#tmp`, `##global`), and an ordinary
  character inside a backtick- or bracket-quoted identifier. It cannot tell them apart, so it now
  declines to pick a reading and says which ambiguity it hit — the same call it already makes for
  a bare `--x`.

  **This is a widening: all four shapes ran before and are refused now**, not just the comment.
  If you use jsonb path operators, SQL Server temp tables, or a `#` inside a quoted identifier,
  those statements will start coming back refused. It is the accepted cost of one grammar; the
  other direction lets a trailing `;DROP` ride through inside text the guard decided to ignore.
  Rewrite a `#` comment as `-- ` or `/* … */`; a `#` inside a name has to be spelled another way.

- **The trust receipt is five sections, and each one says what it did NOT establish (ACE-088).**
  Every answer's receipt now carries `columns`, `tables`, `joins`, `aggregates` and `assumptions`,
  always all five, each an object `{items, undetermined}` beside the `model_version` pin.
  `undetermined` is a plain sentence naming what that section did not check; it is `null` only when
  the section is complete. **This is the point of the change**: before it, a section nobody had
  checked and a section that found nothing were both the empty list, so silence read as clean.
  Aggregate fan-out is the live example — the receipt now states, where you read the answer, that
  whether a join multiplies the rows an aggregate is computed from was not checked, instead of
  shipping an empty section you would read as "no problem". `assumptions` is the section that most
  often has nothing to admit, and it earns its `null` rather than assuming it: it lists at most
  three AI-written column meanings and counts any beyond that onto its own marker, because a
  truncated list under a null marker is a positive claim of completeness.

  The receipt also rides on **every** status, not just `ok`. Every non-ok body carries the bounded
  form — the caller's own identifiers and, per reference, whether the model declares that name;
  nothing else about the model. `tables` is now **one entry per reference** rather than per table,
  so a table read twice is listed twice and a reference the model does not declare says so. A name
  the statement defined for itself — a CTE, including one that shadows a real table — is not a
  declared table in ANY section: it borrows no row estimate, no schema-qualified column label and
  no model-written column meaning, in every spelling of a column reference.

  **Breaking for anything reading the old flat keys.** `tables_used` → `tables.items[]`;
  `relationships` → `joins.items[]`; `metrics` → the `columns.items[]` entries whose `metric` is
  non-null; `assumptions` → `assumptions.items[]`; `warnings` → derive it from `joins.items[]`
  filtered to `review_state != "approved"` (a review state can be counted and linked back to its
  join; a pre-rendered sentence could only be printed); `named_filters` and `sql` are gone (nothing
  ever produced the former, and the statement is on the response body). The HTML report, the MCP
  server instructions, `render_chart.py` and the `agami-query` skill all move with it, and
  `render_chart.py` now **rejects** a receipt that is missing a section rather than silently
  rendering nothing for it.

- **The answer report renders the whole receipt.** The provenance panel draws every section with its
  marker, lists tables per reference (an undeclared one explains itself instead of showing blanks),
  and adds the columns the statement referenced. The unreviewed-join banner is derived from the join
  review states, so it can count them and name each one. The unapproved-metric banner and its
  Approve / Change write-back are unchanged in behaviour. Two long-standing display bugs fixed while
  repointing: an unreviewed entry read "confidence ?" to every user (the phrase tested `confidence`
  as a number; it is a label), and a named-filters block that no producer ever filled is deleted.

### Removed

- **`max_rows` is no longer an argument to `execute_sql`,** and `--max-rows` is gone from the
  command line. It could only ever *lower* the deployment ceiling, so the one case where a caller
  knows better than the operator — wanting more data — was the case it could not serve. Ask for the
  rows you want in the statement: `LIMIT 200` says what it means to everything that reads it.

- **`truncated` is no longer a field on a successful result.** With an oversized result refused, it
  could only ever be `false`, and a field that is always `false` is one a client can only branch on
  wrongly.

- **Agami no longer rewrites your SQL to fix a fan-out join.** A query that aggregated a measure
  across a one-to-many join, touching the many side nowhere but the `ON` clause, used to have that
  join silently dropped and the rewritten statement executed in place of yours. Your statement is
  what runs now, byte for byte — comments, whitespace and quoting included — and that is asserted
  rather than assumed.

- **Four correctness checks stopped refusing.** A fan trap, a chasm trap, a `SUM` of a rate or an
  identifier, and a `SUM` of a balance across time were all refused. They **return a result** now,
  and what the check found rides on the answer's receipt, under `aggregates`.

  This is the point of the change rather than a relaxation of it. Whether a multiplied total is
  *wrong* depends on what you asked: the same statement is wrong for order revenue and right for
  line-item exposure. The check has your SQL and your model and never your question, so it describes
  what it found and leaves the judgement to you — or to the assistant, which does have the question
  and is asked to say out loud when it restructures a query because of a finding.

  A statement that trips two conditions now reports both. The old code stopped at the first, so a
  query that both fanned out *and* summed a rate was reported as having one problem.

- **`sensitive` is a description, not a gate.** Marking a column `sensitive` no longer blocks
  projecting it. The answer's receipt reports which sensitive columns it projected, under
  `columns`, and the authoring guidance asks the assistant to prefer aggregates and to say when it
  did project them.

  **If you relied on this to keep values from coming back, read this.** The gate was never a
  boundary: it inspected the projection list and nothing else, so `WHERE email LIKE …` always
  answered the same question one bit at a time. What it bounded was the *rate* of that, which is an
  access policy, and Agami holds none of its own — it reads exactly as the connecting database role
  reads. Two things do enforce, and neither changed: a column left out of the model is out of scope
  and any statement naming it is refused, and the connecting role's grants and your warehouse's
  masking policies apply as they always did. If a value must not come back, exclude the column from
  the model or make sure the role cannot read it.

- **The `model_safety` refusal rule is gone.** It stood in for two branches that refused without
  naming a rule. Both branches went, so every refusal now names the gate that chose it. A consumer
  keying on `refusal.rule == "model_safety"` will stop matching, which is the point.

- **The `GovernancePolicy` port and the `Adapters.governance` field (ACE-095).** `GovernancePolicy`,
  its `GovernanceVerdict` value type, and the `WarnOnlyGovernancePolicy` default adapter are gone,
  along with the `governance` field on `ports.Adapters`. The port was declared but never called: no
  core call site ever evaluated it, so removing it changes no behaviour and touches nothing on the
  `execute_sql` enforcement path. `Adapters` is public API, so a consumer that constructed it with
  `governance=...` drops that keyword; the remaining ports (`ActivitySink`, `OrgResolver`,
  `AuthProvider`, `Executor`) are unchanged. **`Adapters` is not keyword-only, so a consumer that
  built it POSITIONALLY must also re-check its call**: the 4th positional slot was `governance` and
  is now `executor`, so `Adapters(sink, resolver, auth, my_policy)` still constructs but binds the
  policy as the executor and fails later at query time with `AttributeError: 'MyPolicy' object has
  no attribute 'execute'`. Construct `Adapters` by keyword.

### Fixed

- **Updating the plugin now updates the library it runs on.** The plugin's own files are refetched on
  every version bump — the cache directory is the version — but the pip-installed `agami-core` was
  not, and the loader prefers that installed package over the fresh copy bundled beside the scripts.
  The readiness check only asked whether the library *imported*, which a stale one does perfectly
  well, so updating the plugin left new skills running against an old library with nothing anywhere
  saying so.

  This release is the first where that combination breaks outright rather than drifting quietly: the
  receipt below is five sections, and the chart renderer rejects a receipt shaped the old way, so
  every charted query would have failed on `receipt.columns is missing` until the library was
  upgraded by hand. The launcher now compares the installed distribution against the plugin's own
  version and reinstalls with `--upgrade` when it is behind. A library at or above that floor is left
  alone, so this costs nothing on an ordinary run, and a source checkout with no distribution
  metadata is not disturbed.

- **Catalog and dictionary reads no longer hit the row cap that exists to bound your queries.** The
  executor refuses (never truncates) any result over `AGAMI_SQL_MAX_ROWS`, default 1000. That bound
  is sized for a question someone asks; a *catalog* read exceeds it on schema size alone. The visible
  symptom was `sm enrich-metadata` dying on any platform whose data dictionary is a real table
  (`RuntimeError: … "rule": "resource_limit"`), but the quieter cases were worse: the bulk
  `information_schema.columns` read behind `sm discover`, and the table/foreign-key reads behind
  `sm introspect`, discard a refused read instead of reporting it — so on a wide catalog they
  degraded to one round-trip per table, or produced a model with no join graph, and said nothing
  about either.

  The connect skill now runs those three commands with a raised cap and **tells you it did, along
  with your unchanged query-time cap**. Nothing about the bound on an ordinary question changes: a
  query returning more than the cap is still refused rather than quietly truncated, and no new lever
  is reachable from a generated query.

- **The guard now reads your SQL in your database's own grammar, and refuses what it cannot read
  (ACE-079).** Every model-scoping check decided by parsing the statement, and every one of them
  parsed in a generic SQL dialect rather than your engine's. On MySQL, BigQuery, Databricks and SQL
  Server that is not a subtlety: a backtick is not an identifier quote in the generic grammar, so
  `` SELECT `ssn` FROM `customers` `` parsed to **no tables and no columns**. The scope checks were
  not bypassed by a trick — they inspected the tree, found nothing to object to, and passed. So on
  those engines a query could read any table in the database regardless of what your model declared,
  and the trust receipt reported no tables read, which made the answer look clean.

  The error posture was the other half. `error_level="ignore"` was not a lenient setting, it was no
  setting at all: sqlglot compares that argument against enum members, so a string matched no branch
  and every parse error was silently discarded, leaving a truncated tree that read as valid. Both
  halves are fixed together, because either alone leaves a hole.

  Four situations are now refused rather than run blind, each with the next step it actually needs:

  - the datasource does not say which engine it runs on (undeclared, unmapped, or two connections
    disagreeing) — `model_unavailable`, and the fix is the operator's: declare
    `storage_connections[].storage_type`. It does not invite you to retry the query, because no
    rewrite of the query helps.
  - the statement does not parse in that engine's grammar — `unparseable`, and you can re-emit it.
  - a double-quoted token on a backtick-quoting engine, which means a column under `ANSI_QUOTES` and
    a string literal otherwise. The server setting is not visible to the guard, so rather than guess
    it asks for the statement in the engine's own quoting.
  - the statement parses, reads from something, and resolves to no named table at all — `unscopable`.
    A backstop that does not depend on the engine map being complete.

  A model that declares one engine while its credentials connect to another is also refused
  (`engine_mismatch`): those are two independent pieces of configuration, and a mismatch means the
  statement was checked against the wrong grammar.

  **If your model does not declare a `storage_type`, queries against it now refuse** with the
  message above. This is deliberate: a datasource whose engine is unknown cannot be governed, and
  the alternative was to keep parsing it in a grammar no engine uses.

### Contract changes

- Receipt `tables` items carry an **arm ordinal on `scope`** (ACE-043). When a reference's scope is
  one of two or more arms of a `UNION` / `INTERSECT` / `EXCEPT`, its label gains a trailing 1-based
  `#<n>`: `main#1`, `main#2`, `cte:recent#2`. A plain `SELECT` and a single-arm CTE body are
  unchanged and carry no suffix, and `subquery` never takes one. **If you branch on
  `scope === 'main'` or `scope === 'cte:x'`, that branch stops matching inside a set operation —
  strip the ordinal with `scope.replace(/#\d+$/, '')` (or `scope.rsplit('#', 1)[0]`) and branch on
  that.** Split from the RIGHT, not the left: the CTE-name half is caller-written text, and it is
  only sanitization to an identifier alphabet excluding `#` that keeps a left split working today. The ordinal is the arm's position in the SQL, which
  is not the order of this list: items are in parse-walk order, so a capped receipt can list
  ordinals that are neither contiguous nor monotonic, and the largest one is not the arm count.
- Receipt `tables` items gain **`scope`** and **`filters`** (ACE-099). `ref` is unchanged and is
  still a string. A `refused` or `failed` receipt is unchanged too — it carries `{ref, declared}`
  and neither new field, because a declared filter names the columns and literals the model author
  wrote and a refusal is the one outcome a caller can provoke on purpose.
- `tables.undetermined` is now **`null` when the section is complete** (ACE-099). It used to be a
  fixed sentence on every receipt saying the filter accounting was not done. It now names only what
  was genuinely not established — references whose filters could not be accounted for, references a
  shadowing CTE name stopped resolving, and the count the reference cap dropped. If you branch on
  this field being present, that branch changes meaning: present now means something really is
  missing.
- `sm receipt --applied-filters` is **gone**, and the receipt no longer emits a top-level
  `default_filters_applied` key (ACE-099). Nothing had produced either since the filter injector was
  removed; the fact lives in `tables.items[].filters` now, in one shape rather than two.
- `sm prepare` returns `{sql, findings, units}` and **always exits 0**. It previously returned
  `{action, risk, sql, units, reason}`, or exited 1 with a refusal. It runs the reporting checks,
  not the refusing gates — do not pair it with `--no-safety`.
- `sm preflight` returns `{findings: [...]}`, replacing the single
  `{risk, action, reason, suggestion, triggering_joins}` verdict.
- The receipt's `aggregates` section can now be non-empty, and its `undetermined` sentence changed:
  it says the checks ran and names what they still do not reach, rather than saying the check does
  not happen. `columns` items may carry `sensitive: true`.
- The `{"error": {"kind": "preflight_refused"}}` and `{"kind": "sensitive_columns"}` diagnostics no
  longer appear on stderr. Every refusal is a single JSON object on every path.
- A refused `SELECT *` reports `reason: "undetermined"`, not `reason: "out_of_scope"`. The refusal
  and its message are unchanged; only the reason moves. If you route, count or alert on `reason`,
  this row changes bucket.

### Docs

- **Read-only datasource role is now a stated deploy obligation, not a suggestion (ACE-036).** The
  self-host guide and `readonly-grants.md` now frame the SELECT-only role as the **required**
  `DATASOURCE_URL` posture for a deploy (single-player stays *recommended*), spell out the app-role vs
  operator/owner-role split, and clarify that the role guarantees integrity/confinement but **not**
  availability/recon — bounding runaway work and recon is app-side, and the docs now state which of
  those bounds exist today (the result-row cap) and which do not yet (a per-statement query timeout,
  error-text/recon hardening). Docs only — no behaviour or config change.

## [0.5.3] — 2026-07-31

### Added

- **Override seam on the tool-call audit log (`record_tool_call`).** An embedder that dispatches the
  tool handlers itself can now supply the values the log would otherwise infer from the model's own
  tool arguments and result body — `source`, `thread_id`, `correlation_id`, `user_question`,
  `org_id`, and the outcome (`success`, `row_count`, `error_kind`). Each of these override
  parameters defaults to `None` = "derive it the way you always have" (the recorded fact `raised` is
  a separate boolean, default `False`), so a caller that passes none of them gets byte-identical
  rows — the in-repo transport path is regression-pinned identical to before the seam existed. `_record_query`'s source is likewise
  readable from a context var, so the query log and the tool-call log can't disagree about what drove
  one execution.

### Changed

- **The audit row now fails toward honesty.** A caller with no result body to parse can now record a
  failure (previously the row defaulted to success — the one direction an audit log must never fail
  in); a raise outranks every override; naming an `error_kind` is itself a statement of failure; and
  a success never carries an error kind, so the row can't contradict itself.
- **Honest question provenance in the Activity drawer.** The `· self-reported` trust marker was
  stamped on every question unconditionally (the `source` column was never even selected). The column
  is now projected and a per-turn flag derived, so the marker is dropped only where the caller
  observed the question directly — and kept under any disagreement or uncertainty, because
  overstating trust is the one direction it must not fail.

## [0.5.2] — 2026-07-30

### Added

- **Per-request tool-visibility seam (`Adapters.tool_visibility`).** A consumer can now narrow the
  advertised MCP tool surface per caller via an optional `tool_visibility(tool_name) -> bool` on the
  adapters, applied in `build_server`. It is applied at *both* the list and the call seam — filtering
  only the list would leave an unlisted tool callable by name, a surface that looks narrowed while
  remaining open. A hidden tool answers as *absent* (the same `Unknown tool` a typo gets), not as
  refused, so the list can't be used as an oracle for what exists but is withheld. A predicate that
  raises hides the tool rather than granting it, and is logged rather than propagated, so one broken
  classification can't become an accidental grant or a dead transport for every other caller.
  Subtractive only — a surviving tool's description and input schema pass through untouched. `None`
  (the OSS default) is byte-identical to prior behaviour.

## [0.5.1] — 2026-07-30

### Security

- **Closed a read-only-guard bypass via a welded quoted identifier.** A double-quoted
  identifier is self-delimiting in SQL on **both** ends, so `SELECT*FROM"pg_read_file"(…)`
  and `SELECT "x"INTO evil FROM t` are valid statements with no whitespace either side of
  the quote. The guard's lexer dropped the quote characters without re-supplying those
  boundaries, fusing neighbouring tokens into one (`FROM"pg_class"` → `FROMpg_class`,
  `"x"INTO` → `xINTO`) and destroying the word-boundary anchor every deny-list pattern
  matches on — so the gate stopped *seeing* the token rather than allowing it, and returned
  no rejection. Verified against PostgreSQL 16: the leading form reads a server-side file
  and the trailing form creates a table from `SELECT … INTO`, both while the guard passed
  them. Row locks (`FOR SHARE`) were reachable the same way. The lexer now re-supplies a
  separator on either side, when and only when the quote was actually separating two word
  characters — so a qualified name (`t."current_user"`) still neutralizes to one token,
  `t.current_user`, rather than being split. This restores an invariant the lexer already
  documented for comments and literals ("never empty"); the identifier branch was the one
  place not honouring it. Prior corpus cases all happened to carry a space before the
  quote, which is why this stayed invisible; the corpus now pins both weld directions and
  asserts the neutralized token structure directly, so neither a one-sided fix nor a
  blanket separator can pass.

## [0.5.0] — 2026-07-25

### Changed

- **Renamed the per-datasource model files** so their names say what they are, now that a
  company-wide `organization.yaml` exists at the artifacts root. In each profile,
  `org.yaml` → `datasource.yaml` and `ORGANIZATION.md` → `datasource.md`. The model root's
  display-name field `organization:` is likewise `datasource:`, and the served per-datasource
  memory row uses `kind='datasource'`. The company record (`organization.yaml`, `org_id`,
  tenancy) is unchanged. No on-disk migration is provided — re-run `agami-connect` (or rename
  the two files by hand) for any pre-existing profile.

## [0.4.5] — 2026-07-15

Hosted/self-hosted server hardening: a real-wheel packaging fix, a multi-tenancy seam, and an
append-only instructions hook. **The local plugin path is behaviour-preserving** — everything
resolves to a single `local` org and existing deploys are byte-identical by default.

### Fixed

- **Migrations and static assets now ship inside the wheel.** In a real (non-editable) `pip install`,
  `store.MIGRATIONS_DIR` resolved outside the package, so the server could boot on an **empty schema**
  with no error, and the missing `static/` dir made app construction fail. Migrations moved into the
  package (`packages/agami-core/src/migrations/core`), both are packaged as package-data, and `run_migrations`
  now **raises** on a missing core-migration root instead of silently applying nothing. (Editable
  checkouts and the `pip install -e` Docker deploy were unaffected — which is why CI stayed green.)

### Added

- **Multi-tenancy: `org_id` scoping across serving, runtime logs, and credentials.** One deployment
  can host many tenants whose datasources collide on name (e.g. `prod`). `org_id` (default `local`) is
  threaded through every serving/runtime read+write; a redeploy DELETE is org-scoped (one tenant's
  reseed can't wipe another's same-named datasource); per-tenant credential env vars
  (`<ORG>_DATASOURCE_URL[__<PROFILE>]`) with a **fail-closed** rule (a named tenant never falls back to
  the org-less DSN); and a resolver may raise `PermissionError` to refuse a caller (clean 403, not 500).
  A plain OSS/self-host deploy is unchanged — everything resolves to `local`.
- **Append-only `extra_instructions` seam on the HTTP composition root.** `build_server(...)` /
  `create_app(...)` accept `extra_instructions` so a consumer can add to the model-facing MCP
  instructions without forking core. **Append-only, never replace** — so a consumer can't silently drop
  a safety directive (e.g. the sensitive-column output rule); no-op and byte-identical by default.

## [0.4.4] — 2026-07-14

Onboarding fix for the examples-validation (NL→SQL) dashboard.

### Fixed

- **Examples-validation dashboard: Edit/Add-note no longer fires on every card.** Each example's
  interaction state was keyed on its display number `n`, which `sm seed-validate` assigns **per
  subject area** (1..k) — so a dashboard combining multiple areas carried duplicate `n`, and clicking
  **Edit** (or **Add note**) on one card opened every card that shared that number. It also made the
  "Generate feedback" block ambiguous (`edit N` could match more than one example). The renderer now
  assigns a **stable global `1..N`** in render order — the single numbering shared by the interaction
  key, the `#N` label, the feedback block, and the apply lookup — and normalizes the items file to
  match, so `edit N` resolves unambiguously.

## [0.4.3] — 2026-07-14

Documentation-only release. No behavior changes; the executor and skills from 0.4.2 are unchanged.

### Changed

- **`agami-core` PyPI page is now a readable landing page.** Reframed
  `packages/agami-core/README.md` (the PyPI `long_description`) to lead with the value proposition
  and clarify the plugin-vs-`pip` audiences, and trimmed the deep HTTP-server internals down to a
  summary plus links to `deploy/README.md` and `docs/`. Added `[project.urls]`
  (Homepage/Repository/Documentation/Issues) so PyPI shows sidebar navigation. Publishing this
  version is what refreshes the live PyPI page.

## [0.4.2] — 2026-07-14

Onboarding hardening for the public launch — fixes to the first-run `/agami-connect` path — plus a
documentation pass. No breaking changes; the executor internals from 0.4.1 are unchanged.

### Fixed

- **Seed validation no longer rejects every seed example.** The zero-row validation probe wrapped each
  seed as `SELECT * FROM (<sql>) WHERE 1=0`; its own `SELECT *` tripped the `SELECT *` ban and every seed
  was rejected regardless of its SQL. The probe now projects `SELECT 1` — it still parses and plans the
  inner query, but no longer trips the ban.
- **DuckDB readiness now requires `pytz`.** DuckDB needs `pytz` to materialize `TIMESTAMP WITH TIME ZONE`
  values; the driver probe only checked `import duckdb`, so an interpreter missing `pytz` scored as ready
  and then failed at query time on any `timestamptz` column. `pytz` is now part of the DuckDB probe.
- **Approve operations auto-stamp their timestamp.** An approve op without a `signed_off_at` recorded
  `null` and the validator rejected the whole batch. The timestamp is now stamped at the CLI boundary
  (where the clock is available), so sign-off batches apply cleanly.

### Added

- **Headless sign-off (`sm approve-queue`).** A no-browser path that reads the pending review queue
  (Rule 1 + Rule 2), builds a self-stamped approve op per item, and applies it (`--kind` to narrow,
  `--dry-run` to preview) — so onboarding can complete without opening the review dashboard.
- **The no-DB sample clears its own pre-seed gate.** The sample's silent build auto-approves its pre-seed
  queue as `signer=system` before seeding; real databases keep the human sign-off gate.

### Docs

- **Launch positioning.** The self-hosted team server (`/agami-deploy`) is labeled **Early access (in
  testing)** throughout; the free-vs-paid copy leads with the value the hosted cloud adds.
- **README.** A **Databases supported** section (all engines + how each executes), VS Code/Cursor install
  clarified as Manage-Plugins-UI (not the CLI slash-command form), and the sample-query copy made generic.
- **Guides.** Onboarding docs (`duckdb pytz` install, explicit render flags, the headless sign-off path),
  a plain-English trust-layer intro, an `/agami-serve` (Claude Desktop) usage section, and an accurate
  `migrations/core` README (the self-hosted server schema).

## [0.4.1] — 2026-07-12

The self-hosted HTTP server now runs SQL **in-process** by default — no per-query subprocess fork,
no CSV round-trip — behind a swappable execution seam. Plus a correctness fix for Postgres/Redshift.

### Added

- **Executor seam (`ports.Executor`).** `execute_sql` is split into a shared, un-bypassable *guarded
  envelope* (read-only guard → semantic-model safety → resolve datasource → execute) and a swappable
  **`Executor` port**. The built-in executor is the default and behaviour is unchanged; a consumer can
  inject a custom executor (e.g. pooled / per-user-RBAC) via `create_app(adapters=…)` **behind the same
  guard** — one execution implementation, never forked.

### Changed

- **The HTTP server executes queries in-process by default.** Previously every query forked
  `python -m execute_sql`; now the served path runs through the executor seam in-process — no fork, no
  CSV serialize/re-parse round-trip, native rows. The local stdio path and the `python -m execute_sql`
  CLI still fork (the throwaway-process isolation is kept for the single-user tool). Successful query
  results are identical to before.
- **The per-call row cap is request-scoped.** `--max-rows` now rides a `ContextVar` (was a module
  global), so concurrent in-process queries with different caps can't affect each other.

### Fixed

- **Postgres/Redshift queries returned 0 rows through the Python executor.** A psycopg2 server-side
  (named) cursor reports `description = None` until the first fetch, and the result collector read it
  *before* fetching — so it concluded "no result set" and returned empty. It now fetches first, then
  reads the description. SQLite/MySQL/etc. (client-side cursors) were unaffected. (Present since the
  server-side cursor was introduced for bounded transfer.)

### Performance

- **Tool handler runs off the event loop.** The heavy query handler is offloaded via `run_blocking`
  (completing the async-offload work), so one slow query no longer stalls the server's event loop.

## [0.4.0] — 2026-07-12

Runtime scalability & safety hardening: the server stays responsive and bounded as model size,
result size, and concurrent load grow. Behaviour-preserving unless a note says otherwise.

### Added

- **Bounded result sets.** A query now materialises at most a row cap instead of the whole result:
  `AGAMI_SQL_MAX_ROWS` (default 1000) is the deployment cap; a per-call cap is available via the
  executor's `--max-rows` (which can only lower it). Truncation is flagged (`result.truncated`) so a
  cut-off result is never presented as complete. The SQL is never rewritten (no injected `LIMIT`);
  Postgres uses a server-side cursor so the cap bounds transfer, not just what's written.
- **Multi-worker HTTP server.** The server can run with `--workers=N` (uvicorn import-string
  factory). MCP session state is already stateless (JWT + Postgres), so it scales horizontally.

### Changed

- **OAuth refresh-token storage is now configurable — default `overwrite`.**
  `AGAMI_REFRESH_TOKEN_MODE` selects `overwrite` (default: each refresh UPDATEs the session's single
  token row in place — one row per session, no growing heap of dead tokens) or `rotate` (the prior
  behaviour: insert-new + revoke-old, keeping OAuth 2.1 **stolen-token reuse detection**, plus a
  cleanup that prunes only already-expired revoked rows). **Upgrade note:** the new default
  `overwrite` trades away reuse detection — a replayed stolen refresh token simply fails to
  authenticate instead of revoking the whole family. A deployment that wants family-revocation must
  set `AGAMI_REFRESH_TOKEN_MODE=rotate`. Also: used/expired one-time authorization codes are now
  cleared at authorize, and the query/activity logs (`query_executions` / `tool_calls`) are
  explicitly **retained** — never deleted by any default path — with a new `idx_query_executions_ts`
  index to keep newest-first reads fast as history grows.
- **Hosted safety guard is now fail-closed and DB-backed.** On the hosted server the fan/chasm-trap,
  table/column-scope, SELECT-\* and PII guards resolve the semantic model from the database (not only
  the `/artifacts` disk mount) and **refuse** a query when no model can be resolved — instead of
  silently running it unguarded. The local single-player path is unchanged (a not-yet-built model is
  still fine, not an error).

### Performance (behaviour-preserving)

- **Per-process semantic-model cache + single SQL parse.** The model loads once per process and the
  SQL is parsed once per query, with the safety-guard indices built once and shared — down from a
  reload/re-parse per query on a long-lived server. Biggest latency win.
- **Blocking work runs off the event loop.** Password hashing (argon2), OIDC HTTP calls, and the
  per-call audit write no longer stall the async server — one slow login can't freeze all traffic.
- **Incremental model-authoring validation.** Curation/enrichment re-validates only the edited area,
  and snapshots read each file once, so authoring a large (many-area) model no longer grows
  super-linearly. Same verdicts and snapshots.
- **Faster schema discovery.** `get_datasource_schema` resolves tables via an O(1) index instead of
  re-scanning the model per table — byte-identical output, faster on wide models.

## [0.3.9] — 2026-07-10

### Added

- **Composition seams for downstream extension (no-op by default).**
  `mcp_http.create_app(extra_tools={}, adapters=None)` lets a downstream consumer add MCP tools and
  inject the `ports.py` adapters without forking or monkeypatching core, and `tools.register(...)`
  adds a tool to the shared registry with a duplicate-name guard. An existing deploy is unaffected —
  `create_app()` with no arguments behaves exactly as before, and `execute_sql`'s schema is unchanged.
  (#100)
- **Migration-overlay seam.** The store can layer additional migration roots on top of core's, so a
  downstream package can ship its own migrations alongside agami-core's (empty/duplicate namespaces
  and non-directory roots are rejected). No change for a default install. (#101)

## [0.3.8] — 2026-07-06

### Added

- **OAuth refresh tokens — no more hourly re-login on the self-hosted server.** The token
  endpoint now issues a `refresh_token` and supports the `refresh_token` grant (RFC 6749 §6),
  so a connected client (claude.ai) silently renews the short-lived access token instead of
  redoing the full login every hour. Refresh tokens **rotate** on each use with **reuse
  detection** (replaying a rotated/stolen token revokes the whole family), are stored **hashed,
  never in plaintext**, and are revocable. Access tokens stay short-lived (1h). Both lifetimes are
  now env-configurable (`AGAMI_ACCESS_TOKEN_TTL` / `AGAMI_REFRESH_TOKEN_TTL`, seconds) with the
  same defaults when unset (access 1h, refresh 30-day idle). No action needed on upgrade — the
  new `oauth_refresh_token` table migrates in automatically on boot.

## [0.3.7] — 2026-07-06

### Added

- **Read-only database user guidance.** `/agami-connect` and `/agami-deploy` now
  recommend connecting agami with a **read-only** database user — agami only ever
  runs read-only SELECT queries, so read access is all it needs. A new
  [readonly-grants.md](plugins/agami/shared/readonly-grants.md) ships copy-paste
  `CREATE USER` / `GRANT SELECT` SQL for every supported dialect (Postgres/Redshift,
  MySQL, Snowflake, SQL Server, Oracle, Databricks, Trino, BigQuery). Ask agami for
  "the read-only grant" to get the exact SQL for your database.

### Changed

- **Self-host compose caps container log growth.** Every service now uses the
  `json-file` driver with `max-size: 10m` / `max-file: 3` (≤30 MB per container), so
  a long-running deploy on a small VM can't silently fill the disk — no VM-side
  `daemon.json` step needed. Also silenced a harmless `CLOUDFLARE_TUNNEL_TOKEN … not
  set` warning on non-tunnel deploys.

### Fixed

- **`list_datasources` no longer reports empty on a self-hosted server.** On a
  served deployment the warehouse/model is reached through the store, and the local
  `credentials` file never ships to the container — but `list_datasources` was the
  one tool still reading only that file, so it always returned "No profiles found …
  run agami-connect", even while `get_datasource_schema` and `execute_sql` worked
  against the deployed model. Because clients are told to call it first, they'd
  conclude nothing was connected. It now enumerates the served models from the store
  (the same seam every other tool already uses), and only falls back to the
  credentials file for the local plugin.

### Security

- **Hardened the read-only `execute_sql` gate.** SQL execution now runs through a
  single guard (`sql_guard`) at the shared executor, so the stdio server, the hosted
  HTTP server, the skills, and cron are all protected identically (previously the
  check lived only on the MCP tool path; a direct `python -m execute_sql` call — used
  by the skills and cron — was unguarded). Beyond "must start with `SELECT`/`WITH`",
  it now rejects multi-statement SQL (including bypasses hidden in string literals,
  comments, or double-quoted identifiers), data-modifying CTEs, transaction-control /
  session-state / prepared statements, `SELECT ... INTO`, row-level locks, and
  dangerous server-side functions (`pg_read_file`, `lo_export`, `dblink`,
  `copy_program`, `pg_sleep`, advisory locks, `query_to_xml`, …). Legitimate analytics
  SQL is unaffected — a large false-positive corpus pins that. Enforcement is not
  bypassable via `--no-safety` (that flag only skips the semantic-model pass).
- **Closed a dollar-quote statement-stacking bypass in that gate.** A `'` inside a
  Postgres/Snowflake/DuckDB `$$…$$` (or `$tag$…$tag$`) string desynced the literal
  stripper and could smuggle a second statement (`SELECT $$'$$ ; DROP TABLE x -- '`)
  past the multi-statement check. The gate now neutralizes comments and string /
  dollar literals in a single lexer-faithful pass (first-opened construct wins),
  refuses dialect-ambiguous MySQL comment forms (a bare `--x` and executable
  `/*! … */` comments), and also blocks sequence writes (`setval`/`nextval`) and
  server/replication control
  (`pg_stat_reset*`, `pg_switch_wal`, `pg_drop_replication_slot`, …). The guard module
  is also now packaged in the built wheel (it was missing from `py-modules`, which
  would have broken `import sql_guard` in an installed/containerized deploy).

## [0.3.6] — 2026-07-04

### Changed

- **`/agami-deploy` is easier to find and safe to re-run.** The config file is now
  a **visible `agami.env`** (not a hidden `.env`), and the skill opens it for you.
  A **re-run upgrades in place, non-destructively**: your typed password/secret and
  DSN are kept, any setting new in a version is surfaced (e.g. `DATASOURCE_URL`),
  and the image tag bumps only when you pass one — so a model update is just
  re-run + restart, and a version upgrade tells you exactly what's new.
- **Multi-datasource deploys are an explicit choice.** With more than one model,
  the skill asks which to deploy (all or a subset) and names the per-datasource
  `DATASOURCE_URL__<NAME>` to set; dropping one on a re-run removes it cleanly.

## [0.3.5] — 2026-07-04

### Fixed

- **Self-host deploy no longer crash-loops on artifact permissions.** The team
  server runs as a non-root container user; the deploy now stages the model
  **world-readable**, so the boot-time model load can't fail `Permission denied`
  on `datasource.md` under a mismatched host owner.
- **claude.ai connects to a self-hosted server.** The `/mcp` endpoint no longer
  answers the bare (no-trailing-slash) URL with a `307` redirect that the MCP
  client won't follow — the server normalizes it internally, so `{base}/mcp`
  works on every deploy profile (including the Caddy-less Cloud Run one).

### Changed

- **Warehouse credentials come from the environment (`DATASOURCE_URL`), not a
  mounted file.** The executor resolves a connection DSN from
  `DATASOURCE_URL[__<datasource>]` env-first, falling back to the local
  `credentials` file — one code path, no fork. The self-host bundle now carries
  the DSN in `.env` and **ships no secret**: `local/` (credentials, `.pgpass`)
  is never staged, and a re-run purges any stale copy from an older bundle.

## [0.3.4] — 2026-07-03

### Fixed

- **Table-prune step of a real-DB onboarding no longer crashes on an installed
  build.** The `discover` pass (which renders the prune page where you pick which
  tables to model) failed with `ModuleNotFoundError` on a pip/marketplace install;
  it now resolves its renderer via the plugin root and works everywhere.

### Docs

- Refreshed for the current release: README slimmed (self-hosting moved to
  `docs/self-hosting.md`), the published PyPI install surfaced
  (`pip install "agami-core[model]"`), and the changelog backfilled.

## [0.3.3] — 2026-07-02

### Fixed

- **Marketplace-install reliability.** Credential promotion and the Claude Desktop
  setup (`/agami-serve`) no longer fail on a fresh marketplace install — they resolve
  the bundled library the same way every other script does, and install the model
  engine through the single `sm install` path.
- **Externally-managed Python + package shadowing.** The installer now works on an
  externally-managed interpreter (Homebrew / PEP 668) and can no longer be shadowed by
  a partially-installed package (the model CLI is verified from a neutral path).

### Changed

- **Sample "watch it build" opens the model explorer** when the build completes, and
  skips the prompts that don't apply to the curated sample (no table-prune / org /
  data-dictionary questions).
- **First-time setup no longer shows a placeholder profile name** — it reads as
  "first-time setup" until you name your profile.

## [0.3.2] — 2026-07-01

### Added

- **Published to PyPI.** `pip install "agami-core[model]"` (and `[server]`) installs
  the library from the index, and the plugin's model-build step uses it automatically.
  Published via GitHub trusted publishing (no stored token). The self-host server image
  is published to GHCR (`ghcr.io/agamiai/agami-core`) so a deploy pulls it — no clone,
  no build.

## [0.3.1] — 2026-07-01

### Fixed

- **Marketplace installs can query and build models with no dev checkout.** Bundled the
  stdlib query library into the plugin, so a marketplace install answers questions with
  no `pip install`; and the model-build step installs the engine from a source that
  exists in a marketplace layout (the published package, else git) instead of a
  dev-only path.

## [0.3.0] — 2026-06-24

### Added

- **No-database sample (`/agami-connect sample`).** Ships *Acme Store*, a small
  local SQLite dataset (commerce + subscriptions) with a ready-made, signed-off
  semantic model. Goes from install to a governed, receipted answer in under a
  minute — no connection, no credentials, nothing leaving the machine. The
  bootstrap (Phase 0s) offers a fast copy-the-model path and a "watch it build
  live" rebuild path. Builds deterministically via the `sqlite3` CLI or a pure
  Python-stdlib fallback (no install required).
- **Model snapshots / `model_version`.** A model write now stamps a content-hashed
  snapshot under `<profile>/.snapshots/<hash>/`, so every answer's receipt pins a
  real `model_version` (previously `null` for all profiles) and old answers stay
  reproducible. New `sm snapshot <root>` CLI.
- **Deterministic interaction spine.** The mechanical parts of the skills are now
  scripts that emit a uniform `{ok, data, anomalies, needs_judgment}` contract, so
  the agent only makes judgment calls on genuine ambiguity:
  `connect_resolve.py` (one call resolves profile / credentials / interpreter +
  next-phase decision — fixes choosing a Python that can't connect),
  `parse_prune_block.py` (fixes a shell word-split that mangled table lists),
  `parse_model_feedback.py` (the dashboard back-channel), `csv_to_sections.py`
  (charts/tables get their numbers from the result CSV, not the model), and the
  `sm receipt` / `sm curate-gate` subcommands.

### Changed

- **Renamed LiteBi → agami-core.** The install identity is now `agami-core@agami`
  (marketplace `agami`, plugin `agami-core`); the version bump is breaking, so
  existing `agami@litebi` installs must re-add the marketplace to upgrade
  (`/plugin marketplace add AgamiAI/agami-core` → `/plugin install agami-core@agami`).
- **Relicensed Apache-2.0 → fair-code (the Agami Functional Use License / FUL).**
  Internal/team use stays free; exposing the data or the MCP to people outside your
  organization now requires a commercial license. See [LICENSE](LICENSE) and
  [LICENSING.md](LICENSING.md).
- **Repositioned around the trust layer.** README, marketplace, and plugin
  metadata now lead with the governance/trust stance ("the trust layer between AI
  and your data") instead of natural-language querying. Dropped the "BI" framing.
- **Quickstart leads with the sample** — the fastest path to a first governed
  answer, with the real-database flow following it.

### Security

- **Engine-level PII enforcement.** Raw projection of a column marked `sensitive`
  is refused in the shared executor (`runtime.check_sensitive_projection`, wired
  into `execute_sql.py`), so the same rule protects the Claude Code skill **and**
  the local MCP server. Aggregates, filters, joins, and `GROUP BY` over sensitive
  columns are still allowed — only raw per-row output is blocked.

## [0.2.2] — baseline

First version tracked in this changelog. Earlier history lives in the git log.

- The local-first **trust layer**: confidence + review state on every join,
  metric, and entity; single-reviewer sign-off; per-answer receipts (SQL, tables,
  relationships, metric definitions, freshness); a review dashboard.
- Schema introspection into a provider-portable, git-native YAML semantic model.
- NL→SQL generation and **local execution** across Postgres, Supabase, Redshift,
  MySQL, Snowflake, BigQuery, SQL Server, Oracle, Databricks, Trino, DuckDB, and
  SQLite.
- Corrections with attribution, persisted to an `examples.yaml` few-shot library.
- An optional local **MCP server** (`agami serve`) for use from Claude Desktop and
  other clients — stdio, no auth, no network.
- Fan-trap / chasm-trap pre-flight that refuses to silently double-count.

[0.3.9]: https://github.com/AgamiAI/agami-core/compare/v0.3.8...v0.3.9
[0.3.8]: https://github.com/AgamiAI/agami-core/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/AgamiAI/agami-core/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/AgamiAI/agami-core/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/AgamiAI/agami-core/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/AgamiAI/agami-core/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/AgamiAI/agami-core/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/AgamiAI/agami-core/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/AgamiAI/agami-core/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/AgamiAI/agami-core/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/AgamiAI/agami-core/releases/tag/v0.2.2
