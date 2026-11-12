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
    assert (
        rows[0]["statement"] == "SELECT COUNT(*) AS orders FROM orders WHERE status != 'cancelled'"
    )
    assert rows[1]["statement"].endswith("channel <> 'x;y'")
    assert [(s["text"], s["reason"]) for s in d["skipped"]] == [
        ("DELETE FROM orders", "not a SELECT or WITH statement")
    ]


def test_a_comment_that_asks_a_question_is_the_rows_question(tmp_path):
    f = tmp_path / "q.sql"
    f.write_text(
        "-- How many paid orders are there?\nSELECT COUNT(*) FROM orders WHERE status = 'paid';\n"
        "-- Paid orders\nSELECT COUNT(*) FROM orders WHERE status = 'paid'"
    )
    first, second = intake([f])["rows"]
    assert (first["question"], first["label"]) == ("How many paid orders are there?", None)
    assert (second["question"], second["label"]) == (None, "Paid orders")


def test_a_semicolon_in_a_comment_or_a_doubled_quote_does_not_split(tmp_path):
    f = tmp_path / "q.sql"
    f.write_text("-- one; still the comment\nSELECT 'it''s; fine' AS x;\nSELECT 2 /* a ; b */ AS y")
    d = intake([f])
    assert [r["statement"] for r in d["rows"]] == [
        "SELECT 'it''s; fine' AS x",
        "SELECT 2 /* a ; b */ AS y",
    ]
    assert d["rows"][0]["label"] == "one; still the comment"


def test_a_folder_reads_every_file_it_can_and_names_what_it_cannot(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.sql").write_text("SELECT COUNT(*) FROM orders")
    (tmp_path / "sub" / "b.sql").write_text(
        "-- Paid orders\nSELECT COUNT(*) FROM orders WHERE status = 'paid'"
    )
    (tmp_path / "README.md").write_text("How these queries were written\nSee the team wiki")
    (tmp_path / ".hidden.sql").write_text("SELECT 1")
    (tmp_path / "report.pbix").write_bytes(b"\x00binary")
    d = intake([tmp_path])
    assert [r["provenance"]["file"] for r in d["rows"]] == ["a.sql", "sub/b.sql"]
    assert d["rows"][1]["label"] == "Paid orders"
    reasons = {s["file"]: s["reason"] for s in d["skipped"]}
    assert ".pbit" in reasons["report.pbix"]
    assert "README.md" not in reasons and ".hidden.sql" not in reasons


def test_the_fixture_folder_reads_as_one_intake():
    d = intake([FIXTURES], source="your own examples")
    files = {r["provenance"]["file"] for r in d["rows"]}
    assert files == {
        "acme_orders.twb",
        "acme_orders.view.lkml",
        "acme_queries.sql",
        "acme_questions.csv",
    }
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
        f'<row r="{r}">'
        + "".join(
            f'<c r="{chr(65 + i)}{r}" t="inlineStr"><is><t>{escape(v)}</t></is></c>'
            for i, v in enumerate(cells)
        )
        + "</row>"
        for r, cells in enumerate(rows, 1)
    )
    with zipfile.ZipFile(path, "w") as book:
        book.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{main}"><sheetData>{body}</sheetData></worksheet>',
        )
        book.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
            f'<sheet name="Examples" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        book.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{pkg}"><Relationship Id="rId1" '
            f'Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        book.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{pkg}"><Relationship Id="rId1" '
            f'Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
    return path


def test_a_sheet_of_questions_and_sql_reads_like_the_csv(tmp_path):
    book = _xlsx(
        tmp_path / "examples.xlsx",
        [
            ["question", "sql"],
            [
                "How many orders were placed on the web?",
                "SELECT COUNT(*) FROM orders WHERE channel = 'web'",
            ],
        ],
    )
    d = intake([book])
    (row,) = d["rows"]
    assert row["question"] == "How many orders were placed on the web?"
    assert row["statement"] == "SELECT COUNT(*) FROM orders WHERE channel = 'web'"
    assert (
        row["provenance"]["file"] == "examples.xlsx (Examples)" and row["provenance"]["line"] == 2
    )


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
    assert export["filters"] == ['status is one of "delivered"']
    assert export["fields"] == ["country", "total of amount"] and export["untranslated"] == []
    assert revenue["provenance"]["shape"] == "b"


