"""Read one sheet of an `.xlsx` workbook as rows of text — the standard library and nothing else.

A question bank lives in Excel, and the import door is the one surface that has to open such a file
itself: the Read tool cannot open a workbook, and a model reconstructing one would be a second parser
with no way to check it. So this reads the workbook the way the format is defined — a zip of XML
parts — with `zipfile`, `expat` and `ElementTree`, and adds no dependency to a plugin that installs
with none.

It reads what a person SEES, not what Excel computes: a cell's stored value, a formula's cached
result, a shared string with its rich-text runs joined. Nothing is recalculated, no style is applied,
and a date comes through as the serial number Excel stores for it. That is enough for a column of
questions, ids, statements and tags, which is all the import door asks of a sheet. Both conformance
classes are read — Transitional, which Excel saves by default, and Strict — because they differ only
in the namespaces their parts declare.

Rows keep Excel's own numbering — a row the file omits is an empty row, not a missing one — because
the parse reports skipped rows by number, and a person finds them by that number in their workbook.

The file is untrusted input in every coordinate it carries, not only in its size: a row number or a
column letter is used to place a value, so each is checked against Excel's own limits before it is
used, and the sheet is held sparsely until it is known how much of it actually has anything in it.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Optional
from xml.parsers import expat

# The package-level relationships namespace, which both conformance classes share. The spreadsheet
# vocabulary and the relationship-id attribute are NOT shared — Strict moves them under
# `purl.oclc.org` — so those are read off each part's own root element rather than hard-coded.
_PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Excel's own limits. A coordinate past either is not something Excel wrote, and refusing it before
# it is used to place a value is what keeps a few-byte cell from allocating a billion-entry row.
_MAX_EXCEL_ROWS = 1_048_576
_MAX_EXCEL_COLUMNS = 16_384

# How far down a sheet this reads, and how much it builds. A question bank is hundreds of rows by
# tens of columns. Data further down than this, or a sheet that expands past this many cells once
# empties are trimmed — one filled cell far to the right on every row, say — is not a question
# bank, and building it would be how a small file exhausts memory.
_MAX_READ_ROWS = 100_000
_MAX_CELLS = 2_000_000

# The largest single part this will decompress. The images and embedded objects a workbook carries
# are never read, so a workbook full of screenshots does not come near it.
_MAX_PART_BYTES = 64 * 1024 * 1024

_CELL_REFERENCE = re.compile(r"([A-Z]{1,3})[0-9]{1,7}")

_UNPARSEABLE = "this workbook contains XML this reader will not parse"


class WorkbookError(ValueError):
    """A workbook this reader cannot use, carrying a sentence a person can act on."""


def sheet_names(path: str) -> list[str]:
    """Every sheet in the workbook, in the order Excel shows its tabs."""
    with _open(path) as book:
        return [name for name, _ in _workbook(book)[0]]


def read_sheet(path: str, sheet: Optional[str]) -> tuple[str, list[list[str]]]:
    """One sheet's name and its rows, numbered as Excel numbers them.

    `sheet` may be None only for a workbook with exactly one sheet. Choosing among several is a
    judgement — which tab holds the questions — and the first tab of a real workbook is as likely to
    be a cover page, so the caller is told to ask rather than handed a guess.

    A name is matched exactly, then once more ignoring case and surrounding space, so `questions`
    finds a tab called `Questions `. If that looser match finds more than one tab, the name is
    ambiguous and says so, rather than claiming no tab matched.
    """
    with _open(path) as book:
        sheets, shared_strings_part = _workbook(book)
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
                if len(matches) > 1:
                    raise WorkbookError(
                        f"more than one sheet matches {sheet!r} once case and spaces are ignored — "
                        f"name one exactly. Matching sheets: {_listed(matches)}"
                    )
            if not matches:
                raise WorkbookError(
                    f"this workbook has no sheet named {sheet!r}. Sheets: {_listed(sheets)}"
                )
            name, part = matches[0]
        return name, _rows(_xml(book, part), _shared_strings(book, shared_strings_part))


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
    """One XML part, parsed — refused if it is oversized, missing, malformed or declares a DTD."""
    try:
        info = book.getinfo(part)
    except KeyError as exc:
        raise WorkbookError(f"this workbook is missing a part it refers to ({part})") from exc
    if info.file_size > _MAX_PART_BYTES:
        raise WorkbookError("a part of this workbook is too large to read as a question bank")
    data = book.read(info)
    _refuse_a_dtd(data, part)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise WorkbookError(f"this workbook has a damaged part ({part})") from exc


def _refuse_a_dtd(data: bytes, part: str) -> None:
    """Refuse a part that declares a DTD or an entity, however the part is encoded.

    A spreadsheet part never declares one. One that does is either not a workbook or built to make
    an XML parser expand entities without bound, so it is refused outright. The check is expat's own
    — it has already honoured the part's declared encoding, UTF-16 included — rather than a search
    for bytes spelling `<!DOCTYPE` in ASCII, which a UTF-16 part never contains.
    """
    detector = expat.ParserCreate()

    def _declared(*_: object) -> None:
        raise WorkbookError(_UNPARSEABLE)

    detector.StartDoctypeDeclHandler = _declared
    detector.EntityDeclHandler = _declared
    try:
        detector.Parse(data, True)
    except expat.ExpatError as exc:
        raise WorkbookError(f"this workbook has a damaged part ({part})") from exc


def _namespace(element: ET.Element) -> str:
    """The `{uri}` prefix an element's own tag carries — which conformance class wrote this part."""
    return element.tag[: element.tag.index("}") + 1] if element.tag.startswith("{") else ""


