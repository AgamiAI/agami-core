"""suggest-metrics proposes bindings that run and mean what they say (#514): identifiers quoted
for the dialect, date arithmetic by column type, NULL-safe flag rates, durations that leave out
rows ending before they start, no audit or planned/actual cross pairings — and the command prints
before it writes, test-runs only with --profile, and keeps degenerate results out of the model."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import build, cli  # noqa: E402
from semantic_model import dialects as D  # noqa: E402
from semantic_model import models as m  # noqa: E402


def _table(*cols, rows=None, name="tickets"):
    hints = m.PerformanceHints(estimated_row_count=rows) if rows is not None else None
    return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                   description="t", performance_hints=hints,
                   columns=[m.Column(name="id", type="integer", primary_key=True), *cols])


def _mets(table, db="postgresql"):
    # Every flag column's rate, so the bindings below are checked; none is proposed unasked (#530).
    return {x["name"]: x for x in build.suggest_metrics(table, D.get_dialect(db), flag_rates=True)}


def _binding(met):
    return next(iter(met["bindings"].values()))


# --- the SQL each dialect gets ------------------------------------------------

@pytest.mark.parametrize("db, expected", [
    ("postgresql", '"IsActive"'), ("redshift", '"IsActive"'), ("snowflake", '"IsActive"'),
    ("oracle", '"IsActive"'), ("mysql", "IsActive"), ("sqlite", "IsActive"),
    ("duckdb", "IsActive"), ("bigquery", "IsActive"), ("sqlserver", "IsActive"),
])
def test_a_mixed_case_column_is_quoted_where_the_engine_folds_it(db, expected):
    met = _mets(_table(m.Column(name="IsActive", type="boolean")), db)["tickets_IsActive_rate"]
    assert _binding(met).startswith(f"AVG(CASE WHEN {expected} ")


@pytest.mark.parametrize("name, db, expected", [
    ("is_open", "postgresql", "is_open"), ("ORDERS", "snowflake", "ORDERS"),
    ("orders", "snowflake", "orders"), ("order", "postgresql", '"order"'),
    ("line items", "postgresql", '"line items"'), ("OrderItems", "mysql", "OrderItems"),
    # words one engine reserves beyond the shared set
    ("index", "sqlite", '"index"'), ("index", "postgresql", '"index"'), ("key", "mysql", "`key`"),
    ("rank", "mysql", "`rank`"), ("key", "sqlserver", "[key]"), ("level", "oracle", '"level"'),
    ("qualify", "snowflake", '"qualify"'), ("key", "postgresql", "key"),
    ("status", "sqlite", "status"), ("status", "mysql", "status"),
])
def test_identifiers_are_quoted_only_when_needed(name, db, expected):
    assert D.get_dialect(db).ident(name) == expected


_DATE_PAIR = (m.Column(name="opened_on", type="date"), m.Column(name="closed_on", type="date"))
_TS_PAIR = (m.Column(name="opened_at", type="timestamp"),
            m.Column(name="closed_at", type="timestamp"))


@pytest.mark.parametrize("db, pair, days", [
    # DATE minus DATE is already days on these engines; EXTRACT(EPOCH FROM <integer>) fails.
    ("postgresql", _DATE_PAIR, "(closed_on - opened_on)"),
    ("supabase", _DATE_PAIR, "(closed_on - opened_on)"),
    ("duckdb", _DATE_PAIR, "(closed_on - opened_on)"),
    ("postgresql", _TS_PAIR, "EXTRACT(EPOCH FROM (closed_at - opened_at)) / 86400.0"),
    ("duckdb", _TS_PAIR, "EXTRACT(EPOCH FROM (closed_at - opened_at)) / 86400.0"),
    ("bigquery", _DATE_PAIR, "DATE_DIFF(closed_on, opened_on, DAY)"),
    ("bigquery", _TS_PAIR,
     "TIMESTAMP_DIFF(CAST(closed_at AS TIMESTAMP), CAST(opened_at AS TIMESTAMP), DAY)"),
    ("oracle", _DATE_PAIR, "(closed_on - opened_on)"),
    ("oracle", _TS_PAIR, "(CAST(closed_at AS DATE) - CAST(opened_at AS DATE))"),
    ("redshift", _DATE_PAIR, "DATEDIFF('day', opened_on, closed_on)"),
    ("snowflake", _TS_PAIR, "DATEDIFF('day', opened_at, closed_at)"),
    ("sqlite", _DATE_PAIR, "julianday(closed_on) - julianday(opened_on)"),
])
def test_durations_use_the_dialects_date_arithmetic_for_the_column_types(db, pair, days):
    met = _mets(_table(*pair), db)["tickets_avg_duration_days"]
    s, e = pair[0].name, pair[1].name
    guard = {"bigquery": (f"{e} >= {s}" if pair is _DATE_PAIR
                          else f"CAST({e} AS TIMESTAMP) >= CAST({s} AS TIMESTAMP)"),
             "redshift": f"{e} >= {s}", "snowflake": f"{e} >= {s}"}.get(db, f"{days} >= 0")
    assert _binding(met) == f"AVG(CASE WHEN {guard} THEN {days} END)"


_MIXED_PAIR = (m.Column(name="opened_on", type="date"), m.Column(name="closed_at", type="timestamp"))


@pytest.mark.parametrize("db, pair, guard", [
    # a whole-day difference rounds a reversed same-day pair to 0, so these compare the values
    ("redshift", _TS_PAIR, "closed_at >= opened_at"),
    ("snowflake", _TS_PAIR, "closed_at >= opened_at"),
    ("mysql", _TS_PAIR, "closed_at >= opened_at"),
    ("databricks", _TS_PAIR, "closed_at >= opened_at"),
    ("trino", _TS_PAIR, "closed_at >= opened_at"),
    ("sqlserver", _TS_PAIR, "closed_at >= opened_at"),
    ("bigquery", _DATE_PAIR, "closed_on >= opened_on"),
    ("bigquery", _TS_PAIR, "CAST(closed_at AS TIMESTAMP) >= CAST(opened_at AS TIMESTAMP)"),
    ("bigquery", _MIXED_PAIR, "CAST(closed_at AS TIMESTAMP) >= CAST(opened_on AS TIMESTAMP)"),
    # unrounded differences keep the `days >= 0` form unchanged
    ("postgresql", _TS_PAIR, "EXTRACT(EPOCH FROM (closed_at - opened_at)) / 86400.0 >= 0"),
    ("sqlite", _TS_PAIR, "julianday(closed_at) - julianday(opened_at) >= 0"),
    ("oracle", _TS_PAIR, "(CAST(closed_at AS DATE) - CAST(opened_at AS DATE)) >= 0"),
])
def test_the_duration_guard_compares_values_where_the_day_difference_rounds(db, pair, guard):
    binding = _binding(next(iter(_mets(_table(*pair), db).values())))
    assert binding.startswith(f"AVG(CASE WHEN {guard} THEN ")


def test_a_reversed_same_day_pair_is_left_out_by_a_rounding_engine_shape():
    """SQLite stands in for a rounding engine: its DATE() difference is 0 for a pair a few hours
    apart either way, as DATEDIFF('day') is; the guard on the values still leaves the reversed one out."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (s TEXT, e TEXT)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [
        ("2024-01-01 08:00", "2024-01-03 08:00"), ("2024-01-01 18:00", "2024-01-01 09:00")])
    guard = D.get_dialect("snowflake").duration_guard("s", "e")
    days = "julianday(date(e)) - julianday(date(s))"
    assert con.execute(f"SELECT AVG(CASE WHEN {guard} THEN {days} END) FROM t").fetchone()[0] == 2.0
    assert con.execute(f"SELECT AVG(CASE WHEN {days} >= 0 THEN {days} END) FROM t").fetchone()[0] == 1.0


