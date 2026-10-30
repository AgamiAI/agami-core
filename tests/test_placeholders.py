"""Placeholder values: found by introspection, answered by a person, served to the query agent (#445).

The fixture is a real SQLite database read through a runner shaped like the CSV executor: a date
dimension with an Unknown member (-99999, 2099-12-31, fiscal year 2099), a customer history whose
current rows end 9999-12-31, a status column with '-' filler, a customer table with personal
columns, and a fact table too big to scan. Synthetic, generic names only.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "agami-core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import tools  # noqa: E402
from semantic_model import build, cli, curate  # noqa: E402
from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import placeholders as P  # noqa: E402
from semantic_model import validator as V  # noqa: E402
from semantic_model.loader import get_table_context, load_datasource  # noqa: E402

AT = "2026-10-01T00:00:00Z"


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE date_dim(date_key INTEGER PRIMARY KEY, calendar_date TEXT, fiscal_year INTEGER, month_name TEXT);
    CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, email TEXT, birth_date TEXT, segment TEXT);
    CREATE TABLE customer_history(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
        segment TEXT, valid_from TEXT, valid_to TEXT);
    CREATE TABLE shipments(id INTEGER PRIMARY KEY, status TEXT, shipped_on TEXT);
    CREATE TABLE sales(id INTEGER PRIMARY KEY, date_key INTEGER REFERENCES date_dim(date_key),
        customer_id INTEGER REFERENCES customers(id), channel TEXT, amount REAL);
    """)
    d0 = date(2023, 1, 1)
    days = [d0 + timedelta(i) for i in range(730)]
    dims = [(int(d.strftime("%Y%m%d")), d.isoformat(), d.year, d.strftime("%B")) for d in days]
    dims.append((-99999, "2099-12-31", 2099, "Unknown"))
    con.executemany("INSERT INTO date_dim VALUES (?,?,?,?)", dims)
    # 'N/A' emails and 1900-01-01 birth dates look like placeholders, but these columns are personal.
    con.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", [
        (i, f"Customer {i}", "N/A" if i % 4 == 0 else f"user{i}@example.com",
         "1900-01-01" if i % 3 == 0 else f"19{50 + i % 40}-0{1 + i % 9}-1{i % 9}",
         ["retail", "wholesale", "online"][i % 3]) for i in range(1, 201)])
    hist = []
    for c in range(1, 201):
        if c % 2 == 0:
            hist += [(len(hist) + 1, c, "retail", "2023-01-01", "2024-03-31"),
                     (len(hist) + 2, c, "online", "2024-04-01", "9999-12-31")]
        else:
            hist.append((len(hist) + 1, c, "wholesale", "2023-01-01", "9999-12-31"))
    con.executemany("INSERT INTO customer_history VALUES (?,?,?,?,?)", hist)
    con.executemany("INSERT INTO shipments VALUES (?,?,?)", [
        (i, "-" if i % 6 == 0 else ["shipped", "delivered"][i % 2], f"2024-0{1 + i % 9}-1{i % 9}")
        for i in range(1, 301)])
    keys = [k for k, *_ in dims]
    # A fact table well past the small-table line, with filler of its own that must not be read.
    con.executemany("INSERT INTO sales VALUES (?,?,?,?,?)", [
        (i, -99999 if i % 10 == 0 else keys[i % 730], 1 + i % 200,
         "N/A" if i % 3 == 0 else ["web", "store"][i % 2], float(10 + i % 50)) for i in range(1, 20001)])
    con.commit()
    con.close()


def _runner(db: Path, seen: list[str]):
    """Rows as the CSV executor returns them: every value a string, NULL as ''."""
    def run(sql):
        seen.append(sql)
        con = sqlite3.connect(db)
        try:
            cur = con.execute(sql)
            names = [d[0] for d in cur.description or []]
            return [I._Row({n: "" if v is None else str(v) for n, v in zip(names, row)})
                    for row in cur.fetchall()]
        finally:
            con.close()
    return run


@pytest.fixture
def shop(tmp_path):
    db = tmp_path / "shop.db"
    _make_db(db)
    seen: list[str] = []
    run = _runner(db, seen)
    org, rep = I.introspect("shop", "sqlite", runner=run, artifacts_dir=tmp_path)
    return {"db": db, "art": tmp_path, "root": tmp_path / "shop", "run": run, "seen": seen,
            "org": org, "report": rep}


