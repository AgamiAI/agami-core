"""The flawed-inputs chain, end to end, over the sample store.

This is the feature's done bar: every deterministic step a reconcile run takes with a statement the
person supplied, run in-process against the sample SQLite database the plugin ships. The one thing
missing is the AI: where the skill would ask agami a question, this test uses a stand-in statement
for what agami would have written (the seed example, or a deliberately different query), because the
point here is the grading and the comparison, not the generation.

Python's own sqlite3 module stands in for the execution tier. It runs what the tier would run and
writes the same CSV shape the tier writes, including the zero-byte file a failed probe leaves behind.

The fixtures are the ones the brief names: a join on the wrong key, a miscased value, a required
filter left out, a correct statement the AI gets wrong, a clean count, and three bare questions.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
SAMPLE = REPO_ROOT / "plugins" / "agami" / "samples" / "store"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SAMPLE))

import build_sample  # noqa: E402
import parse_reconcile_grades  # noqa: E402
import reconcile  # noqa: E402
import render_reconcile_grades  # noqa: E402
from semantic_model import cli  # noqa: E402

AREA = "agami-example"


# --- the stand-in execution tier ------------------------------------------------------


def _run_sql(db: Path, sql: str) -> tuple[str, str | None]:
    """The statement's result as CSV text with a header, or an empty string and the error text.
    An empty file is exactly what the tier leaves when it refuses or fails a statement."""
    con = sqlite3.connect(str(db))
    try:
        cur = con.execute(sql)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description or []]
    except sqlite3.Error as exc:
        return "", str(exc)
    finally:
        con.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for row in rows:
        w.writerow(["" if v is None else v for v in row])
    return buf.getvalue(), None


def _classify(error: str) -> str:
    low = error.lower()
    if "no such table" in low:
        return "table_not_found"
    if "no such column" in low:
        return "column_not_found"
    return "syntax"


def _sm(*args: str) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(list(args))
    assert rc == 0, buf.getvalue()
    return json.loads(buf.getvalue())


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    base = tmp_path_factory.mktemp("agami-e2e")
    db = base / "store.db"
    build_sample.build(db, prefer_cli=True)
    root = base / "agami-example"
    shutil.copytree(SAMPLE / "model", root)
    # `snapshot` prints the hash it stamped, not JSON, so it is not read through `_sm`.
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main(["snapshot", str(root)]) == 0
    run = base / "reconcile-run"
    (run / "rows").mkdir(parents=True)
    return {"db": db, "root": root, "run": run, "profile_hash": _tree_hash(root)}


# --- the chain, as the skill's Phase 1.5 walks it ----------------------------------------


def grade_statement(store: dict, n: int, sql: str) -> dict:
    """Phase 1.5 in code: run, receipt, probes, judge, ledger. Returns the ledger."""
    db, root = store["db"], store["root"]
    row_dir = store["run"] / "rows" / str(n)
    row_dir.mkdir(parents=True, exist_ok=True)
    (row_dir / "statement.sql").write_text(sql)

    # 1.5a/b: the zero-row check, then the statement itself.
    _probe, error = _run_sql(db, f"SELECT 1 FROM ({sql}) AS _agami_check WHERE 1=0")
    if error:
        (row_dir / "run.json").write_text(json.dumps(
            {"status": "failed", "rule": None, "kind": _classify(error), "detail": "d"}))
        return reconcile.ledger(row_dir)
    result, error = _run_sql(db, sql)
    (row_dir / "statement.csv").write_text(result)
    (row_dir / "run.json").write_text(json.dumps({"status": "ok", "rule": None, "kind": None, "detail": None}))

    (row_dir / "statement-prepare.json").write_text(json.dumps(
        _sm("prepare", str(root), "--area", AREA, "--sql-file", str(row_dir / "statement.sql"))))
    (row_dir / "statement-receipt.json").write_text(json.dumps(
        _sm("receipt", str(root), "--sql-file", str(row_dir / "statement.sql"))))

    # 1.5d: probes, each through the stand-in tier to the CSV the ledger reads.
    joins = _sm("join-probes", str(root), "--sql-file", str(row_dir / "statement.sql"))
    (row_dir / "join-probes.json").write_text(json.dumps(joins))
    for join in joins["joins"]:
        for i, probe in enumerate(join["probes"]["overlap"]):
            text, _err = _run_sql(db, probe["sql"])
            (row_dir / f"{join['id']}.overlap.{i}.csv").write_text(text)
    for key, probe_sql in joins["cardinality"].items():
        if probe_sql:
            text, _err = _run_sql(db, probe_sql)
            (row_dir / f"cardinality.{key}.csv").write_text(text)
    for join in joins["joins"]:
        probe = join.get("dropped_rows_probe")
        if probe:
            text, _err = _run_sql(db, probe["sql"])
            (row_dir / f"{join['id']}.dropped_rows.csv").write_text(text)
    plan = _sm("filter-values", "plan", str(root), "--sql-file", str(row_dir / "statement.sql"))
    (row_dir / "filter-values.plan.json").write_text(json.dumps(plan))
    for key, column in plan["columns"].items():
        if column["distinct"]:
            text, _err = _run_sql(db, column["distinct"])
            (row_dir / f"{key}.distinct.csv").write_text(text)
    for lit in plan["literals"]:
        exists = lit["probes"].get("exists")
        if not exists:
            continue
        text, _err = _run_sql(db, exists)
        (row_dir / f"{lit['id']}.exists.csv").write_text(text)
        n_rows = list(csv.DictReader(io.StringIO(text)))
        if lit["probes"].get("exists_folded") and n_rows and float(n_rows[0]["n"]) == 0:
            text, _err = _run_sql(db, lit["probes"]["exists_folded"])
            (row_dir / f"{lit['id']}.exists_folded.csv").write_text(text)
    (row_dir / "filter-values.judge.json").write_text(json.dumps(_sm(
        "filter-values", "judge", str(root), "--plan", str(row_dir / "filter-values.plan.json"),
        "--results", str(row_dir))))
    return reconcile.ledger(row_dir)


def scalar(csv_text: str) -> float | None:
    rows = list(csv.reader(io.StringIO(csv_text)))
    if len(rows) != 2 or len(rows[1]) != 1:
        return None
    return reconcile.parse_value(rows[1][0])


def ask_agami(store: dict, n: int, sql: str) -> float | None:
    """The stand-in for Phase 2b: what agami would have run, executed the same way."""
    row_dir = store["run"] / "rows" / str(n)
    row_dir.mkdir(parents=True, exist_ok=True)
    (row_dir / "agami.sql").write_text(sql)
    text, _err = _run_sql(store["db"], sql)
    (row_dir / "actual.csv").write_text(text)
    return scalar(text)


def compare(store: dict, n: int, expected: float | None, actual: float | None,
            tolerance: float = 0.01) -> tuple[dict, dict]:
    row_dir = store["run"] / "rows" / str(n)
    diff = reconcile.diff(expected, actual, tolerance=tolerance)
    claims = _sm("claims", str(store["root"]), "--sql-file", str(row_dir / "agami.sql"),
                 "--against-sql-file", str(row_dir / "statement.sql"))
    (row_dir / "claims.json").write_text(json.dumps(claims))
    ledger = reconcile.ledger(row_dir, with_claims=True)
    return diff, ledger


def record(store: dict, n: int, question: str, statement: str | None, expected, diff: dict | None,
           ledger: dict | None, **extra) -> dict:
    status = reconcile.row_status(diff["match"] if diff else None,
                                  ledger["verdict"] if ledger else None)
    rec = {"row": n, "label": question, "question": question, "statement": statement,
           "expected": expected, "status": status, "ledger_verdict": ledger["verdict"] if ledger else None,
           "provenance": {"shape": "b" if statement else "a", "graded": None}, **extra}
    with (store["run"] / "rows.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def _parts(ledger: dict) -> dict[str, dict]:
    return {row["part"]: row for row in ledger["rows"]}


# --- the fixtures -----------------------------------------------------------------------


def test_1_a_join_on_the_wrong_key_is_the_persons_defect_and_the_expected_value_is_doubtful(store):
    sql = "SELECT SUM(o.total_amount) AS paid_revenue FROM payments pay JOIN orders o ON o.id = pay.id"
    ledger = grade_statement(store, 1, sql)
    parts = _parts(ledger)
    join = parts["join:orders-payments"]
    assert join["verdict"] == "query_defect", join
    assert join["evidence"]["declared_pairs"] == [[["orders", "id"], ["payments", "order_id"]]]
    expected = scalar((store["run"] / "rows" / "1" / "statement.csv").read_text())
    actual = ask_agami(store, 1, "SELECT SUM(o.total_amount) AS paid_revenue FROM payments pay "
                                 "JOIN orders o ON o.id = pay.order_id WHERE o.status != 'cancelled'")
    diff, ledger = compare(store, 1, expected, actual)
    rec = record(store, 1, "What is paid revenue?", sql, expected, diff, ledger)
    # On this data the wrong key gives a total 0.28% away from the right one, inside the default 1%
    # band, so the numbers "agree" while the join is the person's defect: that is the lucky match
    # `match_unverified` exists for, and it never reaches the keep-offer.
    assert rec["status"] == "match_unverified", rec["status"]


def test_1b_the_same_wrong_key_makes_the_expected_value_doubtful_when_the_numbers_differ(store):
    """The brief's case 1 outcome: the totals differ (a 0.1% band sees the 0.28% gap) and the
    person's own defect makes the expected value doubtful rather than a mismatch charged to agami."""
    sql = "SELECT SUM(o.total_amount) AS paid_revenue FROM payments pay JOIN orders o ON o.id = pay.id"
    ledger = grade_statement(store, 9, sql)
    assert _parts(ledger)["join:orders-payments"]["verdict"] == "query_defect"
    expected = scalar((store["run"] / "rows" / "9" / "statement.csv").read_text())
    actual = ask_agami(store, 9, "SELECT SUM(o.total_amount) AS paid_revenue FROM payments pay "
                                 "JOIN orders o ON o.id = pay.order_id WHERE o.status != 'cancelled'")
    diff, ledger = compare(store, 9, expected, actual, tolerance=0.001)
    assert diff["match"] is False
    rec = record(store, 9, "What is paid revenue, tightly?", sql, expected, diff, ledger)
    assert rec["status"] == "expected_doubtful"


