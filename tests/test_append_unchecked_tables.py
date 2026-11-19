"""Finishing a join-candidate scan the probe cap stopped, with `introspect --append` (#693).

Two failures stood between a capped scan and a finished one. A table name reaching a catalog read
with no schema crashed with `'NoneType' object has no attribute 'replace'`: a bare name in the
allowlist, or a `--tables-file` line split at a space inside a table name. And an append over the
tables left unchecked tested the same first pairs again, so the list never emptied. Synthetic,
generic names only.
"""

from __future__ import annotations

import argparse
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

from semantic_model import cli  # noqa: E402
from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as I  # noqa: E402

PG = D.get_dialect("postgres")


# --- a table never reaches a catalog read without a schema -------------------------------------


def _catalog(listing: dict[str, list[str]]):
    """A Postgres catalog that lists `listing` (schema -> tables) and answers nothing else."""
    answers = {PG.sql_schemas(): [{"schema_name": s} for s in listing]}
    for s, tables in listing.items():
        answers[PG.sql_tables(s)] = [{"schema_name": s, "table_name": t, "table_type": "BASE TABLE"} for t in tables]

    def run(sql: str) -> list[dict]:
        return answers.get(sql, [])
    return run


def _report() -> I.IntrospectReport:
    return I.IntrospectReport("demo", "postgres", "out", True)


def test_quote_lit_refuses_none_with_an_error_that_says_what_is_missing():
    with pytest.raises(D.MissingName, match="no schema"):
        PG.quote_lit(None)


def test_a_bare_table_name_takes_the_one_schema_the_catalog_lists_it_in():
    run = _catalog({"public": ["SomeTableName", "Order Lines"], "archive": ["Other"]})
    pairs = I._discover_tables(PG, run, ["SomeTableName", "sometablename", "public.Order Lines"], _report())
    # exact first, then ignoring case with the catalog's spelling; a stated schema is left alone
    assert pairs == [("public", "SomeTableName"), ("public", "SomeTableName"), ("public", "Order Lines")]


def test_a_bare_name_the_catalog_cannot_settle_is_refused_by_name_before_any_read():
    run = _catalog({"public": ["Shared"], "archive": ["Shared"]})
    with pytest.raises(I.TableSchemaUnknown) as e:
        I._discover_tables(PG, run, ["Shared", "Missing", "public.Fine"], _report())
    assert "Shared (in public.Shared, archive.Shared)" in str(e.value)
    assert "Missing (in no schema the catalog lists)" in str(e.value)


def test_a_table_with_no_schema_is_refused_by_name_at_the_read_itself():
    with pytest.raises(I.TableSchemaUnknown, match="SomeTableName"):
        I._build_table(PG, _catalog({}), None, "SomeTableName", "demo_pg", _report())


def test_sqlite_reads_a_bare_name_as_it_always_has():
    run = _catalog({})
    assert I._discover_tables(D.get_dialect("sqlite"), run, ["orders"], _report()) == [(None, "orders")]


def test_a_tables_file_line_is_one_name_even_with_a_space_in_it(tmp_path, monkeypatch):
    # The file is how the report says to finish a capped scan: one `schema.table` per line. Split
    # at the space, `public.Order Lines` became `public.Order` and a bare `Lines`, which then reached
    # a catalog read with no schema.
    f = tmp_path / "unchecked.txt"
    f.write_text("public.Order Lines\n# a comment\n\npublic.SomeTableName\n", encoding="utf-8")
    monkeypatch.setattr(I, "make_execute_sql_runner", lambda *a, **k: (lambda sql: []))
    monkeypatch.setattr(cli, "_may_prompt_to_sign_in", lambda profile: False)
    monkeypatch.setattr(I.PF, "WORKERS", I.PF.WORKERS)
    args = argparse.Namespace(tables=None, tables_file=str(f), batch=None, profile="demo",
                              artifacts=str(tmp_path))
    rc, (tables, _runner, _batch) = cli._introspect_inputs(args)
    assert rc == 0 and tables == ["public.Order Lines", "public.SomeTableName"]