def _workbook(book: zipfile.ZipFile) -> tuple[list[tuple[str, str]], Optional[str]]:
    """Each sheet's name and part, and where the shared strings live — all by relationship.

    Nothing is assumed about file names. The workbook part is found from the package's root
    relationships; a sheet's part is not guaranteed to be `sheet<N>.xml` for the Nth tab (reordering
    tabs in Excel reorders the names and leaves the files); and the shared strings are wherever the
    workbook says they are.
    """
    workbook_part = _workbook_part(book)
    base = posixpath.dirname(workbook_part)
    workbook = _xml(book, workbook_part)
    main = _namespace(workbook)
    relationships = _xml(
        book, posixpath.join(base, "_rels", posixpath.basename(workbook_part) + ".rels")
    )

    targets: dict[str, str] = {}
    shared_strings: Optional[str] = None
    for relationship in relationships.iter(_PACKAGE_REL + "Relationship"):
        part = _resolve(base, relationship.get("Target") or "")
        targets[relationship.get("Id") or ""] = part
        if (relationship.get("Type") or "").endswith("/sharedStrings"):
            shared_strings = part

    found: list[tuple[str, str]] = []
    for sheet in workbook.iter(main + "sheet"):
        # The `r:id` attribute's namespace also differs between the two conformance classes, so it
        # is found by its local name.
        relationship_id = next(
            (value for key, value in sheet.attrib.items() if key.endswith("}id")), ""
        )
        found.append((sheet.get("name") or "", targets.get(relationship_id, "")))
    if not found:
        raise WorkbookError("this workbook has no sheets")
    return found, shared_strings


def _workbook_part(book: zipfile.ZipFile) -> str:
    """Where the workbook part is, from the package's root relationships, else where Excel puts it."""
    if "_rels/.rels" in book.namelist():
        for relationship in _xml(book, "_rels/.rels").iter(_PACKAGE_REL + "Relationship"):
            if (relationship.get("Type") or "").endswith("/officeDocument"):
                return _resolve("", relationship.get("Target") or "")
    return "xl/workbook.xml"


def _resolve(base: str, target: str) -> str:
    """A relationship target as a zip part name: relative to `base` unless written as absolute."""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(base, target))


