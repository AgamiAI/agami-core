"""What the data can't settle, found while a model is built (#486 part 1, and "ask me as you go").

`sm open-questions` proposes candidate ambiguities from patterns in the model, runs both readings of
each on the data, and keeps only those whose answers differ; then it lists coded values and columns
the build couldn't read. Nothing is declared until a person signs the `add` op a candidate carries.

A synthetic store, built by real introspection over SQLite. Generic names only.
"""

from __future__ import annotations

import json
import re
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
    # the second key is empty on half the rows and equal to the first on the rest
    handoffs = ", ".join(
        f"({i + 1}, {i % 5 + 1}, {i % 5 + 1 if i < 10 else 'NULL'})" for i in range(20)
    )
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
        "CREATE TABLE handoffs(id INTEGER PRIMARY KEY, sent_by INTEGER REFERENCES users(id),"
        " received_by INTEGER REFERENCES users(id));"
        f"INSERT INTO handoffs VALUES {handoffs};"
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
    assert history["question"] == (
        "When someone asks about orders, should their recorded history count too?"
    )
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
        "What do the values of Priority on tickets mean? Type them in Other, e.g. P1 = …, P2 = …"
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
        q["question"] == "What does Event date on events hold? Type a short description in Other."
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


def test_ambiguities_come_first_ordered_by_how_far_the_readings_differ(store):
    root, db = store
    out = _ask(root, db)
    kinds = [q["kind"] for q in out["questions"]]
    # an unasked fiscal start first, so the cap never cuts it (ACE-182); then the ambiguities
    assert kinds == sorted(kinds, key=lambda k: (k != "fiscal_start", k != "ambiguity"))
    spreads = [q["spread"] for q in out["questions"] if q["kind"] == "ambiguity"]
    assert spreads == sorted(spreads, reverse=True)


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


def test_a_table_above_the_size_guard_is_read_on_its_recent_rows_only(store, monkeypatch):
    root, db = store
    monkeypatch.setattr(I, "_OVERLAP_PROBE_MAX_ROWS", 100)
    seen: list[str] = []
    out = _ask(root, db, seen)
    # orders (200 rows) has dates, so it is read from April 2026 on, never whole
    on_orders = [sql for sql in seen if re.search(r'FROM "main"\."orders"( |$)', sql)]
    assert on_orders and all(" WHERE " in sql for sql in on_orders)
    assert out["windowed"] == [{"table": "orders", "since": "2026-04-01"}]
    assert _found(out, "status", "orders")["evidence"].startswith("since April 2026: ")
    # its history (400 rows) has no date to narrow it by: listed as not checked, never scanned
    assert {
        "table": "orders_history",
        "reason": "too large to scan, and no date to read only its recent rows",
    } in out["skipped"]
    assert not any('FROM "main"."orders_history"' in sql for sql in seen)
    # a count of recent rows is not the table's size, so neither is compared with another table's
    assert not any(q.get("op", {}).get("name") == "orders_history" for q in out["questions"])
    look = next(q for q in out["questions"] if q["kind"] == "look_alike")
    assert [t["rows"] for t in look["tables"]] == [None, 50]


def test_a_failed_probe_is_listed_as_failed_not_as_settled(store):
    root, db = store
    org = L.load_datasource(root)

    def broken(sql: str) -> list[dict]:
        raise RuntimeError("no connection")

    out = OQ.open_questions(org, broken, D.get_dialect("sqlite"), today=TODAY)
    assert not [q for q in out["questions"] if q["kind"] == "ambiguity"]
    # failures are their own list now, so a run that checked nothing never reads as settled
    assert {"table": "orders", "reason": "RuntimeError"} in out["failed"]
    assert not [d for d in out["dropped"] if d.get("table") == "orders"]


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


# --- Review fixes ---------------------------------------------------------------------------------


