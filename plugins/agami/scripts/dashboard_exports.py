"""Read a dashboard export into the pieces `reconcile.py intake` turns into evidence rows (#645).

A person adding their own examples often has a dashboard, not a folder of SQL. Three exports carry
the SQL behind a dashboard in a text form that can be read without the tool that made it:

- **Tableau** `.twb` (XML) and `.twbx` (a zip holding one): each worksheet's data source (custom SQL
  or a table), the fields on its shelves, its filters, and the calculated fields it uses.
- **Power BI** `.pbit` (a template: a zip holding `DataModelSchema`, JSON) and `.bim` (that JSON on
  its own): the native SQL a table loads with, and each measure's DAX.
- **LookML** `.lkml`: a view's derived-table SQL, and its measures.

What comes out is never trusted. A statement composed here is the worksheet's SQL as far as the
export states it, and it goes through reconcile's part-by-part check like any statement a person
pastes. Where a worksheet uses something this reader does not translate (a calculated field, a date
part, a relative-date filter, a join of several tables), no statement is composed: the row keeps the
pieces and says what was left untranslated, so the skill can ask rather than guess. A Tableau
formula, a DAX expression and a LookML measure are listed as definitions, never converted to SQL.

The file is untrusted input. XML that declares a DTD or an entity is refused before it is parsed
(the check `_xlsx` already makes for a workbook), a zip member is read only up to a size limit, and
names, formulas and SQL read from the file are data: they are returned as strings and never acted on.
Connection details an export carries (server, user, database names) are never read into the output.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path
from typing import Any

import _xlsx

# The largest document read out of an export: a workbook's XML or a model's JSON. A Tableau packaged
# workbook can be hundreds of megabytes of data extract; only its `.twb` is read, and that is small.
_MAX_DOC_BYTES = 64 * 1024 * 1024
# A packaged workbook carries its data extract, so the zip itself may be far larger than the XML
# read out of it; this bounds what is opened at all. LookML is hand-written text: a few hundred KB
# is a large project file, and its parser is not built for more.
_MAX_ZIP_BYTES = 512 * 1024 * 1024
_MAX_ZIP_MEMBERS = 10_000
_MAX_LKML_BYTES = 2 * 1024 * 1024

_STATEMENT_RE = re.compile(r"^\s*(with|select)\b", re.IGNORECASE)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

EXPORT_SUFFIXES = frozenset({".twb", ".twbx", ".pbit", ".bim", ".lkml"})

# Formats people reach for that hold their SQL in a binary or app-only form. Named so the person is
# told what to send instead, never met with "could not read".
UNREADABLE_EXPORTS = {
    ".pbix": "a .pbix keeps its model in a binary form; save it from Power BI Desktop as a template "
    "(File > Export > Power BI template, .pbit), or send a screenshot of the report",
    ".tds": "a Tableau data source file has no worksheets; send the workbook (.twb or .twbx), or a screenshot",
    ".tdsx": "a Tableau data source file has no worksheets; send the workbook (.twb or .twbx), or a screenshot",
    ".hyper": "a Tableau extract holds data, not the questions asked of it; send the workbook (.twb or .twbx)",
}


class ExportError(ValueError):
    """An export this reader cannot use, with a sentence a person can act on."""


def read_export(path: Path) -> dict:
    """`{"rows": [...], "definitions": [...], "skipped": [...]}` for one export file.

    Each row is `{"label", "statement", "line", "export"}`: `statement` is None when the pieces could
    not be composed into one, and `export` holds what was read (and what was not) for the skill to
    show. `line` is the item's position in the file (worksheet, table or measure number), so a row
    can be found again."""
    suffix = path.suffix.lower()
    if suffix in (".twb", ".twbx"):
        return _tableau(path)
    if suffix in (".pbit", ".bim"):
        return _powerbi(path)
    if suffix == ".lkml":
        return _lookml(path)
    raise ExportError(f"{path.name} is not an export this reader knows")


# --- shared -----------------------------------------------------------------------------------


def _zip_member(path: Path, want) -> tuple[str, bytes]:
    """The first member `want(name)` accepts, read up to the size limit. A damaged, encrypted or
    oddly compressed zip is a sentence for the person, never a traceback."""
    if path.stat().st_size > _MAX_ZIP_BYTES:
        raise ExportError(f"{path.name} is too large to read")
    try:
        with zipfile.ZipFile(path) as book:
            members = book.infolist()
            if len(members) > _MAX_ZIP_MEMBERS:
                raise ExportError(
                    f"{path.name} holds {len(members)} files, more than an export does"
                )
            for info in members:
                if info.is_dir() or not want(info.filename):
                    continue
                if info.file_size > _MAX_DOC_BYTES:
                    raise ExportError(f"{info.filename} inside {path.name} is too large to read")
                # The declared size was checked above, and ZipFile reads no further than it.
                return info.filename, book.read(info)
    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError) as exc:
        # RuntimeError is an encrypted member; NotImplementedError a compression zipfile lacks.
        raise ExportError(
            f"{path.name} is not a readable {path.suffix} file ({type(exc).__name__})"
        ) from exc
    raise ExportError(f"{path.name} holds nothing this reader can use")


def _read_file(path: Path) -> bytes:
    if path.stat().st_size > _MAX_DOC_BYTES:
        raise ExportError(f"{path.name} is too large to read")
    return path.read_bytes()


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _words(name: str) -> str:
    """`order_count` → `order count`: how a field is named to a person."""
    return name.replace("_", " ").strip()


# --- Tableau ----------------------------------------------------------------------------------

# A field on a shelf: `[datasource].[derivation:field:suffix]`.
_TABLEAU_REF = re.compile(r"\[([^\]]+)\]\.\[([^\]]+)\]")
_TABLEAU_INSTANCE = re.compile(r"^(?P<deriv>[a-z]+):(?P<field>.+):(?P<suffix>[a-z]+)$")
_TABLEAU_AGG = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "cnt": "COUNT"}
_TABLEAU_DATE_PARTS = frozenset(
    {
        "yr",
        "qr",
        "mn",
        "wk",
        "dy",
        "wd",
        "hr",
        "mi",
        "sc",
        "my",
        "tyr",
        "tqr",
        "tmn",
        "twk",
        "tdy",
        "thr",
        "tmi",
        "tsc",
    }
)
_TABLEAU_AGG_WORDS = {
    "sum": "total",
    "avg": "average",
    "min": "minimum",
    "max": "maximum",
    "cnt": "count",
    "ctd": "distinct count",
}


def _tableau(path: Path) -> dict:
    if path.suffix.lower() == ".twbx":
        part, data = _zip_member(path, lambda n: n.lower().endswith(".twb"))
    else:
        part, data = path.name, _read_file(path)
    try:
        _xlsx._refuse_a_dtd(data, part)
        root = ET.fromstring(data)
    except _xlsx.WorkbookError as exc:
        raise ExportError(f"{path.name}: {exc}") from exc
    except ET.ParseError as exc:
        raise ExportError(f"{path.name} is not a readable Tableau workbook") from exc
    if root.tag != "workbook":
        raise ExportError(f"{path.name} is not a Tableau workbook")

    sources = {
        ds.get("name"): _tableau_source(ds)
        for ds in root.iter("datasource")
        if ds.get("name") and ds.find("connection") is not None
    }
    rows, skipped = [], []
    used_by: dict[tuple[str, str], list[str]] = {}
    for n, sheet in enumerate(root.findall("./worksheets/worksheet"), 1):
        row = _tableau_sheet(sheet, sources, n)
        if row is None:
            skipped.append(
                {"line": n, "reason": f"worksheet {sheet.get('name')!r} shows no fields"}
            )
            continue
        for calc in row["export"]["calculations"]:
            used_by.setdefault((row["export"]["datasource"] or "", calc["name"]), []).append(
                row["label"]
            )
        rows.append(row)
    definitions = []
    for src in sources.values():
        for calc in src["calcs"].values():
            definitions.append(
                {
                    "source": "Tableau",
                    "language": "Tableau calculation",
                    "datasource": src["caption"],
                    "name": calc["name"],
                    "formula": calc["formula"],
                    "used_by": used_by.get((src["caption"] or "", calc["name"]), []),
                }
            )
    return {"rows": rows, "definitions": definitions, "skipped": skipped}


def _tableau_source(ds: ET.Element) -> dict:
    """A data source's base (custom SQL or one table), its field-to-column map and its calculations.
    Its connection's server, user and database attributes are never read."""
    relations: dict[tuple, ET.Element] = {}
    for rel in ds.iter("relation"):
        kind = rel.get("type")
        if kind == "text":
            relations.setdefault(("text", (rel.text or "").strip()), rel)
        elif kind == "table":
            relations.setdefault(("table", rel.get("table") or ""), rel)
        elif kind == "join":
            relations.setdefault(("join", ""), rel)
    base, custom_sql, table, why = None, None, None, None
    if not relations:
        why = "the data source names no table or SQL"
    elif len(relations) > 1 or ("join", "") in relations:
        why = "the data source joins several tables"
    else:
        ((kind, value),) = relations
        if kind == "text":
            custom_sql = value.rstrip().rstrip(";").rstrip()
            if "<[Parameters]." in custom_sql:
                why = "the custom SQL uses a Tableau parameter"
            elif not _STATEMENT_RE.match(custom_sql):
                why = "the custom SQL is not a SELECT"
            else:
                base = f"(\n{custom_sql}\n) t"
        else:
            table = value
            parts = re.findall(r"\[([^\]]+)\]", value)
            if parts and all(_IDENTIFIER.match(p) for p in parts):
                base = ".".join(parts)
            else:
                why = "the table name needs quoting"
    if ds.find("filter") is not None and why is None:
        # A data source filter applies to every worksheet on it; composing without it would be wrong.
        why, base = "the data source has filters of its own", None
    columns = {}
    for m in ds.iter("map"):
        key, value = m.get("key") or "", m.get("value") or ""
        remote = re.findall(r"\[([^\]]+)\]", value)
        if key.startswith("[") and remote:
            columns[key.strip("[]")] = remote[-1]
    calcs = {}
    for col in ds.findall("column"):
        calc = col.find("calculation")
        if calc is not None and calc.get("formula") is not None:
            name = (col.get("name") or "").strip("[]")
            calcs[name] = {"name": col.get("caption") or name, "formula": calc.get("formula")}
    return {
        "caption": ds.get("caption") or ds.get("name"),
        "base": base,
        "custom_sql": custom_sql,
        "base_why": why,
        "columns": columns,
        "calcs": calcs,
        "table": table,
    }