def _col(org, table, column):
    return next(t for sa in org.subject_areas for t in sa.tables_defined if t.name == table).get_column(column)


def _scans(seen):
    return [q for q in seen if "CROSS JOIN" in q]


def _answer(root, value_ops, *, signer="you@example.com", role="owner"):
    ops = [{"op": op, "kind": "placeholder", "name": t, "column": c, "value": v,
            **({"meaning": mn, "at": AT} if mn else {})} for op, t, c, v, mn in value_ops]
    return curate.apply(root, ops, signer=signer, role=role)


# --- detection -------------------------------------------------------------------------------------


def test_introspection_reports_the_unknown_member_the_open_end_and_the_filler(shop):
    lines = shop["report"].placeholders
    assert "date_dim.date_key = '-99999' (a negative key among positive ones; 1 row)" in lines
    assert any(x.startswith("date_dim.calendar_date = '2099-12-31' (a date more than 50 years ahead") for x in lines)
    assert any(x.startswith("date_dim.fiscal_year = '2099'") for x in lines)
    assert "customer_history.valid_to = '9999-12-31' (a date more than 50 years ahead; 200 rows)" in lines
    assert "shipments.status = '-' (filler text on 17% of rows; 50 rows)" in lines
    assert "possible placeholder values (5)" in shop["report"].render()


def test_a_finding_is_only_a_question_until_a_person_answers(shop):
    org = load_datasource(shop["root"])
    c = _col(org, "date_dim", "date_key")
    assert [(p.value, p.review_state, p.meaning) for p in c.placeholders] == [("-99999", "unreviewed", None)]
    assert c.caveats == [] and c.served_placeholders() == []
    q = curate.review_queue(org)
    assert q["counts"]["placeholders"] == 5
    assert any(it["question"].startswith("date_dim.date_key holds '-99999' on 1 row") for it in q["placeholders"])
    served = get_table_context(org, ["date_dim"], include=["caveats"])["tables"]["date_dim"]["columns"]
    assert not any("placeholders" in col for col in served)


def test_the_scan_is_bounded_and_skips_the_big_fact_table(shop):
    scans = _scans(shop["seen"])
    assert scans and all(q.rstrip().endswith("LIMIT 10") for q in scans)
    assert not any('"sales"' in q for q in scans)
    assert not any(line.startswith("sales.") for line in shop["report"].placeholders)
    # one read per checked column, never a second sample of a table
    assert len(scans) == len(set(scans))
    assert sum('SELECT * FROM "main"."date_dim" LIMIT' in q for q in shop["seen"]) == 1


def test_personal_columns_are_never_read_or_reported(shop):
    scans = _scans(shop["seen"])
    assert not any('"email"' in q or '"birth_date"' in q or '"name"' in q for q in scans)
    assert not any(line.startswith(("customers.email", "customers.birth_date")) for line in shop["report"].placeholders)


def test_a_dimension_over_the_small_table_line_is_still_scanned(tmp_path):
    db = tmp_path / "wide.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE stores(store_key INTEGER PRIMARY KEY, region TEXT);"
                      "CREATE TABLE visits(id INTEGER PRIMARY KEY, store_key INTEGER REFERENCES stores(store_key));")
    con.executemany("INSERT INTO stores VALUES (?,?)", [(-1, "Unknown")] + [(i, f"r{i % 5}") for i in range(1, 12001)])
    con.executemany("INSERT INTO visits VALUES (?,?)", [(i, i % 500) for i in range(1, 12001)])
    con.commit()
    con.close()
    seen: list[str] = []
    _, rep = I.introspect("shop", "sqlite", runner=_runner(db, seen), artifacts_dir=tmp_path, dry_run=True)
    assert "stores.store_key = '-1' (a negative key among positive ones; 1 row)" in rep.placeholders
    assert not any('"visits"' in q for q in _scans(seen))  # the many side, past the line


def test_the_scan_shares_the_value_read_budget(shop, tmp_path, monkeypatch):
    monkeypatch.setattr(I, "DISTINCT_READ_CAP", 0)
    seen: list[str] = []
    _, rep = I.introspect("shop", "sqlite", runner=_runner(shop["db"], seen), artifacts_dir=tmp_path / "b",
                          dry_run=True)
    assert not _scans(seen) and not rep.placeholders
    assert any("not checked for placeholder values" in n for n in rep.notes)


