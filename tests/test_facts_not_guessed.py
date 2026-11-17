"""Facts the build must not guess (ACE-182): repeating rows are measured, the fiscal-year start is
asked, and a unit is a value to set rather than a reading to ask about each time.

A synthetic store over SQLite, built by real introspection. Generic names only.
"""

from __future__ import annotations

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

from semantic_model import build as B  # noqa: E402
from semantic_model import cli, curate  # noqa: E402
from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import open_questions as OQ  # noqa: E402
from semantic_model import validator as V  # noqa: E402

TODAY = date(2026, 7, 15)
REPEAT = 5


def _schema() -> str:
    start = TODAY - timedelta(days=200)
    tickets = ", ".join(f"({i + 1}, '{start + timedelta(days=i)}', {i % 2})" for i in range(200))
    # each ticket once per stage: the view repeats every ticket 5 times, differing only in duration
    stages = ", ".join(f"({s}, {s * 3})" for s in range(REPEAT))
    orders = ", ".join(f"({i + 1}, {100 + i}, {(100 + i) * 0.9:.2f})" for i in range(50))
    return (
        "CREATE TABLE tickets(ticket_id INTEGER, opened_at DATE, is_escalated INTEGER);"
        f"INSERT INTO tickets VALUES {tickets};"
        "CREATE TABLE stages(stage INTEGER, hours INTEGER);"
        f"INSERT INTO stages VALUES {stages};"
        "CREATE VIEW tickets_v AS SELECT t.ticket_id, t.opened_at, t.is_escalated,"
        " s.hours AS stage_hours FROM tickets t CROSS JOIN stages s;"
        "CREATE TABLE sales(sales_id INTEGER, amount_usd REAL, amount_eur REAL);"
        f"INSERT INTO sales VALUES {orders};"
    )


def _runner(db: Path, seen: list[str] | None = None):
    def run(sql: str) -> list[dict]:
        if seen is not None:
            seen.append(" ".join(sql.split()))
        con = sqlite3.connect(db)
        try:
            cur = con.execute(sql)
            names = [d[0] for d in cur.description or []]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            con.close()

    return run


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "demo.db"
    con = sqlite3.connect(path)
    con.executescript(_schema())
    con.commit()
    con.close()
    return path


def _large(monkeypatch, *names: str, rows: int = 10_000_000) -> None:
    """Report `names` at `rows` estimated rows, as a warehouse catalog would for a big view."""
    real = I._estimate_rows

    def fake(dialect, runner, schema, table, budget=None):
        return rows if table in names else real(dialect, runner, schema, table, budget)

    monkeypatch.setattr(I, "_estimate_rows", fake)


def _rates(props: list[dict]) -> list[dict]:
    return [m for m in props if m["name"].endswith("_rate")]


def _table(org, name: str):
    return next(t for sa in org.subject_areas for t in sa.tables_defined if t.name == name)


# ---------------------------------------------------------------------------
# 1. Repeating rows
# ---------------------------------------------------------------------------


def test_a_large_view_repeating_each_key_is_caught_with_its_factor(tmp_path, db, monkeypatch):
    _large(monkeypatch, "tickets_v")
    monkeypatch.setattr(I, "_today", lambda: TODAY)
    seen: list[str] = []
    org, rep = I.introspect(
        "demo", "sqlite", runner=_runner(db, seen), artifacts_dir=tmp_path, dry_run=True
    )
    view = _table(org, "tickets_v")
    assert view.grain == []  # never declared one row per ticket
    assert B.repeated_key(view) == ("ticket_id", 5)
    (caveat,) = [c for c in view.caveats if "ticket_id" in c]
    assert "5 times" in caveat and "COUNT(DISTINCT ticket_id)" in caveat
    # bounded: a recent window on its date, never a full scan of the large view
    (probe,) = [s for s in seen if "AS distinct_count" in s and "tickets_v" in s]
    assert '"opened_at" >=' in probe
    assert any("tickets_v" in n and "5 times" in n for n in rep.notes)
    # the base table holds each ticket once: no caveat, and its key is its grain
    assert B.repeated_key(_table(org, "tickets")) is None


