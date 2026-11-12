# Question cards: the rules every skill follows

Every agami decision is a card in the chat (Claude's AskUserQuestion tool), every result is a
table in the chat, and a page opens only when the person asks for one. Link this file from your
skill; do not copy it. `<plugin_root>` is defined at the top of `invocation-conventions.md`.

A card holds 1 to 4 questions. Each question has 2 to 4 options, plus the automatic "Other", and
may allow several picks (`multiSelect`). `header` is at most 12 characters. Give each question a header no other
question in the same flow uses: two different questions both headed "Database" read as the same
question asked twice.

## 1. A decision is a card, never prose

Whenever the person must choose, call AskUserQuestion. Do not write "reply with 1, 2 or 3".

```json
{"questions": [{"header": "Database", "question": "Which database should I connect to?",
  "multiSelect": false,
  "options": [{"label": "PostgreSQL", "description": "A server you run or rent"},
              {"label": "Snowflake", "description": "A Snowflake account"},
              {"label": "Sample data", "description": "A small demo, no connection"}]}]}
```

When picking nothing is valid in a multi-select, offer it as an explicit option ("None of these").

## 2. The recommended option comes first

Put it first and end its label with "(Recommended)", but only when there is a genuine
recommendation. Facts about the person's environment ("which database?") get no recommendation.

```json
{"header": "Next step", "question": "What should I do with the 3 unsigned metrics?", "multiSelect": false,
 "options": [{"label": "Review them now (Recommended)", "description": "Queries using them carry a warning until then"},
             {"label": "Leave them for later", "description": "They still answer, with a warning"}]}
```

## 3. A result is chat output, before the card that acts on it

Show a short markdown table (at most 15 rows, and say how many more there are) and the one or two
sentences that matter. The card then asks what to do about it.

```
| Table     | Rows   |
|-----------|--------|
| orders    | 12,408 |
| customers |  1,950 |

Showing 2 of 2 tables.      (or: "Showing 15 of 42 tables; 27 more.")
```

## 3a. Detail goes in an option's preview, not the question

A card's question is plain text: keep it to the decision alone, in plain words ("Sign off ELA proficiency rate?", not an identifier like `ela_proficiency_rate`), with no result in it. A value that helps decide goes in the option's description. Anything
longer that they need to see to decide (a SQL formula, a sample of rows, what changed) goes in the
`preview` of the option it belongs to, which the card shows in a monospace pane while that option
is focused. Lay SQL out over lines with a short comment on each filter. A live run showed a
formula in the question as an unreadable wall of text. Anything the person signs off that runs SQL
shows that SQL this way: a metric's binding, and an example question's statement too, on the
option that keeps it, so checking it never takes typing "show me the SQL".

## 3b. Things are named in plain words

A table, column, metric or join on a card is named in words, never by its identifier: *"What
currency is the Price column on Widgets in?"*, not *"What currency is `widgets.price` in?"*; an
option **In stock on Widgets**, not `widgets.in_stock`. This holds for the question, the option
labels and the option descriptions. The identifier goes in the option's `preview` when the person
may need it (to type it in "Other", or to find it in the database), never in place of the name.

Take the words from the command's output rather than making them up, so every card says the same
thing: a column's `label` (`sm suggest-units`, `sm set-units`, `sm sensitive`, a placeholder from
`sm review-queue`, whose `question` is already worded), `flag_rates.labels` from
`sm suggest-metrics`, and an item's `plain_name` from `sm review-items`. Where a command gives no
words, turn the identifier into words the same way: the last part of a qualified name, underscores
to spaces, sentence case (`order_items` → *Order items*, `ela_proficiency_rate` → *ELA proficiency
rate*), and a column as *<column> on <table>*.

## 4. Lists longer than a card holds are summarised

Group them (by schema, name pattern, subject area, status) and ask about the groups; "Other" takes
the names of anything to treat differently. **A group option's `preview` lists every item the option
covers**, one per line (up to 15, then "and N more"), and the same items are in a chat table above
the card: an option such as "Add all 16" or "Only 1–8" with nothing to show which items those are
leaves the person nothing to decide by. When items must be decided one by one, ask up to 4 per
card, one question each, and put "Approve all remaining" (or its equivalent) on every card so a long
queue can be finished in a few presses.

**A bulk sign-off never signs an item the person has not been shown.** A sign-off records that a
named person checked that definition. So before applying "Approve all remaining", or an "Approve
all" whose table stopped at 15 rows, list every item it would sign that no table in the chat has
shown yet (name, meaning and SQL, one short row each, in tables of up to 15), then ask one question,
header `Approve all`, *"Approve these <n>?"*: **Approve these <n>** / **Back to one by one** /
**Review on a page** (rule 6a). Each approve op of a bulk sign-off carries `"bulk": true`, which
`sm curate` writes to `curation_log.jsonl` with the op, so the log says it was signed many at once.

```json
{"header": "Metric 1/9", "question": "Sign off Revenue?",
 "multiSelect": false,
 "options": [{"label": "Approve", "description": "Total of the order amounts",
              "preview": "-- revenue\nSUM(orders.amount)"},
             {"label": "Reject", "description": "Drop it from the model"},
             {"label": "Approve all remaining", "description": "Lists the other 8 first, then asks once"},
             {"label": "Review on a page", "description": "Decide the whole list on one page; it saves itself"}]}
```

## 5. Free text goes in "Other"

A definition change, a host name, a reworded question: the person types it in "Other". Never ask
them to type a reply message.

A value the person must type (an email, a host) is asked for in the question text, *"Which email
should sign-offs be recorded under? Type it in Other."*, never offered as an option such as "Type it
in Other": people click that option and nothing gets typed. The options are the alternatives to
typing ("Don't sign anything yet").

