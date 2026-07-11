"""ACE-038 (row-cap) + ACE-044 (per-call cap) — bound result sets at the single materialization
chokepoint: `fetchmany(cap + 1)`, never `fetchall`, truncate at the cap and flag it. The SQL is
never modified (no injected LIMIT). Effective cap = min(--max-rows, AGAMI_SQL_MAX_ROWS, 1000).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_override():
    # _max_rows_override is a module global set by main(); isolate every test from it.
    execute_sql._max_rows_override = None
    yield
    execute_sql._max_rows_override = None


class _FakeCur:
    """A cursor that would return `nrows` rows; records how the sink pulls them."""

    def __init__(self, ncols: int, nrows: int):
        self.description = [(f"c{i}",) for i in range(ncols)]
        self._rows = [tuple(range(ncols)) for _ in range(nrows)]
        self.fetchmany_args: list[int] = []
        self.fetchall_called = False

    def fetchmany(self, n: int):
        self.fetchmany_args.append(n)
        return self._rows[:n]

    def fetchall(self):
        self.fetchall_called = True
        return self._rows


def test_sink_bounds_at_cap_and_flags_truncation(monkeypatch, capsys):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "3")
    cur = _FakeCur(2, 10)  # 10 rows available, cap 3
    execute_sql._write_cursor_csv(cur)
    out = capsys.readouterr()

    assert len(out.out.strip().splitlines()) == 1 + 3  # header + exactly cap rows written
    assert cur.fetchmany_args == [4] and not cur.fetchall_called  # fetchmany(cap+1), never fetchall
    assert json.loads(out.err.strip())["truncated"]["row_cap"] == 3


def test_sink_no_flag_when_result_is_within_cap(monkeypatch, capsys):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "5")
    cur = _FakeCur(1, 2)  # 2 rows, cap 5
    execute_sql._write_cursor_csv(cur)
    out = capsys.readouterr()

    assert len(out.out.strip().splitlines()) == 1 + 2  # header + all rows
    assert out.err.strip() == ""  # not truncated → no flag


def test_effective_cap_is_min_of_env_and_per_call(monkeypatch):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "1000")
    assert execute_sql._resolve_row_cap() == 1000  # env only
    execute_sql._max_rows_override = 50
    assert execute_sql._resolve_row_cap() == 50  # per-call is smaller → wins
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "20")
    assert execute_sql._resolve_row_cap() == 20  # env smaller than per-call → wins
    monkeypatch.delenv("AGAMI_SQL_MAX_ROWS", raising=False)
    execute_sql._max_rows_override = None
    assert execute_sql._resolve_row_cap() == 1000  # missing env → default


def test_sqlite_end_to_end_caps_without_rewriting_sql(tmp_path, monkeypatch, capsys):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (n INTEGER)")
    con.executemany("INSERT INTO t (n) VALUES (?)", [(i,) for i in range(10)])
    con.commit()
    con.close()

    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "4")
    rc = execute_sql._execute_sqlite({"path": str(db)}, "SELECT n FROM t ORDER BY n")
    out = capsys.readouterr()

    assert rc == 0
    lines = out.out.strip().splitlines()
    assert lines[0] == "n" and lines[1:] == ["0", "1", "2", "3"]  # first 4 by the query's own ORDER BY
    assert json.loads(out.err.strip())["truncated"]["row_cap"] == 4
    # The cap came from the bounded fetch, not a rewrite: the SQL passed to sqlite is the caller's
    # verbatim (no LIMIT) — _write_cursor_csv never sees or edits the SQL.


def test_postgres_uses_a_server_side_named_cursor(monkeypatch, capsys):
    # Postgres needs a NAMED (server-side) cursor so the cap bounds transfer — psycopg2's default
    # cursor buffers the whole result. Inject a fake psycopg2 and assert the cursor is named + the
    # SQL is passed verbatim + the bounded fetchmany(cap+1) is used.
    seen: dict = {}

    class FakeCur:
        def __init__(self, name):
            seen["name"] = name
            self.description = [("n",)]
            self.itersize = None
            self._rows = [(i,) for i in range(3)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            seen["sql"] = sql

        def fetchmany(self, n):
            seen["fetchmany"] = n
            return self._rows[:n]

    class FakeConn:
        def cursor(self, name=None):
            return FakeCur(name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            pass

    class FakePG:
        @staticmethod
        def connect(**kw):
            return FakeConn()

    monkeypatch.setitem(sys.modules, "psycopg2", FakePG)
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "2")
    creds = {"host": "h", "port": "5432", "user": "u", "password": "p", "database": "d"}

    rc = execute_sql._execute_postgres(creds, "SELECT n FROM t")
    assert rc == 0
    assert seen["name"] == "agami_bounded"     # server-side cursor (bounds transfer, not just writes)
    assert seen["sql"] == "SELECT n FROM t"    # SQL verbatim — no injected LIMIT
    assert seen["fetchmany"] == 3              # cap(2)+1