def _tableau_sheet(sheet: ET.Element, sources: dict, n: int) -> dict | None:
    name = sheet.get("name") or f"worksheet {n}"
    shelf_text = " ".join((el.text or "") for el in sheet.iter() if el.tag in ("rows", "cols"))
    shelf_text += " " + " ".join(
        el.get("column") or "" for enc in sheet.iter("encodings") for el in enc
    )
    refs = [(ds, inst) for ds, inst in _TABLEAU_REF.findall(shelf_text) if ds != "Parameters"]
    if not refs:
        return None
    ds_names = sorted({ds for ds, _ in refs})
    untranslated: list[str] = []
    src = sources.get(ds_names[0]) if len(ds_names) == 1 else None
    if len(ds_names) > 1:
        untranslated.append(f"the worksheet blends {len(ds_names)} data sources")
    elif src is None:
        untranslated.append("the worksheet's data source is not in the file")
    elif src["base_why"]:
        untranslated.append(src["base_why"])
    calcs_used: list[dict] = []

    def column(field: str) -> str | None:
        if src and field in src["calcs"]:
            calc = src["calcs"][field]
            if calc not in calcs_used:
                calcs_used.append(calc)
            untranslated.append(f"the calculated field {calc['name']!r} is a Tableau formula")
            return None
        col = (src or {}).get("columns", {}).get(field, field)
        if not _IDENTIFIER.match(col):
            untranslated.append(f"the field {field!r} needs quoting")
            return None
        return col

    dims, measures, fields_words = [], [], []
    for _ds, inst in dict.fromkeys(refs):
        m = _TABLEAU_INSTANCE.match(inst)
        if inst.startswith(":") or m is None:
            untranslated.append("the worksheet shows several measures at once")
            continue
        deriv, field = m["deriv"], m["field"]
        col = column(field)
        if deriv == "none":
            fields_words.append(
                _words((src or {}).get("calcs", {}).get(field, {}).get("name") or field)
            )
            if col:
                dims.append(col)
        elif deriv in _TABLEAU_AGG or deriv == "ctd":
            fields_words.append(f"{_TABLEAU_AGG_WORDS[deriv]} of {_words(field)}")
            if col:
                agg = (
                    f"COUNT(DISTINCT {col})" if deriv == "ctd" else f"{_TABLEAU_AGG[deriv]}({col})"
                )
                measures.append(f"{agg} AS {deriv}_{col}")
        elif deriv == "usr":
            fields_words.append(
                _words((src or {}).get("calcs", {}).get(field, {}).get("name") or field)
            )
            if col:
                untranslated.append(
                    f"{_words(field)} is shown by an aggregate this reader does not translate"
                )
        elif deriv in _TABLEAU_DATE_PARTS:
            fields_words.append(f"{_words(field)} ({deriv})")
            untranslated.append(f"{_words(field)} is shown by a date part ({deriv})")
        else:
            fields_words.append(f"{_words(field)} ({deriv})")
            untranslated.append(
                f"{_words(field)} is shown by an aggregation or bin this reader does not translate ({deriv})"
            )
    if any(_TABLEAU_REF.search(el.text or "") for el in sheet.iter("pages")):
        untranslated.append("the worksheet pages through a field, one view per value")
    where, having, filters_words = [], [], []
    for flt in sheet.iter("filter"):
        _tableau_filter(flt, column, where, having, filters_words, untranslated)
    if not measures and not untranslated:
        untranslated.append("the worksheet shows no measure")
    statement = None
    if not untranslated and src and src["base"]:
        lines = ["SELECT " + ", ".join(dims + measures), "FROM " + src["base"]]
        if where:
            lines.append("WHERE " + "\n  AND ".join(where))
        if dims:
            lines.append("GROUP BY " + ", ".join(dims))
        if having:
            lines.append("HAVING " + "\n  AND ".join(having))
        statement = "\n".join(lines)
    return {
        "label": name,
        "statement": statement,
        "line": n,
        "export": {
            "kind": "Tableau worksheet",
            "worksheet": name,
            "datasource": (src or {}).get("caption"),
            "base": "custom SQL"
            if (src or {}).get("custom_sql")
            else ((src or {}).get("table") and "a table"),
            "custom_sql": (src or {}).get("custom_sql"),
            "fields": fields_words,
            "filters": filters_words,
            "calculations": calcs_used,
            "untranslated": list(dict.fromkeys(untranslated)),
            "statement_from": "composed from the worksheet's data source, fields and filters"
            if statement
            else None,
        },
    }