def test_a_large_view_with_no_date_is_checked_on_a_bounded_sample(tmp_path, db, monkeypatch):
    con = sqlite3.connect(db)
    con.execute("CREATE VIEW v_ticket AS SELECT ticket_id, stage_hours FROM tickets_v")
    con.commit()
    con.close()
    _large(monkeypatch, "v_ticket")
    seen: list[str] = []
    org, _ = I.introspect(
        "demo", "sqlite", runner=_runner(db, seen), artifacts_dir=tmp_path, dry_run=True
    )
    assert B.repeated_key(_table(org, "v_ticket")) == ("ticket_id", 5)
    (probe,) = [s for s in seen if "AS distinct_count" in s and "v_ticket" in s]
    assert "LIMIT" in probe


def test_a_small_repeating_view_is_caught_too(tmp_path, db):
    org, _ = I.introspect(
        "demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path, dry_run=True
    )
    assert B.repeated_key(_table(org, "tickets_v")) == ("ticket_id", 5)
    assert _table(org, "tickets").grain == ["ticket_id"]


def test_the_repeat_caveat_is_not_a_measured_figure_and_survives_a_refresh(tmp_path, db):
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path)
    root = tmp_path / "demo"
    org = L.load_datasource(root)
    assert not [w for w in V.validate(org).warnings if "measured_figure" in str(w)]
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path)
    view = _table(L.load_datasource(root), "tickets_v")
    assert B.repeated_key(view) == ("ticket_id", 5)
    assert len([c for c in view.caveats if "ticket_id" in c]) == 1


def test_a_refresh_that_finds_repeats_drops_the_key_as_grain(tmp_path, db):
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path)
    root = tmp_path / "demo"
    assert _table(L.load_datasource(root), "tickets").grain == ["ticket_id"]
    con = sqlite3.connect(db)
    con.execute("INSERT INTO tickets SELECT * FROM tickets")  # every ticket now on two rows
    con.commit()
    con.close()
    # a refresh onto the existing model merges each table into its stored copy
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path, append=True)
    tickets = _table(L.load_datasource(root), "tickets")
    assert B.repeated_key(tickets) == ("ticket_id", 2)
    assert tickets.grain == [] and not any(c.primary_key for c in tickets.columns)


def test_metrics_proposed_for_a_repeating_table_count_distinct_records(tmp_path, db):
    org, _ = I.introspect(
        "demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path, dry_run=True
    )
    view = _table(org, "tickets_v")
    for c in view.columns:
        if c.name == "is_escalated":
            c.type = "boolean"
    (rate,) = _rates(B.suggest_metrics(view, D.get_dialect("sqlite"), flag_rates=True))
    binding = rate["bindings"]["SQLite"]
    assert "COUNT(DISTINCT" in binding and "ticket_id" in binding and "AVG(" not in binding
    # the same table without repeats keeps the per-row average
    view.caveats = []
    (rate,) = B.suggest_metrics(view, D.get_dialect("sqlite"), flag_rates=True)
    assert rate["bindings"]["SQLite"].startswith("AVG(")


def test_a_repeating_table_gets_a_distinct_record_count_that_test_runs_to_the_record_count(
    tmp_path, db
):
    # The repeat check leaves such a table without a one-column grain; its record key is still
    # known, so the count people ask for first is proposed on it, distinct over that key.
    org, _ = I.introspect(
        "demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path, dry_run=True
    )
    sqlite = D.get_dialect("sqlite")
    view = _table(org, "tickets_v")
    assert view.grain == [] and B.repeated_key(view)[0] == "ticket_id"
    props = B.suggest_metrics(view, sqlite)
    (count,) = [m for m in props if m["name"] == B.record_count_name("tickets_v", "ticket_id")]
    assert count["bindings"]["SQLite"] == "COUNT(DISTINCT ticket_id)"
    assert count["_proposal"]["flags"] == [] and count["_proposal"]["columns"] == ["ticket_id"]
    assert not any("duration" in m["name"] or "avg" in m["name"] for m in props)
    res = cli._test_run_table(_runner(db), view, sqlite, props)[count["name"]]
    (truth,) = _runner(db)("SELECT COUNT(*) AS n FROM tickets")
    assert res["value"] == truth["n"] == 200 and res["rows"] == 200 * REPEAT
    assert cli._test_run_flags("tickets_v", res) == []
    # a table that holds each record once is unchanged: no count is proposed on it
    assert B.suggest_metrics(_table(org, "tickets"), sqlite) == []


