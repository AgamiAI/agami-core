"""agami-query answers in the chat, says how the answer was reached, and asks on a card.

The HTML report is rendered only when the person asks for a chart, and it is served from localhost.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILL = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "agami"
    / "skills"
    / "agami-query"
    / "SKILL.md"
)


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    text = _text()
    i = text.index(start)
    return text[i : text.index(end, i + len(start))]


def _phase_4() -> str:
    return _between("## Phase 4:", "## Phase 5:")


def _how_answered() -> str:
    return _between("### 4d.1 ", "### 4d.2 ")


def _answer_card() -> str:
    return _between("### 4d.2 ", "### 4e ")


def _chart() -> str:
    return _between("### 4e ", "### 4e.5 ")


def _before_chart() -> str:
    """Phase 4 up to the optional chart: the default path."""
    p4 = _phase_4()
    return p4[: p4.index("### 4e ")]


def test_skill_links_the_card_conventions():
    assert "shared/question-cards.md" in _text()


def test_order_puts_the_receipt_and_the_card_before_the_follow_ups():
    p4 = _phase_4()
    order = next(line for line in p4.splitlines() if "4a Approach" in line)
    for step in ("4d Table", "4d.1 How this was answered", "4d.2 Answer card", "4f"):
        assert step in order, step
    assert order.index("4d.2") < order.index("4f")
    assert "HTML report path" not in order


def test_how_this_was_answered_comes_from_sm_receipt():
    s = _how_answered()
    assert "How this was answered" in s
    assert 'sm" receipt "$ROOT"' in s
    for part in ("definitions", "signed off", "tables", "filters", "check"):
        assert part in s.lower(), part
    assert "undetermined" in s


def test_answer_card_has_the_four_options():
    s = _answer_card()
    assert "AskUserQuestion" in s
    for label in ("Looks right", "Something's off", "Show as a chart", "Sign off the definitions used"):
        assert label in s, label


def test_sign_off_option_shows_only_when_a_metric_is_unsigned():
    s = _answer_card()
    row = next(line for line in s.splitlines() if "**Sign off the definitions used**" in line)
    # The condition itself: the receipt names a metric whose review_state is not approved.
    assert re.search(
        r"shown only when the receipt names an unsigned metric \(a model metric in `columns\.items` "
        r"whose `review_state` isn't `approved`\)",
        row,
    ), row


def test_the_query_is_logged_before_the_answer_card_is_asked():
    """agami-save-correction reads the last ok log line for its question and SQL; asked first, the card
    would hand a correction to the previous query."""
    p4 = _phase_4()
    order = next(line for line in p4.splitlines() if "4a Approach" in line)
    assert order.index("log the query") < order.index("4d.2 Answer card")
    card = _answer_card()
    assert card.index("**Log the query first.**") < card.index("call AskUserQuestion")
    phase_5 = _between("## Phase 5:", "## Phase 6:")
    assert "before the answer card (4d.2)" in phase_5
    assert "never again at the end of the turn" in phase_5


def test_something_off_names_the_question_and_the_sql():
    off = next(line for line in _answer_card().splitlines() if line.startswith("| Something's off"))
    assert "this answer's question" in off and "the SQL that ran for it" in off


def test_dont_sign_anything_yet_continues_to_the_star_ask_and_follow_ups():
    sign = next(line for line in _answer_card().splitlines() if line.startswith("| Sign off the definitions used"))
    assert "Don't sign anything yet" in sign
    assert "4e.5" in sign and "4f" in sign


def test_card_answers_hand_to_the_right_skill():
    s = _answer_card()
    looks = next(line for line in s.splitlines() if line.startswith("| Looks right"))
    assert "agami-save-golden" in looks
    off = next(line for line in s.splitlines() if line.startswith("| Something's off"))
    assert "agami-save-correction" in off and "Other" in off
    chart = next(line for line in s.splitlines() if line.startswith("| Show as a chart"))
    assert "4e" in chart
    sign = next(line for line in s.splitlines() if line.startswith("| Sign off the definitions used"))
    assert 'sm" curate' in sign and "--signer" in sign


def test_unparseable_other_is_reasked_never_guessed():
    s = _answer_card()
    assert "never guess" in s.lower()
    assert "same card again" in s.lower()


def test_default_path_renders_no_report_and_opens_no_page():
    default = _before_chart()
    assert "render_chart.py \\" not in default
    assert "open_in_browser" not in default
    assert "csv_to_sections.py \\" not in default


def test_chart_is_rendered_and_served_from_localhost_only_on_request():
    s = _chart()
    assert "render_chart.py" in s and "csv_to_sections.py" in s
    assert "serve_page.py" in s and "run_in_background" in s
    assert "localhost" in s
    assert "Show as a chart" in s
    assert re.search(r"asks? for a chart|make that a chart|--chart", s)
    assert "open_in_browser" not in s


def test_no_report_page_is_opened_with_open_in_browser():
    # The only browser hand-off left is the one-time GitHub star link, a web address, not a page.
    for line in _text().splitlines():
        if "open_in_browser" in line:
            assert "github.com/AgamiAI/agami-core" in line, line


def test_reopen_last_chart_serves_the_file():
    s = _between("### 2a.1 ", "### 2b ")
    assert "serve_page.py" in s
    assert "open_in_browser" not in s


def test_receipt_file_is_still_mandatory_for_the_chart():
    s = _chart()
    assert "--receipt-file" in s and "MANDATORY" in s


def test_five_numbered_follow_ups_stay_as_text_after_the_card():
    s = _between("### 4f ", "### 4g ")
    assert "exactly 5 numbered follow-up" in s
    assert "Do not use AskUserQuestion for follow-ups" in s
    assert "after the answer card" in s.lower()


def test_star_ask_and_sample_footer_are_kept():
    text = _text()
    assert "https://github.com/AgamiAI/agami-core" in text
    assert "### 4e.5 " in text and ".optins" in text
    assert ".agami-example-intro" in text
    star = _between("### 4e.5 ", "### 4f ")
    assert "answer card" in star.lower()


def test_chat_table_preview_is_at_most_fifteen_rows():
    p3 = _between("### 3c ", "## Phase 4:")
    assert "first **15** rows" in p3
    assert "first 30" not in _text()


def test_sql_stays_out_of_the_chat():
    assert "### HARD RULE — never echo SQL in chat prose" in _text()
    assert not re.search(r"```sql", _how_answered())


def test_log_records_a_chart_path_only_when_a_chart_was_shown():
    s = _between("## Phase 5:", "## Phase 6:")
    assert "chart_path" in s and "Show as a chart" in s


def _card_headers(text: str) -> list[str]:
    headers = re.findall(r'"header":\s*"([^"]+)"', text)
    # "column header `Avg Outstanding`" in the number-formatting rules is a table heading, not a card.
    headers += re.findall(r"(?<!column )header `([^`]+)`", text)
    return headers


def test_every_card_header_fits_twelve_characters():
    headers = _card_headers(_text())
    assert headers
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


def test_looks_right_on_a_relative_window_asks_the_window_card_first():
    looks = next(line for line in _answer_card().splitlines() if line.startswith("| Looks right"))
    assert "Window" in looks and "Name the window" in looks and "Don't save" in looks
    assert "never re-anchor" in looks.lower()
    for phrase in ("last 30 days", "this month", "YTD"):
        assert phrase in looks


def test_the_star_ask_has_no_emoji_or_exclamation_and_answers_in_the_same_turn():
    s = _between("## Phase 6:", "If `<artifacts_dir>/local/.optins` already exists")
    assert "thank you!" not in s.lower()
    assert not re.search(r"[\U0001F300-\U0001FAFF]", s)
    assert "Next turn" not in s
    assert "same turn" in s
    trigger = _between("### 4e.5 ", "### 4f ")
    assert "End the turn here" not in trigger
