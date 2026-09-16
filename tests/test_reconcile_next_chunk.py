"""The run works five rows at a time and rows.jsonl is its checkpoint: `next-chunk` hands back the
rows not yet finished, never a row twice, and the counts the progress line says."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402


def _run_dir(tmp_path: Path, n_rows: int, done: list[tuple[int, str]] = ()) -> Path:
    run = tmp_path / "20260913-094500"
    run.mkdir()
    rows = [{"row": i, "question": f"Question {i}?", "statement": None, "expected": None, "raw_value": None,
             "provenance": {"shape": "a", "source": None, "file": "questions.txt", "line": i}} for i in range(1, n_rows + 1)]
    (run / "intake.json").write_text(json.dumps({"shape": "a", "skipped": [], "rows": rows}))
    if done:
        (run / "rows.jsonl").write_text("".join(json.dumps({"row": r, "status": st}) + "\n" for r, st in done))
    return run


def test_a_fresh_run_hands_back_the_first_five(tmp_path):
    out = reconcile.next_chunk(_run_dir(tmp_path, 50))
    assert out["chunk_rows"] == [1, 2, 3, 4, 5] and out["chunk_index"] == 1 and out["chunks_total"] == 10
    assert out["finished"] == 0 and out["remaining"] == 50 and out["complete"] is False and out["progress"] == {}


def test_finished_rows_are_never_handed_back(tmp_path):
    run = _run_dir(tmp_path, 12, done=[(1, "match"), (2, "mismatch"), (3, "match"), (4, "error"), (5, "match_unverified")])
    out = reconcile.next_chunk(run)
    assert out["chunk_rows"] == [6, 7, 8, 9, 10] and out["chunk_index"] == 2 and out["chunks_total"] == 3
    assert out["finished"] == 5 and out["remaining"] == 7
    assert out["progress"] == {"match": 2, "mismatch": 1, "error": 1, "match_unverified": 1}
    # A row finished out of order stays finished; the chunk is the next rows that are not.
    (run / "rows.jsonl").open("a").write(json.dumps({"row": 8, "status": "match"}) + "\n")
    assert reconcile.next_chunk(run)["chunk_rows"] == [6, 7, 9, 10, 11]


def test_the_last_chunk_is_short_and_then_the_run_is_complete(tmp_path, capsys):
    run = _run_dir(tmp_path, 7, done=[(i, "match") for i in range(1, 6)])
    out = reconcile.next_chunk(run)
    assert out["chunk_rows"] == [6, 7] and out["complete"] is False
    (run / "rows.jsonl").open("a").write("".join(json.dumps({"row": r, "status": "match"}) + "\n" for r in (6, 7)))
    assert reconcile.main(["next-chunk", "--run-dir", str(run)]) == 4
    printed = json.loads(capsys.readouterr().out)
    assert printed["complete"] is True and printed["chunk"] == [] and printed["progress"] == {"match": 7}


def test_the_size_is_a_choice_and_the_rows_file_seeds_the_run_once(tmp_path, capsys):
    run = tmp_path / "run"
    run.mkdir()
    rows_file = tmp_path / "rows.json"
    rows_file.write_text(json.dumps({"rows": [{"row": i, "question": f"q{i}"} for i in range(1, 8)]}))
    assert reconcile.main(["next-chunk", "--run-dir", str(run), "--rows-file", str(rows_file), "--size", "3"]) == 0
    assert json.loads(capsys.readouterr().out)["chunk_rows"] == [1, 2, 3]
    assert (run / "intake.json").exists()
    # A second call with a different rows file does not overwrite the run's own copy.
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"rows": [{"row": 99, "question": "x"}]}))
    assert reconcile.main(["next-chunk", "--run-dir", str(run), "--rows-file", str(other)]) == 0
    assert json.loads(capsys.readouterr().out)["chunk_rows"] == [1, 2, 3, 4, 5]


def test_a_checkpoint_line_that_cannot_be_read_is_refused_not_skipped(tmp_path, capsys):
    run = _run_dir(tmp_path, 6, done=[(1, "match")])
    (run / "rows.jsonl").open("a").write("{not json\n")
    with pytest.raises(ValueError, match="run twice"):
        reconcile.next_chunk(run)
    assert reconcile.main(["next-chunk", "--run-dir", str(run)]) == 2
    assert "cannot be read" in capsys.readouterr().err


def test_a_run_without_intake_is_refused_with_the_way_to_seed_it(tmp_path, capsys):
    run = tmp_path / "empty"
    run.mkdir()
    assert reconcile.main(["next-chunk", "--run-dir", str(run)]) == 2
    assert "--rows-file" in capsys.readouterr().err
    assert reconcile.main(["next-chunk", "--run-dir", str(tmp_path / "missing")]) == 2


def test_a_row_dropped_on_the_intake_page_never_renumbers_the_others(tmp_path):
    """Row numbers are given once, at intake; the intake page's block drops row 1; the run then works
    rows 2 and 3 under the numbers the page showed."""
    import parse_reconcile_intake as pi
    f = tmp_path / "q.txt"
    f.write_text("What is the refund rate?\nHow many orders?\nWhich category sold most?\n")
    intake = reconcile.intake([f], source=None)
    assert [r["row"] for r in intake["rows"]] == [1, 2, 3]
    applied, counts = pi.apply(intake, [{"row": 1, "keep": False}])
    assert counts == {"kept": 2, "dropped": 1, "edited": 0} and [r["row"] for r in applied["rows"]] == [2, 3]
    run = tmp_path / "run"; run.mkdir()
    (run / "intake.json").write_text(json.dumps(applied))
    assert reconcile.next_chunk(run)["chunk_rows"] == [2, 3]


def test_resume_finds_the_newest_run_with_rows_left(tmp_path, capsys):
    root = tmp_path / "reconcile"; root.mkdir()
    assert reconcile.main(["resume", "--reconcile-dir", str(root)]) == 4  # nothing to resume
    assert json.loads(capsys.readouterr().out) is None
    old = _run_dir(root, 3, done=[(1, "match"), (2, "match"), (3, "match")])
    new = root / "20260914-090000"; new.mkdir()
    (new / "intake.json").write_text((old / "intake.json").read_text())
    (new / "rows.jsonl").write_text(json.dumps({"row": 1, "status": "mismatch"}) + "\n")
    assert reconcile.main(["resume", "--reconcile-dir", str(root)]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["run_dir"].endswith("20260914-090000") and found["remaining"] == 2 and found["chunk_rows"] == [2, 3] and found["progress"] == {"mismatch": 1}
    broken = root / "20260915-090000"; broken.mkdir()
    (broken / "intake.json").write_text("{not json")
    assert reconcile.main(["resume", "--reconcile-dir", str(root)]) == 0  # a run that cannot be read is passed over
    assert json.loads(capsys.readouterr().out)["run_dir"].endswith("20260914-090000")
    assert reconcile.main(["resume", "--reconcile-dir", str(tmp_path / "nope")]) == 2

