# The overview card

> `<plugin_root>`: the plugin's absolute path, see [invocation-conventions.md](invocation-conventions.md).

A picture of the whole model in one glance: one tile per subject area, and one for what needs
sign-off. agami-connect shows it in its summary (Phase 7) and agami-model in its overview (Phase 1).
It is a snapshot in the chat, not a page: the complete browse stays **Show the full view**
([question-cards rule 6](question-cards.md#6-no-page-opens-by-default-and-nothing-is-pasted-back)).
It is the "summary of 4 to 5 numbers" row of [native-elements.md](native-elements.md): the
rules for any inline visual there ([Inline visuals](native-elements.md#inline-visuals)) apply to
each tile, and this file sets how many tiles the card has (below).

## When

Only where the host gives you a tool that draws a visual inline in the chat (an SVG or HTML
widget; in the Claude app it is called `show_widget`). Where there is none, or it fails, show the
same numbers as the markdown table at the end of this file. Never open a page for it.

## The numbers: from `sm overview`, never added up by you

```
bash "<plugin_root>/scripts/sm" overview "<artifacts_dir>/<profile>"
```

It prints `areas` (each: `plain_name`, `tables`, `columns`, `described_pct`, `metrics`,
`metrics_waiting`, `joins`, `joins_waiting`, `examples`) and `totals` (the same, plus
`needs_signoff` and `can_wait`). Every number on the card comes from that output as printed. Don't
compute a total, a percentage or a count yourself, and don't read the model's YAML for one: a card
that shows a wrong number is worse than no card.

## What it shows

- **A header line:** the database's name in words, then `totals`: *"84 tables · 71% of columns
  described · 12 metrics · 40 joins · 22 examples"*.
- **A "Needs your sign-off" tile, first, when `needs_signoff` > 0:** the count, and *"Answers that
  use these carry a warning until you approve them."* When `can_wait` > 0, a smaller line: *"<n>
  joins and entities can wait; they approve as you query."*
- **One tile per area, at most 8**, ordered by `tables` (largest first): the area's `plain_name`,
  then `tables` tables, `described_pct`% described, `metrics` metrics (with *"<metrics_waiting>
  waiting"* when not 0), `joins` joins, `examples` examples. More than 8 areas: the 7 largest and a
  tile *"<n> more areas"*.
- **Names in words** ([question-cards rule 3b](question-cards.md#3b-things-are-named-in-plain-words)):
  the tile title is `plain_name`, never the identifier.
- Plain and readable in light and dark: no colour that carries meaning on its own; a waiting count
  is said in words.

## What a click does

Every tile sends a message into the chat, which carries on there with tables and cards:

| Tile | Sends |
|---|---|
| An area | *"Show me the <plain_name> area"*: its tables as a markdown table, then the next card |
| Needs your sign-off | *"Sign off what's waiting"*: the sign-off cards |
| <n> more areas | *"Show me all the subject areas"*: every area as a markdown table |

Where the tool has no way to send a message, leave the tiles plain: the card that follows offers
the same next steps.

## The fallback table

```
| Area | Tables | Described | Metrics (waiting) | Joins | Examples |
|---|---|---|---|---|---|
| Sales | 21 | 93% | 7 (2) | 19 | 10 |
```

Then the totals line and the sign-off line, as above.