def test_a_duration_leaves_out_rows_ending_before_they_start_and_says_so():
    met = _mets(_table(*_DATE_PAIR))["tickets_avg_duration_days"]
    assert "rows where closed_on is before opened_on are left out" in met["calculation"]
    assert met["unit"] == "days"


@pytest.mark.parametrize("col, db, expected", [
    (m.Column(name="is_urgent", type="boolean"), "postgresql",
     "AVG(CASE WHEN is_urgent THEN 1.0 WHEN NOT is_urgent THEN 0.0 END)"),
    (m.Column(name="is_urgent", type="integer"), "postgresql",
     "AVG(CASE WHEN is_urgent <> 0 THEN 1.0 WHEN is_urgent = 0 THEN 0.0 END)"),
    # a BIT is not a predicate in T-SQL
    (m.Column(name="is_urgent", type="boolean"), "sqlserver",
     "AVG(CASE WHEN is_urgent <> 0 THEN 1.0 WHEN is_urgent = 0 THEN 0.0 END)"),
])
def test_flag_rates_leave_null_out_instead_of_counting_it_false(col, db, expected):
    met = _mets(_table(col), db)["tickets_is_urgent_rate"]
    assert _binding(met) == expected
    assert "NULL are left out" in met["calculation"]


def test_a_nullable_flag_rate_is_the_rate_among_rows_where_it_is_set():
    """On a real engine: 16 true, 9 false, 25 NULL is 0.64 of the rows the flag applies to; the
    old ELSE 0.0 binding gave 0.32, counting every NULL as false."""
    met = _mets(_table(m.Column(name="is_urgent", type="boolean")), "sqlite")
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE tickets (id INTEGER, is_urgent BOOLEAN)")
    con.executemany("INSERT INTO tickets VALUES (?, ?)",
                    [(i, 1 if i < 16 else 0 if i < 25 else None) for i in range(50)])
    sql = f"SELECT {_binding(met['tickets_is_urgent_rate'])} FROM tickets"
    assert con.execute(sql).fetchone()[0] == pytest.approx(0.64)


