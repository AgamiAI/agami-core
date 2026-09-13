"""The report page's back-channel block, parsed deterministically. A keep the run did not score
`match` is refused; any dropped decision sends the block back."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import parse_reconcile_report as pr  # noqa: E402


def _block(decisions) -> str:
    return "profile: demo\nreconcile-run: r\ndecisions:\n" + json.dumps(decisions) + "\ndone\n"


def test_a_clean_block_parses_into_decisions_with_words_where_they_belong():
    data, anomalies, needs = pr.parse(_block([
        {"row": 1, "decision": "keep"}, {"row": 3, "decision": "change", "words": "revenue nets refunds"},
        {"row": 4, "decision": "fix"}, {"row": 5, "decision": "reword", "words": "  ask about orders "},
        {"row": 6, "decision": "nothing"}]), match_rows={1})
    assert needs is None and anomalies == []
    assert data["profile"] == "demo" and data["run"] == "r"
    assert data["decisions"] == [
        {"row": 1, "decision": "keep"}, {"row": 3, "decision": "change", "words": "revenue nets refunds"},
        {"row": 4, "decision": "fix"}, {"row": 5, "decision": "reword", "words": "ask about orders"},
        {"row": 6, "decision": "nothing"}]


def test_a_keep_on_a_row_the_run_did_not_match_is_refused_and_the_block_goes_back():
    data, anomalies, needs = pr.parse(_block([{"row": 2, "decision": "keep"}, {"row": 1, "decision": "keep"}]), match_rows={1})
    assert data["decisions"] == [{"row": 1, "decision": "keep"}]
    assert {a["kind"] for a in anomalies} == {"keep_not_offered"}
    assert needs["kind"] == "decisions_dropped" and needs["rows"] == [2]
    # Without the run's match rows, no keep can be checked and none is applied.
    _data, anomalies, needs = pr.parse(_block([{"row": 1, "decision": "keep"}]))
    assert anomalies[0]["kind"] == "keep_not_offered" and needs is not None


def test_dropped_decisions_send_the_block_back_and_bad_json_is_unparseable(tmp_path, capsys):
    _d, anomalies, needs = pr.parse(_block([{"row": 1, "decision": "kep"}, {"row": 1, "decision": "fix"},
                                            {"row": 1, "decision": "fix"}, {"decision": "fix"}, "x"]))
    assert {a["kind"] for a in anomalies} == {"unknown_decision", "row_decided_twice", "decision_missing_row", "decision_not_an_object"}
    assert needs["kind"] == "decisions_dropped"
    _d, anomalies, needs = pr.parse("profile: demo\nreconcile-run: r\ndecisions:\n[not json\ndone\n")
    assert needs["kind"] == "unparseable_json"
    block = tmp_path / "block.txt"
    block.write_text(_block([{"row": 1, "decision": "kep"}]))
    assert pr.main(["--block-file", str(block), "--match-rows", "1, 4"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is False
