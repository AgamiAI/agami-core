"""`reconcile.py check-run` says whether a run directory and its report page agree, and the renderer's
`--run-dir` door is the only way to a page that passes it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402
import render_reconcile_report as rr  # noqa: E402

from test_reconcile_report_items import ERROR, SCALAR_MATCH, _run  # noqa: E402


def _render(run: Path, words: dict | None = None) -> int:
    args = ["--title", "t", "--profile", "demo", "--run-dir", str(run), "--out", str(run / "report.html")]
    if words is not None:
        (run / "words.json").write_text(json.dumps(words))
        args += ["--words-file", str(run / "words.json")]
    return rr.main(args)


def _intake(run: Path, records: list[dict]) -> None:
    (run / "intake.json").write_text(json.dumps({"rows": [{"row": r["row"], "question": r.get("question")} for r in records]}))
    for r in records:
        row_dir = run / "rows" / str(r["row"])
        row_dir.mkdir(parents=True, exist_ok=True)
        (row_dir / "agami-answer.json").write_text(json.dumps({"sql": r.get("sql"), "error": r.get("error")}))
        if r.get("sql"):
            (row_dir / "actual.csv").write_text("n\n1\n")
        if r.get("statement"):
            (row_dir / "ledger.json").write_text(json.dumps(r.get("ledger") or {"rows": [], "verdict": None}))


def test_a_page_rendered_from_the_run_directory_passes_and_a_changed_checkpoint_fails(tmp_path, capsys):
    run = _run(tmp_path, [SCALAR_MATCH, ERROR])
    _intake(run, [SCALAR_MATCH, ERROR])
    assert _render(run) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("2 rows. Result: ") and "match 1" in out[0] and "could not compare 1" in out[0] and "Fix: " in out[0]
    assert out[1] == f"Report: {run / 'report.html'}" and out[2].startswith("Next: ")
    page = (run / "report.html").read_text()
    items = json.loads((run / "report-items.json").read_text())
    assert reconcile.stamp_for(run.name, items) in page and page.index("<head>") < page.index(reconcile.STAMP_NAME)
    assert reconcile.main(["check-run", "--run-dir", str(run)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "rows": 2, "problems": []}
    # The checkpoint moves on without a re-render: the page is stale and check-run says so.
    with (run / "rows.jsonl").open("a") as fh:
        fh.write(json.dumps(dict(SCALAR_MATCH, row=3)) + "\n")
    _intake(run, [SCALAR_MATCH, ERROR, dict(SCALAR_MATCH, row=3)])
    assert reconcile.main(["check-run", "--run-dir", str(run)]) == 4
    problems = json.loads(capsys.readouterr().out)["problems"]
    assert any("rendered from other items" in p for p in problems)
    assert _render(run) == 0 and reconcile.main(["check-run", "--run-dir", str(run)]) == 0


def test_missing_files_and_a_missing_page_are_named(tmp_path, capsys):
    run = _run(tmp_path, [SCALAR_MATCH, dict(SCALAR_MATCH, row=9)])
    _intake(run, [SCALAR_MATCH])
    assert reconcile.main(["check-run", "--run-dir", str(run)]) == 4
    problems = json.loads(capsys.readouterr().out)["problems"]
    assert "rows.jsonl: row 9 is not in intake.json" in problems and "rows/9/agami-answer.json is missing" in problems
    assert "report.html is missing; render it" in problems
    # A page not written by the renderer carries no stamp.
    (run / "report.html").write_text("<html><head></head><body>typed</body></html>")
    assert reconcile.main(["check-run", "--run-dir", str(run)]) == 4
    assert any("carries no render stamp" in p for p in json.loads(capsys.readouterr().out)["problems"])


def test_the_words_file_may_carry_a_sentence_and_a_change_and_nothing_else(tmp_path, capsys):
    run = _run(tmp_path, [SCALAR_MATCH])
    _intake(run, [SCALAR_MATCH])
    assert _render(run, {"1": {"sentence": "The two answers match.", "change": ["Keep it."]}}) == 0
    item = json.loads((run / "report-items.json").read_text())[0]
    assert item["sentence"] == "The two answers match." and item["change"] == ["Keep it."]
    assert "The two answers match." in (run / "report.html").read_text()
    capsys.readouterr()
    assert _render(run, {"1": {"diff": []}}) == 1
    assert "carries 'diff', which only the run's files may write" in capsys.readouterr().err
    assert _render(run, {"7": {"sentence": "x"}}) == 1 and "row 7 is not in this run" in capsys.readouterr().err
    assert _render(run, {"1": {"change": "not a list"}}) == 1 and "must be a list of sentences" in capsys.readouterr().err
    assert _render(run, [1]) == 1 and "expected an object keyed by row number" in capsys.readouterr().err


def test_the_items_file_door_still_works_for_the_gallery_and_needs_a_run_name(tmp_path, capsys):
    items = tmp_path / "items.json"
    items.write_text(json.dumps([{"row": 1, "question": "q", "status": "match"}]))
    out = tmp_path / "report.html"
    assert rr.main(["--title", "t", "--profile", "demo", "--items-file", str(items), "--out", str(out)]) == 1
    assert "--run is required with --items-file" in capsys.readouterr().err
    assert rr.main(["--title", "t", "--profile", "demo", "--run", "r", "--items-file", str(items), "--out", str(out)]) == 0
    assert reconcile.STAMP_NAME not in out.read_text()
