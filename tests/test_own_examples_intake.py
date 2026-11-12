"""`reconcile.py intake` reads the shapes people bring as their own examples (#645).

A folder of `.sql` files, a `.sql` file whose statements are named by the comment above them, a sheet
of question and SQL, and three dashboard exports: Tableau (`.twb`, `.twbx`), Power BI (`.pbit`,
`.bim`) and LookML (`.lkml`). Every one becomes the evidence row reconcile already reads, so each
statement is checked part by part like any other, and none is trusted as read.

The export file is untrusted input: XML that declares a DTD or an entity is refused, a zip member is
read only up to a limit, and connection details in the file never reach the output. Synthetic
fixtures under `tests/fixtures/own_examples/`, neutral names throughout.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import dashboard_exports  # noqa: E402
import reconcile  # noqa: E402
from reconcile import intake  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "own_examples"
TWB = FIXTURES / "acme_orders.twb"


def _by_label(result: dict) -> dict[str, dict]:
    return {r["label"]: r for r in result["rows"] if r["label"]}


def _one_line(sql: str | None) -> str | None:
    return " ".join(sql.split()) if sql else sql


# --- .sql files and folders ------------------------------------------------------------------


def test_a_comment_above_a_statement_names_it_and_is_not_part_of_it():
    # A live run skipped a .sql file whose statement had a comment above it: the usual way to write one.
    d = intake([FIXTURES / "acme_queries.sql"])
    rows = d["rows"]
    assert [r["label"] for r in rows] == [
        "Orders placed, not counting cancelled ones",
        "Revenue from delivered orders; a ';' inside a string must not split it",
    ]
    assert rows[0]["statement"] == "SELECT COUNT(*) AS orders FROM orders WHERE status != 'cancelled'"
    assert rows[1]["statement"].endswith("channel <> 'x;y'")
    assert [(s["text"], s["reason"]) for s in d["skipped"]] == [
        ("DELETE FROM orders", "not a SELECT or WITH statement")]


def test_a_comment_that_asks_a_question_is_the_rows_question(tmp_path):
    f = tmp_path / "q.sql"
    f.write_text("-- How many paid orders are there?\nSELECT COUNT(*) FROM orders WHERE status = 'paid';\n"
                 "-- Paid orders\nSELECT COUNT(*) FROM orders WHERE status = 'paid'")
    first, second = intake([f])["rows"]
    assert (first["question"], first["label"]) == ("How many paid orders are there?", None)
    assert (second["question"], second["label"]) == (None, "Paid orders")


def test_a_semicolon_in_a_comment_or_a_doubled_quote_does_not_split(tmp_path):
    f = tmp_path / "q.sql"
    f.write_text("-- one; still the comment\nSELECT 'it''s; fine' AS x;\nSELECT 2 /* a ; b */ AS y")
    d = intake([f])
    assert [r["statement"] for r in d["rows"]] == ["SELECT 'it''s; fine' AS x", "SELECT 2 /* a ; b */ AS y"]
    assert d["rows"][0]["label"] == "one; still the comment"


def test_a_folder_reads_every_file_it_can_and_names_what_it_cannot(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.sql").write_text("SELECT COUNT(*) FROM orders")
    (tmp_path / "sub" / "b.sql").write_text("-- Paid orders\nSELECT COUNT(*) FROM orders WHERE status = 'paid'")
    (tmp_path / "README.md").write_text("How these queries were written\nSee the team wiki")
    (tmp_path / ".hidden.sql").write_text("SELECT 1")
    (tmp_path / "report.pbix").write_bytes(b"\x00binary")
    d = intake([tmp_path])
    assert [r["provenance"]["file"] for r in d["rows"]] == ["a.sql", "b.sql"]
    assert d["rows"][1]["label"] == "Paid orders"
    reasons = {s["file"]: s["reason"] for s in d["skipped"]}
    assert ".pbit" in reasons["report.pbix"]
    assert "README.md" not in reasons and ".hidden.sql" not in reasons


def test_the_fixture_folder_reads_as_one_intake():
    d = intake([FIXTURES], source="your own examples")
    files = {r["provenance"]["file"] for r in d["rows"]}
    assert files == {"acme_orders.twb", "acme_orders.view.lkml", "acme_queries.sql", "acme_questions.csv"}
    assert all(r["provenance"]["source"] == "your own examples" for r in d["rows"])
    assert [r["row"] for r in d["rows"]] == list(range(1, len(d["rows"]) + 1))


def test_a_pbix_on_its_own_says_what_to_send_instead(tmp_path, capsys):
    f = tmp_path / "sales.pbix"
    f.write_bytes(b"\x00")
    assert reconcile.main(["intake", "--file", str(f)]) == 4
    out = json.loads(capsys.readouterr().out)
    assert "template" in out["skipped"][0]["reason"] and "screenshot" in out["skipped"][0]["reason"]


def _xlsx(path: Path, rows: list[list[str]]) -> Path:
    """A one-sheet workbook of inline-string cells."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    body = "".join(
        f'<row r="{r}">' + "".join(
            f'<c r="{chr(65 + i)}{r}" t="inlineStr"><is><t>{escape(v)}</t></is></c>' for i, v in enumerate(cells))
        + "</row>" for r, cells in enumerate(rows, 1))
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{main}"><sheetData>{body}</sheetData></worksheet>')
        book.writestr("xl/workbook.xml", f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
                                         f'<sheet name="Examples" sheetId="1" r:id="rId1"/></sheets></workbook>')
        book.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{pkg}"><Relationship Id="rId1" '
                                                    f'Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        book.writestr("_rels/.rels", f'<Relationships xmlns="{pkg}"><Relationship Id="rId1" '
                                     f'Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    return path