@pytest.mark.parametrize("kinds,top,lo,hi,total,rest,expect", [
    ({"date"}, [], "1900-01-01", "2024-01-01", 10, [], "1900-01-01"),
    ({"date"}, [], "0000-00-00", "2024-01-01", 10, [], "0000-00-00"),
    ({"date"}, [("1970-01-01", 40)], "1970-01-01", "2024-05-01", 100, ["2021-01-01", "2023-02-01"], "1970-01-01"),
    ({"key"}, [], "1", "999999", 50, ["10", "40"], "999999"),
    ({"code"}, [("N/A", 30), ("open", 70)], "N/A", "open", 100, [], "N/A"),
    ({"code"}, [("", 20), ("open", 80)], "", "open", 100, [], ""),
])
def test_shapes_are_flagged(kinds, top, lo, hi, total, rest, expect):
    assert expect in [f.value for f in P.find(kinds, top, lo, hi, total, rest, today=date(2026, 1, 1))]


@pytest.mark.parametrize("kinds,top,lo,hi,total,rest", [
    ({"date"}, [], "1970-01-01", "1975-01-01", 10, ["1971-03-01", "1974-01-01"]),   # near the rest
    ({"date"}, [], "2001-01-01", "2060-01-01", 10, []),                              # within 50 years
    ({"key"}, [], "1", "99999", 50, ["10", "99998"]),                               # a real id run
    ({"code"}, [("N/A", 1), ("open", 99)], "N/A", "open", 100, []),                  # a small share
    ({"key"}, [], "-5", "-1", 10, []),                                               # all negative
])
def test_ordinary_values_are_not_flagged(kinds, top, lo, hi, total, rest):
    assert P.find(kinds, top, lo, hi, total, rest, today=date(2026, 1, 1)) == []


def test_a_mysql_zero_date_is_read_as_text():
    sql = D.get_dialect("mysql").placeholder_scan_sql("s", "t", "valid_to", "date", 10)
    assert "MIN(CAST(`valid_to` AS CHAR))" in sql
    assert "CAST" not in D.get_dialect("postgres").placeholder_scan_sql("s", "t", "valid_to", "date", 10)


@pytest.mark.parametrize("db_type", ["postgres", "mysql", "snowflake", "bigquery", "redshift", "sqlite",
                                     "duckdb", "sqlserver", "databricks", "trino", "oracle"])
def test_the_scan_parses_and_passes_the_always_on_gate(db_type):
    import sql_guard
    import sqlglot
    from semantic_model.sql_dialect import sqlglot_dialect

    d = D.get_dialect(db_type)
    sql = d.placeholder_scan_sql("s", "t", "c", "date", 10)
    assert sql_guard.check_read_only(sql) is None and sql_guard.check_no_recon(sql) is None
    sqlglot.parse_one(sql, read=sqlglot_dialect(d.name))


# --- a person's answer -------------------------------------------------------------------------------


def test_each_meaning_writes_its_caveat_and_entry(shop):
    res = _answer(shop["root"], [
        ("approve", "date_dim", "date_key", "-99999", "unknown"),
        ("approve", "customer_history", "valid_to", "9999-12-31", "current"),
        ("approve", "shipments", "status", "-", "not_set"),
    ])
    assert res.validated and not res.skipped, res.as_dict()
    org = load_datasource(shop["root"])
    for table, column, value, meaning in [("date_dim", "date_key", "-99999", "unknown"),
                                          ("customer_history", "valid_to", "9999-12-31", "current"),
                                          ("shipments", "status", "-", "not_set")]:
        c = _col(org, table, column)
        (p,) = c.placeholders
        assert (p.value, p.meaning, p.review_state, p.signed_off_by, p.signed_off_role) == (
            value, meaning, "approved", "you@example.com", "owner")
        assert P.caveat(value, meaning) in c.caveats
    assert "keep them in totals" in P.caveat("-99999", "unknown")
    assert "as of now" in P.caveat("9999-12-31", "current")
    assert "NULL" in P.caveat("-", "not_set")
    assert V.validate(org).ok


