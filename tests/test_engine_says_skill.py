"""agami-connect posts what the engine hands it, asks fewer card rounds, and shows results as tables.

A scripted re-measure and a browser test found the same things after two rounds of fixes: the
closing summary (overview table, "Set up in …") was run but never posted before the closing card;
the task list was refreshed after only about half of the `sm steps` / `sm progress` calls; the
examples card came with no table of the examples before it; the first answer was one sentence with
no rows. Some `sm` commands now print `say` (markdown to post exactly as given) and `then` (what to
do next). One shared file states the rule; the phase files point at it instead of asking for text of
their own. The same runs showed three intake cards where one had been, and the Areas question on a
card of its own: these checks pin the cards back together.
"""

from __future__ import annotations

import re
from pathlib import Path

from skill_docs import PLUGIN, SKILLS_DIR

SHARED = PLUGIN / "shared"
ENGINE_SAYS = SHARED / "engine-says.md"
CONNECT = SKILLS_DIR / "agami-connect"
REFS = CONNECT / "references"
QUERY = SKILLS_DIR / "agami-query" / "SKILL.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


# 1. one rule, stated once
def test_the_engine_says_rule_is_one_short_shared_file():
    text = _read(ENGINE_SAYS)
    assert len(text.splitlines()) < 40
    flat = _flat(text)
    for part in ("post it exactly as written", "no rewording, no summary of it, nothing before it",
                 "as your next chat text", "do that next",
                 "Never skip either because a later instruction seems to cover it",
                 "A card is never asked while a `say` is waiting to be posted"):
        assert part in flat, part


def test_the_rule_is_linked_from_the_hard_rules_and_the_task_list():
    main = _read(CONNECT / "SKILL.md")
    hard_rules = main.split("### HARD RULES", 1)[1].split("### Preflight", 1)[0]
    assert "](../../shared/engine-says.md)" in hard_rules
    assert "](engine-says.md)" in _read(SHARED / "task-list.md")


# 2. the task list follows `then`
def test_the_task_list_follows_then():
    text = _flat(_read(SHARED / "task-list.md"))
    assert ("After a `sm steps` or `sm progress` call whose `then` says to update the task list, update "
            "it from `list` before anything else; when `changed` is false there is nothing to do.") in text
    assert "the very next thing is the task-list update" not in text


# 3. the closing summary is the `say` of the last mark
def test_the_closing_summary_is_posted_from_say():
    rule = "The closing card is never asked in a message that does not begin with the summary."
    for name in ("summary.md", "sample.md"):
        text = _read(REFS / name)
        flat = _flat(text)
        mark = flat.index('steps "<root>" --mark first_question=in_progress')
        assert mark < flat.index("post its `say` exactly", mark), name
        assert rule in flat, name
        assert 'sm" summary' not in text and 'sm" timing' not in text, name
        assert "sm summary" not in text and "sm timing" not in text, name
        assert "closing sentence" not in flat.replace("no closing sentence of your own", ""), name


def test_the_closing_card_comes_after_the_mark_in_the_same_message():
    summary = _read(REFS / "summary.md")
    mark = summary.index('steps "<root>" --mark first_question=in_progress')
    assert mark < summary.index("Then **AskUserQuestion**: `Review what needs sign-off`")
    sample = _read(REFS / "sample.md")
    assert sample.index('steps "<root>" --mark first_question=in_progress') < \
        sample.index("**A card of starter questions")


# 4. the examples table comes from seed-validate
def test_the_examples_table_is_the_say_of_seed_validate():
    text = _read(REFS / "examples.md")
    flat = _flat(text)
    show = flat.index("post its `say` exactly")
    card = flat.index("(header `Examples`)")
    assert show < card
    assert "| # | Question | Rows | Result |" not in text  # no table built by hand
    options = flat[card:].split("| Answer | Do |", 1)[0]
    assert "**Show the full view**" in options


# 5. the first answer shows its rows
def test_the_first_answer_shows_the_table_before_any_sentence_about_it():
    p4 = _read(QUERY).split("## Phase 4: Present", 1)[1].split("### 4a ", 1)[0]
    order = next(line for line in p4.splitlines() if "4a Approach" in line)
    assert order.index("4d Table") < order.index("4c Insight") < order.index("4d.2 Answer card")
    assert "The table is never left out" in _flat(_read(QUERY))


# 6. fewer card rounds
def test_the_intake_questions_share_one_card():
    text = _flat(_read(REFS / "context-and-tables.md"))
    section = text.split("### 1.4 ", 1)[1].split("### 1.5 ", 1)[0]
    assert ("One card, asked right after the connection works and before tables are listed, with up to "
            "three questions on one AskUserQuestion call: the company (once per deployment), what the "
            "person can share (1.5), and this database's one line.") in section
    assert "Three cards" not in text
    # the follow-ups depend on the context answer, so they stay after it
    assert "Ask the follow-up cards one at a time, in the table's order" in text


def test_the_areas_question_rides_on_the_sign_off_card():
    text = _flat(_read(REFS / "review.md"))
    phase3 = text.split("## Phase 3:", 1)[1].split("## Phase 4:", 1)[0]
    assert "is asked on Phase 4's first card" in phase3
    phase4 = text.split("## Phase 4:", 1)[1]
    assert ("The Areas question, the Sign-off question and the personal-data question that apply go on "
            "one card") in phase4


# 7. no silent build, no second validate pass
def test_no_build_promises_a_few_seconds():
    for name in ("build.md", "sample.md"):
        text = _read(REFS / name)
        assert "few seconds" not in text and "~seconds" not in text, name


def test_the_sample_build_follows_the_server_wait_loop():
    text = _flat(_read(REFS / "sample.md"))
    six_b = text.split("**6B (rebuild live", 1)[1].split("**6A** and **6B**", 1)[0]
    assert 'bash "<plugin_root>/scripts/sm" progress "<root>" --wait 20' in six_b
    assert "posting what its `then` says" in six_b


def test_describe_runs_no_separate_validate_pass():
    text = _flat(_read(REFS / "enrich.md"))
    assert "After each enrichment pass, **re-validate**" not in text
    assert "a validator warning is not a reason for another pass" in text


# 8. personal-data card: plain names, identifier in preview
def test_personal_data_options_put_the_identifier_in_preview():
    text = _flat(_read(REFS / "review.md"))
    personal = text.split("**Personal-data columns", 1)[1].split("Hide the picks", 1)[0]
    assert "**Email on Customers**" in personal
    assert "the identifier (`customers.email`) goes in the option's `preview`" in personal
    assert "never in the label or the description" in personal


# 9. chunks of 12 again
def test_chunks_are_12_tables_with_two_from_more_than_8():
    enrich = _flat(_read(REFS / "enrich.md"))
    assert "at most 12 tables" in enrich
    assert "a model of more than 8 tables always gets at least two chunks" in enrich
    assert "one per chunk of up to 12 tables" in _read(CONNECT / "SKILL.md")
    assert "(up to 12 tables of one subject area)" in _read(PLUGIN / "agents" / "agami-enricher.md")
    for doc in [CONNECT / "SKILL.md", *REFS.glob("*.md"), *(PLUGIN / "agents").glob("*.md")]:
        assert not re.search(r"up to 8 tables|at most 8 tables|8 tables or fewer|tables \(8\)",
                             _read(doc)), doc.name


def test_apply_enrichment_and_steps_say_what_to_post():
    enrich = _flat(_read(REFS / "enrich.md"))
    step3 = enrich.split("3. **Collect**", 1)[1].split("4. **Apply each chunk", 1)[0]
    assert "post its `say` exactly and do its `then`" in step3