def test_a_sheet_of_questions_and_sql_reads_like_the_csv(tmp_path):
    book = _xlsx(tmp_path / "examples.xlsx", [
        ["question", "sql"],
        ["How many orders were placed on the web?", "SELECT COUNT(*) FROM orders WHERE channel = 'web'"],
    ])
    d = intake([book])
    (row,) = d["rows"]
    assert row["question"] == "How many orders were placed on the web?"
    assert row["statement"] == "SELECT COUNT(*) FROM orders WHERE channel = 'web'"
    assert row["provenance"]["file"] == "examples.xlsx (Examples)" and row["provenance"]["line"] == 2


# --- Tableau ----------------------------------------------------------------------------------


def test_a_worksheet_on_custom_sql_composes_its_fields_and_filter():
    rows = _by_label(intake([TWB]))
    revenue = rows["Revenue by country"]
    assert _one_line(revenue["statement"]) == _one_line("""
        SELECT country, SUM(amount) AS sum_amount
        FROM (
        SELECT o.id AS order_id, o.status, o.channel, o.total_amount AS amount, o.placed_at AS order_date, c.country
        FROM orders o JOIN customers c ON c.id = o.customer_id
        ) t
        WHERE status IN ('delivered')
        GROUP BY country""")
    export = revenue["provenance"]["export"]
    assert export["kind"] == "Tableau worksheet" and export["base"] == "custom SQL"
    assert export["filters"] == ["status is one of \"delivered\""]
    assert export["fields"] == ["country", "total of amount"] and export["untranslated"] == []
    assert revenue["provenance"]["shape"] == "b"


def test_an_all_but_filter_and_a_text_tile_compose_too():
    row = _by_label(intake([TWB]))["Orders not cancelled"]
    assert "SELECT COUNT(order_id) AS cnt_order_id" in row["statement"]
    assert row["statement"].endswith("WHERE status NOT IN ('cancelled')")


def test_a_table_data_source_maps_a_renamed_field_to_its_column():
    row = _by_label(intake([TWB]))["Orders by channel"]
    assert _one_line(row["statement"]) == "SELECT channel, COUNT(id) AS cnt_id FROM orders GROUP BY channel"