def test_changing_or_withdrawing_an_answer_moves_the_caveat(shop):
    _answer(shop["root"], [("approve", "shipments", "status", "-", "unknown")])
    _answer(shop["root"], [("approve", "shipments", "status", "-", "not_set")])
    c = _col(load_datasource(shop["root"]), "shipments", "status")
    assert P.caveat("-", "unknown") not in c.caveats and P.caveat("-", "not_set") in c.caveats
    _answer(shop["root"], [("reject", "shipments", "status", "-", None)])
    c = _col(load_datasource(shop["root"]), "shipments", "status")
    assert [(p.review_state, p.meaning) for p in c.placeholders] == [("rejected", None)]
    assert not any("'-'" in k for k in c.caveats)


def test_no_meaning_without_a_person(shop):
    root = shop["root"]
    for signer, role in [(None, None), ("system", "system")]:
        res = _answer(root, [("approve", "date_dim", "date_key", "-99999", "unknown")], signer=signer, role=role)
        assert not res.applied and "person's choice" in res.skipped[0]["reason"]
    bad = curate.apply(root, [{"op": "approve", "kind": "placeholder", "name": "date_dim", "column": "date_key",
                               "value": "-99999", "meaning": "maybe", "at": AT}], signer="you@example.com", role="owner")
    assert not bad.applied and "meaning must be one of" in bad.skipped[0]["reason"]
    assert _col(load_datasource(root), "date_dim", "date_key").placeholders[0].review_state == "unreviewed"


def test_approve_queue_never_answers_a_placeholder(shop, capsys):
    rc = cli.main(["approve-queue", str(shop["root"]), "--signer", "you@example.com", "--role", "owner"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["approved"] == 0
    assert len(out["placeholders_awaiting_choice"]) == 5
    assert all(p.review_state == "unreviewed" for sa in load_datasource(shop["root"]).subject_areas
               for t in sa.tables_defined for c in t.columns for p in c.placeholders)


def test_the_validator_refuses_a_meaning_nobody_signed():
    def table(*ps):
        return m.Table(name="t", columns=[m.Column(name="k", type="integer", placeholders=list(ps))])

    assert not V.validate(m.Datasource(datasource="d", subject_areas=[m.SubjectArea(
        name="a", tables_defined=[table(m.Placeholder(value="-1"))])])).errors
    for ps in [(m.Placeholder(value="-1", meaning="unknown"),),
               (m.Placeholder(value="-1", meaning="unknown", review_state="approved"),),
               (m.Placeholder(value="-1"), m.Placeholder(value="-1"))]:
        org = m.Datasource(datasource="d", subject_areas=[m.SubjectArea(name="a", tables_defined=[table(*ps)])])
        assert any("placeholder" in e for e in V.validate(org).errors), ps


# --- old servers ---------------------------------------------------------------------------------------


def test_the_field_is_left_out_when_empty_and_an_old_model_loads(tmp_path):
    c = m.Column(name="k", type="integer")
    assert "placeholders" not in build._model_dump(c)
    assert "placeholders" in build._model_dump(m.Column(name="k", type="integer",
                                                        placeholders=[m.Placeholder(value="-1")]))
    # A column file written before the field existed still loads.
    old = {"name": "k", "type": "integer", "description": "", "caveats": [], "confidence": "confirmed",
           "review_state": "approved"}
    assert m.Column(**old).placeholders == []


def test_a_model_with_nothing_found_carries_no_key(tmp_path):
    db = tmp_path / "plain.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE orders(id INTEGER PRIMARY KEY, status TEXT, placed_on TEXT);")
    con.executemany("INSERT INTO orders VALUES (?,?,?)", [(i, ["open", "closed"][i % 2], "2024-01-0" + str(1 + i % 9))
                                                         for i in range(1, 50)])
    con.commit()
    con.close()
    I.introspect("shop", "sqlite", runner=_runner(db, []), artifacts_dir=tmp_path)
    assert not any("placeholders" in p.read_text() for p in (tmp_path / "shop").rglob("*.yaml"))


# --- serving ---------------------------------------------------------------------------------------------


def test_the_schema_serves_each_meaning(shop, monkeypatch):
    _answer(shop["root"], [("approve", "date_dim", "date_key", "-99999", "unknown"),
                           ("approve", "customer_history", "valid_to", "9999-12-31", "current")])
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(shop["art"]))
    out = tools.tool_get_datasource_schema({"datasource": "shop", "dataset_names": ["date_dim", "customer_history"]})
    reply, _ = json.JSONDecoder().raw_decode(out)
    cols = {(t, c["name"]): c for t, info in reply["tables"].items() for c in info["columns"]}
    assert cols[("date_dim", "date_key")]["placeholders"] == [
        {"value": "-99999", "meaning": "unknown", "how": P.MEANINGS["unknown"]}]
    assert cols[("customer_history", "valid_to")]["placeholders"][0]["meaning"] == "current"
    # an unanswered finding is not served
    assert "placeholders" not in cols[("date_dim", "calendar_date")]