def test_a_sqlite_flag_named_index_is_quoted_and_runs():
    met = _mets(_table(m.Column(name="index", type="boolean")), "sqlite")["tickets_index_rate"]
    assert '"index"' in _binding(met)
    con = sqlite3.connect(":memory:")
    con.execute('CREATE TABLE tickets (id INTEGER, "index" BOOLEAN)')
    con.executemany("INSERT INTO tickets VALUES (?, ?)", [(1, 1), (2, 0), (3, None), (4, 1)])
    assert con.execute(f"SELECT {_binding(met)} FROM tickets").fetchone()[0] == pytest.approx(2 / 3)


def test_a_wide_table_keeps_each_ambiguous_pairing_reason_short():
    starts = [m.Column(name=f"start_{i}", type="timestamp") for i in range(6)]
    ends = [m.Column(name=f"end_{i}", type="timestamp") for i in range(6)]
    pairs = build._duration_pairs([*starts, *ends])
    assert len(pairs) == 36
    for _s, _e, _fam, flags in pairs:
        why = next(f["why"] for f in flags if f["flag"] == "ambiguous_pairing")
        assert why.startswith("36 start/end pairings fit") and why.endswith("… and 32 more")
        assert why.count("→") == 3 and len(why) < 200


def test_a_sqlite_duration_runs_and_skips_negative_rows():
    met = _mets(_table(*_DATE_PAIR), "sqlite")["tickets_avg_duration_days"]
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE tickets (id INTEGER, opened_on DATE, closed_on DATE)")
    con.executemany("INSERT INTO tickets VALUES (?, ?, ?)", [
        (1, "2024-01-01", "2024-01-03"), (2, "2024-01-01", "2024-01-05"),
        (3, "2024-01-10", "2024-01-01")])   # ends before it starts → left out
    assert con.execute(f"SELECT {_binding(met)} FROM tickets").fetchone()[0] == 3.0


def test_duckdb_runs_the_date_pair_that_the_epoch_form_broke():
    duckdb = pytest.importorskip("duckdb")
    met = _mets(_table(*_DATE_PAIR), "duckdb")["tickets_avg_duration_days"]
    con = duckdb.connect()
    con.execute("CREATE TABLE tickets (id INTEGER, opened_on DATE, closed_on DATE)")
    con.execute("INSERT INTO tickets VALUES (1, DATE '2024-01-01', DATE '2024-01-03'), "
                "(2, DATE '2024-01-01', DATE '2024-01-05'), (3, DATE '2024-01-10', DATE '2024-01-01')")
    assert con.execute(f"SELECT {_binding(met)} FROM tickets").fetchone()[0] == 3.0


