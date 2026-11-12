"""agami-model shows an overview in the chat and asks on a card; the explorer page is the full view."""

from __future__ import annotations

import re
from pathlib import Path

SKILLS = Path(__file__).resolve().parent.parent / "plugins" / "agami" / "skills"
SKILL = SKILLS / "agami-model" / "SKILL.md"
CONNECT = SKILLS / "agami-connect" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    text = _text()
    i = text.index(start)
    return text[i : text.index(end, i + len(start))]


def _overview() -> str:
    return _between("## Phase 1:", "## Phase 2:")


def _review() -> str:
    return _between("## Phase 2:", "## Phase 3:")


def _hide() -> str:
    return _between("## Phase 3:", "## Phase 4:")


def _look_up() -> str:
    return _between("## Phase 4:", "## Phase 5:")


def _full_view() -> str:
    return _between("## Phase 5:", "## Phase 6:")


def _default_path() -> str:
    """Everything before the optional full view."""
    text = _text()
    return text[: text.index("## Phase 5:")]


def test_skill_links_the_card_conventions():
    assert "shared/question-cards.md" in _text()


def test_overview_reads_the_engine_and_shows_a_short_table():
    s = _overview()
    assert 'sm" review-items "$ROOT"' in s
    assert 'sm" model-tree "$ROOT"' in s
    assert "15 rows" in s and "more" in s
    assert "AskUserQuestion" in s


def test_overview_card_has_the_four_options():
    s = _overview()
    for label in (
        "Review what needs sign-off",
        "Hide tables or columns",
        "Look up a table or metric",
        "Show the full view",
    ):
        assert label in s, label


def test_default_path_opens_no_page_and_waits_for_no_paste():
    default = _default_path()
    assert "open_in_browser" not in default
    assert not re.search(r"end the turn|paste (it|the block) back|auto-open", default, re.I)
    assert "render_model_explorer.py" not in default


def test_no_page_is_opened_as_a_file_anywhere():
    text = _text()
    assert "open_in_browser" not in text
    assert all("never" in line.lower() for line in text.splitlines() if "file://" in line)


def test_review_reuses_the_connect_review_pattern():
    s = _review()
    assert "agami-connect" in s and "Phase 4" in s
    for label in ("Approve all", "Review one by one", "Skip for now", "Approve all remaining"):
        assert label in s, label
    assert "up to 4" in s
    assert "Approve" in s and "Reject" in s
    # The same wording as agami-connect Phase 4, so the two reviews read as one product.
    connect = CONNECT.read_text(encoding="utf-8")
    for phrase in (
        "asks to stop is a stop, not a change",
        "Any other \"Other\" you can't act on is re-asked, never guessed",
        "Hide these from queries",
        "Keep them all queryable",
        "Flagged and suspected columns are picked the same way",
    ):
        assert phrase in connect and phrase in s, phrase


def test_review_other_is_an_edit_applied_with_curate_then_reasked():
    s = _review()
    assert '"op":"edit"' in s and 'sm" curate' in s
    assert "ask about that item again" in s.lower()
    assert "never hand-edit" in s.lower()


def test_review_reads_suspected_personal_data():
    s = _review()
    assert 'sm" sensitive "$ROOT"' in s
    assert "suspected" in s and "(suspected)" in s


def test_identity_card_is_asked_once_and_never_inferred():
    text = _text()
    assert "reviewer_email" in text and "reviewer_role" in text
    assert "chmod 600" in text
    assert "never infer" in text.lower()
    assert "you@example.com" in text
    assert "Don't sign anything yet" in text
    # The identity is a card now, not a typed reply.
    assert "Reply like:" not in text


def test_hide_asks_with_grouped_multi_picks_and_excludes_with_curate():
    s = _hide()
    assert "multiSelect" in s
    assert "group" in s.lower()
    assert '"op":"exclude","kind":"table"' in s and '"column"' in s
    assert 'sm" curate' in s
    assert "Other" in s
    assert "never guess" in s.lower()


def test_look_up_takes_a_name_in_other_and_prints_description_columns_caveats():
    s = _look_up()
    assert "Other" in s
    for word in ("description", "columns", "caveats"):
        assert word in s.lower(), word
    assert 'sm" context "$ROOT"' in s
    assert "never guess" in s.lower()