def test_the_explorer_lists_the_findings(shop):
    import render_model_explorer as R
    manifest = R.build_manifest(shop["root"], "shop")
    fields = {f["qname"]: f for s in manifest["schemas"] for t in s["tables"] for f in t["fields"]}
    assert fields["shop.date_dim.date_key"]["placeholders"][0]["value"] == "-99999"
    html = (REPO_ROOT / "plugins" / "agami" / "shared" / "model-explorer-template.html").read_text()
    assert "placeholderFindings()" in html and "kind: 'placeholder'" in html


# --- refresh -----------------------------------------------------------------------------------------------


def test_a_refresh_keeps_answers_and_does_not_ask_again(shop):
    _answer(shop["root"], [("approve", "date_dim", "date_key", "-99999", "unknown"),
                           ("reject", "shipments", "status", "-", None)])
    before = {p: p.read_text() for p in shop["root"].rglob("*.yaml")}
    _, rep = I.introspect("shop", "sqlite", runner=shop["run"], artifacts_dir=shop["art"], append=True)
    assert not any(x.startswith(("date_dim.date_key", "shipments.status")) for x in rep.placeholders)
    # still unanswered, still there: still asked
    assert any(x.startswith("date_dim.calendar_date = '2099-12-31'") for x in rep.placeholders)
    assert {p: p.read_text() for p in shop["root"].rglob("*.yaml")} == before
    org = load_datasource(shop["root"])
    assert _col(org, "date_dim", "date_key").placeholders[0].meaning == "unknown"
    assert _col(org, "shipments", "status").placeholders[0].review_state == "rejected"


def test_a_refresh_drops_an_unanswered_value_no_longer_found(shop):
    con = sqlite3.connect(shop["db"])
    con.execute("UPDATE shipments SET status = 'shipped' WHERE status = '-'")
    con.commit()
    con.close()
    _, rep = I.introspect("shop", "sqlite", runner=shop["run"], artifacts_dir=shop["art"], append=True)
    assert not any(x.startswith("shipments.status") for x in rep.placeholders)
    assert _col(load_datasource(shop["root"]), "shipments", "status").placeholders == []


# --- review of f079a484: size guard, refresh identity and flags, copies, date keys, string years -------


def _one_table_db(path: Path, ddl: str, rows: list[tuple]) -> Path:
    con = sqlite3.connect(path)
    con.execute(ddl)
    con.executemany(f"INSERT INTO t VALUES ({','.join('?' * len(rows[0]))})", rows)
    con.commit()
    con.close()
    return path


def test_no_sample_is_not_a_small_table():
    assert not I._placeholder_size_ok([], None)
    assert not I._placeholder_size_ok([], 20_000_000)
    assert I._placeholder_size_ok([], 10)


def test_a_big_table_whose_sample_fails_is_not_scanned(shop, tmp_path, monkeypatch):
    seen: list[str] = []
    run = _runner(shop["db"], seen)

    def failing(sql):
        if sql.startswith('SELECT * FROM "main"."shipments"'):
            raise RuntimeError("sample refused")
        return run(sql)

    # Row count unknown: no sample proves nothing, so the table is skipped.
    monkeypatch.setattr(I, "_estimate_rows", lambda *a, **k: None)
    _, rep = I.introspect("shop", "sqlite", runner=failing, artifacts_dir=tmp_path / "b", dry_run=True)
    assert not any('"shipments"' in q for q in _scans(seen))
    assert not any(x.startswith("shipments.") for x in rep.placeholders)


