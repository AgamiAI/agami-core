"""The import door reads an `.xlsx` workbook: one named sheet, a header wherever it starts, rows
numbered as Excel numbers them — and a crafted workbook costs it a refusal, not memory.

Every workbook here is built in the test from the format's own parts — a zip of XML — so each
fixture is synthetic and a few kilobytes, and the reader is exercised on the structures a real
workbook actually has: shared strings with rich-text runs and phonetic guides, inline strings, typed
cells, sparse cells, a title block above the header, rows Excel formats but never fills, more than
one sheet, and the Strict conformance class alongside the Transitional one. Every question is over
the shipped sample store database.
"""

from __future__ import annotations

import json
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Optional, Union
from xml.sax.saxutils import escape

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import _xlsx  # noqa: E402
import golden_author  # noqa: E402

TRANSITIONAL = (
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
)
STRICT = (
    "http://purl.oclc.org/ooxml/spreadsheetml/main",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"
MAIN = TRANSITIONAL[0]

QUERY = "How many orders have been placed?"
SQL = "SELECT COUNT(*) AS order_count FROM orders"
PROFILE = "demo"


def _column(index: int) -> str:
    letters, index = "", index + 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _workbook(
    tmp_path: Path,
    sheets: dict[str, dict[int, list]],
    filename: str = "bank.xlsx",
    replace_parts: Optional[dict[str, Union[str, bytes]]] = None,
    strict: bool = False,
) -> str:
    """A workbook with one worksheet per entry, each a map of Excel row number to cells.

    A cell is a str (a shared string), an int (a number), None (no cell element at all), or a tuple:
    ("inline", text), ("rich", [run, ...]) — a shared string in runs, with a phonetic guide attached —
    ("bool", value), ("error", code), or ("styled",) — a cell Excel formats but never fills.
    """
    main, rel = STRICT if strict else TRANSITIONAL
    shared: list[Any] = []

    def _shared(item: Any) -> int:
        shared.append(item)
        return len(shared) - 1

    parts: dict[str, Union[str, bytes]] = {}
    for number, rows in enumerate(sheets.values(), start=1):
        body = []
        for row_number, cells in rows.items():
            xml_cells = []
            for index, cell in enumerate(cells):
                ref = f"{_column(index)}{row_number}"
                if cell is None:
                    continue
                if isinstance(cell, tuple):
                    kind = cell[0]
                    if kind == "inline":
                        xml_cells.append(
                            f'<c r="{ref}" t="inlineStr"><is><t>{escape(cell[1])}</t></is></c>'
                        )
                    elif kind == "rich":
                        xml_cells.append(f'<c r="{ref}" t="s"><v>{_shared(cell)}</v></c>')
                    elif kind == "bool":
                        xml_cells.append(f'<c r="{ref}" t="b"><v>{1 if cell[1] else 0}</v></c>')
                    elif kind == "error":
                        xml_cells.append(f'<c r="{ref}" t="e"><v>{escape(cell[1])}</v></c>')
                    elif kind == "styled":
                        xml_cells.append(f'<c r="{ref}" s="1"/>')
                elif isinstance(cell, int):
                    xml_cells.append(f'<c r="{ref}"><v>{cell}</v></c>')
                else:
                    xml_cells.append(f'<c r="{ref}" t="s"><v>{_shared(cell)}</v></c>')
            body.append(f'<row r="{row_number}">{"".join(xml_cells)}</row>')
        parts[f"xl/worksheets/sheet{number}.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{main}">'
            f'<sheetData>{"".join(body)}</sheetData></worksheet>'
        )

    entries = "".join(
        f'<sheet name="{escape(name)}" sheetId="{n}" r:id="rId{n}"/>'
        for n, name in enumerate(sheets, start=1)
    )
    parts["xl/workbook.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{main}" xmlns:r="{rel}">'
        f"<sheets>{entries}</sheets></workbook>"
    )
    relationships = "".join(
        f'<Relationship Id="rId{n}" Type="{rel}/worksheet" Target="worksheets/sheet{n}.xml"/>'
        for n in range(1, len(sheets) + 1)
    )
    relationships += (
        f'<Relationship Id="rIdStrings" Type="{rel}/sharedStrings" Target="sharedStrings.xml"/>'
    )
    parts["xl/_rels/workbook.xml.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PACKAGE}">'
        f"{relationships}</Relationships>"
    )
    parts["_rels/.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PACKAGE}">'
        f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    items = []
    for item in shared:
        if isinstance(item, tuple):
            runs = "".join(f"<r><t>{escape(run)}</t></r>" for run in item[1])
            items.append(f'<si>{runs}<rPh sb="0" eb="1"><t>PHONETIC</t></rPh></si>')
        else:
            items.append(f"<si><t>{escape(item)}</t></si>")
    parts["xl/sharedStrings.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{main}">{"".join(items)}</sst>'
    )
    parts.update(replace_parts or {})

    path = tmp_path / filename
    with zipfile.ZipFile(path, "w") as book:
        for name, content in parts.items():
            book.writestr(name, content)
    return str(path)


def _sheet_part(rows_xml: str) -> str:
    """A raw worksheet part, for coordinates the builder would never write."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{MAIN}"><sheetData>{rows_xml}'
        "</sheetData></worksheet>"
    )


def _parse(tmp_path, monkeypatch, capsys, *argv: str):
    """Run the parse verb and return (exit code, stdout payload, stderr)."""
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    code = golden_author.main(["parse", *argv])
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out.strip() else None), captured.err


# --- reading what the sheet shows ---------------------------------------------------------------


def test_a_workbook_parses_to_the_same_rows_as_the_csv_it_would_export(
    tmp_path, monkeypatch, capsys
):
    """One parser. A workbook and the CSV Excel would export from it produce the same rows, the same
    ids and the same skips — the reader only turns the sheet into rows, and nothing downstream knows
    which kind of file it came from."""
    workbook = _workbook(
        tmp_path,
        {
            "Bank": {
                1: ["question", "sql", "tags"],
                2: [QUERY, SQL, "orders, smoke"],
                3: ["How many customers are on file?", None, "customers"],
            }
        },
    )
    sheet = tmp_path / "bank.csv"
    sheet.write_text(
        f'question,sql,tags\n{QUERY},{SQL},"orders, smoke"\nHow many customers are on file?,,customers\n',
        encoding="utf-8",
    )

    code, from_workbook, _ = _parse(tmp_path, monkeypatch, capsys, "--file", workbook)
    _, from_csv, _ = _parse(tmp_path, monkeypatch, capsys, "--csv", str(sheet))

    assert code == 0
    assert from_workbook["rows"] == from_csv["rows"]
    assert from_workbook["skipped"] == from_csv["skipped"] == []
    assert from_workbook["sheet"] == "Bank" and from_workbook["header_row"] == 1
    assert from_workbook["above_header"] == []


def test_a_strict_open_xml_workbook_reads_like_a_transitional_one(tmp_path):
    """Strict is a valid `.xlsx` too — it only declares different namespaces. A reader that knew one
    family would find no sheets in the other and call a good workbook broken."""
    sheets = {"Bank": {1: ["question", "tags"], 2: [QUERY, "orders"]}}
    transitional = _workbook(tmp_path, sheets, filename="transitional.xlsx")
    strict = _workbook(tmp_path, sheets, filename="strict.xlsx", strict=True)

    assert _xlsx.read_sheet(strict, None) == _xlsx.read_sheet(transitional, None)
    assert _xlsx.read_sheet(strict, None)[1] == [["question", "tags"], [QUERY, "orders"]]


def test_the_header_is_found_below_a_title_block_and_skips_keep_excels_row_numbers(
    tmp_path, monkeypatch, capsys
):
    """A real question bank opens with a title, and the rows below it are numbered by Excel. A skip
    is reported at the number a person sees in their own workbook — across a row the file omits
    entirely — and the title above the header is reported as not read, never silently dropped."""
    workbook = _workbook(
        tmp_path,
        {
            "Questions": {
                1: ["Order questions for the quarterly review"],
                # Row 2 is omitted from the file altogether.
                3: ["Q#", "Question", "Owner"],
                4: [1, QUERY, "analyst"],
                5: [2, ("styled",), "analyst"],
                6: [3, "How many customers are on file?", "analyst"],
            }
        },
    )

    code, payload, err = _parse(tmp_path, monkeypatch, capsys, "--file", workbook)

    assert code == 0
    assert payload["header_row"] == 3
    assert [row["query"] for row in payload["rows"]] == [QUERY, "How many customers are on file?"]
    assert payload["skipped"] == [{"row": 5, "reason": "empty question"}]
    assert payload["above_header"] == [
        {"row": 1, "text": "Order questions for the quarterly review"}
    ]
    assert "1 row(s) were skipped" in err and "above the header" in err


def test_rows_excel_formats_but_never_fills_are_not_reported_as_skips(
    tmp_path, monkeypatch, capsys
):
    """A sheet formatted down to its last row has not got a million questions in it — and building
    a million empty rows to find that out would be its own failure. The trailing rows are never
    built, however far down the formatting goes."""
    workbook = _workbook(
        tmp_path,
        {"Bank": {1: ["question"], 2: [QUERY], 3: [("styled",)], 1_048_576: [("styled",)]}},
    )

    started = time.monotonic()
    code, payload, err = _parse(tmp_path, monkeypatch, capsys, "--file", workbook)

    assert code == 0
    assert payload["summary"] == {"parsed": 1, "skipped": 0}
    assert "skipped" not in err
    assert time.monotonic() - started < 5


def test_cells_read_as_what_the_sheet_shows(tmp_path):
    """Rich text joined, a phonetic guide left out, an inline string, a boolean, a number — and an
    error value blank rather than imported as a question that reads '#N/A'."""
    workbook = _workbook(
        tmp_path,
        {
            "Only": {
                1: [
                    ("rich", ["How many ", "orders", "?"]),
                    ("inline", "typed straight into the cell"),
                    ("bool", True),
                    42,
                    ("error", "#N/A"),
                ]
            }
        },
    )

    name, rows = _xlsx.read_sheet(workbook, None)

    assert name == "Only"
    assert rows == [["How many orders?", "typed straight into the cell", "TRUE", "42"]]
    assert "PHONETIC" not in json.dumps(rows)


def test_sparse_cells_land_in_their_own_columns(tmp_path):
    """Excel writes no element for an empty cell, so position in the row is not the column. A value
    in column C with nothing in B has to land in C — and past Z, in AB."""
    cells: list = ["question", None, "tags"] + [None] * 24 + ["far column"]
    workbook = _workbook(tmp_path, {"Only": {1: cells}})

    _, rows = _xlsx.read_sheet(workbook, None)

    assert rows[0][0] == "question" and rows[0][1] == "" and rows[0][2] == "tags"
    assert rows[0][27] == "far column"


# --- choosing the sheet ---------------------------------------------------------------------------


def test_a_workbook_with_several_sheets_asks_which_one_and_names_them(tmp_path, monkeypatch, capsys):
    """Which tab holds the questions is the person's call — a workbook's first tab is as often a
    cover page — so the parse stops and lists every sheet rather than guessing."""
    workbook = _workbook(
        tmp_path,
        {
            "Overview": {1: ["About this workbook"]},
            "Questions": {1: ["question"], 2: [QUERY]},
        },
    )

    code, payload, err = _parse(tmp_path, monkeypatch, capsys, "--file", workbook)
    assert code == 2 and payload is None
    assert "'Overview'" in err and "'Questions'" in err and "--sheet" in err

    code, payload, _ = _parse(tmp_path, monkeypatch, capsys, "--file", workbook, "--sheet", "Questions")
    assert code == 0 and payload["sheet"] == "Questions" and payload["summary"]["parsed"] == 1

    # A person types the tab's name the way they remember it.
    code, payload, _ = _parse(tmp_path, monkeypatch, capsys, "--file", workbook, "--sheet", " questions")
    assert code == 0 and payload["sheet"] == "Questions"

    code, payload, err = _parse(tmp_path, monkeypatch, capsys, "--file", workbook, "--sheet", "Answers")
    assert code == 2 and payload is None
    assert "'Answers'" in err and "'Overview'" in err and "'Questions'" in err


def test_a_name_matching_two_tabs_loosely_is_called_ambiguous_not_missing(
    tmp_path, monkeypatch, capsys
):
    """Two tabs that differ only in case or a trailing space both match a loosely typed name. That
    is an ambiguity to resolve, and "no sheet named" would send the person looking for a tab that
    is sitting right there."""
    workbook = _workbook(
        tmp_path,
        {"Questions": {1: ["question"], 2: [QUERY]}, "questions ": {1: ["question"], 2: [QUERY]}},
    )

    code, payload, err = _parse(tmp_path, monkeypatch, capsys, "--file", workbook, "--sheet", "QUESTIONS")

    assert code == 2 and payload is None
    assert "more than one sheet matches" in err and "'Questions'" in err and "'questions '" in err
    assert "no sheet named" not in err


# --- the header, and what is never silently dropped -----------------------------------------------


def test_a_csv_header_is_still_its_first_row_so_no_question_vanishes_above_one(
    tmp_path, monkeypatch, capsys
):
    """A CSV has no title block to scan past. Scanning one would turn a headerless bank whose second
    question happens to read "question" into a bank whose first question silently vanished — so a
    CSV is refused unless its first row with anything in it is the header, as it always was."""
    sheet = tmp_path / "bank.csv"
    sheet.write_text(f"{QUERY}\nquestion\nHow many customers are on file?\n", encoding="utf-8")

    code, payload, err = _parse(tmp_path, monkeypatch, capsys, "--file", str(sheet))

    assert code == 2 and payload is None
    assert "no header row" in err and QUERY in err


def test_a_sheet_named_for_a_csv_is_noted_and_the_csv_still_parses(tmp_path, monkeypatch, capsys):
    """A CSV has one table, so `--sheet` has nothing to choose. Said, and not treated as a failure."""
    sheet = tmp_path / "bank.csv"
    sheet.write_text(f"question\n{QUERY}\n", encoding="utf-8")

    code, payload, err = _parse(tmp_path, monkeypatch, capsys, "--file", str(sheet), "--sheet", "Bank")

    assert code == 0 and payload["summary"]["parsed"] == 1
    assert "only applies to a workbook" in err


# --- files that are not workbooks, and workbooks built to hurt ------------------------------------


def test_an_old_xls_file_is_refused_with_a_way_past_it(tmp_path, monkeypatch, capsys):
    """The older binary format is not a zip of XML and cannot be read without a dependency. The
    refusal says how to get past it rather than failing to open a zip."""
    old = tmp_path / "bank.xls"
    old.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

    code, payload, err = _parse(tmp_path, monkeypatch, capsys, "--file", str(old))

    assert code == 2 and payload is None
    assert ".xlsx" in err and "save" in err.lower() and "Traceback" not in err


def test_a_file_named_xlsx_that_is_not_a_workbook_is_refused_rather_than_raising(
    tmp_path, monkeypatch, capsys
):
    fake = tmp_path / "bank.xlsx"
    fake.write_text("question\nHow many orders have been placed?\n", encoding="utf-8")

    code, payload, err = _parse(tmp_path, monkeypatch, capsys, "--file", str(fake))

    assert code == 2 and payload is None
    assert "not a readable .xlsx workbook" in err and "Traceback" not in err


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_a_part_declaring_a_doctype_is_refused_in_any_encoding(
    tmp_path, monkeypatch, capsys, encoding
):
    """A spreadsheet part never declares a DTD, and one that does is how an XML parser is made to
    expand entities without bound. Refused before it is parsed — including in UTF-16, where no
    byte sequence spells `<!DOCTYPE` and a search for one would let the part straight through."""
    bomb = (
        f'<?xml version="1.0" encoding="{encoding.upper()}"?>'
        '<!DOCTYPE worksheet [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        f'<worksheet xmlns="{MAIN}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>&b;</t>'
        "</is></c></row></sheetData></worksheet>"
    ).encode(encoding)
    if encoding == "utf-16":
        assert b"<!DOCTYPE" not in bomb  # the case an ASCII byte search cannot see
    workbook = _workbook(
        tmp_path,
        {"Only": {1: ["question"]}},
        replace_parts={"xl/worksheets/sheet1.xml": bomb},
    )

    code, payload, err = _parse(tmp_path, monkeypatch, capsys, "--file", workbook)

    assert code == 2 and payload is None
    assert "will not parse" in err and "Traceback" not in err


@pytest.mark.parametrize(
    "rows_xml",
    [
        # A row number no workbook has, used as a list length it would be billions of rows.
        '<row r="2147483647"><c r="A2147483647" t="inlineStr"><is><t>x</t></is></c></row>',
        # A row number that is not a number.
        '<row r="lots"><c r="A1" t="inlineStr"><is><t>x</t></is></c></row>',
        # A column past Excel's last one (XFD), used as a list length it would be a wide row.
        '<row r="1"><c r="XFE1" t="inlineStr"><is><t>x</t></is></c></row>',
        # A cell reference that is not a reference.
        '<row r="1"><c r="1A" t="inlineStr"><is><t>x</t></is></c></row>',
        # A real Excel coordinate, but data further down than any question bank runs.
        '<row r="1048576"><c r="A1048576" t="inlineStr"><is><t>x</t></is></c></row>',
    ],
)
def test_a_coordinate_outside_what_a_question_bank_holds_is_refused_before_it_allocates(
    tmp_path, monkeypatch, capsys, rows_xml
):
    """A few bytes of XML can name any row and any column. Each coordinate is checked before it
    places a value, so a crafted cell costs a refusal rather than the memory to build its row."""
    workbook = _workbook(
        tmp_path,
        {"Only": {1: ["question"]}},
        replace_parts={"xl/worksheets/sheet1.xml": _sheet_part(rows_xml)},
    )

    started = time.monotonic()
    code, payload, err = _parse(tmp_path, monkeypatch, capsys, "--file", workbook)

    assert code == 2 and payload is None
    assert "Traceback" not in err
    assert time.monotonic() - started < 5


def test_parsing_a_workbook_writes_nothing(tmp_path, monkeypatch, capsys):
    """SC-3 holds for a workbook exactly as for a CSV: the parse has no write in it."""
    workbook = _workbook(tmp_path, {"Bank": {1: ["question"], 2: [QUERY]}})

    code, _, _ = _parse(tmp_path, monkeypatch, capsys, "--file", workbook)

    assert code == 0
    assert not (tmp_path / PROFILE / "golden_datasets").exists()
