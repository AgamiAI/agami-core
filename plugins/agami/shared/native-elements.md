# Native elements, with a fallback for each

Every agami skill picks the first element in this table that the session offers, and never asks the person to do something a native element could do for them. Link this file from your skill; do not copy it.

| Need | First choice | Otherwise |
|---|---|---|
| A decision | Question card ([question-cards.md](question-cards.md)) | Numbered list (rule 7) |
| Progress through onboarding | The session's task list, from `sm steps` ([task-list.md](task-list.md)) | Markdown checklist, posted again when a step ends |
| A wait over 10 seconds | Background task with a description in the person's words, with progress lines as the phase file's wait loop says | The same command in the foreground; say what is running before it starts and the result when it ends |
| A result of up to 15 rows | Markdown table | Same |
| A summary of 4 to 5 numbers (overview, review groups) | Inline visual with tiles ([overview-card.md](overview-card.md)); a click sends a chat message | Markdown table |
| Anything longer | "Show the full view": a page in the person's browser | The URL (how the page is served: [question-cards rule 6](question-cards.md#6-no-page-opens-by-default-and-nothing-is-pasted-back)) |

## What the session offers

A tool is either in the session's tool list or it is not. Look before choosing; never assume one
is there because another app had it, and never assume one is missing. The Claude Desktop app
offers question cards (AskUserQuestion), a task list, background tasks (Bash with
`run_in_background`) and an inline visual (`show_widget`); a terminal session offers some of
these, and `claude -p` usually none. Never tell the person to install something, or to switch
apps, to see a card: use the fallback and carry on.

## Inline visuals

Three rules for any visual drawn in the chat:

1. **Small.** Each tile: at most 6 numbers, and a click on a tile sends one chat message.
   Besides tile clicks, at most 2 actions. No scrolling inside it. How many tiles a visual has is
   set by its own file ([overview-card.md](overview-card.md) for the overview).
2. **Readable in light and dark.** No colour that carries meaning on its own; a state is said in
   words.
3. **Every number from an `sm` command**, as printed. Never add up, round or estimate one.

A click sends a message into the chat, which carries on there with tables and cards. Where the
tool can't send one, leave the visual plain: the card that follows offers the same next steps.

## Waits

Say what is running, in the person's words (*"Reading your tables"*), and show how much is done
from the engine's own counts, never an estimate of time left. How often to check and what each
progress line says is set by the phase file's wait loop (the build in agami-connect's build.md,
subagents in [parallel-work.md](parallel-work.md)). Where background tasks aren't available, run
the same command in the foreground: say what is running before it starts, and the result when it
ends.

## What a native element never replaces

- **The decision still happens on a card.** A visual, a task list or a page shows; the card (or
  the numbered list) decides. A tile click starts the next card, never a change.
- **The fallback carries the same information.** Whatever a native element shows, the fallback
  shows too, so a person in a terminal or `claude -p` loses nothing but the look.