def test_a_calculated_field_or_a_date_part_is_never_translated_and_says_so():
    rows = _by_label(intake([TWB]))
    net, by_year = rows["Net revenue"], rows["Orders by year"]
    assert net["statement"] is None and by_year["statement"] is None
    assert net["provenance"]["export"]["untranslated"] == ["the calculated field 'Net revenue' is a Tableau formula"]
    assert net["provenance"]["export"]["calculations"] == [
        {"name": "Net revenue", "formula": "SUM(IF [status] != 'refunded' THEN [amount] END)"}]
    assert "date part" in by_year["provenance"]["export"]["untranslated"][0]


def test_calculated_fields_are_listed_as_definitions_with_the_sheets_that_use_them():
    d = intake([TWB])
    defs = {x["name"]: x for x in d["definitions"]}
    assert defs["Net revenue"]["used_by"] == ["Net revenue"]
    assert defs["Unused ratio"]["used_by"] == [] and defs["Unused ratio"]["language"] == "Tableau calculation"
    assert all(x["file"] == "acme_orders.twb" for x in d["definitions"])


def test_a_sheet_with_no_fields_is_skipped_with_a_reason():
    d = intake([TWB])
    assert {"line": 6, "reason": "worksheet 'Blank sheet' shows no fields", "file": "acme_orders.twb"} in d["skipped"]


def test_connection_details_in_the_workbook_never_reach_the_output():
    printed = json.dumps(intake([TWB]))
    assert "your-cluster.example.com" not in printed and "you@example.com" not in printed


def test_a_packaged_workbook_reads_its_twb_and_ignores_the_extract(tmp_path):
    twbx = tmp_path / "acme.twbx"
    with zipfile.ZipFile(twbx, "w") as z:
        z.writestr("Data/Extracts/acme.hyper", b"\x00" * 1024)
        z.write(TWB, "acme_orders.twb")
    assert _by_label(intake([twbx]))["Orders by channel"]["statement"] == _by_label(intake([TWB]))["Orders by channel"]["statement"]


def test_a_data_source_filter_stops_composition(tmp_path):
    text = TWB.read_text().replace(
        "<column caption='Net revenue'",
        "<filter class='categorical' column='[federated.acme1].[none:channel:nk]'>"
        "<groupfilter function='member' member='&quot;web&quot;' /></filter><column caption='Net revenue'")
    f = tmp_path / "f.twb"
    f.write_text(text)
    row = _by_label(intake([f]))["Revenue by country"]
    assert row["statement"] is None
    assert row["provenance"]["export"]["untranslated"] == ["the data source has filters of its own"]


@pytest.mark.parametrize("prolog", [
    "<!DOCTYPE workbook [<!ENTITY a 'aaaaaaaaaa'><!ENTITY b '&a;&a;&a;&a;&a;&a;&a;&a;'>]>",
    "<!DOCTYPE workbook SYSTEM 'http://example.com/evil.dtd'>",
])
def test_a_workbook_declaring_a_dtd_or_an_entity_is_refused_before_parsing(tmp_path, capsys, prolog):
    f = tmp_path / "evil.twb"
    f.write_text(f"<?xml version='1.0'?>{prolog}<workbook><worksheets/></workbook>")
    assert reconcile.main(["intake", "--file", str(f)]) == 2
    assert "will not parse" in capsys.readouterr().err


def test_an_oversized_member_is_refused(tmp_path, monkeypatch, capsys):
    twbx = tmp_path / "big.twbx"
    with zipfile.ZipFile(twbx, "w") as z:
        z.write(TWB, "acme_orders.twb")
    monkeypatch.setattr(dashboard_exports, "_MAX_DOC_BYTES", 100)
    assert reconcile.main(["intake", "--file", str(twbx)]) == 2
    assert "too large" in capsys.readouterr().err


def test_a_file_that_is_not_a_workbook_is_refused_in_words(tmp_path, capsys):
    f = tmp_path / "x.twb"
    f.write_text("<notes>hello</notes>")
    assert reconcile.main(["intake", "--file", str(f)]) == 2
    assert "not a Tableau workbook" in capsys.readouterr().err


# --- Power BI ---------------------------------------------------------------------------------


