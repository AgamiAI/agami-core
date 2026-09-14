"""Read one sheet of an `.xlsx` workbook as rows of text — the standard library and nothing else.

A question bank lives in Excel, and the import door is the one surface that has to open such a file
itself: the Read tool cannot open a workbook, and a model reconstructing one would be a second parser
with no way to check it. So this reads the workbook the way the format is defined — a zip of XML
parts — with `zipfile` and `ElementTree`, and adds no dependency to a plugin that installs with none.

It reads what a person SEES, not what Excel computes: a cell's stored value, a formula's cached
result, a shared string with its rich-text runs joined. Nothing is recalculated, no style is applied,
and a date comes through as the serial number Excel stores for it. That is enough for a column of
questions, ids, statements and tags, which is all the import door asks of a sheet.

Rows keep Excel's own numbering — a row the file omits is an empty row, not a missing one — because
the parse reports skipped rows by number, and a person finds them by that number in their workbook.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Optional

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# The largest single part this will decompress. A sheet of questions is kilobytes; a part expanding
# past this is not a question bank, and reading it whole is how a crafted file exhausts memory. The
# images and embedded objects a workbook carries are never read, so a workbook full of screenshots
# does not come near it.
_MAX_PART_BYTES = 64 * 1024 * 1024

_CELL_REFERENCE = re.compile(r"([A-Z]+)(\d+)")


class WorkbookError(ValueError):
    """A workbook this reader cannot use, carrying a sentence a person can act on."""


def sheet_names(path: str) -> list[str]:
    """Every sheet in the workbook, in the order Excel shows its tabs."""
    with _open(path) as book:
        return [name for name, _ in _sheets(book)]


def read_sheet(path: str, sheet: Optional[str]) -> tuple[str, list[list[str]]]:
    """One sheet's name and its rows, numbered as Excel numbers them.

    `sheet` may be None only for a workbook with exactly one sheet. Choosing among several is a
    judgement — which tab holds the questions — and the first tab of a real workbook is as likely to
    be a cover page, so the caller is told to ask rather than handed a guess.

    A name is matched exactly, then once more ignoring case and surrounding space, so `questions`
    finds a tab called `Questions ` — but only when that looser match is unambiguous.
    """
    with _open(path) as book:
        sheets = _sheets(book)
        if sheet is None:
            if len(sheets) != 1:
                raise WorkbookError(
                    f"this workbook has {len(sheets)} sheets, so which one holds the questions has "
                    f"to be named with --sheet. Sheets: {_listed(sheets)}"
                )
            name, part = sheets[0]
        else:
            matches = [entry for entry in sheets if entry[0] == sheet]
            if not matches:
                wanted = sheet.strip().casefold()
                matches = [entry for entry in sheets if entry[0].strip().casefold() == wanted]
            if len(matches) != 1:
                raise WorkbookError(
                    f"this workbook has no sheet named {sheet!r}. Sheets: {_listed(sheets)}"
                )
            name, part = matches[0]
        return name, _rows(_xml(book, part), _shared_strings(book))


def _open(path: str) -> zipfile.ZipFile:
    """The workbook as the zip it is, or a sentence saying it is not one.

    `FileNotFoundError` and `IsADirectoryError` are left to propagate: the caller already reports
    both in its own words, and a second phrasing of "that path is wrong" would only drift.
    """
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise WorkbookError(
            "this file is not a readable .xlsx workbook — if it is an older Excel file renamed to "
            ".xlsx, save it as .xlsx (or CSV) from Excel and re-invoke"
        ) from exc


def _xml(book: zipfile.ZipFile, part: str) -> ET.Element:
    """One XML part, parsed — refused if it is oversized, missing, malformed or declares a DTD.

    A spreadsheet part never declares a DTD. One that does is either not a workbook or is built to
    make an XML parser expand entities, and refusing it outright is cheaper than reasoning about
    which.
    """
    try:
        info = book.getinfo(part)
    except KeyError as exc:
        raise WorkbookError(f"this workbook is missing a part it refers to ({part})") from exc
    if info.file_size > _MAX_PART_BYTES:
        raise WorkbookError("a part of this workbook is too large to read as a question bank")
    data = book.read(info)
    if b"<!DOCTYPE" in data:
        raise WorkbookError("this workbook contains XML this reader will not parse")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise WorkbookError(f"this workbook has a damaged part ({part})") from exc


def _sheets(book: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Each sheet's name and the zip part holding it, resolved through the workbook's relationships.

    A sheet's part is not guaranteed to be `sheet<N>.xml` for the Nth tab — reordering tabs in Excel
    reorders the names and leaves the files — so the relationship is followed rather than assumed.
    """
    workbook = _xml(book, "xl/workbook.xml")
    relationships = _xml(book, "xl/_rels/workbook.xml.rels")
    targets = {
        relationship.get("Id"): relationship.get("Target") or ""
        for relationship in relationships.iter(_PACKAGE_REL + "Relationship")
    }
    found: list[tuple[str, str]] = []
    for sheet in workbook.iter(_MAIN + "sheet"):
        target = targets.get(sheet.get(_REL + "id"), "")
        # Relative to `xl/`, unless written as an absolute path inside the package.
        if target.startswith("/"):
            part = target.lstrip("/")
        else:
            part = posixpath.normpath(posixpath.join("xl", target))
        found.append((sheet.get("name") or "", part))
    if not found:
        raise WorkbookError("this workbook has no sheets")
    return found