def test_no_duration_average_is_proposed_on_a_repeating_table():
    from semantic_model.models import Column, Table

    t = Table(
        name="tickets_v",
        columns=[
            Column(name="ticket_id", type="integer"),
            Column(name="started_at", type="timestamp"),
            Column(name="ended_at", type="timestamp"),
        ],
    )
    assert B.suggest_metrics(
        t, D.get_dialect("sqlite")
    )  # one row per record: the average is offered
    t.caveats = [B.repeat_caveat("ticket_id", 1.5)]
    assert B.repeated_key(t) == ("ticket_id", 1.5)
    # the distinct-record count alone: no average over the repeats
    assert [m["name"] for m in B.suggest_metrics(t, D.get_dialect("sqlite"))] == [
        B.record_count_name("tickets_v", "ticket_id")
    ]


@pytest.mark.parametrize(
    "key",
    ["Work Order_ID", 'o\'brien "id"', "sales.order_id", "a: b", "ticket_id"],
)
@pytest.mark.parametrize("factor", [1.5, 4.0])
def test_the_repeat_caveat_round_trips_any_key(key, factor):
    # Read back by the caveat's fixed wording, not by whitespace: a key with spaces or punctuation
    # once came back None, so the distinct count was dropped and a row-weighted average proposed.
    text = B.repeat_caveat(key, factor)
    assert B.is_repeat_caveat(text)
    from semantic_model.models import Table

    assert B.repeated_key(Table(name="t", columns=[], caveats=[text])) == (key, factor)


def test_a_repeating_table_whose_key_has_spaces_gets_its_distinct_count():
    from semantic_model.models import Column, Table

    key = "Work Order_ID"
    t = Table(
        name="Work Orders",
        columns=[
            Column(name=key, type="integer"),
            Column(name="started_at", type="timestamp"),
            Column(name="ended_at", type="timestamp"),
        ],
        caveats=[B.repeat_caveat(key, 3.0)],
    )
    names = [m["name"] for m in B.suggest_metrics(t, D.get_dialect("sqlite"))]
    assert names == [B.record_count_name("Work Orders", key)]


def test_a_distinct_rate_binding_runs_and_counts_each_record_once(tmp_path, db):
    org, _ = I.introspect(
        "demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path, dry_run=True
    )
    view = _table(org, "tickets_v")
    for c in view.columns:
        if c.name == "is_escalated":
            c.type = "integer"
    (rate,) = _rates(B.suggest_metrics(view, D.get_dialect("sqlite"), flag_rates=["is_escalated"]))
    (row,) = _runner(db)(f"SELECT {rate['bindings']['SQLite']} AS r FROM tickets_v")
    assert row["r"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 2. The fiscal-year start
# ---------------------------------------------------------------------------


def _ask(root: Path, db: Path) -> dict:
    return OQ.open_questions(
        L.load_datasource(root), _runner(db), D.get_dialect("sqlite"), today=TODAY
    )


def test_a_build_never_writes_a_fiscal_start_nobody_gave(tmp_path, db):
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path)
    root = tmp_path / "demo"
    doc = yaml.safe_load((root / "datasource.yaml").read_text())
    assert "fiscal_year_start_month" not in doc
    assert L.load_datasource(root).fiscal_year_start_month is None


def test_an_unasked_fiscal_start_is_always_a_question_with_months_only(tmp_path, db):
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path)
    (q,) = [q for q in _ask(tmp_path / "demo", db)["questions"] if q["kind"] == "fiscal_start"]
    labels = [o["label"] for o in q["card"]["options"]]
    assert labels[0] == "January" and labels[-1] == "Skip the rest"
    assert "Ask each time" not in labels


