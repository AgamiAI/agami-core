"""What the data can't settle, found while a model is built (#486 part 1, and "ask me as you go").

`sm open-questions` proposes candidate ambiguities from patterns in the model, runs both readings of
each on the data, and keeps only those whose answers differ; then it lists coded values and columns
the build couldn't read. Nothing is declared until a person signs the `add` op a candidate carries.

A synthetic store, built by real introspection over SQLite. Generic names only.
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
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import cli, curate  # noqa: E402
from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import open_questions as OQ  # noqa: E402

TODAY = date(2026, 7, 15)


def _schema() -> str:
    start = date(2026, 1, 1)
    statuses = ("paid", "shipped", "delivered", "cancelled")
    rows = []
    for i in range(200):
        placed = start + timedelta(days=i)
        status = statuses[i % 4]
        shipped = (
            f"'{placed + timedelta(days=3)}'" if status in ("shipped", "delivered") else "NULL"
        )
        sub = 100 + i
        rows.append(
            f"({i + 1}, '{status}', '{placed}', {shipped}, {sub}, {sub * 0.18:.2f}, {sub * 1.18:.2f})"
        )
    events = ", ".join(
        f"({i + 1}, '{start + timedelta(days=i)}', '{start + timedelta(days=i)}')"
        for i in range(30)
    )
    tickets = ", ".join(
        f"({i + 1}, {i % 5 + 1}, {(i % 5 + 1) if i % 2 else (i + 1) % 5 + 1}, 'P{i % 3 + 1}')"
        for i in range(40)
    )
    plans = ", ".join(f"({i + 1}, 'tier {i}', {1 if i < 3 else 0})" for i in range(10))
    history = ", ".join(f"({i + 1}, {i % 200 + 1}, 'note')" for i in range(400))
    old = ", ".join(f"({i + 1}, 'paid')" for i in range(50))
    return (
        "CREATE TABLE orders(id INTEGER PRIMARY KEY, status TEXT, placed_at TEXT, shipped_at TEXT,"
        " subtotal REAL, tax_amount REAL, total_amount REAL);"
        f"INSERT INTO orders VALUES {', '.join(rows)};"
        "CREATE TABLE events(id INTEGER PRIMARY KEY, created_at TEXT, event_date TEXT);"
        f"INSERT INTO events VALUES {events};"
        "CREATE TABLE users(id INTEGER PRIMARY KEY, login TEXT);"
        "INSERT INTO users VALUES (1,'a'),(2,'b'),(3,'c'),(4,'d'),(5,'e');"
        "CREATE TABLE tickets(id INTEGER PRIMARY KEY, opened_by INTEGER REFERENCES users(id),"
        " assigned_to INTEGER REFERENCES users(id), priority TEXT);"
        f"INSERT INTO tickets VALUES {tickets};"
        "CREATE TABLE plans(id INTEGER PRIMARY KEY, name TEXT, is_current INTEGER);"
        f"INSERT INTO plans VALUES {plans};"
        "CREATE TABLE orders_history(id INTEGER PRIMARY KEY, order_id INTEGER, note TEXT);"
        f"INSERT INTO orders_history VALUES {history};"
        "CREATE TABLE orders_old(id INTEGER PRIMARY KEY, status TEXT);"
        f"INSERT INTO orders_old VALUES {old};"
    )


def _sqlite_runner(db: Path, seen: list[str] | None = None):
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


@pytest.fixture
def store(tmp_path) -> tuple[Path, Path]:
    db = tmp_path / "demo.db"
    con = sqlite3.connect(db)
    con.executescript(_schema())
    con.commit()
    con.close()
    I.introspect("demo", "sqlite", runner=_sqlite_runner(db), artifacts_dir=tmp_path)
    return tmp_path / "demo", db


def _ask(root: Path, db: Path, seen: list[str] | None = None) -> dict:
    org = L.load_datasource(root)
    return OQ.open_questions(org, _sqlite_runner(db, seen), D.get_dialect("sqlite"), today=TODAY)


def _found(out: dict, pattern: str, table: str | None) -> dict | None:
    return next(
        (q for q in out["questions"] if q.get("pattern") == pattern and q.get("table") == table),
        None,
    )


def _dropped(out: dict, pattern: str, table: str | None) -> list[str]:
    return [
        d["reason"]
        for d in out["dropped"]
        if d.get("pattern") == pattern and d.get("table") == table
    ]


def test_two_dates_that_count_differently_are_a_candidate(store):
    root, db = store
    q = _found(_ask(root, db), "date", "orders")
    assert q is not None
    assert q["question"] == "When someone says 'orders last month', which date do they mean?"
    assert [o["label"] for o in q["options"]] == ["Placed at", "Shipped at"]
    # June 2026, the last complete month before the data's end: every order by when it was placed,
    # half of them by when they shipped.
    assert q["evidence"] == "June 2026: 30 by Placed at; 15 by Shipped at"
    assert q["op"] == {
        "op": "add",
        "kind": "ambiguity",
        "name": "orders_date",
        "when": "a question about orders in a period ('last month', 'by year') that doesn't say which date",
        "options": [
            {"label": "Placed at", "how": "filter and group orders by orders.placed_at"},
            {"label": "Shipped at", "how": "filter and group orders by orders.shipped_at"},
        ],
        "applies_to": ["orders"],
    }


def test_two_dates_that_always_agree_are_dropped_with_the_reason(store):
    root, db = store
    out = _ask(root, db)
    assert _found(out, "date", "events") is None
    assert _dropped(out, "date", "events") == ["every date gives the same count"]


def test_a_status_where_completed_covers_several_values(store):
    root, db = store
    q = _found(_ask(root, db), "status", "orders")
    assert q["question"] == "When someone says 'completed orders', which statuses count?"
    assert [(o["label"], o["value"]) for o in q["options"]] == [
        ("Only Delivered", 50),
        ("Paid, Shipped or Delivered", 150),
    ]
    assert q["options"][1]["how"] == "orders.status IN ('paid', 'shipped', 'delivered')"


def test_amounts_with_and_without_tax_are_readings_and_the_tax_itself_is_not(store):
    root, db = store
    q = _found(_ask(root, db), "amount", "orders")
    assert [o["label"] for o in q["options"]] == ["Subtotal", "Total amount"]
    assert q["options"][0]["how"] == "SUM(orders.subtotal)"


def test_two_joins_to_the_same_key_that_link_rows_differently(store):
    root, db = store
    q = _found(_ask(root, db), "join", "tickets")
    assert q["question"] == "When someone asks about tickets by user, which user do they mean?"
    assert sorted(o["label"] for o in q["options"]) == ["Assigned to", "Opened by"]
    assert q["evidence"].startswith("20 of 40 rows link to a different user")


def test_a_current_flag_and_a_history_table(store):
    root, db = store
    out = _ask(root, db)
    flag = _found(out, "current", "plans")
    assert [(o["label"], o["value"]) for o in flag["options"]] == [
        ("Only current ones", 3),
        ("Every row, past ones too", 10),
    ]
    history = next(q for q in out["questions"] if q.get("op", {}).get("name") == "orders_history")
    assert history["op"]["applies_to"] == ["orders", "orders_history"]
    assert [o["value"] for o in history["options"]] == [200, 400]


def test_a_look_alike_table_is_asked_which_is_current(store):
    root, db = store
    q = next(q for q in _ask(root, db)["questions"] if q["kind"] == "look_alike")
    assert q["question"] == "Orders and Orders old look alike. Which one holds the current data?"
    assert [t["rows"] for t in q["tables"]] == [200, 50]


def test_answering_a_look_alike_writes_a_caveat_on_the_copy_and_is_not_asked_again(store):
    root, db = store
    q = next(q for q in _ask(root, db)["questions"] if q["kind"] == "look_alike")
    current = next(t for t in q["tables"] if t["table"] == "orders")
    res = curate.apply(root, current["ops"], signer="you@example.com", role="Analyst")
    assert res.validated and res.applied
    old = next(
        t
        for sa in L.load_datasource(root).subject_areas
        for t in sa.tables_defined
        if t.name == "orders_old"
    )
    assert old.caveats == ["Looks like a copy of Orders; the current data is in Orders."]
    assert not [q for q in _ask(root, db)["questions"] if q["kind"] == "look_alike"]


def test_a_fiscal_year_that_does_not_start_in_january(store):
    root, db = store
    assert _dropped(_ask(root, db), "fiscal", None) == []  # January: nothing to ask
    ds = root / "datasource.yaml"
    doc = yaml.safe_load(ds.read_text())
    doc["fiscal_year_start_month"] = 4
    ds.write_text(yaml.safe_dump(doc, sort_keys=False))
    q = _found(_ask(root, db), "fiscal", None)
    assert q["question"] == (
        "When someone says 'this year', do they mean the calendar year or the "
        "fiscal year from April?"
    )
    assert q["op"]["applies_to"] == []
    # every order placed in 2026, against those from April 2026 on
    assert [o["value"] for o in q["options"]] == [200, 110]


def test_only_codes_are_asked_about_never_words_or_counts(store):
    root, db = store
    coded = [q for q in _ask(root, db)["questions"] if q["kind"] == "coded_values"]
    assert [(q["table"], q["column"]) for q in coded] == [("tickets", "priority")]
    assert coded[0]["question"] == (
        "What do the values of Priority on Tickets mean? Type them in Other, e.g. P1 = …, P2 = …"
    )


def test_the_meanings_a_person_types_fill_the_code_list_under_their_name(store):
    root, db = store
    (q,) = [q for q in _ask(root, db)["questions"] if q["kind"] == "coded_values"]
    op = {**q["op"], "value": {**q["op"]["value"], "P1": "Urgent", "P2": "Normal"}}
    res = curate.apply(root, [op], signer="you@example.com", role="Analyst")
    assert res.validated
    col = next(
        c
        for sa in L.load_datasource(root).subject_areas
        for t in sa.tables_defined
        if t.name == "tickets"
        for c in t.columns
        if c.name == "priority"
    )
    assert col.choice_field == {"P1": "Urgent", "P2": "Normal", "P3": ""}
    log = json.loads((root / curate.CURATION_LOG).read_text().splitlines()[-1])
    assert (log["field"], log["signer"]) == ("choice_field", "you@example.com")


def test_an_unreadable_column_is_asked_about(store):
    root, db = store
    curate.apply(
        root,
        [
            {
                "op": "edit",
                "kind": "table",
                "name": "events",
                "column": "event_date",
                "field": "description_source",
                "value": "ai_unknown",
            }
        ],
    )
    q = next(q for q in _ask(root, db)["questions"] if q["kind"] == "unknown_column")
    assert (q["table"], q["column"]) == ("events", "event_date")
    assert (
        q["question"] == "What does Event date on Events hold? Type a short description in Other."
    )
    res = curate.apply(
        root,
        [{**q["op"], "value": "The day the event takes place."}],
        signer="you@example.com",
        role="Analyst",
    )
    assert res.validated
    col = next(
        c
        for sa in L.load_datasource(root).subject_areas
        for t in sa.tables_defined
        if t.name == "events"
        for c in t.columns
        if c.name == "event_date"
    )
    assert (col.description, col.description_source) == ("The day the event takes place.", "human")
    assert not [q for q in _ask(root, db)["questions"] if q["kind"] == "unknown_column"]


def test_ambiguities_come_first_ordered_by_how_far_the_readings_differ_and_are_capped(
    store, monkeypatch
):
    root, db = store
    out = _ask(root, db)
    kinds = [q["kind"] for q in out["questions"]]
    assert kinds == sorted(kinds, key=lambda k: k != "ambiguity")
    spreads = [q["spread"] for q in out["questions"] if q["kind"] == "ambiguity"]
    assert spreads == sorted(spreads, reverse=True)
    monkeypatch.setattr(OQ, "MAX_QUESTIONS", 2)
    capped = _ask(root, db)
    assert len(capped["questions"]) == 2 and capped["more"] == len(out["questions"]) - 2


def test_a_sensitive_column_is_never_read(store):
    root, db = store
    curate.apply(
        root,
        [
            {
                "op": "edit",
                "kind": "table",
                "name": "orders",
                "column": "shipped_at",
                "field": "sensitive",
                "value": True,
            }
        ],
    )
    seen: list[str] = []
    out = _ask(root, db, seen)
    assert not any("shipped_at" in sql for sql in seen)
    assert _found(out, "date", "orders") is None


def test_probes_are_bounded_aggregates(store):
    root, db = store
    seen: list[str] = []
    out = _ask(root, db, seen)
    assert out["statements"] == len(seen)
    assert all(sql.startswith("SELECT ") and " FROM " in sql for sql in seen)
    # one statement per probed table, plus one for a table's date windows
    assert len(seen) <= 2 * out["tables_probed"]


def test_a_table_above_the_size_guard_is_never_scanned(store, monkeypatch):
    root, db = store
    monkeypatch.setattr(I, "_OVERLAP_PROBE_MAX_ROWS", 100)
    seen: list[str] = []
    out = _ask(root, db, seen)
    assert {"table": "orders", "reason": "too large to scan"} in out["skipped"]
    assert not any('FROM "orders"' in sql for sql in seen)


def test_a_failed_probe_drops_the_table_and_says_so(store):
    root, db = store
    org = L.load_datasource(root)

    def broken(sql: str) -> list[dict]:
        raise RuntimeError("no connection")

    out = OQ.open_questions(org, broken, D.get_dialect("sqlite"), today=TODAY)
    assert not [q for q in out["questions"] if q["kind"] == "ambiguity"]
    assert {"table": "orders", "reason": "the probe failed"} in out["dropped"]


# --- Declaring a confirmed candidate ------------------------------------------------------------


def _op(root: Path, db: Path, pattern: str, table: str) -> dict:
    return dict(_found(_ask(root, db), pattern, table)["op"])


def test_a_candidate_is_declared_only_under_a_persons_sign_off(store):
    root, db = store
    op = {**_op(root, db, "date", "orders"), "default": "Placed at"}
    res = curate.apply(root, [op])
    assert not res.applied and "sign it off" in res.skipped[0]["reason"]
    assert not L.load_datasource(root).ambiguities
    res = curate.apply(root, [op], signer="you@example.com", role="system")
    assert not res.applied
    res = curate.apply(root, [op], signer="you@example.com", role="Data Lead")
    assert res.validated and res.applied == ["add ambiguity /orders_date"]
    (amb,) = L.load_datasource(root).ambiguities
    assert amb.behaviour == "answer_and_offer"
    assert [o.label for o in amb.options if o.default] == ["Placed at"]
    log = [json.loads(line) for line in (root / curate.CURATION_LOG).read_text().splitlines()]
    entry = log[-1]
    assert (entry["kind"], entry["name"], entry["signer"], entry["role"]) == (
        "ambiguity",
        "orders_date",
        "you@example.com",
        "Data Lead",
    )
    assert entry["at"]


def test_ask_each_time_declares_no_default(store):
    root, db = store
    op = {**_op(root, db, "status", "orders"), "behaviour": "ask"}
    curate.apply(root, [op], signer="you@example.com", role="Analyst")
    (amb,) = L.load_datasource(root).ambiguities
    assert amb.behaviour == "ask" and not any(o.default for o in amb.options)


def test_a_default_that_is_not_an_option_is_refused(store):
    root, db = store
    op = {**_op(root, db, "date", "orders"), "default": "Delivered at"}
    res = curate.apply(root, [op], signer="you@example.com", role="Analyst")
    assert not res.applied and "not one of the options" in res.skipped[0]["reason"]


def test_a_declared_candidate_is_not_proposed_again_nor_declared_twice(store):
    root, db = store
    op = {**_op(root, db, "date", "orders"), "default": "Placed at"}
    curate.apply(root, [op], signer="you@example.com", role="Analyst")
    out = _ask(root, db)
    assert _found(out, "date", "orders") is None
    assert _dropped(out, "date", "orders") == ["already declared"]
    res = curate.apply(root, [op], signer="you@example.com", role="Analyst")
    assert not res.applied and "already declared" in res.skipped[0]["reason"]


def test_every_proposed_op_validates_once_declared(store):
    root, db = store
    ops = [
        {**q["op"], "default": q["options"][0]["label"]}
        for q in _ask(root, db)["questions"]
        if q["kind"] == "ambiguity"
    ]
    assert len(ops) >= 5
    res = curate.apply(root, ops, signer="you@example.com", role="Analyst")
    assert res.validated and not res.skipped and len(res.applied) == len(ops)


# --- The command and the dialect helper ---------------------------------------------------------


def test_the_command_probes_through_the_profile_and_prints_json(store, monkeypatch, capsys):
    root, db = store
    asked = []
    monkeypatch.setattr(
        I,
        "make_execute_sql_runner",
        lambda profile, **_: asked.append(profile) or _sqlite_runner(db),
    )
    assert cli.main(["open-questions", str(root), "--profile", "demo"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert asked == ["demo"]
    assert out["counts"]["ambiguities"] >= 5 and out["statements"] > 0


def test_the_command_needs_a_profile(store):
    root, _ = store
    with pytest.raises(SystemExit):
        cli.main(["open-questions", str(root)])


@pytest.mark.parametrize(
    "db_type, literal",
    [
        ("postgres", "'2026-06-01'"),
        ("sqlite", "'2026-06-01'"),
        ("bigquery", "'2026-06-01'"),
        ("snowflake", "'2026-06-01'"),
        ("oracle", "DATE '2026-06-01'"),
        ("trino", "DATE '2026-06-01'"),
    ],
)
def test_a_day_is_written_the_way_each_engine_compares_it(db_type, literal):
    assert D.get_dialect(db_type).date_literal("2026-06-01") == literal
