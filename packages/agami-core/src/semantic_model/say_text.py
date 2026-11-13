"""Model-derived text made safe to put in a `say`: the markdown an assistant posts verbatim.

A model folder can come from anyone, and an assistant posts `say` as written, so text taken from the
model (a question, a result value, an area or datasource name, a loader error) must stay visibly
data: one line, capped, and either inside a table cell or in quotes. It can never add a line, a
heading or a table row to what is posted.
"""

from __future__ import annotations

import unicodedata

# The longest piece of model text one `say` carries: enough to recognise it, too short to hold a
# second instruction.
CAP = 80

# Characters that start or end markdown formatting in running text.
MARKDOWN_CONTROL = "\\*_`#[]<>|"


def one_line(text: str, cap: int = CAP) -> str:
    """`text` with every run of whitespace (line breaks included) collapsed to one space, control
    and format characters dropped, cut to `cap` characters with a trailing "…"."""
    kept = "".join(" " if c.isspace() else c for c in str(text)
                   if c.isspace() or unicodedata.category(c) not in ("Cc", "Cf"))
    flat = " ".join(kept.split())
    return flat if len(flat) <= cap else flat[:cap - 1] + "…"


def cell(text: str, cap: int = CAP) -> str:
    """One table cell: one line, capped, with `\\` escaped before `|` (an unescaped `\\` in front of
    an escaped `|` would turn it back into a column break)."""
    return one_line(text, cap).replace("\\", "\\\\").replace("|", "\\|")


def quoted(text: str, cap: int = CAP) -> str:
    """Model text in running prose: one line, capped, in straight double quotes (an inner double
    quote becomes a single one, so the quotes always close where the data ends)."""
    return '"' + one_line(text, cap).replace('"', "'") + '"'


def plain(text: str, cap: int = CAP) -> str:
    """Model text inside engine formatting (the summary's bold first line): one line, capped, with
    every markdown control character escaped, so it can neither end the bold nor start a link."""
    flat = one_line(text, cap)
    return "".join("\\" + c if c in MARKDOWN_CONTROL else c for c in flat)


def count(n: int, singular: str, plural: str) -> str:
    """`n` and the noun, singular for exactly one. The plural is given, never guessed."""
    return f"{n} {singular if n == 1 else plural}"
