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

RULE = ("Run `sm steps` at the start of onboarding, when a step starts or ends, and after every "
        "`sm progress --wait`. Replace the session's task list with its output: one item per step, "
        "in order, using each step's `text` and `status`. Never write a step or a number `sm steps` "
        "did not print.")

# Each phase file and the marks it carries, where its step really starts and ends. The report
# check (`reports`) is 6b of the examples file: that is where the shared reports are checked.
PHASE_MARKS = {
    "credentials.md": ["connect=in_progress", "connect=done"],
    "sample.md": ["connect=in_progress", "connect=done"],
    "context-and-tables.md": ["context=in_progress", "context=done", "tables=in_progress",
                              "tables=done", "tables=skipped"],
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
    for form in ('bash "<plugin_root>/scripts/sm" steps "$ROOT"\n',
                 'bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark <id>=in_progress',
                 'bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark <id>=done',
                 'bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark <id>=skipped'):
        assert form in text, form
    assert len(text.splitlines()) < 60


def test_only_the_shared_file_names_a_host_task_tool():
    offenders = [str(p.relative_to(SKILLS_DIR)) for p in SKILLS_DIR.rglob("*")
                 if p.is_file() and p.suffix in (".md", ".py", ".json", ".yaml", ".html")
                 and re.search(r"TodoWrite|TaskCreate", _text(p))]
    assert not offenders, offenders
    text = _text(TASK_LIST)
    assert "TodoWrite" in text and "TaskCreate" in text


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
        assert f'steps "$ROOT" --mark {mark}' in text, f"{name}: {mark}"
    assert "](../../../shared/task-list.md)" in text, name


def test_build_refreshes_the_list_after_each_wait():
    text = _text(REFS / "build.md")
    wait = re.search(r'scripts/sm" progress "[^"]+" --wait', text)
    assert wait, "no sm progress --wait in build.md"
    # the rest of the wait-loop item, up to the next item (well before the next heading)
    item = text[wait.end():].split("\n4. ", 1)[0]
    assert "\n#" not in item
    assert 'steps "$ROOT"\n' in item  # plain `sm steps`, no mark
    # the loop stops on a failed build too, and the person is told why
    loop = text[:wait.start()].rsplit("\n", 3)[-3]
    assert "in a loop until it says `done` or `failed`" in loop
    assert "`failed`" in item and "`problem`" in item


def test_task_list_says_what_to_do_with_a_problem():
    text = _text(TASK_LIST)
    assert "`problem`" in text
    assert re.search(r"`problem`[^\n]*(\n[^\n]+)*?say it to the person", text)


def test_task_list_ids_table_matches_the_engine():
    rows = re.findall(r"^\| `([a-z_]+)` \| ([^|]+?) \|$", _text(TASK_LIST), re.MULTILINE)
    assert [i for i, _ in rows] == list(steps.STEP_IDS)
    assert {i: label for i, label in rows} == steps.LABELS


def test_connect_skill_stays_under_500_lines():
    assert _text(CONNECT / "SKILL.md").count("\n") < 500