def test_2_a_miscased_value_is_the_persons_defect_with_the_near_miss_named(store):
    sql = "SELECT COUNT(*) AS delivered FROM orders o WHERE o.status = 'Delivered' AND o.status != 'cancelled'"
    ledger = grade_statement(store, 2, sql)
    parts = _parts(ledger)
    lit = parts["literal:orders.status=Delivered"]
    assert lit["verdict"] == "query_defect" and lit["evidence"]["near_miss"] == "delivered"
    assert lit["evidence"]["rows_with_value"] == 0
    # The list is populated, so the semantic model answered; the probe confirmed that no row holds it.
    assert lit["evidence"]["tier"] == "choice_field"
    expected = scalar((store["run"] / "rows" / "2" / "statement.csv").read_text())
    actual = ask_agami(store, 2, "SELECT COUNT(*) AS delivered FROM orders o WHERE o.status = 'delivered' "
                                 "AND o.status != 'cancelled'")
    diff, ledger = compare(store, 2, expected, actual)
    rec = record(store, 2, "How many orders were delivered?", sql, expected, diff, ledger)
    assert expected == 0 and actual and actual > 0 and rec["status"] == "expected_doubtful"


def test_3_a_required_filter_left_out_is_a_gap_of_kind_filter(store):
    sql = "SELECT ROUND(SUM(total_amount), 2) AS revenue FROM orders"
    ledger = grade_statement(store, 3, sql)
    parts = _parts(ledger)
    (flt,) = [row for part, row in parts.items() if part.startswith("default_filter:orders:")]
    assert flt["verdict"] == "model_gap" and flt["kind"] == "filter"
    assert not any(row["verdict"] == "query_defect" for row in ledger["rows"]), ledger["rows"]
    expected = scalar((store["run"] / "rows" / "3" / "statement.csv").read_text())
    actual = ask_agami(store, 3, "SELECT ROUND(SUM(o.total_amount), 2) AS revenue FROM orders o "
                                 "WHERE o.status != 'cancelled'")
    diff, ledger = compare(store, 3, expected, actual)
    rec = record(store, 3, "What is total revenue?", sql, expected, diff, ledger)
    assert diff["match"] is False and rec["status"] == "mismatch"