def test_a_refresh_never_reads_a_column_this_run_finds_personal(tmp_path):
    db = _one_table_db(tmp_path / "m.db", "CREATE TABLE t(id INTEGER PRIMARY KEY, born_on TEXT, tier TEXT)",
                       [(i, f"2024-0{1 + i % 9}-1{i % 9}", ["gold", "silver"][i % 2]) for i in range(1, 101)])
    run = _runner(db, [])
    I.introspect("shop", "sqlite", runner=run, artifacts_dir=tmp_path)
    con = sqlite3.connect(db)
    con.execute("UPDATE t SET born_on = CASE WHEN id % 5 = 0 THEN '1900-01-01' "
                "ELSE '19' || (50 + id % 40) || '-0' || (1 + id % 9) || '-1' || (id % 9) END")
    con.commit()
    con.close()
    seen: list[str] = []
    _, rep = I.introspect("shop", "sqlite", runner=_runner(db, seen), artifacts_dir=tmp_path, append=True)
    assert not any('"born_on"' in q for q in _scans(seen))
    assert not any(x.startswith("t.born_on") for x in rep.placeholders)
    assert _col(load_datasource(tmp_path / "shop"), "t", "born_on").placeholders == []


def test_a_bare_table_refresh_scans_the_saved_schema_table(shop):
    con = sqlite3.connect(shop["db"])
    con.execute("DELETE FROM date_dim WHERE date_key = -99999")
    con.commit()
    con.close()
    _, rep = I.introspect("shop", "sqlite", runner=shop["run"], artifacts_dir=shop["art"], append=True,
                          tables=["date_dim"])
    assert not any(x.startswith("date_dim.") for x in rep.placeholders)
    assert _col(load_datasource(shop["root"]), "date_dim", "date_key").placeholders == []


def test_a_refresh_reports_placeholder_changes(shop):
    con = sqlite3.connect(shop["db"])
    con.execute("UPDATE shipments SET status = 'shipped' WHERE status = '-'")
    con.commit()
    con.close()
    _, rep = I.introspect("shop", "sqlite", runner=shop["run"], artifacts_dir=shop["art"], append=True,
                          dry_run=True)
    assert rep.changes["main.shipments"] == {"placeholders": ["status '-' no longer found"]}
    assert "changed main.shipments: placeholders status '-' no longer found" in rep.render()


def test_a_column_one_copy_excludes_is_still_scanned_for_the_copy_that_serves_it(shop):
    def copy(review: str) -> m.Table:
        return m.Table(name="shipments", schema="main", storage_connection="c", columns=[
            m.Column(name="id", type="integer", primary_key=True),
            m.Column(name="status", type="string", review_state=review)])

    excluded, served = copy("rejected"), copy("approved")
    sample = shop["run"]('SELECT * FROM "main"."shipments"')
    seen: list[str] = []
    rep = I.IntrospectReport(profile="shop", db_type="sqlite", out_dir="", dry_run=True)
    I._scan_placeholders(D.get_dialect("sqlite"), _runner(shop["db"], seen), [excluded, served], [],
                         {("main", "shipments"): (sample, 300)}, I._ReadBudget(10), rep, None)
    assert sum('"status"' in q for q in _scans(seen)) == 1
    assert [p.value for p in served.get_column("status").placeholders] == ["-"]
    assert excluded.get_column("status").placeholders == []


def test_a_yyyymmdd_key_with_its_unknown_member_outside_the_sample(tmp_path):
    days = [int((date(2023, 1, 1) + timedelta(i)).strftime("%Y%m%d")) for i in range(600)]
    db = _one_table_db(tmp_path / "d.db", "CREATE TABLE t(date_key INTEGER, label TEXT)",
                       [(k, "day") for k in days] + [(-99999, "Unknown")])
    _, rep = I.introspect("shop", "sqlite", runner=_runner(db, []), artifacts_dir=tmp_path, dry_run=True)
    assert "t.date_key = '-99999' (a negative key among positive ones; 1 row)" in rep.placeholders


def test_a_text_year_column_reports_its_unknown_year(tmp_path):
    db = _one_table_db(tmp_path / "y.db", "CREATE TABLE t(id INTEGER PRIMARY KEY, fiscal_year TEXT)",
                       [(i, ["2023", "2024"][i % 2]) for i in range(1, 60)] + [(60, "2099")])
    _, rep = I.introspect("shop", "sqlite", runner=_runner(db, []), artifacts_dir=tmp_path, dry_run=True)
    assert any(x.startswith("t.fiscal_year = '2099' (a year far outside the others") for x in rep.placeholders)