def test_a_second_key_that_is_empty_where_the_first_is_set_counts_as_a_different_link(store):
    root, db = store
    q = _found(_ask(root, db), "join", "handoffs")
    assert q is not None
    assert q["evidence"].startswith("10 of 20 rows link to a different user")


def test_a_join_ranks_by_the_share_of_rows_it_moves(store, monkeypatch):
    root, db = store
    out = _ask(root, db)
    join = _found(out, "join", "tickets")
    assert join["spread"] == 0.5  # 20 of 40 rows
    ambiguities = [q for q in out["questions"] if q["kind"] == "ambiguity"]
    assert [q["spread"] for q in ambiguities] == sorted(
        (q["spread"] for q in ambiguities), reverse=True
    )


def test_a_runner_that_always_fails_is_never_reported_as_settled(store):
    root, db = store
    org = L.load_datasource(root)

    class Boom(RuntimeError):
        pass

    def broken(sql: str) -> list[dict]:
        raise Boom("relation orders: value 'secret@example.com' is not valid\nmore detail")

    out = OQ.open_questions(org, broken, D.get_dialect("sqlite"), today=TODAY)
    assert out["counts"]["failed"] == out["tables_probed"] > 0
    assert {"table": "orders", "reason": "Boom"} in out["failed"]
    # the reason is the error's class only: database text can carry row values
    assert "secret" not in json.dumps(out)
    assert not [q for q in out["questions"] if q["kind"] == "ambiguity"]


def test_every_question_comes_with_a_ready_card_within_the_card_limits(store):
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
    out = _ask(root, db)
    total = len(out["questions"])
    assert {q["kind"] for q in out["questions"]} >= {
        "ambiguity",
        "look_alike",
        "coded_values",
        "unknown_column",
    }
    for i, q in enumerate(out["questions"], 1):
        card = q["card"]
        assert card["header"] == f"Ask {i}/{total}" and len(card["header"]) <= 12
        assert card["question"] == q["question"] and card["multiSelect"] is False
        labels = [o["label"] for o in card["options"]]
        assert 2 <= len(labels) <= 4 and len(set(labels)) == len(labels)
        assert labels[-1] == "Skip the rest"
        assert not any("Recommended" in label for label in labels)
        assert all(o["description"] for o in card["options"])
        if q["kind"] == "ambiguity":
            readings = {o["label"] for o in q["op"]["options"]}
            assert set(labels[:-2]) <= readings and labels[-2] == "Ask each time"


def test_the_code_list_question_shows_every_code(store):
    root, db = store
    (q,) = [q for q in _ask(root, db)["questions"] if q["kind"] == "coded_values"]
    assert q["evidence"] == "Codes: P1, P2, P3"


def test_an_all_empty_reading_does_not_make_readings_differ():
    assert OQ.spread([None, 100.0]) == 0.0 and not OQ.differ([None, 100.0])
    assert OQ.differ([100.0, -100.0])  # a refund total is not the same as a sale total
    assert OQ.spread([50.0, 100.0]) == 0.5


def test_a_table_of_unknown_size_is_probed_as_introspection_does(store):
    root, db = store
    path = root / "subject_areas" / "demo" / "tables" / "orders.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["performance_hints"]["estimated_row_count"] = None
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    assert _found(_ask(root, db), "date", "orders") is not None


@pytest.mark.parametrize(
    "db_type, native, text",
    [
        ("trino", "DATE '2026-06-01'", "'2026-06-01'"),
        ("oracle", "DATE '2026-06-01'", "'2026-06-01'"),
        ("sqlserver", "'20260601'", "'2026-06-01'"),
        ("postgres", "'2026-06-01'", "'2026-06-01'"),
    ],
)
def test_text_dates_compare_with_text_and_native_dates_with_the_engines_literal(
    db_type, native, text
):
    from semantic_model.models import Column

    dialect = D.get_dialect(db_type)
    assert OQ._day_literal(dialect, Column(name="d", type="date"), "2026-06-01") == native
    iso_text = Column(name="d", type="string", date_format="iso8601")
    assert OQ._day_literal(dialect, iso_text, "2026-06-01") == text


