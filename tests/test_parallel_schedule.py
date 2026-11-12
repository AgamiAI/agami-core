"""agami-connect runs subagents in the background while the person answers cards.

One schedule (SKILL.md's "What runs at the same time") says what runs while the person is on which
cards; `shared/parallel-work.md` says how a batch is started, collected and given up on. Subagents
only read and return data: the main chat writes, runs every command and asks every question. With
no Agent tool the same work is done inline, so the model comes out the same.
"""

from __future__ import annotations

import re

from skill_docs import PLUGIN, SKILLS_DIR

SKILL = SKILLS_DIR / "agami-connect" / "SKILL.md"
REFS = SKILLS_DIR / "agami-connect" / "references"
PARALLEL = PLUGIN / "shared" / "parallel-work.md"
INLINE = "with no Agent tool the same work is done inline, in this order"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _schedule() -> str:
    text = _read(SKILL)
    start = text.index("## What runs at the same time")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i + len(start))]


def test_the_schedule_names_every_agent_the_plugin_ships():
    section = _schedule()
    shipped = {p.stem for p in (PLUGIN / "agents").glob("*.md")}
    named = set(re.findall(r"`(agami-[a-z-]+)`", section)) - {"agami-connect"}
    assert shipped, "no agents found"
    assert shipped <= named, shipped - named
    assert named <= shipped, named - shipped
    assert "[shared/parallel-work.md](../../shared/parallel-work.md)" in section
    assert "| While the person is on | These run in the background |" in section


def test_parallel_work_rules():
    text = _read(PARALLEL)
    assert len(text.splitlines()) < 80
    assert "run_in_background: true" in text
    assert "wait-files" in text and "--wait 60" in text and "all_ready" in text
    assert "at most 4" in text
    assert "no Agent tool at all" in text and "inline" in text
    low = text.lower()
    assert "never" in low
    for rule in ("writes the model", "runs a command", "asks the person"):
        assert rule in text, rule


def test_enrichers_start_from_two_chunks():
    text = _read(REFS / "enrich.md")
    assert 'enrich-input "$ROOT" --chunks' in text
    assert "2 or more chunks" in text
    assert "more than 30 tables" not in text
    assert re.search(r'enrich-input "\$ROOT" --area <area> --tables <t1,t2,…>', text)
    assert re.search(r'apply-enrichment "\$ROOT" --area <area> --tables <t1,t2,…>', text)


def test_enrichers_start_before_the_join_cards():
    text = _read(REFS / "build.md")
    validator = text.index("The validator gates the write")
    start = text.index("**Start the enrichers now")
    cards = text.index("**Join candidates (tested, not written).**")
    assert validator < start < cards
    # the step marks don't move: build stays in progress until build.md ends
    assert text.index("--mark build=done") > cards


def test_example_writers_start_with_sign_off_and_never_write():
    review = _section(_read(REFS / "review.md"), "## Phase 4", "**4b")
    launch = review.index("agami-example-writer")
    first_card = review.index("**Sign-offs (when `preseed_count` > 0).**")
    assert launch < first_card
    assert "agami-examples-<area>.candidates.json" in review
    assert 'sm" bundle "$ROOT" --area <area> >' in review
    examples = _read(REFS / "examples.md")
    five_a = _section(examples, "**5a", "**5b+5c")
    assert "wait-files" in five_a
    assert "rejected in sign-off" in five_a
    after = examples[examples.index("**5b+5c"):]
    assert after.count('sm" seed-examples "$ROOT"') == 1


def test_no_agent_tool_means_inline():
    assert INLINE in _read(SKILL)
    assert "no Agent tool at all" in _read(PARALLEL)


def test_connect_skill_stays_under_500_lines():
    assert len(_read(SKILL).splitlines()) < 500


def test_the_sample_copy_marks_the_build_done():
    text = _read(REFS / "sample.md")
    six_a = _section(text, "**6A (copy", "**6B")
    assert 'steps "$ROOT" --mark build=done' in six_a
