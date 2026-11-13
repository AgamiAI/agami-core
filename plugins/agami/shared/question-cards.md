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
queue can be finished in one press.

```json
{"header": "Metric 1/9", "question": "Sign off Revenue?",
 "multiSelect": false,
 "options": [{"label": "Approve", "description": "Total of the order amounts",
              "preview": "-- revenue\nSUM(orders.amount)"},
             {"label": "Approve all remaining", "description": "Sign off the other 8 as well"},
             {"label": "Reject", "description": "Drop it from the model"}]}
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

The HTML renderers stay as an optional **Show the full view** option, and it is an option: every
review or sign-off card that has a page behind it offers **Show the full view** while it has room
(a card holds 4 options), so nobody has to know to type it in "Other". After the page is served,
give the URL and ask the same card again: the decision still happens on the card. When one is shown, serve it
from `localhost`, never as a `file://` page: the Code tab's built-in browser shows a local file
outside the project folder as a frozen snapshot. Run `serve_page.py` with `run_in_background`: it
keeps serving until its time is up, so in the foreground it would hold the turn and the URL would
never reach the person. (The page is the last row of [native-elements.md](native-elements.md).)

Always pass `--open`, so the page opens in the person's own browser (Chrome, Safari, Edge…), not in
the Desktop app's narrow built-in pane, where a clicked link would land. Give the URL either way: with
`"opened": true` say it opened in their browser (*"Opened in your browser: <url>"*); with `false` (a
headless or remote machine) say to open the link (*"Open this in your browser: <url>"*).

```bash
bash "<plugin_root>/scripts/py" serve_page.py "<file.html>" --minutes 30 --open
```

Run it with `run_in_background`; its first line is `{"url": "http://localhost:<port>/<name>", "opened": true}`, and it exits when the time is up.

Read that first line from the background task's output and give the person the URL. Render the page with `--read-only`: it hides the page's change controls and its copy-and-paste dialog and says to decide on the cards. Choices come back through a card, not a pasted block.

## 7. Where AskUserQuestion is unavailable

In `claude -p`, or the Agent SDK without the tool, ask the same question as a short numbered list in
text and accept a number or words. The options and their order stay the same.

## 8. Plain voice

Sentence case, no exclamation marks. Say "database" rather than "profile". Name things by what they
do. No hype.
