"""agami-connect draws its task list from `sm steps`, never from its own typing.

The list is the person's progress display in the Desktop app. When the model kept it from prose it
drifted between turns and lost its counts; the skill now copies `sm steps` at every step change, so
the rule lives in one shared file and each phase of agami-connect marks its own step where it really
starts and ends.
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

RULE = (
    "Refresh the session's task list from the `list` in `sm steps` output, or in "
    "`sm progress --wait` output during the build. Run `sm steps` at the start of onboarding and "
    "when a step starts or ends. Replace the task list with that `list`: one item per line, in "
    "order, each line's mark giving the item's state. Never write a step or a number `sm steps` "
    "did not print."
)

# Each section of agami-connect's SKILL.md and the marks it carries, where its step really starts and
# ends. The report check (`reports`) is 6b: that is where the shared reports are checked; nothing
# shared in 1.5 skips it there, so it never shows as pending. 2g runs only on "Ask me as you go".
SECTION_MARKS = {
    "### 0a.4k": ["connect=done"],
    "### 0a.7": ["connect=in_progress"],
    "### 0a.10": ["connect=done"],
    "## Phase 0s": [
        "connect=in_progress",
        "connect=done",
        "build=done",
        "first_question=in_progress",
    ],
    "## Phase 1:": ["context=in_progress"],
    "### 1.5": ["reports=skipped", "open_questions=skipped", "context=done"],
    "### 1.6 ": ["tables=in_progress", "tables=done"],
    "### 1.6s": ["tables=skipped"],
    "### 1.7": ["build=in_progress"],
    "### 1.8": ["build=done"],
    "## Phase 2:": ["describe=in_progress"],
    "### 2f": ["describe=done"],
    "### 2g": ["open_questions=in_progress", "open_questions=done"],
    "## Phase 3:": ["signoff=in_progress"],
    "## Phase 4:": ["signoff=done"],
    "## Phase 5:": ["examples=in_progress"],
    "### 6a": ["examples=done"],
    "### 6b": ["reports=in_progress", "reports=done", "reports=skipped"],
    "## Phase 7:": ["first_question=in_progress"],
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _progress_section(main: str) -> str:
    return main.split("## Progress tracking", 1)[1].split("\n## ", 1)[0]


def _section(heading: str) -> str:
    """One section of agami-connect's SKILL.md, from its heading to the next heading of any level."""
    text = _text(CONNECT / "SKILL.md")
    start = text.index("\n" + heading)
    rest = text[start + 1 :]
    ends = [i for i in (rest.find("\n## ", 1), rest.find("\n### ", 1)) if i > 0]
    return rest[: min(ends)] if ends else rest


def test_task_list_rule_is_stated_once():
    text = _text(TASK_LIST)
    assert RULE in " ".join(text.split())
    body = text.split("\n", 1)[1].lstrip()
    assert " ".join(body.split("\n\n", 1)[0].split()) == RULE  # the opening paragraph
    for form in (
        'bash "<plugin_root>/scripts/sm" steps "<root>"\n',
        'bash "<plugin_root>/scripts/sm" steps "<root>" --mark <id>=in_progress',
        'bash "<plugin_root>/scripts/sm" steps "<root>" --mark <id>=done',
        'bash "<plugin_root>/scripts/sm" steps "<root>" --mark <id>=skipped',
    ):
        assert form in text, form
    assert len(text.splitlines()) < 60