def test_an_all_but_filter_and_a_text_tile_compose_too():
    row = _by_label(intake([TWB]))["Orders not cancelled"]
    assert "SELECT COUNT(order_id) AS cnt_order_id" in row["statement"]
    assert row["statement"].endswith("WHERE status NOT IN ('cancelled')")


def test_a_table_data_source_maps_a_renamed_field_to_its_column():
    row = _by_label(intake([TWB]))["Orders by channel"]
    assert (
        _one_line(row["statement"])
        == "SELECT channel, COUNT(id) AS cnt_id FROM orders GROUP BY channel"
    )


def test_a_calculated_field_or_a_date_part_is_never_translated_and_says_so():
    rows = _by_label(intake([TWB]))
    net, by_year = rows["Net revenue"], rows["Orders by year"]
    assert net["statement"] is None and by_year["statement"] is None
    assert net["provenance"]["export"]["untranslated"] == [
        "the calculated field 'Net revenue' is a Tableau formula"
    ]
    assert net["provenance"]["export"]["calculations"] == [
        {"name": "Net revenue", "formula": "SUM(IF [status] != 'refunded' THEN [amount] END)"}
    ]
    assert "date part" in by_year["provenance"]["export"]["untranslated"][0]


def test_calculated_fields_are_listed_as_definitions_with_the_sheets_that_use_them():
    d = intake([TWB])
    defs = {x["name"]: x for x in d["definitions"]}
    assert defs["Net revenue"]["used_by"] == ["Net revenue"]
    assert (
        defs["Unused ratio"]["used_by"] == []
        and defs["Unused ratio"]["language"] == "Tableau calculation"
    )
    assert all(x["file"] == "acme_orders.twb" for x in d["definitions"])


def test_a_sheet_with_no_fields_is_skipped_with_a_reason():
    d = intake([TWB])
    assert {
        "line": 6,
        "reason": "worksheet 'Blank sheet' shows no fields",
        "file": "acme_orders.twb",
    } in d["skipped"]


def test_connection_details_in_the_workbook_never_reach_the_output():
    printed = json.dumps(intake([TWB]))
    assert "your-cluster.example.com" not in printed and "you@example.com" not in printed


def test_a_packaged_workbook_reads_its_twb_and_ignores_the_extract(tmp_path):
    twbx = tmp_path / "acme.twbx"
    with zipfile.ZipFile(twbx, "w") as z:
        z.writestr("Data/Extracts/acme.hyper", b"\x00" * 1024)
        z.write(TWB, "acme_orders.twb")
    assert (
        _by_label(intake([twbx]))["Orders by channel"]["statement"]
        == _by_label(intake([TWB]))["Orders by channel"]["statement"]
    )


def test_a_data_source_filter_stops_composition(tmp_path):
    text = TWB.read_text().replace(
        "<column caption='Net revenue'",
        "<filter class='categorical' column='[federated.acme1].[none:channel:nk]'>"
        "<groupfilter function='member' member='&quot;web&quot;' /></filter><column caption='Net revenue'",
    )
    f = tmp_path / "f.twb"
    f.write_text(text)
    row = _by_label(intake([f]))["Revenue by country"]
    assert row["statement"] is None
    assert row["provenance"]["export"]["untranslated"] == ["the data source has filters of its own"]


