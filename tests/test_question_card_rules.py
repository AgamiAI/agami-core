"""Guards for two card rules a live run in the Desktop Code tab showed people tripping over.

- Rule 5: a value to type is asked for in the question text, never offered as an option. An option
  such as "Type it in Other" is clicked with nothing typed.
- Rule 5a: a question typed in "Other" ("show me the SQL", "why?") is answered and the same card
  asked again. Every skill's "re-asked, never guessed" fallback must point at that rule, or the
  question is said back as something that didn't match.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from skill_docs import skill_docs, skill_of  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "agami"
SKILLS_DIR = PLUGIN / "skills"

_TYPE_IN_OTHER_OPTION = re.compile(r"\*\*Type [^*\n]{0,40}? in Other\*\*")
_FALLBACK = re.compile(r"re-asked, never guessed|can't place")
_RULE_5A = (
    "question-cards.md#5a-a-question-typed-in-other-is-answered-then-the-same-card-is-asked-again"
)


def _skills() -> list[Path]:
    return skill_docs()


def test_rule_5a_is_in_the_shared_doc() -> None:
    text = (PLUGIN / "shared" / "question-cards.md").read_text()
    assert (
        '### 5a. A question typed in "Other" is answered, then the same card is asked again' in text
    )


def test_no_option_asks_the_person_to_type_in_other() -> None:
    offenders = [
        f"{skill_of(p)}: {m.group(0)}"
        for p in _skills()
        for m in _TYPE_IN_OTHER_OPTION.finditer(p.read_text())
    ]
    assert not offenders, offenders


def test_every_other_fallback_answers_questions_first() -> None:
    offenders = []
    for p in _skills():
        for paragraph in re.split(r"\n\s*\n", p.read_text()):
            if _FALLBACK.search(paragraph) and _RULE_5A not in paragraph:
                offenders.append(f"{skill_of(p)}: {paragraph[:80]!r}")
    assert not offenders, offenders


def test_no_two_questions_in_a_skill_share_a_header():
    # A live run showed three different questions all headed "Database", read as one asked thrice.
    allowed = {("agami-connect", "Tables")}  # the catalog-denied card and 1.6 never both run
    offenders = []
    for p in _skills():
        headers = re.findall(r"header `([^`]+)`", p.read_text())
        for h in sorted({h for h in headers if headers.count(h) > 1}):
            if (skill_of(p), h) not in allowed and not re.search(r"\d|<", h):
                offenders.append(f"{skill_of(p)}: {h}")
    assert not offenders, offenders


def test_rule_3a_puts_detail_in_the_option_preview():
    text = (PLUGIN / "shared" / "question-cards.md").read_text()
    assert "## 3a. Detail goes in an option's preview, not the question" in text


# A page rendered `--read-only` has no Generate button and no paste-back dialog (rule 6), so the
# passage that renders it must not tell the person to generate a block, paste it, or wait for one.
_PASTE_BACK = re.compile(
    r"end the turn|paste (?:it|them) back|generate the block|block to paste here|button gives the person",
    re.IGNORECASE,
)


def _read_only_passages(text: str) -> list[str]:
    """From each `--read-only` render to the next heading: the passage that offers that page."""
    passages = []
    for m in re.finditer(r"--read-only\b", text):
        nxt = re.search(r"^#{1,4} ", text[m.end() :], re.MULTILINE)
        passages.append(text[m.end() : m.end() + nxt.start()] if nxt else text[m.end() :])
    return passages


def test_no_read_only_page_asks_for_a_pasted_block() -> None:
    offenders = [
        f"{skill_of(p)}: {hit.group(0)!r}"
        for p in _skills()
        for passage in _read_only_passages(p.read_text())
        for hit in _PASTE_BACK.finditer(passage)
    ]
    assert not offenders, offenders


def test_the_paste_back_guard_catches_the_old_wording() -> None:
    old = "--read-only --out x.html\n```\nSay: generate the block and paste it back; then **end the turn**.\n"
    assert _PASTE_BACK.search(_read_only_passages(old)[0])


# A card read "What currency is `widgets.price` in?" (tester feedback on the question-cards branch).
# Digits and more than one dot count too: `orders.total_2024`, `sales.orders.total`.
_IDENTIFIER_IN_QUESTION = re.compile(r'\*"[^"\n]*`[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+`[^"\n]*\?"\*')
# The one quoted question that is about how to write an identifier, so it has to show one.
_ABOUT_THE_SPELLING = "Tables need a subject-area prefix"


def test_rule_3b_names_things_in_plain_words() -> None:
    doc = (PLUGIN / "shared" / "question-cards.md").read_text(encoding="utf-8")
    assert "## 3b. Things are named in plain words" in doc
    for field in ("`label`", "`plain_name`", "flag_rates.labels"):
        assert field in doc, field
    # the doc's own example card no longer puts an identifier in the question
    assert "SUM of orders.amount)?" not in doc


def test_no_quoted_card_question_names_a_table_column_identifier() -> None:
    hits = []
    for path in _skills():
        for line in path.read_text(encoding="utf-8").splitlines():
            for m in _IDENTIFIER_IN_QUESTION.finditer(line):
                if _ABOUT_THE_SPELLING not in m.group(0):
                    hits.append(f"{skill_of(path)}: {m.group(0)[:100]}")
    assert not hits, hits


@pytest.mark.parametrize("line", [
    '*"What currency is `widgets.price` in?"*',
    '*"Is `orders.total_2024` in USD?"*',
    '*"Hide `sales.customers.email`?"*',
])
def test_the_identifier_guard_catches_each_form(line: str) -> None:
    assert _IDENTIFIER_IN_QUESTION.search(line)


def test_the_full_view_is_a_card_option_in_rule_6() -> None:
    doc = (PLUGIN / "shared" / "question-cards.md").read_text(encoding="utf-8")
    six = doc[doc.index("## 6."):doc.index("## 7.")]
    assert "every\nreview or sign-off card that has a page behind it offers **Show the full view**" in six
    three_a = doc[doc.index("## 3a."):doc.index("## 3b.")]
    assert "an example question's statement too" in three_a


def test_rule_4_group_options_show_their_items() -> None:
    """A live run's join card offered "add all 16" and "only 1-8" without the joins themselves."""
    text = (PLUGIN / "shared" / "question-cards.md").read_text(encoding="utf-8")
    rule_4 = text[text.index("## 4. "):text.index("## 5. ")]
    assert "A group option's `preview` lists every item the option" in rule_4
    assert "chat table above" in rule_4
