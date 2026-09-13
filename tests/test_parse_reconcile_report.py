"""The report page's back-channel block, parsed deterministically. Which rows may be kept is read
from the run's own files, never typed; a block from another run is refused whole; any dropped
decision sends the block back."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import parse_reconcile_report as pr  # noqa: E402


def _block(decisions, run: str = "r") -> str:
    return f"profile: demo\nreconcile-run: {run}\ndecisions:\n" + json.dumps(decisions) + "\ndone\n"


def _run_dir(tmp_path: Path, records: list[dict], fits: dict[int, str | None] | None = None) -> Path:
    run = tmp_path / "r"
    (run / "rows").mkdir(parents=True)
    with (run / "rows.jsonl").open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    for row, fit in (fits or {}).items():
        d = run / "rows" / str(row)
        d.mkdir(parents=True, exist_ok=True)
        parts = [{"part": "runs", "verdict": "confirmed"}]
        if fit is not None:
            parts.append({"part": "question_fit", "verdict": fit})
        (d / "ledger.json").write_text(json.dumps({"rows": parts, "verdict": "confirmed"}))
    return run


ONE_CELL = {"columns": ["n"], "rows": [[12]]}
TABLE = {"columns": ["a", "b"], "rows": [[1, 2], [3, 4]]}


def test_a_clean_block_parses_into_decisions_with_words_where_they_belong():
    data, anomalies, needs = pr.parse(_block([
        {"row": 1, "decision": "keep"}, {"row": 3, "decision": "change", "words": "revenue nets refunds"},
        {"row": 4, "decision": "fix"}, {"row": 5, "decision": "reword", "words": "  ask about orders "},
        {"row": 6, "decision": "nothing"}]), keepable={1}, run="r")
    assert needs is None and anomalies == []
    assert data["profile"] == "demo" and data["run"] == "r"
    assert data["decisions"] == [
        {"row": 1, "decision": "keep"}, {"row": 3, "decision": "change", "words": "revenue nets refunds"},
        {"row": 4, "decision": "fix"}, {"row": 5, "decision": "reword", "words": "ask about orders"},
        {"row": 6, "decision": "nothing"}]


def test_which_rows_may_be_kept_is_read_from_the_runs_own_files(tmp_path):
    """Phase 3e's predicate, read and never typed: status match, one recorded cell, and a confirmed
    fit on a row that carries both a question and a statement."""
    run = _run_dir(tmp_path, [
        {"row": 1, "question": "q", "statement": None, "status": "match", "recorded": ONE_CELL},
        {"row": 2, "question": "q", "statement": "s", "status": "match", "recorded": ONE_CELL},
        {"row": 3, "question": "q", "statement": "s", "status": "match", "recorded": ONE_CELL},
        {"row": 4, "question": "q", "statement": "s", "status": "match", "recorded": ONE_CELL},
        {"row": 5, "question": "q", "statement": None, "status": "match", "recorded": TABLE},
        {"row": 6, "question": "q", "statement": None, "status": "match_unverified", "recorded": ONE_CELL},
    ], fits={2: "confirmed", 3: None, 4: "unresolved"})
    # 1: no statement, nothing to check. 2: fit confirmed. 3: a question and a statement but no
    # fit part (a `no_question` against a row that carries a question). 4: doubtful. 5: a table.
    # 6: not a match.
    assert pr.keepable_rows(run) == {1, 2}
    assert pr.keepable_rows(tmp_path / "nowhere") == set()


def test_a_keep_the_run_does_not_allow_is_refused_and_the_block_goes_back():
    data, anomalies, needs = pr.parse(_block([{"row": 2, "decision": "keep"}, {"row": 1, "decision": "keep"}]), keepable={1}, run="r")
    assert data["decisions"] == [{"row": 1, "decision": "keep"}]
    assert {a["kind"] for a in anomalies} == {"keep_not_offered"}
    assert needs["kind"] == "decisions_dropped" and needs["rows"] == [2]
    # With nothing keepable, no keep can be applied.
    _data, anomalies, needs = pr.parse(_block([{"row": 1, "decision": "keep"}]), keepable=set(), run="r")
    assert anomalies[0]["kind"] == "keep_not_offered" and needs is not None


def test_words_beside_a_keep_or_a_nothing_are_the_hand_edit_the_parser_catches():
    _data, anomalies, needs = pr.parse(_block([{"row": 1, "decision": "keep", "words": "revenue nets refunds"},
                                               {"row": 2, "decision": "nothing", "words": "x"}]), keepable={1}, run="r")
    assert {a["kind"] for a in anomalies} == {"words_ignored_on_keep", "words_ignored_on_nothing"}
    assert needs["kind"] == "decisions_dropped" and needs["rows"] == [1, 2]


def test_a_block_from_another_run_or_without_its_decisions_goes_back_whole():
    data, anomalies, needs = pr.parse(_block([{"row": 1, "decision": "nothing"}], run="other"), keepable={1}, run="r")
    assert data["decisions"] == [] and needs["kind"] == "run_mismatch" and anomalies[0]["expected"] == "r"
    _data, anomalies, needs = pr.parse("profile: demo\nreconcile-run: r\ndone\n", keepable=set(), run="r")
    assert needs["kind"] == "section_missing" and anomalies[0]["detail"] == "decisions"
    _data, _anomalies, needs = pr.parse("", keepable=set(), run="r")
    assert needs["kind"] == "run_mismatch"  # an empty paste names no run
    _data, _anomalies, needs = pr.parse("profile: demo\nreconcile-run: r\ndecisions:\n{\"row\": 1}\ndone\n", keepable=set(), run="r")
    assert needs["kind"] == "decisions_not_list"


def test_dropped_decisions_send_the_block_back_and_bad_json_is_unparseable(tmp_path, capsys):
    _d, anomalies, needs = pr.parse(_block([{"row": 1, "decision": "kep"}, {"row": 1, "decision": "fix"},
                                            {"row": 1, "decision": "fix"}, {"decision": "fix"}, "x"]), keepable=set(), run="r")
    assert {a["kind"] for a in anomalies} == {"unknown_decision", "row_decided_twice", "decision_missing_row", "decision_not_an_object"}
    assert needs["kind"] == "decisions_dropped"
    _d, anomalies, needs = pr.parse("profile: demo\nreconcile-run: r\ndecisions:\n[not json\ndone\n", keepable=set(), run="r")
    assert needs["kind"] == "unparseable_json"


def test_the_cli_reads_the_run_directory_and_says_ok_false_when_a_block_must_go_back(tmp_path, capsys):
    run = _run_dir(tmp_path, [{"row": 1, "question": "q", "statement": None, "status": "match", "recorded": ONE_CELL}])
    block = tmp_path / "block.txt"
    block.write_text(_block([{"row": 1, "decision": "keep"}], run="r"))
    assert pr.main(["--block-file", str(block), "--run-dir", str(run)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["data"]["decisions"] == [{"row": 1, "decision": "keep"}]
    block.write_text(_block([{"row": 2, "decision": "keep"}], run="r"))
    assert pr.main(["--block-file", str(block), "--run-dir", str(run)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert pr.main(["--block-file", str(block), "--run-dir", str(tmp_path / "nowhere")]) == 2
    assert json.loads(capsys.readouterr().out)["needs_judgment"]["kind"] == "bad_argument"


def test_words_that_are_not_text_drop_the_decision():
    text = "profile: demo\nreconcile-run: r\ndecisions:\n" + json.dumps([{"row": 1, "decision": "change", "words": 5}]) + "\ndone\n"
    data, anomalies, needs = pr.parse(text, set(), "r")
    assert data["decisions"] == [] and {a["kind"] for a in anomalies} == {"words_not_text"} and needs is not None