def _tableau_filter(
    flt: ET.Element, column, where: list, having: list, words: list, untranslated: list
) -> None:
    refs = _TABLEAU_REF.findall(flt.get("column") or "")
    if not refs:
        return
    if refs[0][1].startswith(":"):
        return  # which measures to show, not which rows
    m = _TABLEAU_INSTANCE.match(refs[0][1])
    if m is None:
        # A set, a group or a combined field: it keeps some rows, and dropping it silently would
        # count everything.
        words.append(f"a filter on {refs[0][1]}")
        untranslated.append(f"the filter on {refs[0][1]} (a set or a group) is not translated")
        return
    deriv, field = m["deriv"], m["field"]
    kind = flt.get("class")
    if kind == "categorical" and deriv == "none":
        top = flt.find("groupfilter")
        members, negate = _tableau_members(top)
        if members is None:
            untranslated.append(f"the filter on {_words(field)} is not a plain list of values")
            return
        if not members:
            return  # every value: no filter at all
        words.append(f"{_words(field)} {'is not' if negate else 'is'} one of {', '.join(members)}")
        col = column(field)
        literals = [_tableau_literal(v) for v in members]
        if col and all(lit is not None for lit in literals):
            where.append(f"{col} {'NOT IN' if negate else 'IN'} ({', '.join(literals)})")
        elif col:
            untranslated.append(
                f"the filter on {_words(field)} holds a value this reader does not translate"
            )
        return
    if kind == "quantitative" and (deriv == "none" or deriv in _TABLEAU_AGG):
        if (flt.get("included-values") or "in-range") != "in-range":
            untranslated.append(
                f"the range filter on {_words(field)} also keeps nulls or other values"
            )
            return
        lo, hi = flt.findtext("min"), flt.findtext("max")
        bounds = [b for b in (lo, hi) if b is not None]
        if not bounds or not all(re.fullmatch(r"-?\d+(\.\d+)?", b.strip()) for b in bounds):
            untranslated.append(f"the range filter on {_words(field)} is not a plain number range")
            return
        col = column(field)
        words.append(
            f"{_words(field)} between {lo if lo is not None else 'any'} and {hi if hi is not None else 'any'}"
        )
        if col:
            expr = col if deriv == "none" else f"{_TABLEAU_AGG[deriv]}({col})"
            target = where if deriv == "none" else having
            if lo is not None:
                target.append(f"{expr} >= {lo.strip()}")
            if hi is not None:
                target.append(f"{expr} <= {hi.strip()}")
        return
    words.append(f"a {kind or 'custom'} filter on {_words(field)}")
    untranslated.append(f"the {kind or 'custom'} filter on {_words(field)} is not translated")