def test_4_a_correct_statement_the_ai_gets_wrong_names_the_filter_that_differs(store):
    sql = "SELECT SUM(o.total_amount) AS revenue FROM orders o WHERE o.status != 'cancelled'"
    ledger = grade_statement(store, 4, sql)
    parts = _parts(ledger)
    (flt,) = [row for part, row in parts.items() if part.startswith("default_filter:orders:")]
    assert flt["verdict"] == "confirmed", flt
    # Every part holds: the brief's "correct statement", and the only way to an `example` finding.
    assert ledger["verdict"] == "confirmed", [r for r in ledger["rows"] if r["verdict"] != "confirmed"]
    expected = scalar((store["run"] / "rows" / "4" / "statement.csv").read_text())
    # The seed example for this question omits the filter: that is exactly what agami would write.
    actual = ask_agami(store, 4, "SELECT SUM(total_amount) AS revenue FROM orders")
    diff, ledger = compare(store, 4, expected, actual)
    claims = json.loads((store["run"] / "rows" / "4" / "claims.json").read_text())
    by_name = {c["name"]: c["status"] for c in claims["claims"]}
    assert by_name["filter_predicates"] == "differs" and by_name["tables"] == "agrees"
    rec = record(store, 4, "What is our total revenue?", sql, expected, diff, ledger,
                 claims=claims)
    assert rec["status"] == "mismatch"