@pytest.mark.parametrize(
    "prolog",
    [
        "<!DOCTYPE workbook [<!ENTITY a 'aaaaaaaaaa'><!ENTITY b '&a;&a;&a;&a;&a;&a;&a;&a;'>]>",
        "<!DOCTYPE workbook SYSTEM 'http://example.com/evil.dtd'>",
    ],
)
def test_a_workbook_declaring_a_dtd_or_an_entity_is_refused_before_parsing(
    tmp_path, capsys, prolog
):
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
    return {
        "name": "acme",
        "model": {
            "tables": [
                {
                    "name": "orders",
                    "partitions": [
                        {
                            "source": {
                                "type": "m",
                                "expression": [
                                    "let",
                                    f'    Source = Sql.Database("your-cluster.example.com", "acme", [Query={query_line}])',
                                    "in",
                                    "    Source",
                                ],
                            }
                        }
                    ],
                    "measures": [
                        {
                            "name": "Delivered revenue",
                            "expression": 'CALCULATE(SUM(orders[total_amount]), orders[status] = "delivered")',
                        }
                    ],
                    "columns": [
                        {
                            "name": "is_web",
                            "type": "calculated",
                            "expression": 'orders[channel] = "web"',
                        }
                    ],
                },
                {
                    "name": "LocalDateTable_1",
                    "partitions": [{"source": {"type": "calculated", "expression": "Calendar()"}}],
                },
            ]
        },
    }


_M_QUERY = '"SELECT id, status, total_amount#(lf)FROM orders#(lf)WHERE status <> ""cancelled"""'


def test_a_power_bi_template_reads_each_tables_native_sql_and_lists_measures(tmp_path):
    pbit = tmp_path / "acme.pbit"
    with zipfile.ZipFile(pbit, "w") as z:
        z.writestr("DataModelSchema", json.dumps(_model(_M_QUERY)).encode("utf-16-le"))
        z.writestr("Report/Layout", b"{}")
    d = intake([pbit])
    (row,) = d["rows"]
    assert row["label"] == "orders"
    assert (
        row["statement"]
        == 'SELECT id, status, total_amount\nFROM orders\nWHERE status <> "cancelled"'
    )
    assert row["provenance"]["export"]["kind"] == "Power BI table"
    assert [(x["name"], x["language"]) for x in d["definitions"]] == [
        ("Delivered revenue", "DAX"),
        ("is_web", "DAX (calculated column)"),
    ]
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
    assert (
        _one_line(rows["Delivered revenue"]["statement"])
        == "SELECT SUM(total_amount) AS delivered_revenue FROM orders WHERE status = 'delivered'"
    )
    assert (
        _one_line(rows["order count"]["statement"]) == "SELECT COUNT(*) AS order_count FROM orders"
    )
    assert _one_line(rows["big order count"]["statement"]) == _one_line(
        "SELECT COUNT(DISTINCT id) AS big_order_count FROM ( SELECT id, total_amount FROM orders WHERE total_amount > 100 ) t"
    )
    assert rows["big orders (derived table)"]["statement"].startswith(
        "SELECT id, total_amount FROM orders"
    )


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


_COUNTRY_FILTER = "<filter class='categorical' column='[federated.acme1].[none:status:nk]'>"


@pytest.mark.parametrize(
    "flt, where, untranslated",
    [
        (
            _COUNTRY_FILTER + "<groupfilter function='union'>"
            "<groupfilter function='member' member='&quot;paid&quot;' />"
            "<groupfilter function='member' member='&quot;delivered&quot;' /></groupfilter></filter>",
            "WHERE status IN ('paid', 'delivered')",
            [],
        ),
        (
            "<filter class='quantitative' column='[federated.acme1].[none:amount:qk]'>"
            "<min>10</min><max>500.5</max></filter>",
            "WHERE amount >= 10\n  AND amount <= 500.5",
            [],
        ),
        (
            "<filter class='quantitative' column='[federated.acme1].[sum:amount:qk]'><min>100</min></filter>",
            "HAVING SUM(amount) >= 100",
            [],
        ),
        (
            "<filter class='quantitative' column='[federated.acme1].[none:amount:qk]'><min>#2024-01-01#</min></filter>",
            None,
            ["the range filter on amount is not a plain number range"],
        ),
        (
            "<filter class='relative-date' column='[federated.acme1].[none:order_date:qk]' />",
            None,
            ["the relative-date filter on order date is not translated"],
        ),
        (
            _COUNTRY_FILTER + "<groupfilter function='end' count='10' /></filter>",
            None,
            ["the filter on status is not a plain list of values"],
        ),
        (
            _COUNTRY_FILTER + "<groupfilter function='member' member='%null%' /></filter>",
            None,
            ["the filter on status holds a value this reader does not translate"],
        ),
    ],
)
def test_each_kind_of_worksheet_filter(tmp_path, flt, where, untranslated):
    text = TWB.read_text().replace(
        "<filter class='categorical' column='[federated.acme1].[none:status:nk]'>\n"
        "            <groupfilter function='member' level='[none:status:nk]' member='&quot;delivered&quot;' user:ui-domain='database' />\n"
        "          </filter>",
        flt,
    )
    assert text != TWB.read_text()
    f = tmp_path / "f.twb"
    f.write_text(text)
    row = _by_label(intake([f]))["Revenue by country"]
    assert row["provenance"]["export"]["untranslated"] == untranslated
    if where is None:
        assert row["statement"] is None
    else:
        assert where in row["statement"]


