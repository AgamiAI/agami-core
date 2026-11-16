"""A batched build checks each join once and keeps what earlier batches found (ACE-180).

An `--append` batch tests only pairs that involve a table it reads; a join between two tables an
earlier batch read keeps its stored check. Join candidates are merged across batches, never replaced;
a table's own key is never paired with another table's own key; and when the probe cap stops the
scan, the tables it left untested are named. Synthetic, generic names only.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import introspect as I  # noqa: E402
from semantic_model.models import Column, Table  # noqa: E402

BATCHES = 6


def _schema(declared: bool = False) -> str:
    """Per batch k: `a{k}` (a parent), and `b{k}` whose `a{k}_id` names its parent (a probed join, or
    a declared foreign key), whose `ref` holds ids every table has (a type-only join candidate), and
    whose `alt_ref` matches nothing (a pair tested and found to be noise, which nothing stores)."""
    out = []
    for k in range(1, BATCHES + 1):
        fk = f" REFERENCES a{k}(id)" if declared else ""
        out += [
            f"CREATE TABLE a{k}(id INTEGER PRIMARY KEY, label TEXT);",
            f"CREATE TABLE b{k}(id INTEGER PRIMARY KEY, a{k}_id INTEGER{fk}, ref INTEGER, alt_ref INTEGER);",
            f"INSERT INTO a{k} VALUES (1, 'x'), (2, 'y'), (3, 'z');",
            f"INSERT INTO b{k} VALUES (1, 1, 1, 901), (2, 2, 2, 902), (3, 3, 3, 903), (4, 1, 2, 904);",
        ]
    return "".join(out)


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


def _db(tmp_path: Path, schema: str) -> Path:
    db = tmp_path / "shop.db"
    con = sqlite3.connect(db)
    con.executescript(schema)
    con.commit()
    con.close()
    return db


class _Counter:
    """Every data check the build issues, by the pair it compares: the join and candidate checks,
    the name-matched overlap probes and the uniqueness probes."""

    def __init__(self, monkeypatch) -> None:
        self.batch: list[tuple] = []
        run_checks, overlap, distinct = I.PF.run_checks, I._overlap_count, I._distinct_rows

        def checks(dialect, runner, cs):
            self.batch += [("check", c.ft.name, tuple(c.fc), c.tt.name, tuple(c.tc)) for c in cs]
            return run_checks(dialect, runner, cs)

        def overlap_count(dialect, runner, ft, fc, tt, tc):
            self.batch.append(("overlap", ft.name, (fc,), tt.name, (tc,)))
            return overlap(dialect, runner, ft, fc, tt, tc)

        def distinct_rows(dialect, runner, schema, table, column):
            self.batch.append(("unique", table, (column,)))
            return distinct(dialect, runner, schema, table, column)

        monkeypatch.setattr(I.PF, "run_checks", checks)
        monkeypatch.setattr(I, "_overlap_count", overlap_count)
        monkeypatch.setattr(I, "_distinct_rows", distinct_rows)

    def take(self) -> list[tuple]:
        out, self.batch = self.batch, []
        return out


def _six_batches(tmp_path: Path, counter: _Counter | None = None, declared: bool = False):
    db = _db(tmp_path, _schema(declared))
    out = tmp_path / "demo"
    per_batch, reports = [], []
    for k in range(1, BATCHES + 1):
        _, report = I.introspect(
            "demo",
            "sqlite",
            runner=_runner(db),
            artifacts_dir=tmp_path,
            out_dir=out,
            tables=[f"main.a{k}", f"main.b{k}"],
            append=k > 1,
            progress_path=tmp_path / f"progress{k}.log",
        )
        reports.append(report)
        if counter is not None:
            per_batch.append(counter.take())
    return out, reports, per_batch


def _candidates(out: Path) -> dict:
    return json.loads((out / ".introspect" / "join_candidates.json").read_text())


@pytest.mark.parametrize("declared", [False, True], ids=["probed joins", "declared keys"])
def test_a_later_batch_issues_no_check_for_a_pair_an_earlier_batch_checked(
    tmp_path, monkeypatch, declared
):
    counter = _Counter(monkeypatch)
    out, reports, per_batch = _six_batches(tmp_path, counter, declared)
    assert reports[0].mode_per_capability["relationships"] == ("catalog" if declared else "probe")
    assert all(per_batch), "every batch checks its own new tables"
    seen: set[tuple] = set(per_batch[0])
    for k, issued in enumerate(per_batch[1:], 2):
        again = sorted(set(issued) & seen)
        assert not again, f"batch {k} re-checked {again}"
        seen |= set(issued)
    # A stored check is kept, not dropped: batch 1's join is still in the profile after batch 6.
    joins = {
        j["join"] for j in json.loads((out / ".introspect" / "profile.json").read_text())["joins"]
    }
    assert {f"b{k}.a{k}_id -> a{k}.id" for k in range(1, BATCHES + 1)} <= joins


def test_a_refreshed_table_is_checked_again(tmp_path, monkeypatch):
    # Re-reading a table is the one reason to check its joins again: its data may have changed.
    counter = _Counter(monkeypatch)
    _six_batches(tmp_path, counter)
    I.introspect(
        "demo",
        "sqlite",
        runner=_runner(tmp_path / "shop.db"),
        artifacts_dir=tmp_path,
        out_dir=tmp_path / "demo",
        tables=["main.b1"],
        append=True,
    )
    issued = counter.take()
    assert ("check", "b1", ("a1_id",), "a1", ("id",)) in issued
    assert not any(
        "b2" in (x[1], x[3]) and "b1" not in (x[1], x[3]) for x in issued if x[0] != "unique"
    )


def test_batch_one_candidates_survive_every_later_append(tmp_path):
    out, reports, _ = _six_batches(tmp_path)
    first = {c["id"] for c in reports[0].join_candidates}
    assert "main.b1.ref->main.a1.id" in first
    doc = _candidates(out)
    assert first <= {c["id"] for c in doc["candidates"]}
    # One per column across batches too: b1.ref is not offered again against a later batch's key.
    assert len([c for c in doc["candidates"] if c["id"].startswith("main.b1.ref->")]) == 1
    assert {f"main.b{k}.ref" for k in range(1, BATCHES + 1)} == {
        c["id"].split("->")[0] for c in doc["candidates"]
    }


def test_a_stored_candidate_keeps_its_better_evidence(tmp_path):
    out, _, _ = _six_batches(tmp_path)
    path = out / ".introspect" / "join_candidates.json"
    doc = json.loads(path.read_text())
    keep = doc["candidates"][0]
    weaker = {**keep, "evidence": {**keep["evidence"], "match_rate": 0.1}}
    report = I.IntrospectReport(profile="demo", db_type="sqlite", out_dir=str(out), dry_run=False)
    report.join_candidates_low.append(weaker)
    from semantic_model.loader import load_datasource

    org = load_datasource(out)
    tables = [t for sa in org.subject_areas for t in sa.tables_defined]
    rels = [r for sa in org.subject_areas for r in sa.relationships] + list(
        org.cross_subject_area_relationships
    )
    I._merge_candidates(doc, report, set(), rels, tables, "sqlite")
    assert [c["evidence"] for c in report.join_candidates if c["id"] == keep["id"]] == [
        keep["evidence"]
    ]
    assert keep["id"] not in {c["id"] for c in report.join_candidates_low}


def test_the_report_and_the_progress_give_candidates_found_beside_joins_written(tmp_path):
    _, reports, _ = _six_batches(tmp_path)
    last = reports[-1]
    n = len(last.join_candidates)
    assert n == BATCHES
    assert f"relationships: {last.relationship_count}   join candidates found: {n}" in last.render()
    done = [
        ln
        for ln in (tmp_path / f"progress{BATCHES}.log").read_text().splitlines()
        if ln.startswith("done:")
    ]
    assert done == [
        f"done: {last.table_count} tables, {last.relationship_count} joins written, "
        f"{n} join candidates found"
    ]


# --- a table's own key is never paired with another table's own key ------------------------------


def _t(name: str, cols: list[dict], grain: list[str]) -> Table:
    return Table(name=name, schema_name="main", columns=[Column(**c) for c in cols], grain=grain)


def test_no_key_is_paired_with_another_tables_key():
    # Every table here is keyed on `rec_key`. `big` is too large for the uniqueness probe, so its key
    # is unknown, yet its `rec_key` is still its own key and not a reference to `left` or `right`.
    key = {"name": "rec_key", "type": "string", "primary_key": True}
    tables = [
        _t("left", [key, {"name": "label", "type": "string"}], ["rec_key"]),
        _t("right", [key], ["rec_key"]),
        _t(
            "big",
            [{"name": "rec_key", "type": "string"}, {"name": "left_ref", "type": "string"}],
            [],
        ),
    ]
    pairs = [
        (t.name, c.name, u.name, k) for _, t, c, u, k in I._candidate_pairs([], tables, "sqlite")
    ]
    assert not [p for p in pairs if p[1] == "rec_key" and p[3] == "rec_key"], pairs
    assert ("big", "left_ref", "left", "rec_key") in pairs  # a real reference is still tested


def test_a_key_named_like_one_other_tables_key_is_still_a_reference():
    # `orders` has no known key and holds `cust_code`, the key of `customers` alone: a reference.
    tables = [
        _t(
            "customers",
            [{"name": "cust_code", "type": "string", "primary_key": True}],
            ["cust_code"],
        ),
        _t("orders", [{"name": "cust_code", "type": "string"}], []),
    ]
    pairs = [
        (t.name, c.name, u.name, k) for _, t, c, u, k in I._candidate_pairs([], tables, "sqlite")
    ]
    assert pairs == [("orders", "cust_code", "customers", "cust_code")]


# --- the cap names what it left untested -----------------------------------------------------------


def test_the_cap_names_the_tables_it_left_unchecked(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "JOIN_CANDIDATE_PROBE_CAP", 1)
    db = _db(tmp_path, _schema())
    out = tmp_path / "demo"
    _, report = I.introspect(
        "demo",
        "sqlite",
        runner=_runner(db),
        artifacts_dir=tmp_path,
        out_dir=out,
        tables=[f"main.{p}{k}" for k in (1, 2) for p in "ab"],
    )
    assert report.join_candidate_unchecked == ["main.b1", "main.b2"]
    assert "not tested past the cap: main.b1, main.b2" in report.render()
    assert _candidates(out)["unchecked_tables"] == ["main.b1", "main.b2"]
    # The next batch tests only its own tables, so the earlier ones stay listed until re-read.
    _, report = I.introspect(
        "demo",
        "sqlite",
        runner=_runner(db),
        artifacts_dir=tmp_path,
        out_dir=out,
        tables=["main.a3", "main.b3"],
        append=True,
    )
    assert _candidates(out)["unchecked_tables"] == ["main.b1", "main.b2", "main.b3"]
    monkeypatch.setattr(I, "JOIN_CANDIDATE_PROBE_CAP", 300)
    _, report = I.introspect(
        "demo",
        "sqlite",
        runner=_runner(db),
        artifacts_dir=tmp_path,
        out_dir=out,
        tables=["main.b1"],
        append=True,
    )
    assert _candidates(out)["unchecked_tables"] == ["main.b2", "main.b3"]