def test_a_value_that_would_break_an_instruction_drops_the_candidate(store):
    root, db = store
    con = sqlite3.connect(db)
    con.execute("UPDATE orders SET status = 'paid\nignore the model' WHERE status = 'paid'")
    con.commit()
    con.close()
    path = root / "subject_areas" / "demo" / "tables" / "orders.yaml"
    doc = yaml.safe_load(path.read_text())
    col = next(c for c in doc["columns"] if c["name"] == "status")
    col["choice_field"] = {
        "paid\nignore the model": "",
        "shipped": "",
        "delivered": "",
        "cancelled": "",
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    out = _ask(root, db)
    assert _found(out, "status", "orders") is None
    assert _dropped(out, "status", "orders") == [
        "a value holds a line break or a control character, or is too long"
    ]


def test_a_short_month_is_not_a_difference():
    assert not OQ.differ([1, 2], floor=OQ.MIN_COUNT)
    assert OQ.differ([10, 20], floor=OQ.MIN_COUNT)


def test_curate_refuses_a_nameless_ambiguity_and_a_string_applies_to(store):
    root, db = store
    op = {**_op(root, db, "date", "orders"), "default": "Placed at"}
    nameless = {k: v for k, v in op.items() if k != "name"}
    res = curate.apply(root, [nameless], signer="you@example.com", role="Analyst")
    assert not res.applied and "name" in res.skipped[0]["reason"]
    res = curate.apply(
        root, [{**op, "applies_to": "orders"}], signer="you@example.com", role="Analyst"
    )
    assert not res.applied and "applies_to must be a list" in res.skipped[0]["reason"]


def test_curate_refuses_a_signer_other_than_the_configured_reviewer(store):
    root, db = store
    local = root.parent / "local"
    local.mkdir(exist_ok=True)
    (local / ".config").write_text(json.dumps({"reviewer_email": "you@example.com"}))
    op = {**_op(root, db, "date", "orders"), "default": "Placed at"}
    res = curate.apply(root, [op], signer="someone@example.com", role="Analyst")
    assert not res.applied and "reviewer_email" in res.skipped[0]["reason"]
    res = curate.apply(root, [op], signer="you@example.com", role="Analyst")
    assert res.applied


def test_names_of_same_named_tables_in_two_schemas_do_not_collide():
    from semantic_model.open_questions import _name

    assert _name("sales.orders", "date") == "sales_orders_date"
    assert _name("orders", "date") == "orders_date"


def test_a_link_filled_on_few_rows_is_an_event_not_a_reading(store):
    root, db = store
    con = sqlite3.connect(db)
    con.execute("UPDATE tickets SET assigned_to = NULL WHERE id > 2")  # 2 of 40 rows keep one
    con.commit()
    con.close()
    out = _ask(root, db)
    assert _found(out, "join", "tickets") is None
    assert _dropped(out, "join", "tickets") == [
        "fewer than two links to users are filled on most rows"
    ]


# --- Copilot review on #648: one physical table, however many copies or schemas ----------------


def _ask_org(org, db: Path, seen: list[str] | None = None) -> dict:
    return OQ.open_questions(org, _sqlite_runner(db, seen), D.get_dialect("sqlite"), today=TODAY)


def _table(org, name: str, schema: str = "main"):
    return next(
        t
        for sa in org.subject_areas
        for t in sa.tables_defined
        if t.name == name and t.schema_name == schema
    )


def _copy_into_new_area(org, table, area: str = "other"):
    from semantic_model.models import SubjectArea

    copy = table.model_copy(deep=True)
    org.subject_areas.append(SubjectArea(name=area, tables_defined=[copy]))
    return copy


def test_a_column_sensitive_in_any_copy_of_a_table_is_never_read(store):
    root, db = store
    org = L.load_datasource(root)
    copy = _copy_into_new_area(org, _table(org, "orders"))
    next(
        c for c in copy.columns if c.name == "shipped_at"
    ).sensitive = True  # the copy says personal
    seen: list[str] = []
    out = _ask_org(org, db, seen)
    assert not any("shipped_at" in sql for sql in seen)
    # and the physical table is probed once, not once per copy
    assert (
        sum(
            1
            for sql in seen
            if sql.startswith("SELECT COUNT(*) AS n") and 'FROM "main"."orders"' in sql
        )
        == 1
    )
    assert (
        len(
            [q for q in out["questions"] if q.get("pattern") == "status" and q["table"] == "orders"]
        )
        == 1
    )


def test_a_status_whose_codes_are_decoded_is_read_by_its_meanings(store):
    root, db = store
    con = sqlite3.connect(db)
    con.execute("UPDATE orders SET status = upper(substr(status, 1, 1))")  # P, S, D, C
    con.commit()
    con.close()
    org = L.load_datasource(root)
    status = next(c for c in _table(org, "orders").columns if c.name == "status")
    status.choice_field = {"P": "Paid", "S": "Shipped", "D": "Delivered", "C": "Cancelled"}
    q = _found(_ask_org(org, db), "status", "orders")
    assert [(o["label"], o["value"]) for o in q["options"]] == [
        ("Only Delivered", 50),
        ("Paid, Shipped or Delivered", 150),
    ]
    assert q["options"][1]["how"] == "orders.status IN ('P', 'S', 'D')"


def test_a_join_whose_table_name_carries_its_schema_still_matches(store):
    root, db = store
    org = L.load_datasource(root)
    for r in org.subject_areas[0].relationships:
        if r.from_table == "tickets":
            r.from_table, r.from_schema = "main.tickets", None
    assert _found(_ask_org(org, db), "join", "tickets") is not None


def test_joins_to_same_named_tables_in_two_schemas_get_their_own_names(store):
    root, db = store
    org = L.load_datasource(root)
    _copy_into_new_area(org, _table(org, "users"), "support").schema_name = "support"
    for r in [r for r in org.subject_areas[0].relationships if r.from_table == "tickets"]:
        org.subject_areas[0].relationships.append(r.model_copy(update={"to_schema": "support"}))
    joins = [
        q
        for q in _ask_org(org, db)["questions"]
        if q.get("pattern") == "join" and q["table"] == "tickets"
    ]
    assert sorted(q["op"]["name"] for q in joins) == [
        "tickets_main_users_link",
        "tickets_support_users_link",
    ]
    assert any("= support.users.id" in o["how"] for q in joins for o in q["op"]["options"])


def test_a_copy_pairs_with_the_table_in_its_own_schema(store):
    root, db = store
    org = L.load_datasource(root)
    _copy_into_new_area(org, _table(org, "orders"), "archive").schema_name = "archive"
    q = next(q for q in _ask_org(org, db)["questions"] if q["kind"] == "look_alike")
    assert [t["table"] for t in q["tables"]] == ["main.orders", "orders_old"]


def test_a_fiscal_year_is_checked_on_a_table_with_a_single_date(store):
    from semantic_model.models import Column, PerformanceHints, Table

    root, db = store
    start = date(2026, 1, 1)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE visits(id INTEGER PRIMARY KEY, visited_at TEXT)")
    con.executemany(
        "INSERT INTO visits VALUES (?, ?)",
        [(i + 1, str(start + timedelta(days=i))) for i in range(250)],
    )
    con.commit()
    con.close()
    org = L.load_datasource(root)
    org.fiscal_year_start_month = 4
    org.subject_areas[0].tables_defined.append(
        Table(
            name="visits",
            schema="main",
            performance_hints=PerformanceHints(estimated_row_count=250),
            columns=[
                Column(name="id", type="integer", primary_key=True),
                Column(name="visited_at", type="string", date_format="iso8601"),
            ],
        )
    )
    q = _found(_ask_org(org, db), "fiscal", None)
    assert q["evidence"].startswith("visits by Visited at")
    assert [o["value"] for o in q["options"]] == [250, 160]


def test_answering_one_look_alike_pair_leaves_the_others_asked(store):
    from semantic_model.models import Column, Table

    root, db = store
    con = sqlite3.connect(db)
    for name in ("orders_new", "orders_backup"):
        con.execute(f"CREATE TABLE {name}(id INTEGER PRIMARY KEY)")
        con.executemany(f"INSERT INTO {name} VALUES (?)", [(i,) for i in range(1, 11)])
    con.commit()
    con.close()
    org = L.load_datasource(root)
    for name in ("orders_new", "orders_backup"):
        org.subject_areas[0].tables_defined.append(
            Table(
                name=name,
                schema="main",
                columns=[Column(name="id", type="integer", primary_key=True)],
            )
        )
    out = _ask_org(org, db)
    pair = next(
        q
        for q in out["questions"]
        if q["kind"] == "look_alike"
        and {t["table"] for t in q["tables"]} == {"orders", "orders_new"}
    )
    (op,) = next(t for t in pair["tables"] if t["table"] == "orders_new")["ops"]
    _table(org, "orders").caveats = op["value"]  # what applying the answer writes
    pairs = [
        {t["table"] for t in q["tables"]}
        for q in _ask_org(org, db)["questions"]
        if q["kind"] == "look_alike"
    ]
    assert {"orders", "orders_new"} not in pairs
    assert {"orders", "orders_backup"} in pairs and {"orders", "orders_old"} in pairs


# --- ACE-183: large tables, no cap, readings a person would mean, plain wording -----------------


def _case_table(org, db: Path, rows: int = 20_000_000):
    """A ticket-like table named in code (`support_case`), far above the size guard, with three
    business dates and a status whose closed-but-incomplete value contradicts "completed". Its
    entity calls the rows cases. 100 rows from 2024 sit outside the recent window."""
    # the data ends in July, so June is the last complete month every date reaches
    from semantic_model.models import Column, Entity, EntityMapping, PerformanceHints, Table

    states = ("resolved", "closed_incomplete", "fulfilled")
    data = []
    for i in range(84):  # two cases opened a day, from June 1 to July 12 2026
        opened = date(2026, 6, 1) + timedelta(days=i // 2)
        data.append(
            (
                i + 1,
                states[i % 3],
                str(opened),
                str(opened + timedelta(days=3)),
                str(opened + timedelta(days=10)),
            )
        )
    for i in range(100):
        old = str(date(2024, 3, 1) + timedelta(days=i))
        data.append((1000 + i, "fulfilled", old, old, old))
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE support_case(id INTEGER PRIMARY KEY, state TEXT, opened_at TEXT,"
        " resolved_at TEXT, closed_at TEXT)"
    )
    con.executemany("INSERT INTO support_case VALUES (?, ?, ?, ?, ?)", data)
    con.commit()
    con.close()
    iso = {"type": "string", "date_format": "iso8601"}
    sa = org.subject_areas[0]
    sa.tables_defined.append(
        Table(
            name="support_case",
            schema="main",
            performance_hints=PerformanceHints(estimated_row_count=rows),
            columns=[
                Column(name="id", type="integer", primary_key=True),
                Column(
                    name="state",
                    type="string",
                    choice_field={
                        "resolved": "Resolved",
                        "closed_incomplete": "Closed Incomplete",
                        "fulfilled": "Fulfilled",
                    },
                ),
                Column(name="opened_at", **iso),
                Column(name="resolved_at", **iso),
                Column(name="closed_at", **iso),
            ],
        )
    )
    sa.entities.append(
        Entity(
            name="Case", maps_to=[EntityMapping(table="support_case", column="id", primary=True)]
        )
    )


def test_a_twenty_million_row_table_is_asked_about_its_dates_from_its_recent_rows(store):
    root, db = store
    org = L.load_datasource(root)
    _case_table(org, db)
    seen: list[str] = []
    out = _ask_org(org, db, seen)
    q = _found(out, "date", "support_case")
    assert q is not None
    assert [o["label"] for o in q["options"]] == ["Opened at", "Resolved at", "Closed at"]
    # June 2026 by each date; a 12-month window would reach before the rows read, so it is not used
    assert q["evidence"] == "June 2026: 60 by Opened at; 54 by Resolved at; 40 by Closed at"
    on_cases = [sql for sql in seen if '"support_case"' in sql]
    assert len(on_cases) == 2 and all(" WHERE " in sql for sql in on_cases)
    assert {"table": "support_case", "since": "2026-04-01"} in out["windowed"]
    assert not [s for s in out["skipped"] if s["table"] == "support_case"]


def test_a_large_table_with_no_date_is_listed_as_not_checked(store):
    root, db = store
    org = L.load_datasource(root)
    _table(org, "tickets").performance_hints.estimated_row_count = 20_000_000
    seen: list[str] = []
    out = _ask_org(org, db, seen)
    assert {
        "table": "tickets",
        "reason": "too large to scan, and no date to read only its recent rows",
    } in out["skipped"]
    assert not any('FROM "main"."tickets"' in sql for sql in seen)


def test_the_largest_tables_are_probed_first_when_there_are_more_than_the_cap(store, monkeypatch):
    root, db = store
    org = L.load_datasource(root)
    _case_table(org, db)
    monkeypatch.setattr(OQ, "MAX_TABLES", 1)
    out = _ask_org(org, db)
    assert _found(out, "date", "support_case") is not None
    assert {"table": "orders", "reason": "over the 1-table probe cap"} in out["skipped"]


def test_a_status_that_contradicts_the_word_is_never_a_reading_on_its_own(store):
    root, db = store
    org = L.load_datasource(root)
    _case_table(org, db)
    q = _found(_ask_org(org, db), "status", "support_case")
    # "Closed Incomplete" is the most final-sounding value, but no one means it by "completed"
    assert [(o["label"], o["value"]) for o in q["options"]] == [
        ("Only Resolved", 28),
        ("Fulfilled or Resolved", 56),
        ("Fulfilled or Resolved, and also Closed Incomplete", 84),
    ]
    assert q["evidence"] == "since April 2026: 28 Fulfilled; 28 Resolved; 28 Closed Incomplete"


def test_questions_name_the_rows_in_plain_words_never_a_table_name(store):
    root, db = store
    org = L.load_datasource(root)
    _case_table(org, db)
    out = _ask_org(org, db)
    assert _found(out, "date", "support_case")["question"] == (
        "When someone says 'cases last month', which date do they mean?"
    )
    assert _found(out, "status", "support_case")["question"] == (
        "When someone says 'completed cases', which statuses count?"
    )
    names = {t.name for sa in org.subject_areas for t in sa.tables_defined if "_" in t.name}
    for q in out["questions"]:
        if q["kind"] != "ambiguity":
            continue
        text = q["question"].lower()
        assert not any(n.lower() in text or n.replace("_", " ") in text for n in names), text
        assert "support" not in text
        if q["pattern"] == "date":  # the example word never names one of the readings
            words = {w for o in q["options"] for w in o["label"].lower().split()} - {"at", "on"}
            assert not words & set(re.findall(r"[a-z]+", text)), text


def test_an_open_status_question_says_open_never_one_of_its_readings(store):
    root, db = store
    con = sqlite3.connect(db)
    con.execute("UPDATE orders SET status = 'pending' WHERE status = 'cancelled' AND id % 8 = 4")
    con.execute("UPDATE orders SET status = 'awaiting' WHERE status = 'cancelled'")
    con.commit()
    con.close()
    org = L.load_datasource(root)
    status = next(c for c in _table(org, "orders").columns if c.name == "status")
    status.choice_field = {v: "" for v in ("paid", "shipped", "delivered", "pending", "awaiting")}
    q = next(
        q
        for q in _ask_org(org, db)["questions"]
        if q.get("pattern") == "status" and q["op"]["name"] == "orders_status_open"
    )
    assert q["question"] == "When someone says 'open orders', which statuses count?"


def test_every_candidate_is_returned_past_a_dozen_widest_apart_first(store):
    from semantic_model.models import Column, PerformanceHints, Table

    root, db = store
    org = L.load_datasource(root)
    con = sqlite3.connect(db)
    iso = {"type": "string", "date_format": "iso8601"}
    for k in range(15):
        name = f"jobs_{chr(97 + k)}"
        con.execute(f"CREATE TABLE {name}(id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT)")
        same = 36 - 2 * k  # rows that end in June too; the rest end in July
        con.executemany(
            f"INSERT INTO {name} VALUES (?, ?, ?)",
            [
                (i + 1, f"2026-06-{i % 28 + 1:02d}", "2026-06-29" if i < same else "2026-07-02")
                for i in range(40)
            ]
            + [(41, "2026-07-05", "2026-07-06")],  # the data runs into July, so June is complete
        )
        org.subject_areas[0].tables_defined.append(
            Table(
                name=name,
                schema="main",
                performance_hints=PerformanceHints(estimated_row_count=40),
                columns=[
                    Column(name="id", type="integer", primary_key=True),
                    Column(name="started_at", **iso),
                    Column(name="ended_at", **iso),
                ],
            )
        )
    con.commit()
    con.close()
    out = _ask_org(org, db)
    assert "more" not in out
    jobs = [q["table"] for q in out["questions"] if (q.get("table") or "").startswith("jobs_")]
    # all 15, with the job table whose two dates differ most first
    assert jobs == [f"jobs_{chr(97 + k)}" for k in reversed(range(15))]
    ambiguities = [q for q in out["questions"] if q["kind"] == "ambiguity"]
    assert len(ambiguities) > 12
    spreads = [q["spread"] for q in ambiguities]
    assert spreads == sorted(spreads, reverse=True)


def test_a_month_before_the_recent_rows_is_never_compared(store, monkeypatch):
    root, db = store
    monkeypatch.setattr(I, "_OVERLAP_PROBE_MAX_ROWS", 100)
    org = L.load_datasource(root)
    # orders end in July; read from July on, June's counts would miss its rows dated only in June
    out = OQ.open_questions(
        org, _sqlite_runner(db), D.get_dialect("sqlite"), today=date(2026, 10, 15)
    )
    assert _dropped(out, "date", "orders") == [
        "the rows since July 2026 hold no complete month to compare"
    ]


def test_a_table_read_on_its_recent_rows_is_not_compared_with_its_history(store):
    root, db = store
    org = L.load_datasource(root)
    _table(org, "orders").performance_hints.estimated_row_count = 20_000_000
    out = _ask_org(org, db)
    assert not any(q.get("op", {}).get("name") == "orders_history" for q in out["questions"])
    assert _dropped(out, "current", "orders") == [
        "one table was read on its recent rows only, so their sizes can't be compared"
    ]


def test_a_status_whose_every_done_value_failed_is_not_asked(store):
    root, db = store
    con = sqlite3.connect(db)
    con.execute("UPDATE orders SET status = 'closed_failed' WHERE status IN ('paid', 'delivered')")
    con.execute("UPDATE orders SET status = 'closed_cancelled' WHERE status = 'shipped'")
    con.commit()
    con.close()
    org = L.load_datasource(root)
    status = next(c for c in _table(org, "orders").columns if c.name == "status")
    status.choice_field = {"closed_failed": "", "closed_cancelled": "", "cancelled": ""}
    out = _ask_org(org, db)
    assert _found(out, "status", "orders") is None
    assert _dropped(out, "status", "orders") == [
        "every done value of status also says it didn't succeed"
    ]


def test_an_entity_without_a_plural_is_made_plural():
    assert [OQ._plural(w) for w in ("category", "day", "box", "case")] == [
        "categories",
        "days",
        "boxes",
        "cases",
    ]


def test_a_card_header_stays_within_twelve_characters_however_many_questions():
    assert OQ._header(3, 40) == "Ask 3/40"
    assert OQ._header(1234, 5678) == "Ask 1234"
    assert all(len(OQ._header(i, 10**6)) <= 12 for i in (1, 999, 10**6))


# --- ACE-183 follow-up: one question per meaning, and the plain reading offered ------------------


def _status_table(org, db: Path, name: str, columns: tuple[str, ...], values: dict[str, int]):
    """A small table whose status columns all hold `values` (value -> rows), each labelled itself."""
    from semantic_model.models import Column, PerformanceHints, Table

    rows = [v for v, n in values.items() for _ in range(n)]
    con = sqlite3.connect(db)
    con.execute(
        f"CREATE TABLE {name}(id INTEGER PRIMARY KEY, {', '.join(f'{c} TEXT' for c in columns)})"
    )
    con.executemany(
        f"INSERT INTO {name} VALUES (?{', ?' * len(columns)})",
        [(i + 1, *([v] * len(columns))) for i, v in enumerate(rows)],
    )
    con.commit()
    con.close()
    org.subject_areas[0].tables_defined.append(
        Table(
            name=name,
            schema="main",
            performance_hints=PerformanceHints(estimated_row_count=len(rows)),
            columns=[
                Column(name="id", type="integer", primary_key=True),
                *(
                    Column(name=c, type="string", choice_field={v: v for v in values})
                    for c in columns
                ),
            ],
        )
    )


def _status_questions(out: dict, table: str) -> list[dict]:
    return [q for q in out["questions"] if q.get("pattern") == "status" and q["table"] == table]


def test_two_status_columns_holding_the_same_values_are_asked_once(store):
    root, db = store
    org = L.load_datasource(root)
    # a ticketing system keeps the state twice, under a generic and a table-specific name
    _status_table(
        org,
        db,
        "problems",
        ("state", "problem_state"),
        {"Open": 10, "Work in Progress": 15, "Pending": 30},
    )
    out = _ask_org(org, db)
    (q,) = _status_questions(out, "problems")
    assert q["question"] == "When someone says 'open problems', which statuses count?"
    assert "the same question" in " ".join(_dropped(out, "status", "problems"))


def test_an_open_status_offers_the_value_called_open_first(store):
    root, db = store
    org = L.load_datasource(root)
    _status_table(
        org, db, "problems", ("state",), {"Open": 10, "Work in Progress": 15, "Pending": 30}
    )
    (q,) = _status_questions(_ask_org(org, db), "problems")
    # Pending holds the most rows, but "open problems" read plainly is the ones marked Open
    assert [(o["label"], o["value"]) for o in q["options"]] == [
        ("Only Open", 10),
        ("Open, Work in Progress or Pending", 55),
    ]


def test_a_completed_status_offers_the_value_called_completed_first(store):
    root, db = store
    org = L.load_datasource(root)
    _status_table(
        org,
        db,
        "projects",
        ("status",),
        {
            "Completed": 20,
            "Fieldwork Completed": 5,
            "Closed Complete": 12,
            "Closed Incomplete": 4,
            "Cancelled": 3,
        },
    )
    (q,) = _status_questions(_ask_org(org, db), "projects")
    assert q["question"] == "When someone says 'completed projects', which statuses count?"
    assert [(o["label"], o["value"]) for o in q["options"]] == [
        ("Only Completed", 20),
        ("Closed Complete, Completed or Fieldwork Completed", 37),
        # the contradicting value is never a reading on its own, only something to also include
        ("Closed Complete, Completed or Fieldwork Completed, and also Closed Incomplete", 41),
    ]