# --- which columns pair -------------------------------------------------------

def test_an_audit_column_is_not_paired_when_a_real_start_exists():
    mets = _mets(_table(m.Column(name="sys_created_on", type="timestamp"),
                        m.Column(name="created_at", type="timestamp"),
                        m.Column(name="opened_at", type="timestamp"),
                        m.Column(name="resolved_at", type="timestamp")))
    assert list(mets) == ["tickets_avg_duration_days"]
    met = mets["tickets_avg_duration_days"]
    assert "from opened_at to resolved_at" in met["calculation"]
    assert met["_proposal"]["flags"] == []


def test_an_audit_only_pairing_is_offered_flagged():
    met = _mets(_table(m.Column(name="sys_created_on", type="timestamp"),
                       m.Column(name="resolved_at", type="timestamp")))["tickets_avg_duration_days"]
    assert [f["flag"] for f in met["_proposal"]["flags"]] == ["audit_column"]


def test_planned_pairs_with_planned_and_actual_with_actual():
    mets = _mets(_table(m.Column(name="planned_start", type="timestamp"),
                        m.Column(name="planned_end", type="timestamp"),
                        m.Column(name="actual_start", type="timestamp"),
                        m.Column(name="actual_end", type="timestamp")))
    assert set(mets) == {"tickets_avg_planned_duration_days", "tickets_avg_actual_duration_days"}
    assert "from planned_start to planned_end" in mets["tickets_avg_planned_duration_days"]["calculation"]
    assert "from actual_start to actual_end" in mets["tickets_avg_actual_duration_days"]["calculation"]
    # a planned start with only an actual end is no duration at all
    assert _mets(_table(m.Column(name="planned_start", type="timestamp"),
                        m.Column(name="actual_end", type="timestamp"))) == {}


def test_an_ambiguous_pairing_is_offered_flagged_not_picked():
    mets = _mets(_table(m.Column(name="opened_at", type="timestamp"),
                        m.Column(name="resolved_at", type="timestamp"),
                        m.Column(name="closed_at", type="timestamp")))
    assert set(mets) == {"tickets_avg_days_opened_at_to_resolved_at",
                         "tickets_avg_days_opened_at_to_closed_at"}
    for met in mets.values():
        assert [f["flag"] for f in met["_proposal"]["flags"]] == ["ambiguous_pairing"]


def test_a_tiny_table_flags_its_proposals_low_confidence():
    col = m.Column(name="is_urgent", type="boolean")
    assert _mets(_table(col, rows=12))["tickets_is_urgent_rate"]["_proposal"]["flags"][0]["flag"] \
        == "low_confidence"
    assert _mets(_table(col, rows=5000))["tickets_is_urgent_rate"]["_proposal"]["flags"] == []


# --- the command: dry run, --write, --profile -----------------------------------

def _model(root: Path, storage_type: str = "SQLite") -> None:
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": storage_type}))
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": "main", "table": "tickets"}]}))
    (root / "subject_areas" / "s" / "tables" / "tickets.yaml").write_text(yaml.safe_dump({
        "name": "tickets", "schema": "main", "storage_connection": "c", "grain": ["id"],
        "description": "t",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "is_urgent", "type": "boolean"},
                    {"name": "is_test", "type": "boolean"},      # constant in the data
                    {"name": "is_gone", "type": "boolean"}]}))   # dropped from the database


def _db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY, is_urgent BOOLEAN, is_test BOOLEAN)")
    con.executemany("INSERT INTO tickets VALUES (?, ?, 0)",
                    [(i, 1 if i % 3 == 0 else None if i % 3 == 1 else 0) for i in range(90)])
    con.commit()
    con.close()


