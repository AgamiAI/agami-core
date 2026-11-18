"""A join the data dictionary states is offered on a real Postgres catalog, past a spent scan cap (#694).

The fixture declares no foreign keys, mixes the case of its names and lives in one schema. The
dictionary says where two columns point; the scan's probe cap is already spent. One reference holds
on the data and is offered; the other does not and comes back as a note with its numbers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import itdeps  # noqa: E402

itdeps.importorfail("psycopg2")

import harness  # noqa: E402

if not harness.PG_ENABLED:
    itdeps.skip_or_fail(
        "set AGAMI_IT_PG_PASSWORD to check dictionary-stated joins against the compose fixture",
        module_level=True,
    )

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from semantic_model import cli  # noqa: E402
from semantic_model import dialects as D  # noqa: E402
from semantic_model import dictionary_joins as DJ  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model.loader import load_datasource  # noqa: E402

SCHEMA = "dictionary_joins"


def _ddl() -> str:
    return (
        f"CREATE SCHEMA {SCHEMA};"
        f'CREATE TABLE {SCHEMA}."ParentTable" ("ParentKey" text PRIMARY KEY, label text);'
        f'CREATE TABLE {SCHEMA}."ChildTable" (id int PRIMARY KEY, "parentRef" text, wrong_ref text);'
        f"INSERT INTO {SCHEMA}.\"ParentTable\" SELECT g::text, 'x' FROM generate_series(1, 4) g;"
        f"INSERT INTO {SCHEMA}.\"ChildTable\" SELECT g, (1 + g % 4)::text, 'Q' || g FROM generate_series(1, 20) g;"
    )


@pytest.fixture(scope="module")
def runner():
    assert harness.PG_HOST in harness.LOOPBACK_HOSTS, (
        "this fixture creates and drops a schema; local only"
    )
    owner = psycopg2.connect(harness.pg_dsn(harness.PG_USER, harness.PG_PASSWORD))
    owner.autocommit = True
    with owner.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(_ddl())

    def run(sql: str) -> list[dict]:
        with owner.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()] if cur.description else []

    try:
        yield run
    finally:
        with owner.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        owner.close()


def test_a_stated_reference_is_offered_past_a_spent_cap_and_one_that_fails_is_a_note(
    runner, tmp_path, monkeypatch
):
    monkeypatch.setattr(I, "JOIN_CANDIDATE_PROBE_CAP", 0)
    out = tmp_path / "acme"
    I.introspect(
        "acme",
        "postgres",
        runner=runner,
        artifacts_dir=tmp_path,
        out_dir=out,
        tables=[f"{SCHEMA}.ParentTable", f"{SCHEMA}.ChildTable"],
    )
    dic = tmp_path / "dictionary.json"
    dic.write_text(
        json.dumps(
            [
                {
                    "schema": SCHEMA,
                    "table": "ChildTable",
                    "column": "parentRef",
                    "description": "TEXT. FK -> parenttable.parentkey",
                },
                {
                    "schema": SCHEMA,
                    "table": "ChildTable",
                    "column": "wrong_ref",
                    "description": f"Foreign key referencing {SCHEMA}.ParentTable.ParentKey",
                },
            ]
        )
    )
    assert cli.main(["describe-file", str(out), "--source", "metadata", "--file", str(dic)]) == 0

    found = DJ.find(load_datasource(out), runner, D.get_dialect("postgres"), "postgres")
    DJ.store(out, found)
    assert [c["id"] for c in found.candidates] == [
        f"{SCHEMA}.ChildTable.parentRef->{SCHEMA}.ParentTable.ParentKey"
    ]
    [cand] = found.candidates
    assert cand["evidence"]["found_by"] == "dictionary" and cand["evidence"]["match_rate"] == 1.0
    [note] = found.notes
    assert note["column"] == f"{SCHEMA}.ChildTable.wrong_ref" and note["match_rate"] == 0.0
    doc = json.loads((out / ".introspect" / "join_candidates.json").read_text())
    assert cand["id"] in {c["id"] for c in doc["candidates"]}