def test_only_the_shared_file_names_a_host_task_tool():
    docs = [
        p
        for p in PLUGIN.rglob("*")
        if p.is_file()
        and p != TASK_LIST
        and p.suffix in (".md", ".py", ".json", ".yaml", ".html", ".sh")
    ]
    offenders = [
        str(p.relative_to(PLUGIN))
        for p in docs
        if re.search(r"TodoWrite|TaskCreate", p.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not offenders, offenders
    text = _text(TASK_LIST)
    assert "TodoWrite" in text and "TaskCreate" in text
    # and the rule is stated in one place only
    stated = [
        p
        for p in [*docs, TASK_LIST]
        if p.suffix == ".md"
        and RULE in " ".join(p.read_text(encoding="utf-8", errors="replace").split())
    ]
    assert stated == [TASK_LIST]


def test_the_list_also_moves_during_describe_and_sign_off():
    # the paragraph right after the opening rule and its `<plugin_root>` pointer
    paragraphs = [" ".join(p.split()) for p in _text(TASK_LIST).split("\n\n")]
    after = paragraphs.index(next(p for p in paragraphs if p.startswith("> `<plugin_root>`"))) + 1
    assert paragraphs[after] == (
        "The list also moves during describe and sign-off: `sm coverage` and "
        "`sm add-example` carry `list` and a `then` that says to refresh it when a count "
        "on the list changed, and after each sign-off card is answered, run "
        '`sm steps "<root>"` and do what its `then` says.'
    )


def test_connect_links_the_task_list_rule():
    section = _progress_section(_text(CONNECT / "SKILL.md"))
    assert "[shared/task-list.md](../../shared/task-list.md)" in section
    assert "sm steps" in section


def test_skill_steps_match_the_engine(tmp_path):
    section = _progress_section(_text(CONNECT / "SKILL.md"))
    block = section.split("```", 2)[1]
    labels = [m.group(1) for m in re.finditer(r"^\d+\. (.+)$", block, re.MULTILINE)]
    engine = [s["label"] for s in steps.onboarding_steps(tmp_path / "acme")["steps"]]
    assert labels == engine
    assert len(labels) == 10


@pytest.mark.parametrize("heading,marks", sorted(SECTION_MARKS.items()))
def test_each_phase_marks_its_step(heading, marks):
    text = _section(heading)
    for mark in marks:
        assert f'steps "<root>" --mark {mark}' in text, f"{heading}: {mark}"
    assert "](../../shared/task-list.md)" in text, heading


def test_every_step_is_marked_started_somewhere():
    text = _text(CONNECT / "SKILL.md")
    for step_id in steps.STEP_IDS:
        assert f"--mark {step_id}=in_progress" in text, step_id


def test_build_refreshes_the_list_after_each_wait():
    text = _section("### 1.7")
    wait = re.search(r'scripts/sm" progress "[^"]+" --wait', text)
    assert wait, "no sm progress --wait in build.md"
    # the rest of the wait-loop item, up to the next item (well before the next heading)
    item = text[wait.end() :].split("\n4. ", 1)[0]
    assert "\n#" not in item
    # the poll's own `list` refreshes the task list: one call per pass, no `sm steps` beside it
    assert (
        "Its output also carries `list`" in item and "No separate `sm steps` call per pass" in item
    )
    # the loop stops when `then` says the build has ended (a failed build too) or a killed build
    # shows `·`, and the person is told why from the problem `say`
    assert (
        "in a loop that stops when its `then` says the build has ended, or when its `list` shows the "
        "build step not started again (`·`)" in " ".join(text.split())
    )
    assert "the build has ended" in item and "its `say`" in item


def test_task_list_says_what_to_do_with_a_problem():
    text = _text(TASK_LIST)
    assert "`problem`" in text
    assert re.search(r"`problem`[^\n]*(\n[^\n]+)*?say it to the person", text)


def test_task_list_ids_table_matches_the_engine():
    rows = re.findall(r"^\| `([a-z_]+)` \| ([^|]+?) \|$", _text(TASK_LIST), re.MULTILINE)
    assert [i for i, _ in rows] == list(steps.STEP_IDS)
    assert {i: label for i, label in rows} == steps.LABELS


def test_every_path_shows_the_same_steps():
    # one list on every path: the sample doesn't seed a list of its own
    assert "Todo seed" not in _section("## Phase 0s")
    assert "own short list" not in _text(CONNECT / "SKILL.md")


def test_the_last_step_is_done_only_once_the_first_question_is_answered():
    # A live run ticked "Summary and first question" while the question was still unanswered. The
    # answer comes from agami-query, in a later turn, so agami-query marks it done.
    for heading in ("## Phase 7:", "## Phase 0s"):
        flat = " ".join(_section(heading).split())
        assert "stays in progress until the first question is answered" in flat, heading
        assert "agami-query marks it done then" in flat, heading
        assert "--mark first_question=done" not in flat, heading
    assert "End the turn, with the last step still in progress" in " ".join(
        _text(CONNECT / "SKILL.md").split()
    )
    query = " ".join(_text(SKILLS_DIR / "agami-query" / "SKILL.md").split())
    fourf = query[query.index("### 4f") :]
    assert "still shows *Summary and first question* in progress" in fourf
    assert "--mark first_question=done" in fourf[:1200]
    assert "Never when the list shows that step done or shows no onboarding steps" in fourf


def test_build_stops_waiting_on_a_problem():
    text = _section("### 1.7")
    assert "or when its `list` shows the build step not started again (`·`)" in text
    assert (
        'run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" steps "<root>"` once, and do what its `then` says'
        in text
    )
    assert "post its `say`, do what its `then` says, then handle it" in " ".join(text.split())
    assert '"The validator gates the write"' in text
    assert "\nThe validator gates the write" in _text(CONNECT / "SKILL.md")