def _model(query_line: str) -> dict:
    return {"name": "acme", "model": {"tables": [
        {"name": "orders",
         "partitions": [{"source": {"type": "m", "expression": [
             "let",
             f'    Source = Sql.Database("your-cluster.example.com", "acme", [Query={query_line}])',
             "in", "    Source"]}}],
         "measures": [{"name": "Delivered revenue",
                       "expression": "CALCULATE(SUM(orders[total_amount]), orders[status] = \"delivered\")"}],
         "columns": [{"name": "is_web", "type": "calculated", "expression": "orders[channel] = \"web\""}]},
        {"name": "LocalDateTable_1", "partitions": [{"source": {"type": "calculated", "expression": "Calendar()"}}]},
    ]}}


_M_QUERY = '"SELECT id, status, total_amount#(lf)FROM orders#(lf)WHERE status <> ""cancelled"""'


def test_a_power_bi_template_reads_each_tables_native_sql_and_lists_measures(tmp_path):
    pbit = tmp_path / "acme.pbit"
    with zipfile.ZipFile(pbit, "w") as z:
        z.writestr("DataModelSchema", json.dumps(_model(_M_QUERY)).encode("utf-16-le"))
        z.writestr("Report/Layout", b"{}")
    d = intake([pbit])
    (row,) = d["rows"]
    assert row["label"] == "orders"
    assert row["statement"] == "SELECT id, status, total_amount\nFROM orders\nWHERE status <> \"cancelled\""
    assert row["provenance"]["export"]["kind"] == "Power BI table"
    assert [(x["name"], x["language"]) for x in d["definitions"]] == [
        ("Delivered revenue", "DAX"), ("is_web", "DAX (calculated column)")]
    assert "1 table(s) load without a SQL query" in d["skipped"][0]["reason"]
    assert "your-cluster.example.com" not in json.dumps(d)


def test_a_bim_model_file_reads_the_same(tmp_path):
    bim = tmp_path / "model.bim"
    bim.write_text(json.dumps(_model(_M_QUERY)), encoding="utf-8")
    assert intake([bim])["rows"][0]["statement"].startswith("SELECT id, status")


def test_a_template_without_a_model_is_refused_in_words(tmp_path, capsys):
    pbit = tmp_path / "empty.pbit"
    with zipfile.ZipFile(pbit, "w") as z:
        z.writestr("Report/Layout", b"{}")
    assert reconcile.main(["intake", "--file", str(pbit)]) == 2
    assert "holds nothing this reader can use" in capsys.readouterr().err


# --- LookML -----------------------------------------------------------------------------------


def test_lookml_measures_with_a_plain_column_and_value_compose():
    rows = _by_label(intake([FIXTURES / "acme_orders.view.lkml"]))
    assert _one_line(rows["Delivered revenue"]["statement"]) == \
        "SELECT SUM(total_amount) AS delivered_revenue FROM orders WHERE status = 'delivered'"
    assert _one_line(rows["order count"]["statement"]) == "SELECT COUNT(*) AS order_count FROM orders"
    assert _one_line(rows["big order count"]["statement"]) == _one_line(
        "SELECT COUNT(DISTINCT id) AS big_order_count FROM ( SELECT id, total_amount FROM orders WHERE total_amount > 100 ) t")
    assert rows["big orders (derived table)"]["statement"].startswith("SELECT id, total_amount FROM orders")


def test_lookml_measures_that_need_lookers_own_grammar_are_definitions_only():
    d = intake([FIXTURES / "acme_orders.view.lkml"])
    labels = {r["label"] for r in d["rows"]}
    assert "revenue per order" not in labels and "open orders" not in labels
    defs = {x["name"]: x["formula"] for x in d["definitions"]}
    assert defs["open orders"] == "type: count; filters: status: -cancelled,-delivered"
    assert "explore 'orders': its joins are not read here" in [s["reason"] for s in d["skipped"]]


def test_broken_lookml_is_refused_in_words(tmp_path, capsys):
    f = tmp_path / "x.view.lkml"
    f.write_text('view: x { dimension: y { sql: ${TABLE}.y ;; } } :: "')
    assert reconcile.main(["intake", "--file", str(f)]) == 2
    assert "not readable LookML" in capsys.readouterr().err