def test_a_january_answer_is_recorded_as_asked_and_kept_by_a_rebuild(tmp_path, db):
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path)
    root = tmp_path / "demo"
    res = curate.set_fiscal_year_start(root, 1)
    assert res.validated and res.applied == ["fiscal_year_start_month"]
    assert yaml.safe_load((root / "datasource.yaml").read_text())["fiscal_year_start_month"] == 1
    assert not [q for q in _ask(root, db)["questions"] if q["kind"] == "fiscal_start"]
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path)  # a reload
    assert L.load_datasource(root).fiscal_year_start_month == 1


def test_a_january_an_older_build_wrote_unasked_is_still_a_question(tmp_path, db):
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path)
    root = tmp_path / "demo"
    ds = root / "datasource.yaml"
    doc = yaml.safe_load(ds.read_text())
    ds.write_text(yaml.safe_dump({**doc, "fiscal_year_start_month": 1}, sort_keys=False))
    assert [q for q in _ask(root, db)["questions"] if q["kind"] == "fiscal_start"]
    # any other month on file was someone's answer: the build never wrote one
    ds.write_text(yaml.safe_dump({**doc, "fiscal_year_start_month": 9}, sort_keys=False))
    assert not [q for q in _ask(root, db)["questions"] if q["kind"] == "fiscal_start"]


def test_set_fiscal_year_command_and_its_bounds(tmp_path, db, capsys):
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path)
    root = tmp_path / "demo"
    assert cli.main(["set-fiscal-year", str(root), "--month", "9"]) == 0
    assert L.load_datasource(root).fiscal_year_start_month == 9
    assert cli.main(["set-fiscal-year", str(root), "--month", "13"]) == 1
    assert (
        L.load_datasource(root).fiscal_year_start_month == 9
    )  # the refused write left it as it was
    capsys.readouterr()


def test_an_unset_fiscal_start_is_left_out_of_every_dump(tmp_path, db):
    from semantic_model.models import Datasource

    assert "fiscal_year_start_month" not in Datasource(datasource="x").model_dump()
    assert (
        Datasource(datasource="x", fiscal_year_start_month=1).model_dump()[
            "fiscal_year_start_month"
        ]
        == 1
    )


# ---------------------------------------------------------------------------
# 3. Units
# ---------------------------------------------------------------------------


def test_a_unit_question_offers_values_and_no_behaviour(tmp_path, db):
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path)
    out = _ask(tmp_path / "demo", db)
    (q,) = [q for q in out["questions"] if q.get("table") == "sales"]
    assert q["pattern"] == "unit"
    assert q["question"] == "When someone asks how much sales came to, which currency do they mean?"
    labels = [o["label"] for o in q["card"]["options"]]
    assert labels == ["USD", "EUR", "Skip the rest"]
    assert "Ask each time" not in labels
    assert [o["how"] for o in q["options"]] == [
        "SUM(sales.amount_usd)",
        "SUM(sales.amount_eur)",
    ]


def test_a_key_named_for_another_record_is_never_checked():
    from semantic_model.models import Column

    cols = [Column(name="order_id", type="integer"), Column(name="sku", type="string")]
    assert I._record_key("order_items", cols) is None  # one row per line: counting rows is right
    assert I._record_key("orders", cols) == "order_id"
    assert I._record_key("vw_orders", cols) == "order_id"


def test_a_repeat_check_that_reads_nothing_settles_nothing():
    from semantic_model.models import Column

    cols = [Column(name="id", type="integer"), Column(name="opened_at", type="date")]
    sqlite = D.get_dialect("sqlite")
    seen: list[str] = []

    def empty(sql):
        seen.append(sql)
        return [{"total": 0, "distinct_count": 0}]

    # an empty window, then an empty sample: no answer, and never more than the two statements
    assert I._repeat_check(sqlite, empty, None, "events", cols, est_rows=10_000_000) is None
    assert len(seen) == 2
    garbled = lambda sql: [{"total": "n/a", "distinct_count": "n/a"}]  # noqa: E731
    assert I._repeat_check(sqlite, garbled, None, "events", cols) is None


# ---------------------------------------------------------------------------
# Review round
# ---------------------------------------------------------------------------


