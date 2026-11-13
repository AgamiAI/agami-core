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
INLINE = "With no Agent tool the same work is done inline, in this order."


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _schedule() -> str:
    text = _read(SKILL)
    start = text.index("## What runs at the same time")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def _flat(text: str) -> str:
    """The text with each run of whitespace as one space, so a test doesn't pin line wrapping."""
    return " ".join(text.split())


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
    # the example run happens before its review card, so the row names the wait, not the card
    assert "| The wait for the example run (6) | `sm seed-validate` as a background task |" in section
    assert "The example review card" not in section


def test_parallel_work_rules():
    text = _read(PARALLEL)
    assert len(text.splitlines()) < 90
    assert "run_in_background: true" in text
    assert "wait-files" in text and "--wait 60" in text and "all_ready" in text
    assert "at most 4" in text
    assert "no Agent tool at all" in text and "inline" in text
    never = _flat(text[text.index("## 6. Never"):])
    assert "Never a subagent that writes the model, runs a command, or asks the person anything" in never
    assert "Never two writes at once" in never
    assert "Never a second batch for the same tables" in never
    assert "Never a number shown to the person that no `sm` command printed" in never


def test_the_give_up_limit_counts_per_subagent():
    text = _flat(_read(PARALLEL))
    give_up = text[text.index("## 4. Give up cleanly"):text.index("## 5.")]
    assert "10 minutes of `--wait 60` passes spent waiting on that subagent" in give_up
    assert "Quick `--wait 0` checks after a card don't count" in give_up
    assert "do that piece yourself, inline" in give_up


def test_queued_subagents_start_after_each_pass_and_each_card():
    collect = _flat(_read(PARALLEL))
    collect = collect[collect.index("## 3. Collect"):collect.index("## 4.")]
    assert "start one queued subagent for each one that finished" in collect
    assert "never more than 4 running" in collect
    assert "whenever the person answers a card while a batch runs" in collect


def test_every_batch_file_name_carries_the_batch_stamp():
    assert 'scripts/sm" batch-id' in _read(PARALLEL)
    names = {
        "enrich.md": ["agami-enrich-<batch>-<chunk>.input.json",
                      "agami-enrich-<batch>-<chunk>.result.json"],
        "review.md": ["agami-examples-<batch>-<area>.input.json",
                      "agami-examples-<batch>-<area>.candidates.json"],
        "examples.md": ["agami-examples-<batch>-<area>.candidates.json"],
        "context-and-tables.md": ["agami-doc-<batch>-<n>.result.json"],
    }
    for doc, wanted in names.items():
        text = _read(REFS / doc)
        for name in wanted:
            assert name in text, (doc, name)
        # no unstamped subagent file name is left behind
        assert not re.search(r"agami-(enrich|examples|doc)-<(chunk|area|n)>", text), doc
        if doc != "examples.md":
            assert 'scripts/sm" batch-id' in text, doc


def test_a_pause_collects_what_is_ready_and_a_resume_starts_the_writers_again():
    review = _flat(_read(REFS / "review.md"))
    assert "leave them; don't wait for them or draft their examples yourself" in review
    assert "collect what is ready" not in review
    parallel = _flat(_read(PARALLEL))
    assert "The one exception is a Pause the person asks for" in parallel
    assert "**On resume**, the writers are always started again with a new batch stamp" in review
    assert "5a uses the files they wrote" not in review


def test_with_a_model_spec_the_enrichers_wait_for_phase_2():
    build = _read(REFS / "build.md")
    start = build[build.index("**Start the enrichers now"):build.index("**Join candidates")]
    assert "unless a model spec was attached" in start
    assert "With `parallel: false` (one chunk, or a small model), or with no Agent tool, nothing starts" \
        in start
    enrich = _read(REFS / "enrich.md")
    assert "**When 1.7 started the enrichers**" in enrich
    assert "pick up at 2a step 3 (collect)" in enrich


def test_one_asked_question_is_kept_once():
    five_a = _section(_read(REFS / "examples.md"), "**5a", "**5b+5c")
    assert "one question can come back from two areas: keep one" in five_a