def _tableau_members(gf: ET.Element | None) -> tuple[list[str] | None, bool]:
    """The values a categorical filter keeps, and whether it keeps everything BUT them.

    `member` is one value, `union` several, `except` over `level-members` is "all but", and a lone
    `level-members` is every value. Anything else (a condition, a top-N, a wildcard) is None."""
    if gf is None:
        return [], False
    fn = gf.get("function")
    if fn == "member":
        return [gf.get("member") or ""], False
    if fn == "level-members":
        return [], False
    if fn == "union":
        out: list[str] = []
        if not gf.findall("groupfilter"):
            return None, False  # keeps no value at all, which is not the same as no filter
        for child in gf.findall("groupfilter"):
            got, neg = _tableau_members(child)
            if got is None or neg:
                return None, False
            out.extend(got)
        return out, False
    if fn == "except":
        kids = gf.findall("groupfilter")
        if len(kids) == 2 and kids[0].get("function") == "level-members":
            got, neg = _tableau_members(kids[1])
            if got is not None and not neg:
                return got, True
    return None, False


def _tableau_literal(member: str) -> str | None:
    """A Tableau member as SQL: `"text"` is a string, a bare number is a number; a null, a date or
    anything else is not translated."""
    m = member.strip()
    if len(m) >= 2 and m[0] == '"' and m[-1] == '"':
        return _quote(m[1:-1])
    if re.fullmatch(r"-?\d+(\.\d+)?", m):
        return m
    return None