def _run(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(args)
    return rc, json.loads(buf.getvalue())


def _canned_runner(db: Path, seen: list[str]):
    def run(sql: str) -> list[dict]:
        seen.append(sql)
        con = sqlite3.connect(db)
        try:
            cur = con.execute(sql)
            names = [d[0] for d in cur.description]
            return [{k: "" if v is None else str(v) for k, v in zip(names, r)} for r in cur]
        finally:
            con.close()
    return run


def _metrics_dir(root: Path) -> Path:
    return root / "subject_areas" / "s" / "metrics"


def test_the_default_is_a_dry_run_that_writes_nothing(tmp_path):
    _model(tmp_path)
    rc, out = _run(["suggest-metrics", str(tmp_path), "--flag-rates", "all"])
    assert rc == 0 and out["dry_run"] and out["written"] == 0, out
    assert not _metrics_dir(tmp_path).exists()
    assert {p["name"] for p in out["proposals"]} == {
        "tickets_is_urgent_rate", "tickets_is_test_rate", "tickets_is_gone_rate"}
    p = out["proposals"][0]
    assert {"binding", "calculation", "source_table", "flags"} <= set(p)
    assert "NOT test-run" in out["note"] and not out["test_run"]


def test_write_saves_the_proposals_without_the_proposal_details(tmp_path):
    _model(tmp_path)
    rc, out = _run(["suggest-metrics", str(tmp_path), "--write", "--flag-rates", "all"])
    assert rc == 0 and out["written"] == 3, out
    met = yaml.safe_load((_metrics_dir(tmp_path) / "tickets_is_urgent_rate.yaml").read_text())
    assert "_proposal" not in met and met["review_state"] == "unreviewed"


def test_profile_test_runs_and_keeps_broken_and_degenerate_bindings_out(tmp_path, monkeypatch):
    from semantic_model import introspect
    _model(tmp_path / "m")
    _db(tmp_path / "db.sqlite")
    seen: list[str] = []
    monkeypatch.setattr(introspect, "make_execute_sql_runner",
                        lambda profile, **kw: _canned_runner(tmp_path / "db.sqlite", seen))
    rc, out = _run(["suggest-metrics", str(tmp_path / "m"), "--profile", "demo", "--write",
                    "--flag-rates", "all"])
    assert rc == 0 and out["test_run"], out
    by = {p["name"]: p for p in out["proposals"]}
    assert "is_gone" in by["tickets_is_gone_rate"]["error"]            # the binding errors
    assert [f["flag"] for f in by["tickets_is_test_rate"]["flags"]] == ["degenerate"]
    assert by["tickets_is_urgent_rate"]["test_run"]["value"] == pytest.approx(0.5)
    assert sorted(f.name for f in _metrics_dir(tmp_path / "m").iterdir()) == [
        "tickets_is_urgent_rate.yaml"]
    # bounded: the statement reads a capped sample of only the columns the bindings need
    assert "LIMIT 10000" in seen[0] and "SELECT *" not in seen[0]


def test_a_named_flagged_proposal_is_written_but_a_named_broken_one_never(tmp_path, monkeypatch):
    from semantic_model import introspect
    _model(tmp_path / "m")
    _db(tmp_path / "db.sqlite")
    monkeypatch.setattr(introspect, "make_execute_sql_runner",
                        lambda profile, **kw: _canned_runner(tmp_path / "db.sqlite", []))
    rc, out = _run(["suggest-metrics", str(tmp_path / "m"), "--profile", "demo", "--write",
                    "--flag-rates", "all", "--names", "tickets_is_test_rate", "tickets_is_gone_rate"])
    assert rc == 0, out
    assert sorted(f.name for f in _metrics_dir(tmp_path / "m").iterdir()) == [
        "tickets_is_test_rate.yaml"]


def test_a_storage_type_with_no_dialect_fails_instead_of_using_postgres(tmp_path, monkeypatch):
    _model(tmp_path)
    monkeypatch.delitem(D._REGISTRY, "sqlite")
    rc, out = _run(["suggest-metrics", str(tmp_path)])
    assert rc == 2 and "SQLite" in out["error"], out


def _edit_table(root: Path, **fields) -> None:
    p = root / "subject_areas" / "s" / "tables" / "tickets.yaml"
    doc = yaml.safe_load(p.read_text())
    doc.update(fields)
    p.write_text(yaml.safe_dump(doc))


def _profile_run(tmp_path, monkeypatch, *extra):
    from semantic_model import introspect
    seen: list[str] = []
    monkeypatch.setattr(introspect, "make_execute_sql_runner",
                        lambda profile, **kw: _canned_runner(tmp_path / "db.sqlite", seen))
    rc, out = _run(["suggest-metrics", str(tmp_path / "m"), "--profile", "demo",
                    "--flag-rates", "all", *extra])
    return rc, out, seen


def test_a_sql_defined_table_is_test_run_over_its_sql(tmp_path, monkeypatch):
    _model(tmp_path / "m")
    _db(tmp_path / "db.sqlite")
    _edit_table(tmp_path / "m", source_type="sql", sql="SELECT * FROM tickets -- the live rows\n;",
                columns=[{"name": "id", "type": "integer", "primary_key": True},
                         {"name": "is_urgent", "type": "boolean"}])
    rc, out, seen = _profile_run(tmp_path, monkeypatch, "--write")
    assert rc == 0, out
    (p,) = out["proposals"]
    assert p["test_run"]["value"] == pytest.approx(0.5) and p["flags"] == [] and p["write"], p
    assert "SELECT * FROM tickets -- the live rows\n) src" in seen[0] and "LIMIT 10000" in seen[0]
    assert (_metrics_dir(tmp_path / "m") / "tickets_is_urgent_rate.yaml").exists()


def test_a_sql_defined_table_whose_read_fails_is_flagged_not_test_run(tmp_path, monkeypatch):
    _model(tmp_path / "m")
    _db(tmp_path / "db.sqlite")
    _edit_table(tmp_path / "m", source_type="sql", sql="SELECT * FROM no_such_table",
                columns=[{"name": "id", "type": "integer", "primary_key": True},
                         {"name": "is_urgent", "type": "boolean"}])
    rc, out, _ = _profile_run(tmp_path, monkeypatch, "--write")
    (p,) = out["proposals"]
    assert rc == 0 and p["test_run"] is None and "error" not in p, p
    assert [f["flag"] for f in p["flags"]] == ["not_test_run"] and not p["write"]
    assert "defined by SQL; its bindings could not be test-run" in p["flags"][0]["why"]
    assert not _metrics_dir(tmp_path / "m").exists()
    # named, it is written like any flagged proposal
    rc, out, _ = _profile_run(tmp_path, monkeypatch, "--write", "--names", "tickets_is_urgent_rate")
    assert rc == 0 and out["written"] == 1, out


def test_a_live_row_count_replaces_the_estimates_low_confidence(tmp_path, monkeypatch):
    _model(tmp_path / "m")
    _db(tmp_path / "db.sqlite")   # 90 rows
    _edit_table(tmp_path / "m", performance_hints={"estimated_row_count": 12},
                columns=[{"name": "id", "type": "integer", "primary_key": True},
                         {"name": "is_urgent", "type": "boolean"}])
    rc, out, _ = _profile_run(tmp_path, monkeypatch, "--write")
    (p,) = out["proposals"]
    assert rc == 0 and p["flags"] == [] and p["write"] and out["written"] == 1, p
    # without a test run the estimate still flags it
    _model(tmp_path / "m2")
    _edit_table(tmp_path / "m2", performance_hints={"estimated_row_count": 12})
    rc, out = _run(["suggest-metrics", str(tmp_path / "m2"), "--flag-rates", "all"])
    assert {f["flag"] for p in out["proposals"] for f in p["flags"]} == {"low_confidence"}, out


# --- flag rates only when asked (#530) ----------------------------------------

_TEN_FLAGS = [f"is_flag_{i}" for i in range(10)]


def _flag_model(root: Path) -> None:
    """tickets with ten boolean columns and a start/end pair."""
    _model(root)
    _edit_table(root, columns=[{"name": "id", "type": "integer", "primary_key": True},
                               *({"name": n, "type": "boolean"} for n in _TEN_FLAGS),
                               {"name": "opened_at", "type": "timestamp"},
                               {"name": "closed_at", "type": "timestamp"}])


def test_no_flag_rate_is_proposed_unasked_only_a_summary(tmp_path):
    _flag_model(tmp_path)
    rc, out = _run(["suggest-metrics", str(tmp_path)])
    assert rc == 0, out
    assert [p["name"] for p in out["proposals"]] == ["tickets_avg_duration_days"]
    fr = out["flag_rates"]
    assert fr["available"] == 10 and fr["columns"] == [f"tickets.{n}" for n in _TEN_FLAGS]
    assert "--flag-rates" in fr["hint"] and "more" not in fr


def test_the_summary_lists_twenty_columns_and_counts_the_rest():
    from semantic_model.cli import _flag_rate_summary
    fr = _flag_rate_summary([f"t.c{i}" for i in range(25)])
    assert fr["available"] == 25 and len(fr["columns"]) == 20 and fr["more"] == 5


def test_flag_rates_names_columns_case_insensitively(tmp_path):
    _flag_model(tmp_path)
    rc, out = _run(["suggest-metrics", str(tmp_path), "--flag-rates",
                    "TICKETS.is_flag_3,tickets.IS_FLAG_7"])
    assert rc == 0, out
    assert {p["name"] for p in out["proposals"]} == {
        "tickets_is_flag_3_rate", "tickets_is_flag_7_rate", "tickets_avg_duration_days"}
    assert out["flag_rates"]["available"] == 8


def test_flag_rates_all_proposes_every_one(tmp_path):
    _flag_model(tmp_path)
    rc, out = _run(["suggest-metrics", str(tmp_path), "--flag-rates", "all",
                    "--max-per-table", "20"])
    names = {p["name"] for p in out["proposals"]}
    assert rc == 0 and names == {f"tickets_{n}_rate" for n in _TEN_FLAGS} | {
        "tickets_avg_duration_days"}, out
    assert out["flag_rates"] == {"available": 0, "columns": []}


@pytest.mark.parametrize("name, why", [
    ("tickets.no_such", "no such column"), ("orders.is_flag_1", "no such column"),
    ("tickets.opened_at", "not a boolean or flag column"), ("is_flag_1", "table.column"),
])
def test_an_unusable_flag_rates_name_is_reported(tmp_path, name, why):
    _flag_model(tmp_path)
    rc, out = _run(["suggest-metrics", str(tmp_path), "--flag-rates", f"tickets.is_flag_2,{name}"])
    assert rc == 1, out
    assert len(out["errors"]) == 1 and why in out["errors"][0] and name in out["errors"][0]
    # the usable one is still proposed
    assert "tickets_is_flag_2_rate" in {p["name"] for p in out["proposals"]}


def test_cite_puts_the_source_in_the_written_description(tmp_path):
    _flag_model(tmp_path)
    rc, out = _run(["suggest-metrics", str(tmp_path), "--flag-rates", "tickets.is_flag_4",
                    "--cite", "Support KPI handbook", "--write"])
    assert rc == 0 and out["written"] == 2, out
    by = {p["name"]: p for p in out["proposals"]}
    assert by["tickets_is_flag_4_rate"]["description"] == "Defined in: Support KPI handbook."
    assert "description" not in by["tickets_avg_duration_days"]
    met = yaml.safe_load((_metrics_dir(tmp_path) / "tickets_is_flag_4_rate.yaml").read_text())
    assert met["description"] == "Defined in: Support KPI handbook."
    dur = yaml.safe_load((_metrics_dir(tmp_path) / "tickets_avg_duration_days.yaml").read_text())
    assert not dur.get("description")


def test_cite_without_flag_rates_is_refused(tmp_path):
    _flag_model(tmp_path)
    rc, out = _run(["suggest-metrics", str(tmp_path), "--cite", "Support KPI handbook"])
    assert rc == 2 and "--flag-rates" in out["error"], out


def test_a_rate_already_in_the_model_is_kept_and_not_counted(tmp_path):
    _flag_model(tmp_path)
    _run(["suggest-metrics", str(tmp_path), "--flag-rates", "tickets.is_flag_0", "--write"])
    path = _metrics_dir(tmp_path) / "tickets_is_flag_0_rate.yaml"
    before = path.read_text()
    rc, out = _run(["suggest-metrics", str(tmp_path), "--write"])
    assert rc == 0 and out["written"] == 0, out
    assert path.read_text() == before
    assert "tickets.is_flag_0" not in out["flag_rates"]["columns"]
    assert out["flag_rates"]["available"] == 9
    # asked for again by name, it is still kept as it is, and the output says so
    rc, out = _run(["suggest-metrics", str(tmp_path), "--flag-rates", "tickets.is_flag_0",
                    "--cite", "Support KPI handbook", "--write"])
    assert rc == 0 and out["written"] == 0 and path.read_text() == before, out
    assert out["flag_rates"]["already_in_model"] == ["tickets_is_flag_0_rate"]


def test_flag_rates_split_on_the_last_dot(tmp_path):
    # The column is after the last dot: schema.table.column, or a table name holding a dot.
    _flag_model(tmp_path)
    rc, out = _run(["suggest-metrics", str(tmp_path), "--flag-rates", "MAIN.tickets.is_flag_3"])
    assert rc == 0 and "tickets_is_flag_3_rate" in {p["name"] for p in out["proposals"]}, out
    _edit_table(tmp_path, name="ops.tickets", schema=None)
    area = tmp_path / "subject_areas" / "s" / "subject_area.yaml"
    area.write_text(yaml.safe_dump({"name": "s", "tables": [
        {"storage_connection": "c", "table": "ops.tickets"}]}))
    rc, out = _run(["suggest-metrics", str(tmp_path)])
    assert "ops.tickets.is_flag_3" in out["flag_rates"]["columns"], out
    rc, out = _run(["suggest-metrics", str(tmp_path), "--flag-rates", "ops.tickets.is_flag_3"])
    assert rc == 0 and "ops.tickets_is_flag_3_rate" in {p["name"] for p in out["proposals"]}, out


def _second_area(root: Path) -> None:
    """Area b with its own `tickets`, in schema billing."""
    (root / "subject_areas" / "b" / "tables").mkdir(parents=True)
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["subject_areas"].append("subject_areas/b")
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds))
    (root / "subject_areas" / "b" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "b", "tables": [{"storage_connection": "c", "schema": "billing",
                                 "table": "tickets"}]}))
    (root / "subject_areas" / "b" / "tables" / "tickets.yaml").write_text(yaml.safe_dump({
        "name": "tickets", "schema": "billing", "storage_connection": "c", "grain": ["id"],
        "description": "t",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "is_flag_0", "type": "boolean"}]}))


