"""Long text ends its turn: a table, a list or a summary is the last thing in its message.

Ported from #662. Prose over about 200 characters written before a card or another tool call in
the same message reaches the person as a one-sentence summary in Claude apps, so the table or the
summary never arrives. The rule is stated once, in shared/long-text.md, and each skill's
last-message shape follows it: onboarding ends on one closing message, an answer ends on a
typed-reply line, a sign-off list ends on "approve all / review each / later".
"""

from __future__ import annotations

from pathlib import Path

from skill_docs import PLUGIN, SKILLS_DIR

SHARED = PLUGIN / "shared"
LONG_TEXT = SHARED / "long-text.md"
CARDS = SHARED / "question-cards.md"
QUERY = SKILLS_DIR / "agami-query" / "SKILL.md"
CONNECT = SKILLS_DIR / "agami-connect" / "SKILL.md"
MODEL = SKILLS_DIR / "agami-model" / "SKILL.md"


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _between(path: Path, start: str, end: str) -> str:
    text = path.read_text(encoding="utf-8")
    i = text.index(start)
    return text[i : text.index(end, i + len(start))]


def test_the_rule_is_stated_once_with_its_reason():
    flat = _flat(LONG_TEXT)
    assert "**Long text ends its turn.**" in flat
    assert "can run past about 200 characters (a table, a list, a summary)" in flat
    assert "no card and no other tool call follows it in the same message" in flat
    assert "does not reach the person (they get a one-sentence summary of it)" in flat
    assert 'Reply "approve all", "review each" or "later".' in flat


def test_question_cards_points_at_it_and_a_preview_never_holds_what_decides():
    flat = _flat(CARDS)
    assert "**Long text ends its turn** ([long-text.md](long-text.md))" in flat
    assert "Nothing a person needs to answer a card lives only in an option's preview" in flat
    text = CARDS.read_text(encoding="utf-8")
    rule_3 = text[text.index("## 3. ") : text.index("## 3a. ")]
    assert "A result is chat output, and it ends its turn" in rule_3
    rule_4 = text[text.index("## 4. ") : text.index("## 5. ")]
    assert "chat table above" not in rule_4
    assert "never above the card in its own message" in " ".join(rule_4.split())


def test_a_question_answered_from_other_ends_its_turn_when_it_is_long():
    text = CARDS.read_text(encoding="utf-8")
    rule_5a = " ".join(text[text.index("### 5a. ") : text.index("## 6. ")].split())
    assert "anything longer (SQL, rows) ends its turn as text" in rule_5a


def test_an_answer_ends_on_a_typed_reply_line_with_no_card_after_it():
    text = QUERY.read_text(encoding="utf-8")
    p4 = text[text.index("## Phase 4:") : text.index("## Phase 5:")]
    order = next(line for line in p4.splitlines() if "4a Approach" in line)
    assert order.index("4f Numbered follow-ups") < order.index("the reply line (4d.2)")
    assert "AskUserQuestion" not in order
    section = _between(QUERY, "### 4d.2 ", "### 4e ")
    assert "**Never call AskUserQuestion after an answer in the same turn.**" in section
    assert 'Does this look right? Reply "looks right"' in section
    assert "always the answer's last line" in " ".join(section.split())
    follow = _between(QUERY, "### 4f ", "### 4g ")
    assert 'Does this look right? Reply "looks right"' in follow
    star = _between(QUERY, "### 4e.5 ", "### 4f ")
    assert "never in the same turn as the answer" in " ".join(star.split())


def test_the_closing_message_is_the_last_thing_onboarding_says():
    section = _between(CONNECT, "## Phase 7:", "## Phase 8:")
    sec = " ".join(section.split())
    assert "The closing message ends onboarding's last turn, and no card follows it" in sec
    assert "[long-text.md](../../shared/long-text.md)" in sec
    assert "AskUserQuestion" not in section.split("Then Phase 8's list", 1)[1]
    assert 'sm" summary "<root>"' in section
    assert "which ends with the reply line" in sec
    eight = _between(CONNECT, "## Phase 8:", "## Phase 8.5:")
    assert "This is the end of the closing message, not a message of its own" in eight
    assert "AskUserQuestion" not in eight


def test_a_sign_off_list_ends_its_turn_on_typed_replies():
    flat = _flat(MODEL)
    assert 'Reply "approve all", "review each" or "later"' in flat
    section = _between(MODEL, "## Phase 2:", "**Rule 1 guard.**")
    assert "**The list ends the turn**" in section
    assert "No card follows the list" in section
    overview = _between(MODEL, "## Phase 1:", "## Phase 2:")
    assert "No card follows the overview: it ends the turn (" in " ".join(overview.split())


def test_connect_never_asks_a_card_after_a_table_in_the_same_message():
    """The sign-off, the join list, the schema summary, the seed results and the hand-off each end
    their turn on a typed-reply line; none is followed by a card in the same message."""
    flat = _flat(CONNECT)
    signoff = _between(CONNECT, "**Sign-offs (when `preseed_count` > 0).**", "**One by one.**")
    joins = _between(CONNECT, "**Show every join before asking", "Never accept them all unasked")
    tables = _between(CONNECT, "**Summarise it in the chat, ending the turn on a reply line**", "### 1.6s ")
    examples = _between(CONNECT, "**Show what ran, then a reply line**", "**Add my own** is in the reply line")
    handoff = _between(CONNECT, "### 0a.9 ", "### 0a.10 ")
    for name, text in (("sign-off", signoff), ("joins", joins), ("tables", tables),
                       ("examples", examples), ("hand-off", handoff)):
        assert "Then call AskUserQuestion" not in text, name
        assert "Reply " in text, name
    assert 'Reply "approve all", "review each" or "later"' in " ".join(signoff.split())
    assert 'Reply "add the <k> recommended", "review each" or "add none".' in " ".join(joins.split())
    assert "the table ending its turn on the same reply line" in flat
    assert "then the card" not in " ".join(joins.split()).lower()