# --- Power BI ---------------------------------------------------------------------------------

# An M string literal: "..." with "" as an escaped quote.
_M_STRING = re.compile(r'"((?:[^"]|"")*)"')
_M_ESCAPES = {"#(lf)": "\n", "#(cr)": "", "#(tab)": "\t", "#(cr,lf)": "\n"}


def _powerbi(path: Path) -> dict:
    if path.suffix.lower() == ".pbit":
        _name, data = _zip_member(path, lambda n: n == "DataModelSchema")
    else:
        data = _read_file(path)
    text = None
    for enc in ("utf-16", "utf-8-sig"):
        try:
            text = data.decode(enc)
            doc = json.loads(text)
            break
        except (UnicodeDecodeError, json.JSONDecodeError):
            text = None
    if text is None or not isinstance(doc, dict) or not isinstance(doc.get("model"), dict):
        raise ExportError(f"{path.name} is not a readable Power BI model")
    rows, definitions, skipped = [], [], []
    no_sql = 0
    for table in _dicts(doc["model"].get("tables")):
        tname = str(table.get("name") or "")
        found = []
        for part in _dicts(table.get("partitions")):
            source = part.get("source") if isinstance(part.get("source"), dict) else {}
            expr = _text(
                source.get("query") if source.get("type") == "query" else source.get("expression")
            )
            found.extend(
                _m_statements(str(expr))
                if source.get("type") != "query"
                else ([expr.strip()] if _STATEMENT_RE.match(expr) else [])
            )
        if not found:
            no_sql += 1
        for sql in dict.fromkeys(found):
            rows.append(
                {
                    "label": tname,
                    "statement": sql.rstrip().rstrip(";").rstrip(),
                    "line": len(rows) + 1,
                    "export": {
                        "kind": "Power BI table",
                        "table": tname,
                        "untranslated": [],
                        "statement_from": "the native SQL the table loads with",
                    },
                }
            )
        for measure in _dicts(table.get("measures")):
            expr = _text(measure.get("expression"))
            definitions.append(
                {
                    "source": "Power BI",
                    "language": "DAX",
                    "datasource": tname,
                    "name": str(measure.get("name") or ""),
                    "formula": expr,
                    "used_by": [],
                }
            )
        for col in _dicts(table.get("columns")):
            if col.get("type") == "calculated":
                expr = _text(col.get("expression"))
                definitions.append(
                    {
                        "source": "Power BI",
                        "language": "DAX (calculated column)",
                        "datasource": tname,
                        "name": str(col.get("name") or ""),
                        "formula": expr,
                        "used_by": [],
                    }
                )
    if no_sql:
        skipped.append(
            {
                "line": 0,
                "reason": f"{no_sql} table(s) load without a SQL query of their own "
                "(a table picked by name, or Power Query steps); not read",
            }
        )
    return {"rows": rows, "definitions": definitions, "skipped": skipped}


