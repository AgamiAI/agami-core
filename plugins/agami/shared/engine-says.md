# Do what the engine says

Some agami commands print two keys beside their data, so the chat says what the model on disk says,
with nothing counted, timed or reworded by hand in between:

- **`say`**: markdown for the chat. When an agami command's output has `say`, post it exactly as
  written (no rewording, no summary of it, nothing before it) as your next chat text. A table stays
  a table and a line stays a line.
- **`then`**: one short sentence saying what to do next. When the output has `then`, do that next.

Never skip either because a later instruction seems to cover it. A phase file that says "post a
progress line", "show the summary" or "refresh the task list" is met by the `say` and the `then`,
not by words of your own beside them or in place of them. A card is never asked while a `say` is
waiting to be posted: post it first, at the top of the message that asks the card when the `then`
says to ask one there. A `say` is never moved into a card's question.

Where they appear:

| Command | `say` | `then` |
|---|---|---|
| `sm steps` | on the mark `first_question=in_progress`, the whole closing summary; when `problem` is set, the problem line | whether to update the task list from `list` |
| `sm progress` | none | whether to post `line`, update the task list, both, or call again |
| `sm seed-validate` with `--out` | the table of the examples that ran | post it and ask the examples card |
| `sm apply-enrichment` | one line for the chunk just applied | post it and refresh the task list |

A command whose output has neither key is read as its phase file says.