# --- appends finish the scan -------------------------------------------------------------------

TABLES = 6


def _schema() -> str:
    """Per table: `ref` holds ids every table has (borne out against any key), and `miss` holds
    values no key has, so it is tested against every key and found to be noise. Text, not whole
    numbers: a whole number is never tested on its type alone (#694)."""
    out = []
    for k in range(1, TABLES + 1):
        out += [f"CREATE TABLE t{k}(id TEXT PRIMARY KEY, ref TEXT, miss TEXT);",
                f"INSERT INTO t{k} VALUES ('1', '1', '901'), ('2', '2', '902'), ('3', '3', '903'), ('4', '2', '904');"]
    return "".join(out)


def _runner(db: Path):
    def run(sql: str) -> list[dict]:
        con = sqlite3.connect(db)
        try:
            cur = con.execute(sql)
            names = [d[0] for d in cur.description or []]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            con.close()
    return run


@pytest.fixture
def build(tmp_path, monkeypatch):
    db = tmp_path / "shop.db"
    con = sqlite3.connect(db)
    con.executescript(_schema())
    con.commit()
    con.close()
    out = tmp_path / "demo"
    tested: list[list[tuple]] = []   # the pairs each run tested
    run_checks = I.PF.run_checks

    def checks(dialect, runner, cs, **kw):
        tested[-1].extend((c.ft.name, c.fc[0], c.tt.name, c.tc[0]) for c in cs)
        return run_checks(dialect, runner, cs, **kw)

    monkeypatch.setattr(I.PF, "run_checks", checks)

    def go(tables=None, append=False):
        tested.append([])
        _, report = I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path, out_dir=out,
                                 tables=tables, append=append)
        doc = json.loads((out / ".introspect" / "join_candidates.json").read_text())
        return report, doc, tested[-1]
    return go


def test_one_round_tests_a_column_against_one_key_at_a_time(build):
    # Every `ref` is borne out by the first key it meets. Tested a column at a time, a round spent
    # its checks on one `ref` against every key: one column settled per round.
    _, doc, tested = build()
    refs = [p for p in tested if p[1] == "ref"]
    assert len(refs) == TABLES
    assert len(doc["candidates"]) == TABLES and not doc["cap_hit"]


def test_appends_over_the_unchecked_tables_finish_a_capped_scan(build, monkeypatch):
    monkeypatch.setattr(I, "JOIN_CANDIDATE_PROBE_CAP", 4)
    _, doc, first = build()
    assert doc["cap_hit"] and doc["unchecked_tables"]
    runs = [first]
    while doc["unchecked_tables"]:
        assert len(runs) <= 2 * TABLES * TABLES // 4, "the appends stopped making progress"
        _, doc, tested = build(tables=doc["unchecked_tables"], append=True)
        runs.append(tested)
    # No append tested a pair an earlier run of the unfinished scan had tested ...
    seen: set = set()
    for tested in runs:
        assert not seen & set(tested)
        seen |= set(tested)
    # ... every pair was tested once in all, each `ref` kept its candidate, and the finished scan
    # stores no memory, so the next refresh tests on the data as it is then.
    assert len(seen) == TABLES + TABLES * (TABLES - 1)   # one key per `ref`, every key per `miss`
    assert sorted(c["op"]["from_table"] for c in doc["candidates"]) == [f"t{k}" for k in range(1, TABLES + 1)]
    assert doc["tested_pairs"] == []


def test_a_damaged_memory_entry_costs_probes_not_the_run():
    doc = {"tested_pairs": [["main", "t1", "miss", "main", "t2", "id"], ["short"], "nope", [1, 2, 3, 4, 5, 6]]}
    assert I._tested_pairs(doc) == {(("main", "t1", "miss"), ("main", "t2", "id"))}