def test_5_a_clean_count_matches_and_may_be_kept(store):
    sql = "SELECT COUNT(*) AS order_count FROM orders o WHERE o.status != 'cancelled'"
    ledger = grade_statement(store, 5, sql)
    assert ledger["verdict"] == "confirmed", ledger["rows"]
    expected = scalar((store["run"] / "rows" / "5" / "statement.csv").read_text())
    actual = ask_agami(store, 5, sql)
    diff, ledger = compare(store, 5, expected, actual)
    rec = record(store, 5, "How many orders have been placed?", sql, expected, diff, ledger)
    assert rec["status"] == "match"


def test_6_three_bare_questions_are_graded_on_one_page_and_the_grades_re_enter(store, tmp_path):
    answers = {
        6: ("How many orders come from the web channel?",
            "SELECT COUNT(*) AS orders FROM orders o WHERE o.channel = 'web' AND o.status != 'cancelled'"),
        7: ("How many customers do we have?", "SELECT COUNT(*) AS customers FROM customers"),
        8: ("What is the refund rate?", "SELECT 0.5 AS refund_rate"),
    }
    items = []
    for n, (question, agami_sql) in answers.items():
        actual = ask_agami(store, n, agami_sql)
        items.append({"row": n, "question": question, "answer": str(actual), "signals": []})
    items_file = tmp_path / "items.json"
    items_file.write_text(json.dumps(items))
    out = tmp_path / "grade.html"
    assert render_reconcile_grades.main(["--title", "Grade agami's answers · demo", "--profile", "demo",
                                          "--run", "e2e", "--items-file", str(items_file), "--out", str(out)]) == 0
    assert "How many customers do we have?" in out.read_text()

    block = ("profile: demo\nreconcile-run: e2e\ngrades:\n"
             + json.dumps([{"row": 6, "grade": "right"},
                           {"row": 7, "grade": "unsure"},
                           {"row": 8, "grade": "wrong", "words": "the refund rate should divide refunds by payments"}])
             + "\ndone\n")
    data, anomalies, needs = parse_reconcile_grades.parse(block)
    assert needs is None and anomalies == []
    grades = {g["row"]: g for g in data["grades"]}

    # right: the answer becomes the expected value and the row matches by construction.
    actual = scalar((store["run"] / "rows" / "6" / "actual.csv").read_text())
    rec = record(store, 6, answers[6][0], None, actual, reconcile.diff(actual, actual), None)
    rec["provenance"]["graded"] = "right"
    assert rec["status"] == "match"
    # unsure: nothing happens.
    assert grades[7]["grade"] == "unsure"
    # wrong with words: a finding carrying the words, and no statement to grade.
    record(store, 8, answers[8][0], None, None, None, None, words=grades[8]["words"],
           provenance={"shape": "a", "graded": "wrong"})


def test_7_the_findings_name_the_gaps_and_list_the_defects_apart(store):
    out = reconcile.findings(store["run"])
    keys = {f["key"] for f in out["findings"]}
    # One declared filter, omitted by row 3's statement, is one finding whatever alias spelt it.
    assert len([k for k in keys if k.startswith("filter:orders:")]) == 1, keys
    # Row 4's statement held on every part and agami answered differently: a worked example.
    assert "example:what is our total revenue?" in keys, keys
    assert "description:what is the refund rate?" in keys
    words = next(f for f in out["findings"] if f["kind"] == "description")
    assert words["words"] == "the refund rate should divide refunds by payments"
    defects = {(d["row"], d["part"]) for d in out["query_defects"]}
    assert (1, "join:orders-payments") in defects
    assert (2, "literal:orders.status=Delivered") in defects
    # Nothing about the person's defects became a finding about the semantic model.
    assert not any("payments" in k for k in keys)
    for name in ("findings.json", "query_defects.json", "ledger.json"):
        assert (store["run"] / name).exists()


