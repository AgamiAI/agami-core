"""`reconcile.py status` — the status a row gets, applied by code and never by feel.

Two rules carry the weight. A number that matches while a part of the person's statement is not
confirmed is `match_unverified`, because two wrong statements agree easily and Phase 3e must never see
it. A number that differs beside a defect in the person's statement is `expected_doubtful`, because
the expected value itself is in doubt and the row must not be counted against the AI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402
from reconcile import row_status  # noqa: E402


@pytest.mark.parametrize("match, verdict, expected", [
    (True, None, "match"),
    (True, "confirmed", "match"),
    (True, "model_gap", "match_unverified"),
    (True, "unresolved", "match_unverified"),
    (True, "query_defect", "match_unverified"),
    (False, None, "mismatch"),
    (False, "confirmed", "mismatch"),
    (False, "model_gap", "mismatch"),
    (False, "unresolved", "mismatch"),
    (False, "query_defect", "expected_doubtful"),
    (None, None, "error"),
    (None, "confirmed", "error"),
])
def test_the_status_rules(match, verdict, expected):
    assert row_status(match, verdict) == expected


def test_the_verb_prints_the_same_answer(capsys):
    assert reconcile.main(["status", "--match", "true", "--ledger-verdict", "unresolved"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "match_unverified"}
    assert reconcile.main(["status", "--match", "false", "--ledger-verdict", "query_defect"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "expected_doubtful"}
    assert reconcile.main(["status", "--match", "none"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "error"}