# --- review fixes (#645) ------------------------------------------------------------------------


def test_a_folder_never_follows_a_link_out_of_itself(tmp_path):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "creds").write_text("host,password\nyour-cluster.example.com,not-a-real-one\n")
    folder = tmp_path / "shared"
    folder.mkdir()
    (folder / "ok.sql").write_text("SELECT 1 AS n")
    (folder / "notes.csv").symlink_to(secret / "creds")
    (folder / "inside.sql").symlink_to(folder / "ok.sql")  # a link, even to a file inside
    d = intake([folder])
    assert [r["provenance"]["file"] for r in d["rows"]] == ["ok.sql"]
    assert "not-a-real-one" not in json.dumps(d)
    reasons = {s["file"]: s["reason"] for s in d["skipped"]}
    assert "a link" in reasons["notes.csv"] and "a link" in reasons["inside.sql"]


def test_a_folder_walk_prunes_hidden_folders_and_stops_at_the_cap(tmp_path, monkeypatch):
    for d in (".git", "node_modules", ".venv"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "x.sql").write_text("SELECT 1")
    for n in range(4):
        (tmp_path / f"q{n}.sql").write_text(f"SELECT {n} AS n")
    monkeypatch.setattr(reconcile, "_MAX_FOLDER_FILES", 2)
    d = intake([tmp_path])
    assert [r["provenance"]["file"] for r in d["rows"]] == ["q0.sql", "q1.sql"]
    assert [s["file"] for s in d["skipped"]] == ["q2.sql"]  # the walk stopped at the first one past
    assert "only the first 2" in d["skipped"][0]["reason"]


def test_two_files_of_one_name_in_different_folders_stay_apart(tmp_path):
    for sub in ("sales", "finance"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "queries.sql").write_text("SELECT 1 AS n")
    d = intake([tmp_path])
    assert [r["provenance"]["file"] for r in d["rows"]] == [
        "finance/queries.sql",
        "sales/queries.sql",
    ]


def test_one_unreadable_file_in_a_folder_does_not_cost_the_rest(tmp_path, capsys):
    (tmp_path / "good.sql").write_text("SELECT 1 AS n")
    (tmp_path / "latin1.csv").write_bytes("question,sql\ncaf\xe9?,SELECT 1\n".encode("latin-1"))
    (tmp_path / "bad.view.lkml").write_text("view: x { measure: y { sql: ${TABLE}.y ")
    d = intake([tmp_path])
    assert [r["provenance"]["file"] for r in d["rows"]] == ["good.sql"]
    reasons = {s["file"]: s["reason"] for s in d["skipped"]}
    assert reasons["latin1.csv"].startswith("could not be read") and reasons[
        "bad.view.lkml"
    ].startswith("could not be read")
    # a file named on its own still says it could not be read
    assert reconcile.main(["intake", "--file", str(tmp_path / "latin1.csv")]) == 2


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "-- Sales queries, kept by the team\n-- updated monthly\n\n-- Paid orders\nSELECT 1 AS n",
            [("SELECT 1 AS n", "Paid orders")],
        ),
        (
            "SELECT 1 AS a; -- the first one\nSELECT 2 AS b",
            [("SELECT 1 AS a", None), ("SELECT 2 AS b", None)],
        ),
        (
            "SELECT 1 AS a; /* first */\n-- Second\nSELECT 2 AS b",
            [("SELECT 1 AS a", None), ("SELECT 2 AS b", "Second")],
        ),
    ],
)
def test_only_the_comment_block_directly_above_names_a_statement(text, expected):
    assert reconcile._split_sql(text) == expected