def test_an_ambiguous_flag_rates_name_is_refused_before_writing(tmp_path):
    _flag_model(tmp_path)
    _second_area(tmp_path)
    rc, out = _run(["suggest-metrics", str(tmp_path), "--flag-rates", "tickets.is_flag_0",
                    "--cite", "Billing KPI handbook", "--write"])
    assert rc == 2 and "more than one" in out["error"], out
    assert out["ambiguous"] == {"tickets.is_flag_0": ["b: billing.tickets.is_flag_0",
                                                      "s: main.tickets.is_flag_0"]}
    assert not _metrics_dir(tmp_path).exists()
    assert not (tmp_path / "subject_areas" / "b" / "metrics").exists()
    # the summary names them schema-qualified, and that name picks the one table
    rc, out = _run(["suggest-metrics", str(tmp_path)])
    assert "billing.tickets.is_flag_0" in out["flag_rates"]["columns"], out
    assert "main.tickets.is_flag_0" in out["flag_rates"]["columns"]
    rc, out = _run(["suggest-metrics", str(tmp_path), "--flag-rates", "billing.tickets.is_flag_0",
                    "--write"])
    assert rc == 0, out
    assert [(p["subject_area"], p["name"]) for p in out["proposals"]
            if p["name"].endswith("_rate")] == [("b", "tickets_is_flag_0_rate")]
    assert not (_metrics_dir(tmp_path) / "tickets_is_flag_0_rate.yaml").exists()
    # --area settles it too
    rc, out = _run(["suggest-metrics", str(tmp_path), "--flag-rates", "tickets.is_flag_1",
                    "--area", "s"])
    assert rc == 0 and "tickets_is_flag_1_rate" in {p["name"] for p in out["proposals"]}, out
