"""Name suggestions: which real names a caller most plausibly meant by one the model lacks.

One matcher for every surface that answers a wrong name — `get_datasource_schema`,
`get_prompt_examples`, and the column-scope refusal — so the same typo gets the same suggestion
wherever it is made. A second copy of these rules is how two surfaces come to disagree.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

from .models import bare_name

# difflib's similarity floor for a fuzzy suggestion. Measured on an 80-area, 2,900-table synthetic
# model: 0.6 (difflib's default) suggested something for 17% of unrelated dictionary words; 0.7
# cut that to about 1% with the same recall on single and double typos; 0.75 began losing
# abbreviations (`ord_items` for `order_items`).
FUZZY_CUTOFF = 0.7


def word_set(name: str) -> frozenset[str]:
    """The words of a name, ignoring case, order and separators."""
    return frozenset(w for w in re.split(r"[\s_-]+", bare_name(name or "").casefold()) if w)


def did_you_mean(name: str, candidates: Iterable[str], n: int = 3) -> list[str]:
    """Up to `n` of `candidates` a caller most plausibly meant by `name`, best first.

    An agent's wrong name is nearly always one edit from a real one — a case change, a plural
    dropped, a separator lost, a schema prefix added, words swapped — so those are tried before a
    fuzzy match, which alone ranks `order` nearer `order_items` than `orders`. Nothing close
    returns `[]`: an unrelated suggestion reads as an answer, and is how a guess reaches SQL. For
    the same reason an exact match is returned alone, and the name asked for is never suggested
    back — a suggestion the caller already tried is a loop, not a hint.
    """

    def fold(s: str) -> str:
        # Spaces too: metrics are often named in words (`order count`) and asked for in
        # snake_case (`order_count`).
        return re.sub(r"[\s_-]+", "", bare_name(s or "").casefold())

    asked = bare_name(name or "")
    # Folded once per candidate: this runs over every table in the model, once per missed name.
    pool = {c: fold(c) for c in candidates if c and bare_name(c) != asked}
    want = fold(name)
    if not want:
        return []
    exact = [c for c, f in pool.items() if f == want]
    if exact:
        return exact[:n]
    # The same words in another order (`orders_fact` for `fact_orders`) is as certain as a case
    # change, and neither containment nor a character-level fuzzy match finds it.
    words = word_set(name)
    same_words = [c for c in pool if len(words) > 1 and word_set(c) == words]
    if same_words:
        return same_words[:n]
    # Containment either way, shortest gap first. The length floor stops a two-letter name from
    # matching half the model.
    contains = sorted(
        (c for c, f in pool.items() if len(want) >= 3 and (want in f or f in want)),
        key=lambda c: abs(len(pool[c]) - len(want)),
    )
    by_fold = {f: c for c, f in reversed(pool.items())}  # first occurrence wins
    fuzzy = [
        by_fold[f] for f in difflib.get_close_matches(want, list(by_fold), n=n, cutoff=FUZZY_CUTOFF)
    ]
    return list(dict.fromkeys(contains + fuzzy))[:n]