def test_lookml_is_read_in_linear_time_and_capped(tmp_path, monkeypatch, capsys):
    import time

    f = tmp_path / "big.view.lkml"
    f.write_text(
        "view: v {\n"
        + "  dimension: d { sql: ${TABLE}.d ;; }\n" * 4000
        + "  sql_table_name: t ;;\n}"
    )
    start = time.monotonic()
    dashboard_exports.read_export(f)
    assert time.monotonic() - start < 2.0
    # an unterminated sql: value over a long tail was quadratic before
    g = tmp_path / "slow.view.lkml"
    g.write_text("view: v { sql: " + "x " * 50_000 + "}")
    start = time.monotonic()
    with pytest.raises(dashboard_exports.ExportError):
        dashboard_exports.read_export(g)
    assert time.monotonic() - start < 2.0
    monkeypatch.setattr(dashboard_exports, "_MAX_LKML_BYTES", 100)
    assert reconcile.main(["intake", "--file", str(f)]) == 2
    assert "too large" in capsys.readouterr().err


def test_a_hash_inside_a_lookml_string_is_not_a_comment(tmp_path):
    f = tmp_path / "v.view.lkml"
    f.write_text(
        "view: orders {  # the orders view\n"
        "  sql_table_name: orders ;;\n"
        '  dimension: id { label: "Order #" sql: ${TABLE}.id ;; }\n'
        '  measure: revenue { type: sum value_format: "$#,##0.00" sql: ${TABLE}.total_amount ;; }\n'
        "}\n"
    )
    (row,) = intake([f])["rows"]
    assert row["statement"] == "SELECT SUM(total_amount) AS revenue\nFROM orders"


@pytest.mark.parametrize(
    "value",
    [
        "NULL",
        "NOT NULL",
        "EMPTY",
        "not paid",
        "1 to 5",
        "last 7 days",
        "yes",
        "-paid",
        "paid,shipped",
        "p%",
        "p_d",
    ],
)
def test_a_looker_filter_word_is_never_read_as_a_literal(tmp_path, value):
    f = tmp_path / "v.view.lkml"
    f.write_text(
        "view: orders {\n  sql_table_name: orders ;;\n"
        "  dimension: status { sql: ${TABLE}.status ;; }\n"
        f'  measure: n {{ type: count filters: [status: "{value}"] }}\n}}\n'
    )
    d = intake([f])
    assert d["rows"] == []
    assert d["definitions"][0]["name"] == "n"


def test_a_lookml_filter_with_one_plain_word_still_composes(tmp_path):
    f = tmp_path / "v.view.lkml"
    f.write_text(
        "view: orders {\n  sql_table_name: orders ;;\n"
        "  dimension: status { sql: ${TABLE}.status ;; }\n"
        '  measure: n { type: count filters: [status: "paid"] }\n}\n'
    )
    assert (
        intake([f])["rows"][0]["statement"]
        == "SELECT COUNT(*) AS n\nFROM orders\nWHERE status = 'paid'"
    )


def test_a_lookml_measure_name_that_is_not_an_identifier_is_not_composed():
    body = [("type", "", "count")]
    row, _definition = dashboard_exports._lookml_measure(
        "orders", "n; DROP TABLE x", body, {}, "orders"
    )
    assert row is None


def test_a_set_filter_on_a_worksheet_stops_composition(tmp_path):
    text = TWB.read_text().replace(
        "<filter class='categorical' column='[federated.acme1].[none:status:nk]'>\n"
        "            <groupfilter function='member' level='[none:status:nk]' member='&quot;delivered&quot;' user:ui-domain='database' />\n"
        "          </filter>",
        "<filter class='categorical' column='[federated.acme1].[Top Customers]'>"
        "<groupfilter function='member' member='&quot;In&quot;' /></filter>",
    )
    f = tmp_path / "f.twb"
    f.write_text(text)
    row = _by_label(intake([f]))["Revenue by country"]
    assert row["statement"] is None
    assert row["provenance"]["export"]["untranslated"] == [
        "the filter on Top Customers (a set or a group) is not translated"
    ]


