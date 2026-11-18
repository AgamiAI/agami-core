"""`sm steps` and `sm progress` read the build log main's introspection writes.

Each line of `<root>/.introspect/progress.log` starts with its UTC time (`2026-11-17T10:04:00Z
columns+grain 30/52`), and the build logs main's own stages: batches, join checks on the data,
profiling, a heartbeat while a statement is slow, and `done: … joins written`. A reader that matched
the bare message saw none of it: every line was quoted raw, and a finished or failed build never
read as ended. A build that raises now ends its log with `failed: <reason>`, so the list stops
reading "building".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli, steps  # noqa: E402
from semantic_model import introspect as INTRO  # noqa: E402

STAMP = "2026-11-17T10:04:00Z "


def _log(root: Path, *lines: str) -> Path:
    log = root / ".introspect" / "progress.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(STAMP + line + "\n" for line in lines), encoding="utf-8")
    return log


def test_the_last_line_is_read_without_its_stamp(tmp_path):
    log = _log(tmp_path, "discovered 3 tables — reading columns, grain, keys", "columns+grain 2/3")
    assert steps.last_progress_line(log) == "columns+grain 2/3"


def test_an_unstamped_line_reads_as_it_is(tmp_path):
    log = tmp_path / ".introspect" / "progress.log"
    log.parent.mkdir(parents=True)
    log.write_text("columns+grain 2/3\n", encoding="utf-8")
    assert steps.last_progress_line(log) == "columns+grain 2/3"


@pytest.mark.parametrize(
    "raw, line",
    [
        ("columns+grain 30/52", "Reading columns ▰▰▰▰▱▱▱▱ 30/52 tables"),
        ("joins checked 140/300", "Checking joins on the data ▰▰▰▱▱▱▱▱ 140/300 joins"),
        ("profiled 20/42 tables", "Profiling columns ▰▰▰▱▱▱▱▱ 20/42 tables"),
        ("batch 3 of 5", "Batch 3 of 5"),
        ("joins: checking 78 on the data", "Checking 78 joins on the data"),
        ("profiling 42 tables…", "Profiling columns"),
        ("join candidates: testing columns no join covers…", "Testing possible joins"),
        ("join candidates: 4 found (120 probe(s) this run)", "Testing possible joins: 4 found"),
        ("done: 34 tables, 7 joins written, 4 join candidates found", "Done: 34 tables, 7 joins"),
        ("done: 34 tables, 7 relationships", "Done: 34 tables, 7 joins"),
    ],
)
def test_mains_log_lines_read_in_the_persons_words(raw, line):
    assert cli._progress_line(raw)["line"] == line


def test_a_slow_statement_says_how_long_and_quotes_what_it_waits_on():
    out = cli._progress_line("waiting on the database for 12 min: checking joins of orders")
    assert out["line"] == 'Waiting on the database for 12 min: "checking joins of orders"'
    assert out["done"] is False and out["failed"] is False


def test_progress_ends_on_a_stamped_done_or_failed_line(tmp_path, capsys):
    import json

    root = tmp_path / "acme"
    _log(root, "columns+grain 3/3", "done: 3 tables, 2 joins written, 0 join candidates found")
    assert cli.main(["progress", str(root), "--wait", "5"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["done"] is True and out["line"] == "Done: 3 tables, 2 joins"
    _log(root, "columns+grain 1/3", "failed: permission denied for schema demo")
    assert cli.main(["progress", str(root), "--wait", "5"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["failed"] is True and "permission denied" in out["line"]


def test_steps_reads_a_stamped_failure_as_the_builds_problem(tmp_path):
    root = tmp_path / "acme"
    steps.mark(root, "build", "in_progress")
    _log(root, "columns+grain 1/3", "failed: permission denied for schema demo")
    result = steps.onboarding_steps(root)
    assert result["problem"] == 'The build didn\'t finish: "permission denied for schema demo"'
    assert {s["id"]: s["status"] for s in result["steps"]}["build"] == "pending"


def test_a_build_that_raises_ends_its_log_with_failed(tmp_path):
    log = tmp_path / "progress.log"

    def runner(sql):
        raise RuntimeError("connection reset by peer\nsecond line")

    with pytest.raises(Exception):
        INTRO.introspect(
            "acme",
            "sqlite",
            runner=runner,
            artifacts_dir=tmp_path,
            out_dir=tmp_path / "acme",
            progress_path=log,
        )
    last = steps.last_progress_line(log)
    assert last.startswith("failed: ")
    assert "\n" not in last
