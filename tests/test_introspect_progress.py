"""Build progress you can see (#677): stamped progress lines, a heartbeat while the database is slow,
tables read a few at a time, and the local page that shows it all."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import progress_page as PP  # noqa: E402
from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import profile as PF  # noqa: E402

STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ")
N_TABLES = 8


@pytest.fixture
def shop_db(tmp_path) -> Path:
    """Eight small tables: a customer table, six that reference it, and one on its own."""
    db = tmp_path / "shop.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, region TEXT, tier TEXT)")
    con.executemany("INSERT INTO customers VALUES (?, ?, ?)",
                    [(i, ("north", "south", "east")[i % 3], ("gold", "silver")[i % 2]) for i in range(1, 41)])
    for k in range(1, N_TABLES - 1):
        con.execute(f"CREATE TABLE orders_{k} (id INTEGER PRIMARY KEY, customer_id INTEGER "
                    f"REFERENCES customers(id), status TEXT, amount REAL, placed_at TEXT)")
        con.executemany(f"INSERT INTO orders_{k} VALUES (?, ?, ?, ?, ?)",
                        [(i, 1 + i % 40, ("open", "shipped", "closed")[i % 3], i * 1.5, f"2026-01-{1 + i % 28:02d}")
                         for i in range(1, 61)])
    con.execute("CREATE TABLE warehouses (code TEXT PRIMARY KEY, city TEXT)")
    con.executemany("INSERT INTO warehouses VALUES (?, ?)", [("w1", "alpha"), ("w2", "beta")])
    con.commit()
    con.close()
    return db


def _runner(db: Path, delay: float = 0.0, slow_on: str = "", slow_for: float = 0.0):
    """A SQLite runner, each statement `delay` seconds slower; one matching `slow_on` waits `slow_for`."""
    def run(sql: str) -> list[dict]:
        if delay:
            time.sleep(delay)
        if slow_on and slow_on in sql:
            time.sleep(slow_for)
        con = sqlite3.connect(db)
        try:
            cur = con.execute(sql)
            names = [d[0] for d in cur.description or []]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            con.close()
    return run


# --- 1. every line is stamped ----------------------------------------------------------------------

def test_every_progress_line_starts_with_a_utc_timestamp(tmp_path, shop_db):
    pp = tmp_path / "progress.log"
    I.introspect("shop", "sqlite", runner=_runner(shop_db), artifacts_dir=tmp_path, dry_run=True,
                 progress_path=pp)
    lines = pp.read_text().splitlines()
    assert lines and all(STAMP.match(ln) for ln in lines), lines
    # The message after the stamp is what it always was, so a reader looking for it still finds it.
    msgs = [STAMP.sub("", ln) for ln in lines]
    assert msgs[0].startswith(f"discovered {N_TABLES} tables")
    assert any(m == f"columns+grain {N_TABLES}/{N_TABLES}" for m in msgs)
    assert msgs[-1].startswith("done:")


def test_a_batched_build_names_its_batch_first(tmp_path, shop_db):
    pp = tmp_path / "progress.log"
    I.introspect("shop", "sqlite", runner=_runner(shop_db), artifacts_dir=tmp_path, dry_run=True,
                 progress_path=pp, batch=(2, 5))
    assert STAMP.sub("", pp.read_text().splitlines()[0]) == "batch 2 of 5"


def test_join_checks_report_their_count(tmp_path, shop_db):
    pp = tmp_path / "progress.log"
    I.introspect("shop", "sqlite", runner=_runner(shop_db), artifacts_dir=tmp_path, dry_run=True,
                 progress_path=pp)
    msgs = [STAMP.sub("", ln) for ln in pp.read_text().splitlines()]
    checking = next(m for m in msgs if m.startswith("joins: checking "))
    total = int(checking.split()[2])
    assert f"joins checked {total}/{total}" in msgs, msgs


# --- 2. heartbeat ----------------------------------------------------------------------------------

def test_a_quiet_step_writes_a_heartbeat_naming_what_it_waits_on(tmp_path, shop_db, monkeypatch):
    monkeypatch.setattr(I, "HEARTBEAT_S", 0.2)
    pp = tmp_path / "progress.log"
    # One table's column read hangs for well over the heartbeat interval.
    I.introspect("shop", "sqlite", runner=_runner(shop_db, slow_on="'warehouses'", slow_for=1.2),
                 artifacts_dir=tmp_path, dry_run=True, progress_path=pp)
    msgs = [STAMP.sub("", ln) for ln in pp.read_text().splitlines()]
    beats = [m for m in msgs if m.startswith("waiting on the database for ")]
    assert beats, msgs
    assert any("reading columns and keys of warehouses" in b for b in beats), beats
    # Names only: no value the tables hold reaches the log.
    assert not any(v in "\n".join(msgs) for v in ("alpha", "beta", "gold", "north"))


def test_no_heartbeat_while_lines_keep_coming(tmp_path, shop_db, monkeypatch):
    monkeypatch.setattr(I, "HEARTBEAT_S", 30.0)
    pp = tmp_path / "progress.log"
    I.introspect("shop", "sqlite", runner=_runner(shop_db), artifacts_dir=tmp_path, dry_run=True,
                 progress_path=pp)
    assert "waiting on the database" not in pp.read_text()


def test_the_heartbeat_stops_with_the_build(tmp_path, shop_db, monkeypatch):
    monkeypatch.setattr(I, "HEARTBEAT_S", 0.1)
    pp = tmp_path / "progress.log"
    before = threading.active_count()
    I.introspect("shop", "sqlite", runner=_runner(shop_db), artifacts_dir=tmp_path, dry_run=True,
                 progress_path=pp)
    time.sleep(0.3)
    size = pp.stat().st_size
    time.sleep(0.4)
    assert pp.stat().st_size == size   # nothing written after the build returned
    assert threading.active_count() <= before


def test_a_heartbeat_says_minutes_once_past_one():
    assert I._quiet_for(45) == "45 s"
    assert I._quiet_for(61) == "1 min"
    assert I._quiet_for(31 * 60 + 5) == "31 min"


# --- 4. tables read a few at a time ----------------------------------------------------------------

def _tree(out: Path) -> dict[str, str]:
    # A fresh build mints a fresh org id; everything else must match byte for byte.
    return {str(p.relative_to(out)): re.sub(r"(?m)^org_id: \w+$", "org_id: X", p.read_text())
            for p in sorted(out.rglob("*"))
            # A snapshot is named by when it was taken; the progress log is stamped.
            if p.is_file() and p.name != "progress.log" and ".snapshots" not in p.parts}


def test_reading_tables_in_parallel_writes_the_same_model(tmp_path, shop_db, monkeypatch):
    runner = _runner(shop_db, delay=0.002)
    monkeypatch.setattr(PF, "WORKERS", 1)
    _, seq = I.introspect("shop", "sqlite", runner=runner, artifacts_dir=tmp_path / "seq")
    monkeypatch.setattr(PF, "WORKERS", 4)
    _, par = I.introspect("shop", "sqlite", runner=runner, artifacts_dir=tmp_path / "par")
    a, b = _tree(tmp_path / "seq" / "shop"), _tree(tmp_path / "par" / "shop")
    assert a.keys() == b.keys() and len(a) > 3
    for name in a:
        assert a[name].replace(str(tmp_path / "seq"), "X") == b[name].replace(str(tmp_path / "par"), "X"), name
    assert seq.render().replace(str(tmp_path / "seq"), "X") == par.render().replace(str(tmp_path / "par"), "X")


def _read(db: Path, workers: int, monkeypatch, delay: float) -> tuple[list, I.IntrospectReport, float]:
    monkeypatch.setattr(PF, "WORKERS", workers)
    dialect = D.get_dialect("sqlite")
    runner = _runner(db, delay=delay)
    pairs = I._discover_tables(dialect, runner, None, I.IntrospectReport("shop", "sqlite", "o", True))
    report = I.IntrospectReport("shop", "sqlite", "o", True)
    keys_unread: set = set()
    samples: dict = {}
    started = time.monotonic()
    tables = I._read_tables(dialect, runner, pairs, "shop_sqlite", report, I._CountBudget(600.0),
                            I._ReadBudget(I.DISTINCT_READ_CAP), keys_unread, samples)
    return [t.model_dump() for t in tables] + [sorted(keys_unread), sorted(samples)], report, \
        time.monotonic() - started


def test_tables_are_read_four_at_a_time_in_a_fixed_order(shop_db, monkeypatch):
    seq, seq_rep, seq_s = _read(shop_db, 1, monkeypatch, delay=0.03)
    par, par_rep, par_s = _read(shop_db, 4, monkeypatch, delay=0.03)
    assert par == seq
    assert par_rep == seq_rep
    # Eight tables of equal cost on four workers: about four times faster; 2.5 leaves room for a busy CI box.
    assert seq_s / par_s >= 2.5, (seq_s, par_s)


def test_one_worker_when_the_profile_signs_in_through_a_browser(shop_db, monkeypatch):
    seen: set[str] = set()
    real = _runner(shop_db)

    def runner(sql: str) -> list[dict]:
        seen.add(threading.current_thread().name)
        return real(sql)

    monkeypatch.setattr(PF, "WORKERS", 1)
    dialect = D.get_dialect("sqlite")
    pairs = I._discover_tables(dialect, runner, None, I.IntrospectReport("shop", "sqlite", "o", True))
    seen.clear()
    I._read_tables(dialect, runner, pairs, "c", I.IntrospectReport("shop", "sqlite", "o", True),
                   I._CountBudget(600.0), I._ReadBudget(10), set(), {})
    assert len(seen) == 1


def test_the_value_list_cap_holds_across_workers(shop_db, monkeypatch):
    monkeypatch.setattr(PF, "WORKERS", 4)
    dialect = D.get_dialect("sqlite")
    runner = _runner(shop_db)
    pairs = I._discover_tables(dialect, runner, None, I.IntrospectReport("shop", "sqlite", "o", True))
    reads = I._ReadBudget(3)
    tables = I._read_tables(dialect, runner, pairs, "c", I.IntrospectReport("shop", "sqlite", "o", True),
                            I._CountBudget(600.0), reads, set(), {})
    listed = sum(1 for t in tables for c in t.columns if c.choice_field)
    assert reads.left == 0 and listed <= 3 and reads.skipped > 0


# --- 3. the progress page --------------------------------------------------------------------------

LOG = """\
2026-11-17T10:00:00Z batch 2 of 5
2026-11-17T10:00:01Z discovered 52 tables — reading columns, grain, keys
2026-11-17T10:04:00Z columns+grain 30/52
2026-11-17T10:09:00Z columns+grain 52/52
2026-11-17T10:09:01Z building relationships (52 tables; FK + value-overlap probes)…
2026-11-17T10:10:00Z joins: checking 300 on the data
2026-11-17T10:20:00Z joins checked 140/300
2026-11-17T10:22:00Z waiting on the database for 2 min: checking joins of orders, items
"""


def _at(stamp: str) -> float:
    return PP.parse_stamp(stamp)


def test_the_summary_reads_stage_count_batch_and_elapsed():
    s = PP.summarise(LOG, now=_at("2026-11-17T10:23:30Z"))
    assert s["batch"] == {"n": 2, "of": 5}
    assert s["tables"] == {"done": 52, "of": 52}
    assert s["stage"] == "Checking joins on the data"
    assert s["count"] == {"label": "joins checked", "done": 140, "of": 300}
    assert s["elapsed_s"] == 23 * 60 + 30
    # Quiet since the last real line, at 10:20: the heartbeat is not progress.
    assert s["quiet_s"] == 3 * 60 + 30
    assert s["waiting_on"] == "checking joins of orders, items"
    states = {st["name"]: st["state"] for st in s["stages"]}
    assert states["Reading columns and keys"] == "done"
    assert states["Checking joins on the data"] == "current"
    assert states["Profiling columns"] == "todo"
    assert s["finished"] is False
    assert "remaining" not in json.dumps(s) and "estimate" not in json.dumps(s)


def test_the_summary_of_a_finished_and_an_empty_log():
    done = PP.summarise(LOG + "2026-11-17T10:40:00Z profiling 42 tables…\n"
                        "2026-11-17T10:41:00Z profiled 20/42 tables\n"
                        "2026-11-17T10:50:00Z done: 52 tables, 61 relationships\n",
                        now=_at("2026-11-17T11:00:00Z"))
    assert done["finished"] is True and done["stage"] == "Done"
    assert done["elapsed_s"] == 50 * 60 and done["quiet_s"] == 0
    assert all(st["state"] == "done" for st in done["stages"])
    empty = PP.summarise("", now=0.0)
    assert empty["stage"] is None and empty["count"] is None and empty["elapsed_s"] == 0
    # An old unstamped line is read, and only the clock is unknown.
    old = PP.summarise("columns+grain 3/9\n", now=0.0)
    assert old["tables"] == {"done": 3, "of": 9} and old["elapsed_s"] == 0


def test_the_summary_matches_what_the_engine_writes(tmp_path, shop_db):
    pp = tmp_path / "progress.log"
    I.introspect("shop", "sqlite", runner=_runner(shop_db), artifacts_dir=tmp_path, dry_run=True,
                 progress_path=pp)
    s = PP.summarise(pp.read_text(), now=time.time())
    assert s["finished"] is True and s["tables"] == {"done": N_TABLES, "of": N_TABLES}
    assert all(st["state"] == "done" for st in s["stages"])


@pytest.fixture
def page(tmp_path):
    log = tmp_path / "progress.log"
    log.write_text("2026-11-17T10:00:01Z discovered 13 tables — reading columns, grain, keys\n"
                   "2026-11-17T10:00:09Z columns+grain 9/13\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(SCRIPTS / "progress_page.py"), str(log), "--minutes", "1"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = proc.stdout.readline()
    assert line, proc.stderr.read()
    yield log, json.loads(line)["url"]
    proc.kill()
    proc.wait()


def _get(url: str, host: str | None = None) -> tuple[int, str, dict]:
    req = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, "", {}


def test_the_page_is_served_and_its_status_refreshes_from_the_log(page):
    log, url = page
    assert url.startswith("http://127.0.0.1:") and url.endswith("/")
    code, body, headers = _get(url)
    assert code == 200 and "<title>" in body and "status" in body
    assert "script-src 'sha256-" in headers["Content-Security-Policy"]
    code, body, _ = _get(url + "status")
    s = json.loads(body)
    assert code == 200 and s["tables"] == {"done": 9, "of": 13} and s["count"]["label"] == "tables read"
    with log.open("a", encoding="utf-8") as fh:
        fh.write("2026-11-17T10:00:20Z columns+grain 13/13\n"
                 "2026-11-17T10:00:21Z building relationships (13 tables; FK + value-overlap probes)…\n")
    s = json.loads(_get(url + "status")[1])
    assert s["tables"] == {"done": 13, "of": 13} and s["stage"] == "Finding relationships"


def test_the_page_answers_nothing_without_its_token_or_from_another_host(page):
    _, url = page
    root = url.rsplit("/", 2)[0] + "/"
    assert _get(root)[0] == 404
    assert _get(root + "status")[0] == 404
    assert _get(url + "../etc")[0] == 404
    assert _get(url, host="evil.example:80")[0] == 404


def test_a_missing_log_reads_as_not_started(tmp_path):
    s = PP.read_status(tmp_path / "nope.log", first_seen=None, now=0.0)
    assert s["stage"] is None and s["started"] is False