def test_full_view_renders_the_explorer_and_serves_it_from_localhost():
    s = _full_view()
    assert "render_model_explorer.py" in s
    assert "serve_page.py" in s
    assert "run_in_background" in s


def test_full_view_still_accepts_its_pasted_block():
    s = _full_view()
    assert "parse_model_feedback.py" in s
    assert "Generate feedback for Claude" in s


def test_model_writes_go_through_sm_only():
    text = _text()
    assert "Write the ops array to the file with the Write tool" in text
    assert 'sm" curate "$ROOT"' in text


def test_plain_voice_says_database_not_profile_in_cards():
    for section in (_overview(), _review(), _hide(), _look_up()):
        for q in re.findall(r'\*"([^"]+)"\*', section):
            assert "profile" not in q.lower(), q


def _card_headers(text: str) -> list[str]:
    headers = re.findall(r'"header":\s*"([^"]+)"', text)
    headers += re.findall(r"header `([^`]+)`", text)
    headers += re.findall(r"`((?:Metric|Entity|Join) \d+/\d+)`", text)
    return headers


def test_every_card_header_fits_twelve_characters():
    headers = _card_headers(_text())
    assert len(headers) >= 5
    too_long = [h for h in headers if len(h) > 12]
    assert not too_long, too_long


def _option_lists(text: str) -> list[list[str]]:
    lists = [re.findall(r'"label"', m) for m in re.findall(r'"options":\s*\[(.*?)\]', text, re.S)]
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "AskUserQuestion" not in line or not line.rstrip().endswith(":"):
            continue
        bullets, j = [], i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        while j < len(lines) and lines[j].startswith("- **"):
            bullets.append(lines[j])
            j += 1
        if bullets:
            lists.append(bullets)
    return lists


def test_no_card_lists_more_than_four_options():
    lists = _option_lists(_text())
    assert lists
    too_many = [opts for opts in lists if len(opts) > 4]
    assert not too_many, too_many


def test_the_numbered_list_fallback_is_stated():
    assert "Where AskUserQuestion is unavailable, ask the same as a short numbered list" in _text()


def test_a_review_started_from_connect_returns_to_its_starter_questions():
    assert "agami-connect" in _review() and "Phase 8" in _review()


def test_review_cards_offer_a_page_that_saves_on_every_question() -> None:
    # Tester feedback: the explorer opened only when "show the full view" was typed in Other. Then
    # (#651): the page was offered on the first question of a card only, and could not take a decision.
    review = _review()
    assert review.count("- **Review on a page**") == 2      # the Sign-off card and the item cards
    assert "| Review on a page | Phase 5 with `initial_tab=review`, then this card again, recounted. |" in review
    assert "Offer it on every question of the card, not only the first" in review
    assert "first question only" not in _text()
    assert "Then ask again the card it was picked on" in _full_view()


def test_the_full_view_saves_through_the_helper_and_applies_with_the_same_commands() -> None:
    s = _full_view()
    assert "--save-to-helper" in s and "--read-only" not in s
    assert "serve_page.py" in s and "--decisions" in s
    assert "question-cards.md#6a-a-page-that-saves-its-decisions" in s
    assert "parse_model_feedback.py --block-file" in s
    for deep_link in ("`metrics`, `entities`, `joins`", "`pii` from the personal-data question"):
        assert deep_link in s, deep_link
    # who signs is the saved identity, never what a page sends
    assert "the signer is always the saved `reviewer_email` / `reviewer_role`" in s
    # the say-back counts what the commands applied, not what the page claimed
    assert "never from the page's own count" in s


def test_a_bulk_sign_off_lists_what_it_signs_first() -> None:
    review = _review()
    assert "a sign-off is never recorded for a definition the person hasn't seen" in review
    assert '"bulk":true' in review
    assert "list the ones the table above didn't show first" in review


def test_the_personal_data_question_offers_the_page_when_the_list_is_long() -> None:
    review = _review()
    assert "**Review on a page** (Phase 5 with `initial_tab=pii`" in review


def test_cards_name_tables_and_columns_in_words() -> None:
    text = _text()
    assert "**Email on Customers**" in text and "`customers.email`, or" not in text
    assert "item's `plain_name`" in text
    assert "**Orders (5: Orders, Order items, …)**" in _hide()