def _shared_strings(book: zipfile.ZipFile, part: Optional[str]) -> list[str]:
    """The workbook's shared string table. Most text cells are an index into it."""
    if not part or part not in book.namelist():
        return []
    table = _xml(book, part)
    main = _namespace(table)
    return [_text(item, main) for item in table.iter(main + "si")]


def _text(item: ET.Element, main: str) -> str:
    """A string item's text: its own `<t>`, then every rich-text run's, in order.

    Only direct children are read. A phonetic guide (`<rPh>`) also carries a `<t>`, and a
    whole-subtree search would splice its reading into the middle of the cell.
    """
    parts = [item.findtext(main + "t") or ""]
    parts += [run.findtext(main + "t") or "" for run in item.findall(main + "r")]
    return "".join(parts)


def _rows(sheet: ET.Element, strings: list[str]) -> list[list[str]]:
    """Every row up to the last with anything in it, at Excel's row number, each cell at its column.

    Held sparsely first — row number to column to value, filled cells only — so that a coordinate is
    checked before it places anything, and nothing is built for a row or a column that holds nothing.
    Trailing rows with nothing in them are never built: a sheet formatted down to row 1000 has not
    got 950 empty questions in it. Empty rows BETWEEN filled ones are built, because they hold the
    numbering of everything after them.
    """
    main = _namespace(sheet)
    filled: dict[int, dict[int, str]] = {}
    previous_row = 0
    for row in sheet.iter(main + "row"):
        number = _row_number(row.get("r"), previous_row + 1)
        previous_row = number
        previous_column = -1
        for cell in row.findall(main + "c"):
            column = _column(cell.get("r"), previous_column + 1)
            previous_column = column
            value = _value(cell, strings, main)
            if value.strip():
                filled.setdefault(number, {})[column] = value
    if not filled:
        return []
    last = max(filled)
    if last > _MAX_READ_ROWS:
        raise WorkbookError(
            f"this sheet has data on row {last:,}, further down than a question bank runs — "
            "if the questions are near the top, delete the rows below them and re-invoke"
        )
    rows: list[list[str]] = []
    built = 0
    for number in range(1, last + 1):
        cells = filled.get(number, {})
        width = max(cells) + 1 if cells else 0
        built += width + 1
        if built > _MAX_CELLS:
            raise WorkbookError("this sheet is too large to read as a question bank")
        rows.append([cells.get(column, "") for column in range(width)])
    return rows


def _row_number(raw: Optional[str], default: int) -> int:
    """A row's number, checked against Excel's limit before it is used to place anything."""
    if raw is None:
        number = default
    elif raw.isdigit():
        number = int(raw)
    else:
        raise WorkbookError(f"this workbook has a row with an unreadable number ({raw!r})")
    if not 1 <= number <= _MAX_EXCEL_ROWS:
        raise WorkbookError(f"this workbook has a row numbered {number}, outside anything Excel writes")
    return number


def _column(reference: Optional[str], default: int) -> int:
    """A cell's column index, from its reference (`C7` is 2), checked against Excel's last column."""
    if reference is None:
        index = default
    else:
        match = _CELL_REFERENCE.fullmatch(reference)
        if not match:
            raise WorkbookError(f"this workbook has a cell with an unreadable reference ({reference!r})")
        index = 0
        for letter in match.group(1):
            index = index * 26 + (ord(letter) - ord("A") + 1)
        index -= 1
    if not 0 <= index < _MAX_EXCEL_COLUMNS:
        raise WorkbookError("this workbook has a cell past Excel's last column")
    return index


def _value(cell: ET.Element, strings: list[str], main: str) -> str:
    """A cell as text, by the type Excel recorded for it."""
    kind = cell.get("t")
    if kind == "inlineStr":
        inline = cell.find(main + "is")
        return _text(inline, main) if inline is not None else ""
    raw = cell.findtext(main + "v") or ""
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
