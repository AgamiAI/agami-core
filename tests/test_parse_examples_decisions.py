"""parse_examples_decisions.py: what the seed examples page saved is checked in code, not prose (#651)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import parse_examples_decisions as P  # noqa: E402

ITEMS = {
    "items": [
        {
            "n": 1,
            "area": "sales",
            "question": "How many orders?",
            "sql": "SELECT COUNT(*) FROM orders",
        },
        {"n": 2, "area": "sales", "question": "Revenue?", "sql": "SELECT SUM(amount) FROM orders"},
        {
            "n": 3,
            "area": "sales",
            "question": "Broken?",
            "sql": "SELECT nope",
            "error": "column nope",
        },
    ]
}


def _run(
    tmp_path: Path, doc: object, *extra: str, items: object = ITEMS, capsys=None
) -> tuple[int, dict]:
    d = tmp_path / "decisions.json"
    d.write_text(json.dumps(doc))
    i = tmp_path / "items.json"
    i.write_text(json.dumps(items))
    rc = P.main(
        ["--decisions-file", str(d), "--items-file", str(i), "--expect-profile", "demo", *extra]
    )
    return rc, json.loads(capsys.readouterr().out)


def _doc(*decisions: dict, **over) -> dict:
    return {
        "page": "examples",
        "profile": "demo",
        "decisions": list(decisions),
        "note_all": None,
        **over,
    }


def _d(n: int, action: str, **more) -> dict:
    it = ITEMS["items"][n - 1]
    return {"n": n, "area": it["area"], "question": it["question"], "action": action, **more}


def test_each_action_lands_where_its_command_reads_it(tmp_path, capsys):
    doc = _doc(
        _d(1, "keep"),
        _d(2, "fix", sql=" SELECT 2 "),
        _d(3, "drop"),
        _d(2, "note", note="in USD") | {"n": 2},
        note_all=" amounts are in USD ",
    )
    rc, out = _run(tmp_path, doc, "--out-dir", str(tmp_path / "out"), capsys=capsys)
    assert rc == 0, out
    data = out["data"]
    # the kept SQL is the items file's, never the post's
    assert data["keep"] == {
        "sales": [{"question": "How many orders?", "sql": "SELECT COUNT(*) FROM orders"}]
    }
    assert data["fix"] == {"sales": [{"question": "Revenue?", "sql": "SELECT 2"}]}
    assert data["drop"] == ["sales/Broken?"]
    assert data["note_all"] == "amounts are in USD"
    assert out["refused"] == [{"n": 2, "reason": "decided twice"}]
    assert json.loads(Path(data["files"]["keep"]["sales"]).read_text()) == data["keep"]["sales"]


@pytest.mark.parametrize(
    ("decision", "reason"),
    [
        ("not a dict", "not an object"),
        (_d(1, "keep") | {"signer": "x@example.com"}, "unknown keys: signer"),
        (_d(1, "keep") | {"n": "1"}, "n is not a number"),
        (_d(1, "approve"), "action 'approve' is not one of"),
        (_d(1, "keep") | {"action": []}, "is not one of"),  # unhashable: a refusal, not a crash
        (_d(1, "keep") | {"action": {}}, "is not one of"),
        (_d(2, "fix"), "a fix needs its sql"),
        (_d(2, "fix", sql="  "), "a fix needs its sql"),
        (_d(2, "note"), "a note needs its note"),
        (_d(1, "keep", sql="DROP TABLE orders"), "a keep carries no sql"),
        (_d(1, "drop", note="x"), "a drop carries no note"),
        (_d(1, "keep") | {"n": 9}, "no such example on the page"),
        (_d(1, "keep") | {"question": "Revenue?"}, "not that example's"),
        (_d(1, "keep") | {"area": "finance"}, "not that example's"),
        (_d(3, "keep"), "it failed, so it has no result to keep"),
    ],
)
def test_a_bad_decision_is_refused_and_the_rest_still_apply(tmp_path, capsys, decision, reason):
    rc, out = _run(tmp_path, _doc(decision, _d(2, "drop")), capsys=capsys)
    assert rc == 0
    assert out["data"]["drop"] == ["sales/Revenue?"]
    assert len(out["refused"]) == 1 and reason in out["refused"][0]["reason"]
    assert not out["data"]["keep"] and not out["data"]["fix"]


@pytest.mark.parametrize(
    ("doc", "error"),
    [
        ([1], "not a JSON object"),
        (_doc(page="model-explorer"), "not the examples page"),
        (_doc(profile="other"), "another model"),
        (_doc() | {"extra": 1}, "unknown keys: extra"),
        (_doc() | {"decisions": {}}, "decisions is not a list"),
        (_doc(note_all=3), "note_all is not text"),
    ],
)
def test_a_file_for_another_page_or_model_or_malformed_is_refused_whole(
    tmp_path, capsys, doc, error
):
    rc, out = _run(tmp_path, doc, capsys=capsys)
    assert rc == 2 and error in out["error"]


def test_a_single_area_items_file_needs_its_area(tmp_path, capsys):
    items = [{"n": 1, "question": "How many orders?", "sql": "SELECT 1"}]
    doc = _doc({"n": 1, "area": None, "question": "How many orders?", "action": "keep"})
    rc, out = _run(tmp_path, doc, items=items, capsys=capsys)
    assert out["refused"] == [{"n": 1, "reason": "the items carry no area; pass --area"}]
    rc, out = _run(tmp_path, doc, "--area", "sales", items=items, capsys=capsys)
    assert out["data"]["keep"] == {"sales": [{"question": "How many orders?", "sql": "SELECT 1"}]}


def test_unreadable_files_are_an_error_not_a_crash(tmp_path, capsys):
    (tmp_path / "decisions.json").write_text("{" * 5)
    (tmp_path / "items.json").write_text(json.dumps(ITEMS))
    rc = P.main(
        [
            "--decisions-file",
            str(tmp_path / "decisions.json"),
            "--items-file",
            str(tmp_path / "items.json"),
            "--expect-profile",
            "demo",
        ]
    )
    assert rc == 2 and "could not read" in json.loads(capsys.readouterr().out)["error"]
    (tmp_path / "items.json").write_text('{"items": 3}')
    (tmp_path / "decisions.json").write_text("{}")
    rc = P.main(
        [
            "--decisions-file",
            str(tmp_path / "decisions.json"),
            "--items-file",
            str(tmp_path / "items.json"),
            "--expect-profile",
            "demo",
        ]
    )
    assert rc == 2


def test_each_check_writes_its_files_to_its_own_private_folder(tmp_path, capsys):
    """Two saves checked at once for the same area must not overwrite each other's files."""
    out = tmp_path / "tmp"
    _, first = _run(tmp_path, _doc(_d(1, "keep")), "--out-dir", str(out), capsys=capsys)
    _, second = _run(tmp_path, _doc(_d(2, "keep")), "--out-dir", str(out), capsys=capsys)
    a, b = (
        Path(first["data"]["files"]["keep"]["sales"]),
        Path(second["data"]["files"]["keep"]["sales"]),
    )
    assert a != b and a.parent != b.parent and a.parent.parent == out
    assert json.loads(a.read_text())[0]["question"] == "How many orders?"
    assert json.loads(b.read_text())[0]["question"] == "Revenue?"
    assert a.parent.stat().st_mode & 0o777 == 0o700