def test_10_a_count_through_declared_many_to_one_joins_is_confirmed_and_the_drops_are_noted(store):
    """`COUNT(*)` names no column, so the pre-flight cannot bind it. Both joins bring the one side in
    (order_items -> orders, order_items -> products), so nothing multiplies the count; the ledger
    confirms it, states what each inner join dropped, and the row can reach the keep-offer."""
    sql = ("SELECT COUNT(*) AS n FROM order_items oi JOIN orders o ON o.id = oi.order_id "
           "JOIN products p ON p.id = oi.product_id WHERE o.status != 'cancelled'")
    ledger = grade_statement(store, 10, sql)
    parts = _parts(ledger)
    fan = parts["fan_out:COUNT(*)"]
    assert fan["verdict"] == "confirmed" and "one row at most" in fan["note"], fan
    noted = {p: row for p, row in parts.items() if p.startswith("dropped_rows:")}
    assert set(noted) == {"dropped_rows:order_items-orders", "dropped_rows:order_items-products"}, noted
    for row in noted.values():
        assert row["verdict"] == "noted" and row["evidence"]["total"] > 0 and row["evidence"]["dropped"] >= 0
    assert ledger["verdict"] == "confirmed", [r for r in ledger["rows"] if r["verdict"] not in ("confirmed", "noted")]
    expected = scalar((store["run"] / "rows" / "10" / "statement.csv").read_text())
    actual = ask_agami(store, 10, sql)
    diff, ledger = compare(store, 10, expected, actual)
    rec = record(store, 10, "How many items were ordered?", sql, expected, diff, ledger)
    assert rec["status"] == "match"


def test_11_a_join_that_brings_the_many_side_in_leaves_the_count_open_and_says_why(store):
    sql = ("SELECT COUNT(*) AS n FROM orders o JOIN order_items oi ON oi.order_id = o.id "
           "WHERE o.status != 'cancelled'")
    parts = _parts(grade_statement(store, 11, sql))
    assert parts["join:order_items-orders"]["evidence"]["one_row_on_right"] is False
    fan = parts["fan_out:COUNT(*)"]
    assert fan["verdict"] == "unresolved"
    assert "could not bind this aggregate to one table: the aggregate names no column" in fan["note"], fan


def test_12_a_filter_on_a_column_with_no_declared_values_is_a_gap_said_once(store):
    """`categories.slug` holds three values and the semantic model lists none of them. The value
    itself checks out against the warehouse and says so; the column's missing list is the finding."""
    sql = ("SELECT COUNT(*) AS n FROM products p JOIN categories c ON c.id = p.category_id "
           "WHERE c.slug = 'apparel'")
    ledger = grade_statement(store, 12, sql)
    parts = _parts(ledger)
    gap = parts["values_declared:categories.slug"]
    assert gap["verdict"] == "model_gap" and gap["kind"] == "description", gap
    assert re.search(r"holds \d+ distinct values and the semantic model lists none", gap["note"]), gap["note"]
    (lit,) = [row for p, row in parts.items() if p.startswith("literal:categories.slug")]
    assert lit["verdict"] == "confirmed" and "graded against the warehouse" in lit["note"]
    expected = scalar((store["run"] / "rows" / "12" / "statement.csv").read_text())
    actual = ask_agami(store, 12, sql)
    diff, ledger = compare(store, 12, expected, actual)
    rec = record(store, 12, "How many apparel products are there?", sql, expected, diff, ledger)
    # The gap is the semantic model's, not the statement's, and a gap is not a confirmed part.
    assert rec["status"] == "match_unverified"
    # It reaches the findings file under the key a stale list would use: one column, one gap.
    keys = {f["key"] for f in reconcile.findings(store["run"])["findings"]}
    assert "description:categories.slug" in keys, keys


def test_13_a_metric_matched_by_shape_on_another_table_is_open(store):
    """`SUM(pay.amount)` over payments equals the `total refunds` metric's `SUM(refunds.amount)` once
    the qualifiers are stripped for the match. The statement never reads refunds, so the match is
    by shape alone and the part stays open rather than confirming a metric it did not compute."""
    sql = "SELECT SUM(pay.amount) AS total_refunds FROM payments pay"
    parts = _parts(grade_statement(store, 13, sql))
    metric = parts["metric:total_refunds"]
    assert metric["verdict"] == "unresolved", metric
    assert "defined on refunds, which this statement does not read" in metric["note"]


def test_8_the_profile_was_never_written_to(store):
    assert _tree_hash(store["root"]) == store["profile_hash"]