@pytest.mark.parametrize(
    "swap, reason",
    [
        (
            "<rows>[federated.acme1].[none:country:nk]</rows>",
            "<rows>[federated.acme1].[none:country:nk]</rows><pages>[federated.acme1].[none:channel:nk]</pages>",
        ),
        ("[federated.acme1].[sum:amount:qk]", "[federated.acme1].[usr:amount:qk]"),
        ("[federated.acme1].[sum:amount:qk]", "[federated.acme1].[med:amount:qk]"),
    ],
)
def test_pages_a_user_aggregate_or_a_median_stop_composition(tmp_path, swap, reason):
    f = tmp_path / "f.twb"
    f.write_text(TWB.read_text().replace(swap, reason, 1))
    row = _by_label(intake([f]))["Revenue by country"]
    assert row["statement"] is None and row["provenance"]["export"]["untranslated"]


def test_an_empty_union_or_a_range_keeping_nulls_stops_composition(tmp_path):
    base = TWB.read_text()
    old = (
        "<filter class='categorical' column='[federated.acme1].[none:status:nk]'>\n"
        "            <groupfilter function='member' level='[none:status:nk]' member='&quot;delivered&quot;' user:ui-domain='database' />\n"
        "          </filter>"
    )
    for new in (
        "<filter class='categorical' column='[federated.acme1].[none:status:nk]'><groupfilter function='union' /></filter>",
        "<filter class='quantitative' column='[federated.acme1].[none:amount:qk]' included-values='in-range-or-null'>"
        "<min>1</min></filter>",
    ):
        f = tmp_path / "f.twb"
        f.write_text(base.replace(old, new))
        assert _by_label(intake([f]))["Revenue by country"]["statement"] is None


@pytest.mark.parametrize("make", ["not_zip", "encrypted", "too_many"])
def test_a_damaged_zip_is_a_sentence_not_a_traceback(tmp_path, capsys, monkeypatch, make):
    f = tmp_path / "x.twbx"
    if make == "not_zip":
        f.write_bytes(b"PK\x03\x04 not really a zip")
    elif make == "encrypted":
        with zipfile.ZipFile(f, "w") as z:
            z.write(TWB, "acme_orders.twb")
        raw = bytearray(f.read_bytes())
        raw[6] |= 0x1  # the encrypted flag on the member
        cd = raw.rfind(b"PK\x01\x02")
        raw[cd + 8] |= 0x1
        f.write_bytes(bytes(raw))
    else:
        with zipfile.ZipFile(f, "w") as z:
            for n in range(5):
                z.writestr(f"m{n}.txt", "x")
        monkeypatch.setattr(dashboard_exports, "_MAX_ZIP_MEMBERS", 3)
    assert reconcile.main(["intake", "--file", str(f)]) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err and "x.twbx" in err


def test_a_model_file_with_odd_entries_is_read_around_them(tmp_path):
    model = _model(_M_QUERY)
    table = model["model"]["tables"][0]
    table["partitions"].insert(0, "not an object")
    table["partitions"].append({"source": "not an object"})
    table["measures"].append(None)
    table["columns"].append(7)
    model["model"]["tables"].append(["not", "a", "table"])
    bim = tmp_path / "model.bim"
    bim.write_text(json.dumps(model), encoding="utf-8")
    d = intake([bim])
    assert [r["label"] for r in d["rows"]] == ["orders"]
    assert [x["name"] for x in d["definitions"]] == ["Delivered revenue", "is_web"]


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "SELECT 1 AS a; /* note */ SELECT 2 AS b;",
            [("SELECT 1 AS a", None), ("SELECT 2 AS b", None)],
        ),
        (
            "SELECT 1 AS a; /* a note\n   over two lines */\nSELECT 2 AS b",
            [("SELECT 1 AS a", None), ("SELECT 2 AS b", None)],
        ),
        (
            "SELECT 1 AS a; /* x */ -- y\n-- Second\nSELECT 2 AS b",
            [("SELECT 1 AS a", None), ("SELECT 2 AS b", "Second")],
        ),
    ],
)
def test_a_block_comment_after_a_semicolon_ends_at_its_close(text, expected):
    assert reconcile._split_sql(text) == expected


