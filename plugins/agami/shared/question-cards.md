# Question cards: the rules every skill follows

Every agami decision is a card in the chat (Claude's AskUserQuestion tool), every result is a
table in the chat, and a page opens only when the person asks for one. Link this file from your
skill; do not copy it. `<plugin_root>` is defined at the top of `invocation-conventions.md`.

**Long text ends its turn** ([engine-says.md](engine-says.md)): a card is asked only at the start
of a turn or after a line or two, never after a table, a list or a summary in the same message.

**Nothing a person needs to answer a card lives only in an option's preview**: some apps show a
preview only under the option already picked, as plain grey text, and none on a multi-select, so it
goes in the question or the options' descriptions, or, when it is a real table or list, ends its own
turn as text with a typed-reply line; a preview only repeats or expands what is already visible.

A card holds 1 to 4 questions. Each question has 2 to 4 options, plus the automatic "Other", and
may allow several picks (`multiSelect`). `header` is at most 12 characters. Give each question a header no other
question in the same flow uses: two different questions both headed "Database" read as the same
question asked twice.

## 1. A decision is a card, never prose

Whenever the person must choose, call AskUserQuestion. Do not write "reply with 1, 2 or 3". The
one exception is a choice about long text the person must read first: that text ends its turn with
the choices as typed replies in one line (rule 3).

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

## 3. A result is chat output, and it ends its turn

Show a short markdown table (at most 15 rows, and say how many more there are) and the one or two
sentences that matter. A table, a list or a summary is the last thing in its turn: what to do about
it is one line of typed replies at its end (*Reply "approve all", "review each" or "later".*),
handled as a card's options would be, or a card asked at the start of the next turn. A card may
follow a result only when the result is a line or two (a count, one name).

```
| Table     | Rows   |
|-----------|--------|
| orders    | 12,408 |
| customers |  1,950 |

Showing 2 of 2 tables.      (or: "Showing 15 of 42 tables; 27 more.")
```

## 3a. The question and the descriptions carry what decides; a preview only expands it

A card's question is plain text: keep it to the decision, in plain words ("Sign off ELA proficiency rate?", not an identifier like `ela_proficiency_rate`), with no result table in it. What the person needs to decide goes where it is always shown: a short list of names in the question (*"What currency are these 3 money columns in: Amount on Orders, Price on Products, Amount on Refunds?"*), the meaning or a value in the option's description. A preview is drawn only under the option already picked in some apps, so it may only repeat or expand what is visible: the identifier, the SQL of a definition the description states in words (laid out over lines, a short comment on each filter), every name of a list the description summarises. Anything the person signs off that runs SQL shows that SQL this way on the option that keeps it. What does not fit the question or a description (a table, a list of more than about six names) ends its own turn as text with a typed-reply line instead of a card (rule 3).

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
the names of anything to treat differently. **A group option's description names the items it
covers** (up to about six, then "and N more"), and an option such as "Add all 16" or "Only 1–8" with
nothing to show which items those are leaves the person nothing to decide by; its `preview` may list
every one. When the items themselves must be read to decide, they end their own turn as a table with
a typed-reply line (rule 3), never above the card in its own message. When items must be decided one by one, ask up to 4 per
card, one question each, and put "Approve all remaining" (or its equivalent) on every card so a long
queue can be finished in one press.

```json
{"header": "Metric 1/9", "question": "Sign off Revenue?",
 "multiSelect": false,
 "options": [{"label": "Approve", "description": "Total of the order amounts, refunds excluded",
              "preview": "-- revenue\nSUM(orders.amount)"},
             {"label": "Approve all remaining", "description": "Sign off the other 8 as well"},
             {"label": "Reject", "description": "Drop it from the model"}]}
```

## 5. Free text goes in "Other"

A definition change, a host name, a reworded question: the person types it in "Other". Never ask
them to type a reply message.

A value the person must type (an email, a host) is asked for in the question text, *"Which email
should sign-offs be recorded under? Type it in Other, or decide later."*, never offered as an option
such as "Type it in Other": people click that option and nothing gets typed. The options are the
alternatives to typing: a value the engine already knows on this computer (git's email, from
`sm identity`), which the person picks or not, and a way to decide later ("Don't sign anything
yet").

### 5a. A question typed in "Other" is answered, then the same card is asked again

People use "Other" to ask before they decide: "show me the SQL", "show me the rows", "why do I
need this?". That is not a change, a stop, or something you can't place. Answer it, then ask the
same card again, with only the questions still open. The answer must reach the person: a line or
two goes before the re-asked card; anything longer (SQL, rows) ends its turn as text, with the
card's choices as a typed-reply line at its end, and nothing after it (rule 1's exception):

- **The SQL**: the item's SQL (its binding for a metric, the statement for an example or an
  answer), laid out over lines, with the tables it reads.
- **The rows or values**: up to 15 rows of the result, or of a breakdown behind a single number
  (by its most telling column), run through the same guarded path the skill already uses.
- **Why**: one or two sentences, before the card, on what the step is for and what happens if they
  skip it.

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
