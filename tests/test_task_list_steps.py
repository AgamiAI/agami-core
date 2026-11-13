"""agami-connect draws its task list from `sm steps`, never from its own typing.

The list is the person's progress display in the Desktop app. When the model kept it from prose it
drifted between turns and lost its counts; the skill now copies `sm steps` at every step change, so
the rule lives in one shared file and each phase file marks its own step where it really starts and
ends.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from skill_docs import PLUGIN, SKILLS_DIR

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import steps  # noqa: E402

TASK_LIST = PLUGIN / "shared" / "task-list.md"
CONNECT = SKILLS_DIR / "agami-connect"
REFS = CONNECT / "references"

RULE = ("Refresh the session's task list from the `list` in `sm steps` output, or in "
        "`sm progress --wait` output during the build. Run `sm steps` at the start of onboarding and "
        "when a step starts or ends. Replace the task list with that `list`: one item per line, in "
        "order, each line's mark giving the item's state. Never write a step or a number `sm steps` "
        "did not print.")

# Each phase file and the marks it carries, where its step really starts and ends. The report
# check (`reports`) is 6b of the examples file: that is where the shared reports are checked.
PHASE_MARKS = {
    "credentials.md": ["connect=in_progress", "connect=done"],
    "sample.md": ["connect=in_progress", "connect=done", "first_question=in_progress",
                  "first_question=done"],
    # nothing shared in 1.5: the report check is skipped there, so it never shows as pending
    "context-and-tables.md": ["context=in_progress", "context=done", "tables=in_progress",
                              "tables=done", "tables=skipped", "reports=skipped"],
    "build.md": ["build=in_progress", "build=done"],
    "enrich.md": ["describe=in_progress", "describe=done"],
    "review.md": ["signoff=in_progress", "signoff=done"],
    "examples.md": ["examples=in_progress", "examples=done", "reports=in_progress",
                    "reports=done", "reports=skipped"],
    "summary.md": ["first_question=in_progress", "first_question=done"],
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _progress_section(main: str) -> str:
    return main.split("## Progress tracking", 1)[1].split("\n## ", 1)[0]


def test_task_list_rule_is_stated_once():
    text = _text(TASK_LIST)
    assert RULE in " ".join(text.split())
    body = text.split("\n", 1)[1].lstrip()
    assert " ".join(body.split("\n\n", 1)[0].split()) == RULE  # the opening paragraph
    for form in ('bash "<plugin_root>/scripts/sm" steps "<root>"\n',
                 'bash "<plugin_root>/scripts/sm" steps "<root>" --mark <id>=in_progress',
                 'bash "<plugin_root>/scripts/sm" steps "<root>" --mark <id>=done',
                 'bash "<plugin_root>/scripts/sm" steps "<root>" --mark <id>=skipped'):
        assert form in text, form
    assert len(text.splitlines()) < 60


def test_only_the_shared_file_names_a_host_task_tool():
    docs = [p for p in PLUGIN.rglob("*") if p.is_file() and p != TASK_LIST
            and p.suffix in (".md", ".py", ".json", ".yaml", ".html", ".sh")]
    offenders = [str(p.relative_to(PLUGIN)) for p in docs
                 if re.search(r"TodoWrite|TaskCreate", p.read_text(encoding="utf-8", errors="replace"))]
    assert not offenders, offenders
    text = _text(TASK_LIST)
    assert "TodoWrite" in text and "TaskCreate" in text
    # and the rule is stated in one place only
    stated = [p for p in [*docs, TASK_LIST] if p.suffix == ".md"
              and RULE in " ".join(p.read_text(encoding="utf-8", errors="replace").split())]
    assert stated == [TASK_LIST]


def test_the_list_also_moves_during_describe_and_sign_off():
    # the paragraph right after the opening rule and its `<plugin_root>` pointer
    paragraphs = [" ".join(p.split()) for p in _text(TASK_LIST).split("\n\n")]
    after = paragraphs.index(next(p for p in paragraphs if p.startswith("> `<plugin_root>`"))) + 1
    assert paragraphs[after] == ("The list also moves during describe and sign-off: after each applied chunk, "
                                 "`apply-enrichment`'s `then` says to refresh it, and after each sign-off card "
                                 "is answered, run `sm steps \"<root>\"` and do what its `then` says.")


def test_connect_links_the_task_list_rule():
    section = _progress_section(_text(CONNECT / "SKILL.md"))
    assert "[shared/task-list.md](../../shared/task-list.md)" in section
    assert "sm steps" in section


def test_skill_steps_match_the_engine(tmp_path):
    section = _progress_section(_text(CONNECT / "SKILL.md"))
    block = section.split("```", 2)[1]
    labels = [m.group(1) for m in re.finditer(r"^\d\. (.+)$", block, re.MULTILINE)]
    engine = [s["label"] for s in steps.onboarding_steps(tmp_path / "acme")["steps"]]
    assert labels == engine
    assert len(labels) == 9


@pytest.mark.parametrize("name,marks", sorted(PHASE_MARKS.items()))
def test_each_phase_marks_its_step(name, marks):
    text = _text(REFS / name)
    for mark in marks:
        assert f'steps "<root>" --mark {mark}' in text, f"{name}: {mark}"
    assert "](../../../shared/task-list.md)" in text, name


def test_build_refreshes_the_list_after_each_wait():
    text = _text(REFS / "build.md")
    wait = re.search(r'scripts/sm" progress "[^"]+" --wait', text)
    assert wait, "no sm progress --wait in build.md"
    # the rest of the wait-loop item, up to the next item (well before the next heading)
    item = text[wait.end():].split("\n4. ", 1)[0]
    assert "\n#" not in item
    # the poll's own `list` refreshes the task list: one call per pass, no `sm steps` beside it
    assert "Its output also carries `list`" in item and "No separate `sm steps` call per pass" in item
    # the loop stops when `then` says the build has ended (a failed build too) or a killed build
    # shows `·`, and the person is told why from the problem `say`
    assert "in a loop that stops when its `then` says the build has ended, or when its `list` shows the " \
        "build step not started again (`·`)" in " ".join(text.split())
    assert "the build has ended" in item and "its `say`" in item


def test_task_list_says_what_to_do_with_a_problem():
    text = _text(TASK_LIST)
    assert "`problem`" in text
    assert re.search(r"`problem`[^\n]*(\n[^\n]+)*?say it to the person", text)


def test_task_list_ids_table_matches_the_engine():
    rows = re.findall(r"^\| `([a-z_]+)` \| ([^|]+?) \|$", _text(TASK_LIST), re.MULTILINE)
    assert [i for i, _ in rows] == list(steps.STEP_IDS)
    assert {i: label for i, label in rows} == steps.LABELS


def test_every_path_shows_the_same_nine_steps():
    # one list on every path: the sample doesn't seed a list of its own
    assert "Todo seed" not in _text(REFS / "sample.md")
    assert "own short list" not in _text(CONNECT / "SKILL.md")


def test_the_last_step_is_done_before_the_turn_ends():
    # the first answer comes in a later turn, from agami-query: a mark after it would never run
    text = _text(REFS / "summary.md")
    assert text.index("--mark first_question=done") < text.index("End the turn.")


def test_build_stops_waiting_on_a_problem():
    text = _text(REFS / "build.md")
    assert "or when its `list` shows the build step not started again (`·`)" in text
    assert 'run `bash "<plugin_root>/scripts/sm" steps "<root>"` once, and do what its `then` says' in text
    assert "post its `say`, then refresh the task list from its `list`, then handle it" in " ".join(text.split())
    assert '"The validator gates the write"' in text and "\nThe validator gates the write" in text


def test_connect_skill_stays_under_500_lines():
    assert _text(CONNECT / "SKILL.md").count("\n") < 500