@pytest.mark.parametrize(
    "line, statements, pieced",
    [
        # a server name that starts with "select" is never a statement
        (
            'Sql.Database("select-db.example.com", "acme", [Query="SELECT 1 AS n"])',
            ["SELECT 1 AS n"],
            False,
        ),
        ('Sql.Database("select-db.example.com", "acme")', [], False),
        # a query put together from pieces is never read in part
        (
            'Sql.Database("your-cluster.example.com", "acme", [Query="SELECT id FROM orders" & " WHERE status = \'paid\'"])',
            [],
            True,
        ),
        (
            'Value.NativeQuery(Source, "SELECT id FROM orders WHERE status = " & Status, null)',
            [],
            True,
        ),
        ("Value.NativeQuery(Source, Sql, null)", [], True),
        (
            'Value.NativeQuery(Source, "SELECT ""a"" AS x FROM t", null, [EnableFolding=true])',
            ['SELECT "a" AS x FROM t'],
            False,
        ),
        ('Odbc.Query("dsn=select-dsn", "SELECT id FROM orders")', ["SELECT id FROM orders"], False),
    ],
)
def test_power_query_is_read_only_from_whole_native_query_literals(line, statements, pieced):
    got, n = dashboard_exports._m_statements(line)
    assert got == statements and bool(n) is pieced
    assert not any("example.com" in g or "select-dsn" in g for g in got)


def test_a_pieced_power_bi_query_is_named_in_skipped(tmp_path):
    bim = tmp_path / "model.bim"
    bim.write_text(json.dumps(_model('"SELECT id FROM orders" & " WHERE x = 1"')), encoding="utf-8")
    d = intake([bim])
    assert d["rows"] == []
    assert any(
        "table 'orders': its native query is put together" in s["reason"] for s in d["skipped"]
    )


def _lkml_view(tmp_path, dims: str, filters: str) -> Path:
    f = tmp_path / "v.view.lkml"
    f.write_text(
        "view: orders {\n  sql_table_name: orders ;;\n"
        + dims
        + f"  measure: n {{ type: count filters: [{filters}] }}\n}}\n"
    )
    return f


@pytest.mark.parametrize(
    "dims, filters, where",
    [
        (
            "  dimension: postal_code { type: string sql: ${TABLE}.postal_code ;; }\n",
            'postal_code: "00123"',
            "WHERE postal_code = '00123'",
        ),
        (
            "  dimension: postal_code { sql: ${TABLE}.postal_code ;; }\n",
            'postal_code: "00123"',
            "WHERE postal_code = '00123'",
        ),  # LookML's default type is string
        ("  dimension: qty { type: number sql: ${TABLE}.qty ;; }\n", 'qty: "5"', "WHERE qty = 5"),
        (
            "  dimension: status { sql: ${TABLE}.status ;; }\n",
            'orders.status: "paid"',
            "WHERE status = 'paid'",
        ),
        ("  dimension: qty { type: number sql: ${TABLE}.qty ;; }\n", 'qty: "five"', None),
        (
            "  dimension: is_paid { type: yesno sql: ${TABLE}.is_paid ;; }\n",
            'is_paid: "true"',
            None,
        ),
        # another view's dimension, even when this view has one of the same name
        ("  dimension: status { sql: ${TABLE}.status ;; }\n", 'customers.status: "paid"', None),
    ],
)
def test_a_lookml_filter_is_typed_by_its_dimension_and_bound_to_its_view(
    tmp_path, dims, filters, where
):
    rows = intake([_lkml_view(tmp_path, dims, filters)])["rows"]
    if where is None:
        assert rows == []
    else:
        assert rows[0]["statement"].endswith(where)