### 5a. A question typed in "Other" is answered, then the same card is asked again

People use "Other" to ask before they decide: "show me the SQL", "show me the rows", "why do I
need this?". That is not a change, a stop, or something you can't place. Answer it in the chat,
then ask the same card again, with only the questions still open:

- **The SQL**: the item's SQL in a fenced block (its binding for a metric, the statement for an
  example or an answer), with the tables it reads in one line.
- **The rows or values**: up to 15 rows of the result, or of a breakdown behind a single number
  (by its most telling column), run through the same guarded path the skill already uses.
- **Why**: one or two sentences on what the step is for and what happens if they skip it.

Answers given to the card's other questions stand: apply them before asking again.

## 6. No page opens by default, and nothing is pasted back

The HTML pages are optional, and a page is an option on the card, never typed in "Other" to find.
There are two kinds, and nothing comes back from either by copy and paste:

- **A page that takes decisions saves them itself** (6a), the way the password page saves the
  password. Every review or sign-off question that has such a page behind it offers
  **Review on a page** (or the skill's wording for it, such as **Mark them on a page**), on every
  question of the card, not only the first: the page opens on the list that question is about.
- **A page that only shows something** is the **Show the full view** option, rendered `--read-only`:
  it hides the page's change controls and copy-and-paste dialog and says in a banner that nothing on
  it is saved. After the URL, ask the same card again: the decision still happens on the card.

Either is served from `localhost`, never as a `file://` page: the Code tab's built-in browser shows a
local file outside the project folder as a frozen snapshot. Run `serve_page.py` with
`run_in_background`: it keeps serving until its time is up or the page is saved, so in the foreground
it would hold the turn and the URL would never reach the person. A view-only page:

```
bash "<plugin_root>/scripts/py" serve_page.py <file.html> --minutes 30
# run_in_background; its first line is {"url": "http://localhost:<port>/<name>"}, and it exits when the time is up
```

Read that first line from the background task's output and give the person the URL.

### 6a. A page that saves its decisions

1. **Where a page can't reach this computer, use the cards.** In a remote or SSH session
   (`SSH_CONNECTION` is set), the person's browser can't open this computer's `127.0.0.1`: say so in
   one line ("pages open on this computer, which your browser can't reach from here") and carry on
   with the cards. A cloud or web session can be remote without setting it, so the link may still
   not open there: **Back to the cards** (step 4) is the way out.
2. **Render the page with `--save-to-helper`** (and the skill's deep link), to a new timestamped
   file. Its Save button posts the decisions to the helper serving it; approvals on it are signed as
   the saved `reviewer_email` / `reviewer_role` from `<artifacts_dir>/local/.config`, which the page
   shows and cannot change. A page that will take approvals needs that identity first: when it is
   missing, ask the identity card before rendering.
3. **Start the helper in the background** with `run_in_background`, writing to a new decisions file:

   ```
   bash "<plugin_root>/scripts/py" serve_page.py <file.html> --decisions "<artifacts_dir>/local/tmp/agami-page-<ts>.json" --minutes 30
   ```

   Wait for its first line with the Monitor tool (an until-loop on the task's output file that stops
   at the first JSON line or when the task exits; never a foreground `sleep`), or, where Monitor isn't
   available, re-read the output file at spaced intervals, each read its own tool call. The first line
   is `{"url": "http://127.0.0.1:<port>/<token>/", "expires_in": 1800}`, usually within a second. No
   line within about 15 seconds, or the task exited: the helper couldn't start (a refused port, say);
   say so in one line with its last stderr line, and carry on with the cards.
4. **Post the link**, then a card so it isn't missed: *"[Review on a page](<url>). Mark your decisions
   there, press Save decisions, then come back here and press I've saved it. The link lasts 30
   minutes."* Then one question (header `Page`), *"Save your decisions on the page, then come back
   here."*: **I've saved it** (or the skill's wording for its page; the saved page tells the person
   to press it) / **Back to the cards**. Assume an open card is not interrupted when the helper
   exits; if the exit does bring you back first, go to step 5 and don't ask the card again.
   - **I've saved it** → read the helper's output. Its final line is there → step 5. None yet → say
     the page hasn't been saved, and ask the `Page` card again.
   - **Back to the cards** → read the helper's output **first**, before stopping anything. A `saved`
     line means the page was saved before the person went back: say so, and ask one question (header
     `Saved page`), *"The page was saved before you went back. Apply what it saved?"*: **Apply it**
     (step 5) / **Discard it** (delete the decisions file; nothing changes). No final line yet →
     stop the helper with TaskStop (nothing is saved after that), then ask the card the page was
     picked on again.
5. **Read the helper's final line.** `{"status": "saved", "decisions": "<file>"}`: apply the file with
   the skill's commands, the same ones a card uses (its parser, `sm curate`, `sm add-example`,
   `sm remove-example`, with the saved identity as `--signer` / `--role`), never by hand-editing a
   YAML. Then say back in one line what changed, counted from the commands' own output (*"Approved 18
   metrics and hid 6 columns."*), list anything they skipped or refused, delete the decisions file,
   and carry on where the skill says. `{"status": "expired"}`: *"The page expired before it was saved;
   nothing changed."*, then the card it was picked on again.

## 7. Where AskUserQuestion is unavailable

In `claude -p`, or the Agent SDK without the tool, ask the same question as a short numbered list in
text and accept a number or words. The options and their order stay the same.

## 8. Plain voice

Sentence case, no exclamation marks. Say "database" rather than "profile". Name things by what they
do. No hype.