def _shared_strings(book: zipfile.ZipFile) -> list[str]:
    """The workbook's shared string table. Most text cells are an index into it."""
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    return [_text(item) for item in _xml(book, "xl/sharedStrings.xml").iter(_MAIN + "si")]


def _text(item: ET.Element) -> str:
    """A string item's text: its own `<t>`, then every rich-text run's, in order.

    Only direct children are read. A phonetic guide (`<rPh>`) also carries a `<t>`, and a
    whole-subtree search would splice its reading into the middle of the cell.
    """
    parts = [item.findtext(_MAIN + "t") or ""]
    parts += [run.findtext(_MAIN + "t") or "" for run in item.findall(_MAIN + "r")]
    return "".join(parts)


def _rows(sheet: ET.Element, strings: list[str]) -> list[list[str]]:
    """Every row of the sheet as text, placed at Excel's row number and each cell at its column.

    Trailing rows with nothing in them are dropped: a sheet formatted down to row 1000 has not got
    950 empty questions in it, and reporting each as a skip would bury the skips that matter. Empty
    rows BETWEEN filled ones are kept, because they hold the numbering of everything after them.
    """
    rows: list[list[str]] = []
    for row in sheet.iter(_MAIN + "row"):
        number = int(row.get("r") or len(rows) + 1)
        # Excel omits a row it has nothing to say about; that row is still one a person counts.
        while len(rows) < number - 1:
            rows.append([])
        cells: list[str] = []
        for cell in row.findall(_MAIN + "c"):
            reference = _CELL_REFERENCE.fullmatch(cell.get("r") or "")
            column = _column_index(reference.group(1)) if reference else len(cells)
            while len(cells) < column:
                cells.append("")
            value = _value(cell, strings)
            if column < len(cells):
                cells[column] = value
            else:
                cells.append(value)
        rows.append(cells)
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    return rows


def _column_index(letters: str) -> int:
    """`A` is 0, `Z` is 25, `AA` is 26 — Excel's column letters as a list index."""
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return index - 1


def _value(cell: ET.Element, strings: list[str]) -> str:
    """A cell as text, by the type Excel recorded for it."""
    kind = cell.get("t")
    if kind == "inlineStr":
        inline = cell.find(_MAIN + "is")
        return _text(inline) if inline is not None else ""
    raw = cell.findtext(_MAIN + "v") or ""
    if kind == "s":
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return {"1": "TRUE", "0": "FALSE"}.get(raw, raw)
    if kind == "e":
        # An error value (#N/A, #REF!) is not text anybody wrote. Blank, it is treated as the empty
        # cell it effectively is, rather than imported as a question that reads "#N/A".
        return ""
    return raw


def _listed(sheets: list[tuple[str, str]]) -> str:
    return ", ".join(repr(name) for name, _ in sheets)