def test_enrichers_start_from_two_chunks():
    text = _read(REFS / "enrich.md")
    assert 'enrich-input "$ROOT" --chunks' in text
    assert "`parallel: true`" in text and "2 or more chunks" not in text
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
    assert "agami-examples-<batch>-<area>.candidates.json" in review
    assert 'sm" bundle "$ROOT" --area <area> >' in review
    examples = _read(REFS / "examples.md")
    five_a = _section(examples, "**5a", "**5b+5c")
    assert "wait-files" in five_a
    assert "rejected in sign-off" in five_a
    after = examples[examples.index("**5b+5c"):]
    assert after.count('sm" seed-examples "$ROOT"') == 1


def test_an_agent_call_that_returns_at_once_is_a_background_run():
    fallback = _flat(_read(PARALLEL))
    fallback = fallback[fallback.index("## 5. Fallbacks"):fallback.index("## 6.")]
    assert "the Desktop app's has none" in fallback
    assert "If it returns before the subagent has finished" in fallback
    assert "the subagent is in the background: go on with the paired cards and collect with `wait-files`" \
        in fallback
    assert "Only if it returns the subagent's finished result is it foreground" in fallback
    for doc in ("enrich.md", "review.md"):
        assert "`run_in_background: true` where the Agent tool has that parameter" in _read(REFS / doc), doc


def test_a_subagent_that_finished_without_its_file_is_given_up_at_once():
    text = _flat(_read(PARALLEL))
    give_up = text[text.index("## 4. Give up cleanly"):text.index("## 5.")]
    assert "So is a subagent that has finished with no result file" in give_up
    assert "give it up at once, with no wait" in give_up


def test_a_resume_restamps_every_batch():
    assert "a resume starts every batch again (readers, enrichers and writers alike) with a new stamp" \
        in _flat(_read(PARALLEL))


def test_describe_shows_no_chunk_count_of_its_own():
    enrich = _flat(_read(REFS / "enrich.md"))
    assert "of 9 chunks" not in enrich
    assert '"✓ Sales 1 of 3: 12 tables described"' in enrich
    assert 'After each applied chunk, run `sm steps "$ROOT"` and refresh the task list' in enrich
    review = _flat(_read(REFS / "review.md"))
    assert 'run `sm steps "$ROOT"` and refresh the task list ([task-list.md]' in review


def test_a_resumed_build_with_a_model_goes_to_the_join_cards():
    build = _flat(_read(REFS / "build.md"))
    assert "a model was written since this build was started" in build
    assert "go to the join cards below; do not build again" in build
    raw = _read(REFS / "build.md")
    assert raw.index("**On a resume**") < raw.index('scripts/sm" introspect')  # top of 1.7


def test_the_example_phase_says_what_is_true_and_never_waits_silently():
    examples = _flat(_read(REFS / "examples.md"))
    assert "Expect 1–3 min" not in examples
    assert "Collecting the example questions drafted during sign-off" in examples
    assert "post one line saying it is still running" in examples


def test_entities_that_span_chunks_reach_step_6():
    enrich = _flat(_read(REFS / "enrich.md"))
    assert "`cross_area`, `notes`) for step 6" in enrich
    assert "the entities that span chunks of one area from every chunk's `notes` and `cross_area`" in enrich
    assert "within 10 passes" not in enrich and "`applied`)" not in enrich
    enricher = _flat(_read(PLUGIN / "agents" / "agami-enricher.md"))
    assert "goes in `notes`, for the main chat" in enricher
    assert '"tables_described": 12' in enricher


def test_no_agent_tool_means_inline():
    assert INLINE in _read(SKILL)
    assert "no Agent tool at all" in _read(PARALLEL)


def test_connect_skill_stays_under_500_lines():
    assert len(_read(SKILL).splitlines()) < 500


def test_the_sample_copy_marks_the_build_done():
    text = _read(REFS / "sample.md")
    six_a = _section(text, "**6A (copy", "**6B")
    assert 'steps "$ROOT" --mark build=done' in six_a


def test_readers_start_as_each_path_arrives():
    assert "The one exception is the document readers (1.5): each starts as its document's path arrives" \
        in _flat(_read(PARALLEL))


def test_the_example_run_is_waited_on_without_ending_the_turn():
    text = _flat(_read(REFS / "examples.md"))
    phase6 = text[text.index("## Phase 6"):]
    run = phase6[phase6.index("**On a server database, run it in the background**"):]
    run = run[:run.index("SQLite and DuckDB run in the foreground")]
    assert ('scripts/sm" wait-files --file "<artifacts_dir>/local/tmp/agami-examples-items.json" '
            "--wait 60") in run
    assert "if you end your turn while it runs" not in run