def _merged(old_grain, fresh_caveats, *, repeat_read=True, old_caveats=()):
    from semantic_model.models import Column, Table

    def table(**kw):
        return Table(
            name="tickets_v",
            columns=[
                Column(name="ticket_id", type="integer", primary_key=old_grain == ["ticket_id"]),
                Column(name="stage", type="string"),
            ],
            **kw,
        )

    old = table(grain=old_grain, caveats=list(old_caveats))
    fresh = table(grain=[], caveats=fresh_caveats)
    for c in fresh.columns:
        c.primary_key = False
    return I._merge_table(old, fresh, repeat_read=repeat_read)[0]


def test_a_measured_repeat_leaves_a_composite_grain_a_person_stated():
    caveat = [B.repeat_caveat("ticket_id", 5)]
    assert _merged(["ticket_id", "stage"], caveat).grain == ["ticket_id", "stage"]
    single = _merged(["ticket_id"], caveat)
    assert single.grain == [] and not any(c.primary_key for c in single.columns)


def test_a_stored_repeat_goes_only_when_a_check_finds_none():
    stored = [B.repeat_caveat("ticket_id", 5), "Kept by a person."]
    cleared = _merged([], [], old_caveats=stored)
    assert cleared.caveats == ["Kept by a person."]
    unread = _merged([], [], repeat_read=False, old_caveats=stored)
    assert unread.caveats == stored  # the check failed: nothing is known to have changed


def test_a_refresh_that_finds_no_repeat_drops_the_caveat(tmp_path, db):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO tickets SELECT * FROM tickets")
    con.commit()
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path)
    root = tmp_path / "demo"
    assert B.repeated_key(_table(L.load_datasource(root), "tickets")) == ("ticket_id", 2)
    con.execute(
        "DELETE FROM tickets WHERE rowid NOT IN (SELECT MIN(rowid) FROM tickets GROUP BY ticket_id)"
    )
    con.commit()
    con.close()
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path, append=True)
    tickets = _table(L.load_datasource(root), "tickets")
    assert B.repeated_key(tickets) is None and tickets.grain == ["ticket_id"]


def test_a_failed_repeat_check_keeps_the_stored_caveat(tmp_path, db):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO tickets SELECT * FROM tickets")
    con.commit()
    con.close()
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path)
    root = tmp_path / "demo"
    real = _runner(db)

    def flaky(sql):
        if "COUNT(DISTINCT" in sql and '"tickets"' in sql:
            raise RuntimeError("connection dropped")
        return real(sql)

    I.introspect("demo", "sqlite", runner=flaky, artifacts_dir=tmp_path, append=True)
    assert B.repeated_key(_table(L.load_datasource(root), "tickets")) == ("ticket_id", 2)


def test_the_admin_page_says_an_unset_fiscal_start_is_not_set():
    import admin
    from semantic_model.models import Datasource

    unset = admin.model_overview_html(Datasource(datasource="acme"), None, "acme", ["acme"])
    assert "Not set yet" in unset and "None" not in unset.split("Fiscal year")[1][:120]
    sept = Datasource(datasource="acme", fiscal_year_start_month=9)
    assert "Starts month 9" in admin.model_overview_html(sept, None, "acme", ["acme"])


def test_a_unit_answer_is_recorded_as_facts_not_a_behaviour(tmp_path, db):
    I.introspect("demo", "sqlite", runner=_runner(db), artifacts_dir=tmp_path)
    root = tmp_path / "demo"
    (q,) = [q for q in _ask(root, db)["questions"] if q.get("table") == "sales"]
    assert q["op"] is None  # nothing that would declare an ambiguity
    usd = next(o for o in q["options"] if o["label"] == "USD")
    res = curate.apply(root, usd["ops"], signer="you@example.com", role="Analyst")
    assert res.validated, res.errors
    org = L.load_datasource(root)
    assert not org.ambiguities
    sales = _table(org, "sales")
    assert {c.name: c.unit for c in sales.columns if c.unit} == {
        "amount_usd": "USD",
        "amount_eur": "EUR",
    }
    assert any(c.startswith("Amounts are reported in USD") for c in sales.caveats)
    assert not [q for q in _ask(root, db)["questions"] if q.get("table") == "sales"]