def _dicts(value: Any) -> list[dict]:
    """The objects in a JSON list: a model file is untrusted, so anything else in it is passed over."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    """A model expression, which the format writes as one string or a list of lines."""
    if isinstance(value, list):
        return "\n".join(str(v) for v in value if isinstance(v, (str, int, float)))
    return value if isinstance(value, str) else ""


def _m_statements(expr: str) -> list[str]:
    """Every M string literal in a Power Query expression that holds a SELECT or a WITH: the native
    query, wherever the connector puts it (`[Query="..."]`, `Value.NativeQuery(_, "...")`)."""
    out = []
    for m in _M_STRING.finditer(expr):
        text = m.group(1).replace('""', '"')
        for esc, ch in _M_ESCAPES.items():
            text = text.replace(esc, ch)
        if _STATEMENT_RE.match(text):
            out.append(text.strip())
    return out


# --- LookML -----------------------------------------------------------------------------------

# The next thing the pre-pass must act on: a string, a comment, or a key whose value runs to `;;`.
_LKML_NEXT = re.compile(r'["#]|(?<![\w.])(sql\w*|html|expression)[ \t]*:')
# Looker's filter grammar gives these words meanings of their own (`NULL`, `NOT x`, `last 7 days`,
# `yes` on a yes/no field), so a value that is one of them is never read as a literal.
_LKML_FILTER_WORDS = frozenset(
    {
        "null",
        "empty",
        "not",
        "to",
        "before",
        "after",
        "last",
        "this",
        "next",
        "ago",
        "yes",
        "no",
        "today",
        "yesterday",
        "tomorrow",
        "is",
        "any",
        "time",
        "from",
        "for",
        "until",
        "and",
        "or",
    }
)
_LKML_TOKEN = re.compile(
    r'\s*(?:(?P<brace>[{}\[\],])|(?P<str>"(?:[^"\\]|\\.)*")|(?P<key>[A-Za-z_][\w.]*)\s*:|'
    r'(?P<word>[^\s{}\[\],:"]+))'
)
_LKML_AGG = {
    "sum": "SUM",
    "average": "AVG",
    "min": "MIN",
    "max": "MAX",
    "count_distinct": "COUNT_DISTINCT",
}
_LKML_TABLE_COL = re.compile(r"^\$\{TABLE\}\.([A-Za-z_][A-Za-z0-9_]*)$")
_LKML_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _lookml(path: Path) -> dict:
    if path.stat().st_size > _MAX_LKML_BYTES:
        raise ExportError(
            f"{path.name} is too large to read as LookML; send the view files on their own"
        )
    text = path.read_bytes().decode("utf-8", errors="replace")
    try:
        text, blocks = _lkml_prepare(text)
        doc = _lkml_parse(text, blocks)
    except (ValueError, IndexError) as exc:
        raise ExportError(f"{path.name} is not readable LookML ({exc})") from exc
    rows, definitions, skipped = [], [], []
    for key, name, body in doc:
        if key == "explore":
            skipped.append({"line": 0, "reason": f"explore {name!r}: its joins are not read here"})
        if key != "view" or not isinstance(body, list):
            continue
        view = {k: v for k, _n, v in body if not isinstance(v, list)}
        dims = {
            n: v
            for k, n, v in body
            if k in ("dimension", "dimension_group") and isinstance(v, list)
        }
        derived = next(
            (v for k, _n, v in body if k == "derived_table" and isinstance(v, list)), None
        )
        derived_sql = next((v for k, _n, v in (derived or []) if k == "sql"), None)
        if derived_sql and _STATEMENT_RE.match(derived_sql):
            base = f"(\n{derived_sql.rstrip().rstrip(';')}\n) t"
            rows.append(
                {
                    "label": f"{_words(name)} (derived table)",
                    "statement": derived_sql.rstrip().rstrip(";"),
                    "line": len(rows) + 1,
                    "export": {
                        "kind": "LookML derived table",
                        "view": name,
                        "untranslated": [],
                        "statement_from": "the view's derived-table SQL",
                    },
                }
            )
        else:
            table = str(view.get("sql_table_name") or "").strip()
            base = table if table and all(_IDENTIFIER.match(p) for p in table.split(".")) else None
        for k, mname, mbody in body:
            if k != "measure" or not isinstance(mbody, list):
                continue
            row, definition = _lookml_measure(name, mname, mbody, dims, base)
            definitions.append(definition)
            if row:
                row["line"] = len(rows) + 1
                rows.append(row)
    return {"rows": rows, "definitions": definitions, "skipped": skipped}


def _lkml_prepare(text: str) -> tuple[str, list[str]]:
    """One linear pass: every `sql:`-like value (which runs to `;;` and may hold anything) is set
    aside as a placeholder, and every `#` comment outside a string is dropped, so `"$#,##0.00"` or
    `"Order #"` survive. Each step moves forward and never re-reads, whatever the file holds."""
    out: list[str] = []
    blocks: list[str] = []
    i = 0
    while True:
        m = _LKML_NEXT.search(text, i)
        if m is None:
            out.append(text[i:])
            return "".join(out), blocks
        out.append(text[i : m.start()])
        if m.group(1):
            end = text.find(";;", m.end())
            if end < 0:
                raise ValueError(f"a {m.group(1)}: value has no closing ;;")
            blocks.append(text[m.end() : end].strip())
            out.append(f"{m.group(1)}: \x00{len(blocks) - 1}\x00")
            i = end + 2
        elif m.group(0) == "#":
            end = text.find("\n", m.start())
            i = len(text) if end < 0 else end
        else:
            j = m.start() + 1
            while j < len(text) and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[m.start() : j + 1])
            i = j + 1


def _lkml_parse(text: str, blocks: list[str]) -> list:
    """LookML as nested `(key, name, value)` triples: `value` is a list for a `{ }` block, a list of
    strings for a `[ ]` list, or a string."""
    tokens = []
    pos = 0
    while pos < len(text):
        m = _LKML_TOKEN.match(text, pos)
        if m is None:
            if text[pos:].strip():
                raise ValueError(f"unexpected text near {text[pos : pos + 20]!r}")
            break
        pos = m.end()
        tokens.append(m)
    out, _ = _lkml_block(tokens, 0, blocks)
    return out


def _lkml_value(tok: re.Match, blocks: list[str]) -> str:
    raw = tok.group("str") or tok.group("word") or ""
    if raw.startswith('"'):
        return raw[1:-1].replace('\\"', '"')
    if raw.startswith("\x00"):
        return blocks[int(raw.strip("\x00"))]
    return raw


def _lkml_block(tokens: list, i: int, blocks: list[str]) -> tuple[list, int]:
    out = []
    while i < len(tokens):
        tok = tokens[i]
        if tok.group("brace") == "}":
            return out, i + 1
        if not tok.group("key"):
            i += 1
            continue
        key = tok.group("key")
        i += 1
        name = ""
        if (
            i < len(tokens)
            and (tokens[i].group("word") or tokens[i].group("str"))
            and i + 1 < len(tokens)
            and tokens[i + 1].group("brace") == "{"
        ):
            name = _lkml_value(tokens[i], blocks)
            i += 1
        if i < len(tokens) and tokens[i].group("brace") == "{":
            body, i = _lkml_block(tokens, i + 1, blocks)
            out.append((key, name, body))
        elif i < len(tokens) and tokens[i].group("brace") == "[":
            # `[a, b]` is a list of values; `[status: "x", region: "y"]` a list of `field: value` pairs.
            items, i = [], i + 1
            while i < len(tokens) and tokens[i].group("brace") != "]":
                t = tokens[i]
                if t.group("key") and i + 1 < len(tokens) and not tokens[i + 1].group("brace"):
                    items.append(f"{t.group('key')}: {_lkml_value(tokens[i + 1], blocks)}")
                    i += 1
                elif not t.group("brace"):
                    items.append(_lkml_value(t, blocks))
                i += 1
            out.append((key, name, items))
            i += 1
        elif i < len(tokens):
            out.append((key, name, _lkml_value(tokens[i], blocks)))
            i += 1
    return out, i


def _lkml_column(sql: str, dims: dict) -> str | None:
    """`${TABLE}.amount` → `amount`, and `${status}` → the column a dimension names the same way."""
    sql = (sql or "").strip()
    m = _LKML_TABLE_COL.match(sql)
    if m:
        return m.group(1)
    ref = _LKML_REF.match(sql)
    if ref and ref.group(1) in dims:
        dim_sql = next((v for k, _n, v in dims[ref.group(1)] if k == "sql"), None)
        return _lkml_column(dim_sql or "", {})
    return None


def _lookml_measure(
    view: str, name: str, body: list, dims: dict, base: str | None
) -> tuple[dict | None, dict]:
    props = {k: v for k, _n, v in body}
    mtype = str(props.get("type") or "count")
    sql = props.get("sql")
    label = props.get("label") or _words(name)
    filters = props.get("filters")
    definition = {
        "source": "LookML",
        "language": "LookML measure",
        "datasource": view,
        "name": label,
        "formula": f"type: {mtype}"
        + (f"; sql: {sql}" if sql else "")
        + (
            f"; filters: {', '.join(str(f) for f in filters)}"
            if isinstance(filters, list) and filters
            else ""
        ),
        "used_by": [],
    }
    untranslated = []
    if mtype == "count" and not sql:
        agg = "COUNT(*)"
    elif mtype in _LKML_AGG:
        col = _lkml_column(str(sql or ""), dims)
        if col is None:
            untranslated.append("the measure's SQL is more than one column")
            agg = None
        else:
            agg = (
                f"COUNT(DISTINCT {col})"
                if mtype == "count_distinct"
                else f"{_LKML_AGG[mtype]}({col})"
            )
    else:
        untranslated.append(f"a {mtype} measure is not translated")
        agg = None
    where = []
    for clause in filters if isinstance(filters, list) else []:
        # Only `field: value` with one plain word: Looker's filter grammar gives `-x`, `a,b`, `%`,
        # `_`, `>1`, `1 to 5`, `NULL`, `NOT x` and `last 7 days` meanings of their own, and a guess
        # at one would be a wrong statement saved under the person's name.
        field, _, value = clause.partition(":") if isinstance(clause, str) else ("", "", "")
        field, value = field.strip().split(".")[-1], value.strip()
        col = _lkml_column("${" + field + "}", dims) if field else None
        plain = re.fullmatch(r"[A-Za-z0-9.]+", value) and value.lower() not in _LKML_FILTER_WORDS
        if col is None or not plain:
            untranslated.append(f"the filter {str(clause).strip()!r} is not a plain value")
            continue
        where.append(
            f"{col} = {value if re.fullmatch(r'-?[0-9]+([.][0-9]+)?', value) else _quote(value)}"
        )
    if not isinstance(filters, (list, type(None))):
        untranslated.append("the measure's filters are not a plain list")
    if base is None:
        untranslated.append("the view names no plain table or derived-table SQL")
    if not _IDENTIFIER.match(name):
        untranslated.append(f"the measure name {name!r} is not a plain identifier")
    if untranslated:
        return None, definition
    statement = f"SELECT {agg} AS {name}\nFROM {base}" + (
        ("\nWHERE " + "\n  AND ".join(where)) if where else ""
    )
    return {
        "label": label,
        "statement": statement,
        "export": {
            "kind": "LookML measure",
            "view": view,
            "measure": name,
            "untranslated": [],
            "statement_from": "composed from the measure's type, SQL and filters",
        },
    }, definition
