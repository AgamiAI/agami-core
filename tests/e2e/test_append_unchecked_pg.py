"""Appends finish a join-candidate scan the probe cap stopped, on a real Postgres catalog (#693).

The documented way to finish a capped scan is `introspect --append` over the tables it left
unchecked. On Postgres that crashed: a table with no schema reached a catalog read as None, and every
catalog read there names its schema. Then, once it ran, each append tested the same first pairs. The
fixture has more candidate pairs than the cap allows, mixed-case table names and one schema.
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
        "set AGAMI_IT_PG_PASSWORD to finish a capped join-candidate scan against the compose fixture",
        module_level=True,
    )

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from semantic_model import introspect as I  # noqa: E402

SCHEMA = "append_unchecked"
TABLES = [f"SomeTable{k}Name" for k in range(1, 7)]
CAP = 6


def _ddl() -> str:
    # `ref` holds ids every table has; `miss` holds values no key has, so it is tested against every key.
    # Text, not whole numbers: a whole number is never tested on its type alone (#694).
    out = [f"CREATE SCHEMA {SCHEMA};"]
    for t in TABLES:
        out += [f'CREATE TABLE {SCHEMA}."{t}" ("{t}Id" text PRIMARY KEY, ref text, miss text);',
                f'INSERT INTO {SCHEMA}."{t}" SELECT g::text, g::text, (900 + g)::text FROM generate_series(1, 4) g;']
    return "".join(out)


@pytest.fixture(scope="module")
def runner():
    assert harness.PG_HOST in harness.LOOPBACK_HOSTS, "this fixture creates and drops a schema; local only"
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


def _build(runner, tmp_path: Path, tables: list[str], append: bool = False) -> dict:
    out = tmp_path / "demo"
    I.introspect("demo", "postgres", runner=runner, artifacts_dir=tmp_path, out_dir=out, tables=tables,
                 append=append)
    return json.loads((out / ".introspect" / "join_candidates.json").read_text())


def test_appends_over_the_unchecked_tables_finish_the_scan(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(I, "JOIN_CANDIDATE_PROBE_CAP", CAP)
    doc = _build(runner, tmp_path, [f"{SCHEMA}.{t}" for t in TABLES])
    assert doc["cap_hit"] and doc["unchecked_tables"]
    assert all(u.startswith(f"{SCHEMA}.SomeTable") for u in doc["unchecked_tables"])
    appends = 0
    while doc["unchecked_tables"]:
        appends += 1
        assert appends <= len(TABLES) * len(TABLES), "the appends stopped making progress"
        doc = _build(runner, tmp_path, doc["unchecked_tables"], append=True)
    assert sorted(c["op"]["from_table"] for c in doc["candidates"]) == TABLES
    assert doc["tested_pairs"] == []


def test_a_bare_mixed_case_name_is_read_in_its_one_schema(runner, tmp_path):
    # The crash itself: `SomeTable1Name` with no schema reached `table_schema = <None>`.
    _build(runner, tmp_path, [f"{SCHEMA}.{t}" for t in TABLES])
    doc = _build(runner, tmp_path, ["SomeTable1Name"], append=True)
    assert f"{SCHEMA}.SomeTable1Name" not in doc["unchecked_tables"]
