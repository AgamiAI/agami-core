"""`databricks` target, Unity Catalog DDL (ACE-175): comments, informational keys, certification.

The statements are checked against Databricks' SQL reference (COMMENT ON, ALTER TABLE ... ALTER
COLUMN / ADD CONSTRAINT / DROP CONSTRAINT, SET TAG / UNSET TAG, and "Flag data as certified or
deprecated"). sqlglot parses `COMMENT ON ... IS '<text>'` and every `ALTER TABLE` form here in the
databricks dialect, so those are parsed. It parses neither `COMMENT ON ... IS NULL` nor `SET TAG` /
`UNSET TAG`, so those are held to a strict whole-statement pattern instead, and their object names
are parsed on their own as a table (or column) reference.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import itertools
import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sqlglot = pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

from sqlglot import exp  # noqa: E402
from sqlglot.errors import ErrorLevel  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PKG_SRC = REPO / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import cli  # noqa: E402
from semantic_model import export as X  # noqa: E402
from semantic_model import export_databricks as DB  # noqa: E402
from semantic_model import export_databricks_uc as UC  # noqa: E402  (registers the artifact)
from semantic_model.loader import load_datasource  # noqa: E402

SAMPLE = REPO / "plugins" / "agami" / "samples" / "store" / "model"
AREA = "sales"
FLAGS = ("--uc-comments", "--uc-constraints", "--uc-certify")
SECRET = ("email", "ssn")  # the synthetic model's sensitive columns
NOTE = "It's a \"note\" \\ C:\\path\nnext\tline $HOME {{x}} — café 東京 \x01 end"


def _parse(sql: str):
    return sqlglot.parse_one(sql, dialect="databricks", error_level=ErrorLevel.RAISE)


# ---------------------------------------------------------------------------
# A synthetic model: acme's SALES_DATA, written for Databricks
# ---------------------------------------------------------------------------


def _col(name, type_="integer", **kw):
    return {"name": name, "type": type_, **kw}


def _rel(a, b, card="many_to_one", **kw):
    return {"from_table": a, "to_table": b, "relationship": card, "review_state": "approved", **kw}


def _write_model(root: Path) -> Path:
    def put(rel, doc):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")

    def table(name, *cols, **kw):
        put(f"subject_areas/{AREA}/tables/{name.replace(' ', '_')}.yaml",
            {"name": name, "schema": "SALES_DATA", "columns": list(cols), **kw})

    put("datasource.yaml", {
        "datasource": "acme", "description": "Acme demo sales.",
        "storage_connections": [{"name": "acme_dbx", "storage_type": "Databricks", "storage_config": {}}],
        "subject_areas": [f"subject_areas/{AREA}"]})
    put(f"subject_areas/{AREA}/subject_area.yaml", {"name": AREA, "description": "Orders and money."})
    table("orders", _col("id"), _col("customer_id"), _col("store_id"), _col("placed_at", "date"),
          _col("amount", "decimal", description="Order total.", unit="USD",
               caveats=["Excludes tax.", "Gross of refunds."]),
          _col("notes", "string", description=NOTE),
          _col("email", "string", sensitive=True, description="Who to write to."),
          grain=["id"], description="One row per order.", caveats=["Test orders included."])
    table("customers", _col("id"), _col("Segment Name", "string", description="Segment."),
          grain=["id"], description="People who buy.")
    table("Order Items", _col("id"), _col("line"), _col("order_id"), grain=["id", "line"],
          description="Lines.")
    table("order-items", _col("id"), grain=["id"])          # names the same constraint as above
    table("stores", _col("id"), _col("name", "string"))     # no key
    table("profiles", _col("customer_id"), grain=["customer_id"])
    table("accounts", _col("ssn", "string", sensitive=True), _col("name", "string"),
          grain=["ssn"], description="Accounts.")           # its only key is sensitive
    table("big_orders", _col("id"), source_type="sql", sql="SELECT id FROM SALES_DATA.orders",
          grain=["id"], description="Large orders.")
    table("drafts", _col("id"), grain=["id"], review_state="unreviewed", description="Not yet.")
    put(f"subject_areas/{AREA}/relationships.yaml", [
        _rel("orders", "customers", from_column="customer_id", to_column="id"),
        _rel("customers", "orders", "one_to_many", from_column="id", to_column="customer_id"),
        _rel("Order Items", "orders", from_column="order_id", to_column="id"),
        _rel("orders", "stores", from_column="store_id", to_column="id"),
        _rel("orders", "customers", on="orders.customer_id = customers.id"),
        _rel("orders", "big_orders", from_column="id", to_column="id"),
        _rel("orders", "customers", from_column="placed_at", to_column="id"),
        _rel("profiles", "customers", "one_to_one", from_column="customer_id", to_column="id"),
        _rel("stores", "Order Items", from_column="id", to_column="id"),
    ])
    metrics = [
        {"name": "revenue", "calculation": "Sum of amount.", "bindings": {"Databricks": "SUM(orders.amount)"},
         "source_tables": ["orders"], "review_state": "approved", "unit": "USD"},
        {"name": "customer count", "calculation": "Customers.",
         "bindings": {"Databricks": "COUNT(customers.id)"}, "source_tables": ["customers"],
         "review_state": "approved"},
        {"name": "draft total", "calculation": "Draft.", "bindings": {"Databricks": "SUM(orders.id)"},
         "source_tables": ["orders"], "review_state": "unreviewed"},
    ]
    for m in metrics:
        put(f"subject_areas/{AREA}/metrics/{m['name'].replace(' ', '_')}.yaml", m)
    return root


@pytest.fixture()
def model(tmp_path: Path) -> Path:
    return _write_model(tmp_path / "art" / "acme")


def _sm(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["export", *argv])
    return rc, buf.getvalue()


def _export(root: Path, out: Path, *flags: str) -> tuple[dict, dict[str, str]]:
    rc, text = _sm(str(root), "--target", "databricks", "--out", str(out), *flags)
    assert rc == 0, text
    report = json.loads(text)
    base = out / "databricks"
    files = {p.relative_to(base).as_posix(): p.read_text(encoding="utf-8")
             for p in sorted(base.rglob("*")) if p.is_file()}
    return report, files


def _uc(files: dict[str, str]) -> dict[str, str]:
    return {k.removeprefix(f"{UC.UC_DIR}/"): v for k, v in files.items()
            if k.startswith(f"{UC.UC_DIR}/")}


def _sections(text: str) -> dict[str, list[str]]:
    """Each `-- ===== <title> =====` section's statements, one per line."""
    out: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = re.fullmatch(r"-- ===== (\w+) =====", line)
        if m:
            current = out.setdefault(m.group(1), [])
        elif line and not line.startswith("--"):
            assert current is not None, line
            current.append(line)
    return out


def _statements(text: str) -> list[str]:
    return [s for ss in _sections(text).values() for s in ss]


def _tagged(text: str) -> list[str]:
    """The objects the Certification section tags, its check aside."""
    return [_shape(s)[1].group(1) for s in _sections(text)["Certification"]
            if _shape(s)[0] == "tag"]


def _losses(root: Path, *flags: str, unreviewed: bool = False
            ) -> dict[tuple[str, str], list[tuple[str, str, str]]]:
    """What this artifact alone reports, by item; the report mixes in every artifact's."""
    org = load_datasource(root)
    opts = X.ExportOptions(("databricks",), include_unreviewed=unreviewed,
                           target_args={f[2:].replace("-", "_"): True for f in flags})
    view, _ = X.build_view(org, org.subject_areas[0], X.TARGETS["databricks"], opts, "Databricks",
                           [], [])
    out: dict = {}
    for x in UC.write_uc_ddl(view, opts).losses:
        out.setdefault((x.kind, x.name), []).append((x.outcome, x.reason, x.detail))
    return out


# ---------------------------------------------------------------------------
# The statement shapes, from Databricks' SQL reference
# ---------------------------------------------------------------------------

IDENT = r"(?:[A-Za-z_][A-Za-z0-9_]*|`(?:[^`]|``)+`)"
NAME = rf"{IDENT}(?:\.{IDENT}){{0,3}}"
LITERAL = r"'(?:[^'\\\n]|\\.)*'"
CONSTRAINT = r"[a-z_][a-z0-9_]*"
GUARD = re.compile(
    r"SELECT raise_error\(concat\((?P<msg>'(?:[^'\\]|\\.)*'), "
    r"array_join\(sort_array\(collect_set\(table_name\)\), ', '\)\)\) "
    r"FROM (?:(?P<catalog>[^ ]+)\.)?information_schema\.table_tags "
    r"WHERE lower\(schema_name\) = (?P<schema>'[^']*'|lower\(current_schema\(\)\)) "
    r"AND tag_name = 'system\.certification_status' "
    r"AND lower\(table_name\) IN \((?P<tables>'[^']*'(?:, '[^']*')*)\) HAVING count\(\*\) > 0;")
SHAPES = {
    "comment": re.compile(rf"COMMENT ON (TABLE|COLUMN) ({NAME}) IS {LITERAL};"),
    "uncomment": re.compile(rf"COMMENT ON (TABLE|COLUMN) ({NAME}) IS NULL;"),
    "not_null": re.compile(rf"ALTER TABLE ({NAME}) ALTER COLUMN ({IDENT}) (SET|DROP) NOT NULL;"),
    "pk": re.compile(rf"ALTER TABLE ({NAME}) ADD CONSTRAINT ({CONSTRAINT}) PRIMARY KEY "
                     rf"\(({IDENT}(?:, {IDENT})*)\);"),
    "fk": re.compile(rf"ALTER TABLE ({NAME}) ADD CONSTRAINT ({CONSTRAINT}) FOREIGN KEY \(({IDENT})\) "
                     rf"REFERENCES ({NAME}) \(({IDENT})\);"),
    "drop": re.compile(rf"ALTER TABLE ({NAME}) DROP CONSTRAINT IF EXISTS ({CONSTRAINT});"),
    "tag": re.compile(rf"SET TAG ON TABLE ({NAME}) `system\.certification_status` = `certified`;"),
    "untag": re.compile(rf"UNSET TAG ON TABLE ({NAME}) `system\.certification_status`;"),
    "guard": GUARD,
}


def _shape(stmt: str) -> tuple[str, re.Match]:
    hits = [(k, m) for k, rx in SHAPES.items() if (m := rx.fullmatch(stmt))]
    assert len(hits) == 1, stmt
    return hits[0]


def _check(stmt: str) -> list[str]:
    """Every way `stmt` is not a statement Databricks documents; empty when it is one."""
    try:
        kind, m = _shape(stmt)
    except AssertionError:
        return [f"no documented shape: {stmt}"]
    if kind in ("comment", "not_null", "pk", "fk", "drop", "guard"):
        tree = _parse(stmt.removesuffix(";"))
        if isinstance(tree, exp.Command) or tree is None:
            return [f"parsed only as a command: {stmt}"]
    else:  # sqlglot reads neither IS NULL comments nor tags: parse the object's name alone
        name = m.group(2) if kind == "uncomment" else m.group(1)
        if kind == "uncomment" and m.group(1) == "COLUMN":
            table, _, column = name.rpartition(".")
            _parse(f"SELECT {column} FROM {table}")
        else:
            _parse(f"SELECT 1 FROM {name}")
    return []


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


def test_by_default_no_file_is_written_and_one_entry_names_the_flags(model, tmp_path):
    report, files = _export(model, tmp_path / "o")
    assert not _uc(files)
    section = report["targets"]["databricks"]
    entries = [x for x in section["losses"] if x["kind"] == "unity_catalog"]
    assert entries == [{"area": AREA, "kind": "unity_catalog", "name": UC.ddl_path(AREA),
                        "outcome": "degraded", "reason": "detail_lost", "detail": entries[0]["detail"]}]
    assert all(f in entries[0]["detail"] for f in FLAGS)
    assert not any(k.startswith("uc_") for k in report["options"]["target_args"])
    # Nothing else of this artifact reaches the report when it writes nothing.
    assert not [x for x in section["losses"] if "Unity Catalog holds no" in x["detail"]
                or "Unity Catalog foreign key" in x["detail"]]


def test_the_flags_are_declared_off_and_suppressed():
    import argparse

    sp = argparse.ArgumentParser()
    UC.add_arguments(sp)
    assert vars(sp.parse_args([])) == {}
    assert vars(sp.parse_args(list(FLAGS))) == {"uc_comments": True, "uc_constraints": True,
                                                "uc_certify": True}
    assert UC.write_uc_ddl in DB.ARTIFACTS and UC.write_uc_ddl in DB.ARTIFACT_ARGUMENTS


@pytest.mark.parametrize("flags", [c for n in (1, 2, 3) for c in itertools.combinations(FLAGS, n)],
                         ids=lambda c: "+".join(f.removeprefix("--uc-") for c_ in [c] for f in c_))
def test_each_flag_combination_writes_exactly_its_sections(model, tmp_path, flags):
    report, files = _export(model, tmp_path / "o", *flags)
    uc = _uc(files)
    assert set(uc) == {"sales.sql", "sales_undo.sql"}
    want = [t for f, t in zip(FLAGS, ("Comments", "Keys", "Certification")) if f in flags]
    assert list(_sections(uc["sales.sql"])) == want
    assert list(_sections(uc["sales_undo.sql"])) == want[::-1]
    kinds = {"Comments": {"comment"}, "Keys": {"not_null", "pk", "fk"},
             "Certification": {"tag", "guard"}}
    for title, stmts in _sections(uc["sales.sql"]).items():
        assert stmts and {_shape(s)[0] for s in stmts} <= kinds[title], title
    args = report["options"]["target_args"]
    assert {k for k in args if k.startswith("uc_")} == {f[2:].replace("-", "_") for f in flags}
    assert not [x for x in report["targets"]["databricks"]["losses"] if x["kind"] == "unity_catalog"]


# ---------------------------------------------------------------------------
# Every statement is one Databricks documents
# ---------------------------------------------------------------------------


@pytest.fixture()
def everything(model, tmp_path):
    return _export(model, tmp_path / "all", *FLAGS)


def test_every_statement_has_a_documented_shape_and_parses_where_sqlglot_can(everything, tmp_path):
    _, files = everything
    _, sample = _export(SAMPLE, tmp_path / "s", "--location", "DEMO.SALES_DATA", *FLAGS)
    for uc in (_uc(files), _uc(sample)):
        for name, text in uc.items():
            stmts = _statements(text)
            assert stmts, name
            for s in stmts:
                assert _check(s) == [], (name, s)


@pytest.mark.parametrize("stmt", [
    "COMMENT ON TABLE a.b IS 'x'",                                    # no semicolon
    "COMMENT ON TABLE a.b IS 'it's';",                                # an unescaped quote
    "COMMENT ON VIEW a.b IS 'x';",                                    # not a form this file writes
    "ALTER TABLE a ADD CONSTRAINT Bad-Name PRIMARY KEY (id);",
    "ALTER TABLE a ADD CONSTRAINT a_pk PRIMARY KEY (id) RELY;",       # never RELY
    "SET TAG ON TABLE a.b system.certification_status = certified;",  # an unquoted dotted key
    "SET TAG ON TABLE a.b `system.certification_status` = `approved`;",
    "ALTER TABLE a.b ALTER COLUMN x SET NOT NULL",
])
def test_the_shape_check_rejects_what_databricks_would_not_take(stmt):
    assert _check(stmt)


def test_identifiers_follow_the_metric_views_quoting_rule(everything):
    _, files = everything
    text = _uc(files)["sales.sql"]
    assert "COMMENT ON COLUMN SALES_DATA.customers.`Segment Name` IS 'Segment.';" in text
    assert ("ALTER TABLE SALES_DATA.`Order Items` ADD CONSTRAINT agami__order_items__pk "
            "PRIMARY KEY (id, line);") in text
    for stmt in _statements(text):
        for ident in re.findall(rf"\b(?:TABLE|COLUMN|REFERENCES) ({NAME})", stmt):
            parts = re.findall(IDENT, ident)
            assert ".".join(parts) == ident
            assert all(p == DB._quote(p.strip("`").replace("``", "`")) for p in parts), ident


def test_comments_carry_description_unit_and_caveats_escaped_for_databricks(everything):
    _, files = everything
    stmts = _statements(_uc(files)["sales.sql"])
    assert "COMMENT ON TABLE SALES_DATA.orders IS 'One row per order. Caveat: Test orders included.';" in stmts
    assert ("COMMENT ON COLUMN SALES_DATA.orders.amount IS "
            "'Order total. Unit: USD. Caveat: Excludes tax. Caveat: Gross of refunds.';") in stmts
    (notes,) = [s for s in stmts if "orders.notes" in s]
    literal = _parse(notes.removesuffix(";")).expression
    # Every character comes back as written, the one control character as a space.
    assert literal.this == NOTE.replace("\x01", " ")
    assert "\n" not in notes and "\\$HOME" in notes and "東京" in notes
    # A column or table with nothing to say gets no statement, so its comment is left alone.
    assert not [s for s in stmts if s.startswith("COMMENT") and (
        "orders.placed_at" in s or "SALES_DATA.stores " in s)]


@pytest.mark.parametrize("text, literal", [
    ("plain", "'plain'"),
    ("it's", "'it\\'s'"),
    ("a\\b", "'a\\\\b'"),
    ("1\n2\r3\t4\x005\b6\x1a7", "'1\\n2\\r3\\t4\\05\\b6\\Z7'"),
    ("$x", "'\\$x'"),
    ("bell\x07\x7f", "'bell  '"),
    ("née — 東京", "'née — 東京'"),
    ("\\'", "'\\\\\\''"),
])
def test_sql_string_escapes_every_character_databricks_would_read_differently(text, literal):
    assert UC.sql_string(text) == literal
    # Read back, each character is the one written; a control character with no escape is a space.
    kept = "\n\r\t\0\b\x1a"
    want = "".join(" " if (c < " " and c not in kept) or c == "\x7f" else c for c in text)
    assert _parse(f"SELECT {literal}").expressions[0].this == want


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def test_keys_come_from_whole_grains_and_column_equalities_onto_them(everything, model):
    _, files = everything
    stmts = _statements(_uc(files)["sales.sql"])
    keys = [s for s in stmts if _shape(s)[0] in ("not_null", "pk", "fk")]
    assert keys == [
        "ALTER TABLE SALES_DATA.`Order Items` ALTER COLUMN id SET NOT NULL;",
        "ALTER TABLE SALES_DATA.`Order Items` ALTER COLUMN line SET NOT NULL;",
        "ALTER TABLE SALES_DATA.`Order Items` ADD CONSTRAINT agami__order_items__pk PRIMARY KEY (id, line);",
        "ALTER TABLE SALES_DATA.customers ALTER COLUMN id SET NOT NULL;",
        "ALTER TABLE SALES_DATA.customers ADD CONSTRAINT agami__customers__pk PRIMARY KEY (id);",
        "ALTER TABLE SALES_DATA.orders ALTER COLUMN id SET NOT NULL;",
        "ALTER TABLE SALES_DATA.orders ADD CONSTRAINT agami__orders__pk PRIMARY KEY (id);",
        "ALTER TABLE SALES_DATA.profiles ALTER COLUMN customer_id SET NOT NULL;",
        "ALTER TABLE SALES_DATA.profiles ADD CONSTRAINT agami__profiles__pk PRIMARY KEY (customer_id);",
        "ALTER TABLE SALES_DATA.`Order Items` ADD CONSTRAINT agami__order_items__order_id__fk "
        "FOREIGN KEY (order_id) REFERENCES SALES_DATA.orders (id);",
        "ALTER TABLE SALES_DATA.orders ADD CONSTRAINT agami__orders__customer_id__fk "
        "FOREIGN KEY (customer_id) REFERENCES SALES_DATA.customers (id);",
        "ALTER TABLE SALES_DATA.profiles ADD CONSTRAINT agami__profiles__customer_id__fk "
        "FOREIGN KEY (customer_id) REFERENCES SALES_DATA.customers (id);",
    ]
    names = [_shape(s)[1].group(2) for s in keys if _shape(s)[0] in ("pk", "fk")]
    assert len(names) == len(set(names)) and all(len(n) <= UC.MAX_NAME for n in names)
    losses = _losses(model, *FLAGS)

    def reason(kind, name):
        return [(o, r) for o, r, _ in losses[(kind, name)]]

    assert reason("table", "order-items") == [("degraded", "name_collision")]
    assert reason("relationship", "orders->stores") == [("degraded", "no_home_in_target")]
    assert reason("relationship", "orders->customers#2") == [("degraded", "no_home_in_target")]
    assert reason("relationship", "orders->big_orders") == [("degraded", "no_home_in_target")]
    # agami types the two columns differently, and a foreign key takes its key's type.
    assert reason("relationship", "orders->customers#3") == [("degraded", "target_rule")]
    # customers->orders (one-to-many) declared orders.customer_id already; one key per column.
    assert reason("relationship", "orders->customers") == [("degraded", "target_rule")]
    assert ("customers->orders") not in {n for _, n in losses}
    # Onto part of a composite key: a foreign key references the whole key.
    assert reason("relationship", "stores->Order Items") == [("degraded", "no_home_in_target")]
    assert "SQL condition" in losses[("relationship", "orders->customers#2")][0][2]
    # A table defined by a query has no catalog object; a table whose key is sensitive has no key.
    assert reason("table", "big_orders") == [("degraded", "no_home_in_target")]
    assert not [s for s in stmts if "big_orders" in s or "accounts" in s and "KEY" in s]


def test_a_constraint_name_too_long_is_reported(model, tmp_path, monkeypatch):
    monkeypatch.setattr(UC, "MAX_NAME", len("agami__orders__pk"))
    _, files = _export(model, tmp_path / "o", "--uc-constraints")
    stmts = _statements(_uc(files)["sales.sql"])
    assert [_shape(s)[1].group(2) for s in stmts if _shape(s)[0] == "pk"] == ["agami__orders__pk"]
    losses = _losses(model, "--uc-constraints")
    assert losses[("table", "customers")][0][:2] == ("degraded", "target_limit")
    assert losses[("relationship", "Order Items->orders")][0][:2] == ("degraded", "target_limit")


def test_the_header_says_what_each_section_overwrites_needs_and_how_to_undo_it(everything):
    _, files = everything
    text = _uc(files)["sales.sql"]
    for needle in ("Nothing in this file has been applied", "sales_undo.sql", "REPLACES",
                   "MODIFY", "NOT ENFORCED", "without RELY", "validates every", "ownership",
                   "APPLY TAG", "ASSIGN", "system.certification_status", "SELECT on each"):
        assert needle in text, needle


# ---------------------------------------------------------------------------
# Certification
# ---------------------------------------------------------------------------


def test_approved_tables_and_fully_approved_metric_views_are_certified(everything, model):
    _, files = everything
    tagged = _tagged(_uc(files)["sales.sql"])
    assert tagged == ["SALES_DATA.`Order Items`", "SALES_DATA.accounts", "SALES_DATA.customers",
                      "SALES_DATA.`order-items`", "SALES_DATA.orders", "SALES_DATA.profiles",
                      "SALES_DATA.stores", "SALES_DATA.sales__customers", "SALES_DATA.sales__orders"]
    org = load_datasource(model)
    opts = X.ExportOptions(("databricks",))
    view, _ = X.build_view(org, org.subject_areas[0], X.TARGETS["databricks"], opts, "Databricks",
                           [], [])
    assert DB.metric_view_identifier(view, opts, "orders") == tagged[-1]


def test_nothing_unreviewed_is_certified_or_certifies_a_view(model, tmp_path):
    _, files = _export(model, tmp_path / "o", "--uc-certify", "--include-unreviewed")
    tagged = _tagged(_uc(files)["sales.sql"])
    assert "SALES_DATA.drafts" not in tagged and "SALES_DATA.orders" in tagged
    assert not [t for t in tagged if "sales__" in t]
    losses = _losses(model, "--uc-certify", unreviewed=True)
    assert losses[("table", "drafts")] == [("degraded", "not_approved", losses[("table", "drafts")][0][2])]
    (orders,) = losses[("metric_view", "sales__orders")]
    assert orders[:2] == ("degraded", "not_approved") and "draft total" in orders[2]
    (customers,) = losses[("metric_view", "sales__customers")]
    assert "drafts" in customers[2]


def test_reserved_word_names_are_back_quoted_in_every_statement(model, tmp_path):
    p = model / f"subject_areas/{AREA}/tables/order.yaml"
    p.write_text(yaml.safe_dump({"name": "order", "schema": "SALES_DATA", "grain": ["from"],
                                 "description": "Orders.",
                                 "columns": [{"name": "from", "type": "integer",
                                              "description": "Where from."}]}))
    _, files = _export(model, tmp_path / "o", "--uc-comments", "--uc-constraints")
    text = _uc(files)["sales.sql"]
    assert "COMMENT ON TABLE SALES_DATA.`order` IS 'Orders.';" in text
    assert "COMMENT ON COLUMN SALES_DATA.`order`.`from` IS 'Where from.';" in text
    assert "ALTER TABLE SALES_DATA.`order` ALTER COLUMN `from` SET NOT NULL;" in text
    assert "PRIMARY KEY (`from`);" in text


def test_a_metric_view_the_export_did_not_write_is_not_certified(model, tmp_path, monkeypatch):
    real = DB.definition_body
    monkeypatch.setattr(DB, "definition_body", lambda d: None if DB.dollar_fields(d) else real(d))
    path = model / f"subject_areas/{AREA}/metrics/revenue.yaml"
    path.write_text(yaml.safe_dump({**yaml.safe_load(path.read_text()), "description": "Costs $$."}))
    _, files = _export(model, tmp_path / "o", "--uc-certify")
    assert "metric_views/sales__orders.sql" not in files
    tagged = _tagged(_uc(files)["sales.sql"])
    assert "SALES_DATA.sales__orders" not in tagged and "SALES_DATA.sales__customers" in tagged


# ---------------------------------------------------------------------------
# Undo, determinism, names, privacy
# ---------------------------------------------------------------------------

_UNDO = {
    # A column comment is cleared with an empty string, a table comment with NULL.
    "comment": lambda m: f"COMMENT ON {m.group(1)} {m.group(2)} IS "
                         + ("NULL;" if m.group(1) == "TABLE" else "'';"),
    "not_null": lambda m: f"ALTER TABLE {m.group(1)} ALTER COLUMN {m.group(2)} DROP NOT NULL;",
    "pk": lambda m: f"ALTER TABLE {m.group(1)} DROP CONSTRAINT IF EXISTS {m.group(2)};",
    "fk": lambda m: f"ALTER TABLE {m.group(1)} DROP CONSTRAINT IF EXISTS {m.group(2)};",
    # Commented out: UNSET TAG removes the tag whoever set it.
    "tag": lambda m: f"-- UNSET TAG ON TABLE {m.group(1)} `system.certification_status`;",
}


def _sections_keeping_untag(text: str) -> dict[str, list[str]]:
    """`_sections` of an undo file, with its commented-out tag removals counted as its lines."""
    out: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = re.fullmatch(r"-- ===== (\w+) =====", line)
        if m:
            current = out.setdefault(m.group(1), [])
        elif line and (not line.startswith("--") or line.startswith("-- UNSET TAG")):
            current.append(line)
    return out


def test_undo_statements_pair_one_to_one_in_reverse(everything, tmp_path):
    _, files = everything
    _, sample = _export(SAMPLE, tmp_path / "s", "--location", "DEMO.SALES_DATA", *FLAGS)
    for uc, stem in ((_uc(files), "sales"), (_uc(sample), "agami_example")):
        apply, undo = _sections(uc[f"{stem}.sql"]), _sections_keeping_untag(uc[f"{stem}_undo.sql"])
        assert list(undo) == list(apply)[::-1]
        # No tag removal runs as written; each is a line the customer enables.
        assert "\nUNSET TAG" not in uc[f"{stem}_undo.sql"]
        for title, stmts in apply.items():
            # The certification check reads; it has nothing to reverse.
            stmts = [s for s in stmts if _shape(s)[0] != "guard"]
            assert undo[title] == [_UNDO[_shape(s)[0]](_shape(s)[1]) for s in reversed(stmts)]
            assert len(undo[title]) == len(stmts) and stmts


def test_two_runs_are_byte_identical(model, tmp_path):
    a = _export(model, tmp_path / "one", *FLAGS)
    b = _export(model, tmp_path / "two", *FLAGS)
    assert a == b and _uc(a[1])


def test_physical_names_follow_the_location_rule(model, tmp_path):
    # A model for another engine takes catalog and schema from --location, as every artifact does.
    _, sample = _export(SAMPLE, tmp_path / "s", "--location", "DEMO.SALES_DATA", "--uc-comments")
    stmts = _statements(_uc(sample)["agami_example.sql"])
    assert stmts and all(_shape(s)[1].group(2).startswith("DEMO.SALES_DATA.") for s in stmts)
    # A Databricks model keeps its own schema, under the --location catalog when one is given.
    _, files = _export(model, tmp_path / "l", "--location", "DEMO.OTHER", "--uc-comments")
    stmts = _statements(_uc(files)["sales.sql"])
    assert all(_shape(s)[1].group(2).startswith("DEMO.SALES_DATA.") for s in stmts)


@pytest.mark.parametrize("flags", [(), FLAGS, (*FLAGS, "--include-unreviewed")] + [
    (f,) for f in FLAGS])
def test_no_emitted_byte_names_a_sensitive_column(model, tmp_path, flags):
    out = tmp_path / "o"
    _export(model, out, *flags)
    secret = re.compile(rf"(?<![\w$])({'|'.join(SECRET)})(?![\w$])", re.I)
    for p in out.rglob("*"):
        if p.is_file():
            assert not secret.search(p.read_text(encoding="utf-8")), p


def test_the_privacy_scan_covers_the_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("privacy", REPO / "tests" / "test_privacy_no_network.py")
    privacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(privacy)
    module = PKG_SRC / "semantic_model" / "export_databricks_uc.py"
    assert module in privacy.SCRIPTS
    assert not any(rx.search(module.read_text(encoding="utf-8")) for rx in privacy._FORBIDDEN_RE)


# ---------------------------------------------------------------------------
# Review-wave fixes: one owner per table across areas; undo only what succeeded; strict certify
# ---------------------------------------------------------------------------


def _second_area(model: Path) -> None:
    """`service`, after `sales` by name: its own `refunds` table, and `sales`' orders and customers."""
    ds = model / "datasource.yaml"
    doc = yaml.safe_load(ds.read_text(encoding="utf-8"))
    doc["subject_areas"].append("subject_areas/service")
    ds.write_text(yaml.safe_dump(doc), encoding="utf-8")
    area = model / "subject_areas/service"
    (area / "tables").mkdir(parents=True)
    (area / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "service", "description": "Refunds.", "tables": [
            {"storage_connection": "acme_dbx", "schema": "SALES_DATA", "table": t}
            for t in ("orders", "customers")]}), encoding="utf-8")
    (area / "tables/refunds.yaml").write_text(yaml.safe_dump({
        "name": "refunds", "schema": "SALES_DATA", "grain": ["id"], "description": "Refunds.",
        "columns": [_col("id"), _col("order_id")]}), encoding="utf-8")
    (area / "relationships.yaml").write_text(yaml.safe_dump([
        _rel("refunds", "orders", from_column="order_id", to_column="id"),
        _rel("orders", "customers", from_column="customer_id", to_column="id")]), encoding="utf-8")


def test_a_table_two_areas_carry_gets_its_statements_once(model, tmp_path):
    _second_area(model)
    report, files = _export(model, tmp_path / "o", *FLAGS)
    uc = _uc(files)
    every = [s for f in ("sales.sql", "service.sql") for s in _statements(uc[f])]
    # Each statement once across the run: a second ADD CONSTRAINT or SET TAG would fail.
    assert len(every) == len(set(every))
    service = _statements(uc["service.sql"])
    assert not [s for s in service if "SALES_DATA.orders " in s or "SALES_DATA.customers " in s]
    assert "ALTER TABLE SALES_DATA.refunds ADD CONSTRAINT agami__refunds__pk PRIMARY KEY (id);" \
        in service
    # Each undo reverses only its own file.
    undo = _sections_keeping_untag(uc["service_undo.sql"])
    assert not [s for ss in undo.values() for s in ss if "SALES_DATA.orders " in s]
    losses = {(x["area"], x["kind"], x["name"]): x for x in report["targets"]["databricks"]["losses"]
              if x["reason"] == "target_rule" and "Unity Catalog statements" in x["detail"]}
    entry = losses[("service", "table", "orders")]
    assert "sales" in entry["detail"] and entry["outcome"] == "degraded"


def test_a_foreign_key_follows_its_child_tables_owner_and_names_the_file_to_run_first(model):
    org = load_datasource(model)
    opts = X.ExportOptions(("databricks",), target_args={"uc_constraints": True})
    view, _ = X.build_view(org, org.subject_areas[0], X.TARGETS["databricks"], opts, "Databricks",
                           [], [])
    phys = {et.table.name: X.physical_key(et.physical) for et in view.tables}
    # customers is an earlier area's: orders' key onto it is declared here, after that file.
    out = UC.write_uc_ddl(dataclasses.replace(view, claimed={phys["customers"]: "billing"}), opts)
    text = out.files["unity_catalog/sales.sql"].decode()
    assert "-- Run billing.sql first" in text
    assert "ADD CONSTRAINT agami__orders__customer_id__fk" in text
    assert "ADD CONSTRAINT agami__customers__pk" not in text
    # orders is an earlier area's: its foreign key is that area's to declare.
    out = UC.write_uc_ddl(dataclasses.replace(view, claimed={phys["orders"]: "billing"}), opts)
    text = out.files["unity_catalog/sales.sql"].decode()
    assert "agami__orders__" not in text and "ALTER TABLE SALES_DATA.orders " not in text
    # Order Items' key onto orders still is declared here, after the file that declares orders'.
    assert "ADD CONSTRAINT agami__order_items__order_id__fk" in text and "-- Run billing.sql" in text
    lost = {(x.kind, x.name): x.detail for x in out.losses if x.reason == "target_rule"}
    assert "billing" in lost[("relationship", "orders->customers")]


def test_every_constraint_is_named_agami_and_both_headers_say_to_reverse_only_what_succeeded(
        everything):
    _, files = everything
    uc = _uc(files)
    names = [_shape(s)[1].group(2) for s in _statements(uc["sales.sql"])
             if _shape(s)[0] in ("pk", "fk")]
    assert names and all(n.startswith("agami__") for n in names)
    assert "only those may be reversed" in uc["sales.sql"]
    assert "Run only the lines whose statement in that file" in uc["sales_undo.sql"]
    assert "commented out" in uc["sales_undo.sql"]


def test_a_table_carrying_an_unreviewed_column_is_not_certified(model, tmp_path):
    path = model / f"subject_areas/{AREA}/tables/customers.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["columns"][-1]["review_state"] = "unreviewed"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    _, files = _export(model, tmp_path / "o", "--uc-certify", "--include-unreviewed")
    tagged = _tagged(_uc(files)["sales.sql"])
    assert "SALES_DATA.customers" not in tagged and "SALES_DATA.orders" in tagged
    losses = _losses(model, "--uc-certify", unreviewed=True)
    (entry,) = losses[("table", "customers")]
    assert entry[:2] == ("degraded", "not_approved") and "Segment Name" in entry[2]


def test_a_metric_view_holding_a_level_of_detail_measure_is_not_certified(model, tmp_path):
    (model / f"subject_areas/{AREA}/metrics/peak.yaml").write_text(yaml.safe_dump({
        "name": "peak daily revenue", "calculation": "Peak.", "bindings": {"Databricks": "MAX({revenue})"},
        "source_tables": ["orders"], "base_metrics": ["revenue"], "inner_grain": ["placed_at"],
        "review_state": "approved"}), encoding="utf-8")
    _, files = _export(model, tmp_path / "o", "--uc-certify")
    assert "partition" in files["metric_views/sales__orders.sql"]
    tagged = _tagged(_uc(files)["sales.sql"])
    assert "SALES_DATA.sales__orders" not in tagged and "SALES_DATA.sales__customers" in tagged
    (entry,) = _losses(model, "--uc-certify")[("metric_view", "sales__orders")]
    assert entry[:2] == ("degraded", "target_rule") and "level-of-detail" in entry[2]


# ---------------------------------------------------------------------------
# The certification guard, column-comment undo, and the keys notes
# ---------------------------------------------------------------------------

def _note(text: str, title: str) -> str:
    """A section's leading comment lines as one text, the `-- ` of each line dropped."""
    lines = text.split(f"-- ===== {title} =====\n", 1)[1].splitlines()
    return " ".join(line[3:] for line in itertools.takewhile(lambda x: x.startswith("--"), lines))


def _guards(text: str) -> list[re.Match]:
    return [m for s in _sections(text).get("Certification", []) if (m := GUARD.fullmatch(s))]


def test_certification_starts_with_a_guard_per_schema_naming_every_object_it_tags(
        everything, model, tmp_path):
    _, files = everything
    text = _uc(files)["sales.sql"]
    stmts = _sections(text)["Certification"]
    (guard,) = _guards(text)
    assert stmts[0] == guard.group(0)            # the check runs before any SET TAG
    tagged = [_shape(s)[1].group(1) for s in stmts[1:]]
    names = [n.strip("'") for n in guard.group("tables").split(", ")]
    assert names == [t.rsplit(".", 1)[1].strip("`").lower() for t in tagged]
    assert guard.group("schema") == "'sales_data'" and guard.group("catalog") is None
    assert "system.certification_status" in guard.group("msg")
    tree = _parse(guard.group(0).removesuffix(";"))
    assert tree.find(exp.Having) is not None and not isinstance(tree, exp.Command)
    # With a location the metric views are created in another schema: one guard per catalog and
    # schema.
    _, files = _export(model, tmp_path / "l", "--location", "acme.METRICS",
                       "--uc-certify")
    guards = _guards(_uc(files)["sales.sql"])
    assert [(g.group("catalog"), g.group("schema"), g.group("tables")) for g in guards] == [
        ("acme", "'sales_data'", "'order items', 'accounts', 'customers', 'order-items', 'orders', "
                                 "'profiles', 'stores'"),
        ("acme", "'metrics'", "'sales__customers', 'sales__orders'")]


def test_the_certification_header_promises_only_what_the_guard_does(everything):
    _, files = everything
    note = _note(_uc(files)["sales.sql"], "Certification")
    assert "never overwrites" not in note and "SET TAG fails where" not in note
    assert "stops the section" in note and "already carries" in note
    assert "delete its SET TAG line and its name in the check" in note
    undo = _uc(files)["sales_undo.sql"]
    assert undo.count("-- UNSET TAG ON TABLE") == len(_sections(_uc(files)["sales.sql"])[
        "Certification"]) - 1


def test_the_undo_clears_a_column_comment_with_an_empty_string(everything):
    _, files = everything
    undo = _sections(_uc(files)["sales_undo.sql"])["Comments"]
    columns = [s for s in undo if s.startswith("COMMENT ON COLUMN")]
    tables = [s for s in undo if s.startswith("COMMENT ON TABLE")]
    assert columns and all(s.endswith(" IS '';") for s in columns)
    assert tables and all(s.endswith(" IS NULL;") for s in tables)
    note = _note(_uc(files)["sales.sql"], "Comments")
    assert "each column comment to an empty string" in note


def test_the_keys_note_covers_an_existing_primary_key_and_a_second_run(everything):
    _, files = everything
    text = _uc(files)["sales.sql"]
    note = _note(text, "Keys")
    for needle in ("at most one primary key", "already has one", "same columns",
                   "skip that table's lines in the undo file", "cannot run twice",
                   "undo file's Keys section first"):
        assert needle in note, needle
    query = [line[3:] for line in text.splitlines() if line.startswith("-- SELECT")]
    assert query == ["SELECT table_catalog, table_schema, table_name, constraint_name FROM "
                     "information_schema.table_constraints WHERE constraint_type = 'PRIMARY KEY' "
                     "AND lower(table_schema) = 'sales_data' AND lower(table_name) IN "
                     "('order items', 'customers', 'orders', 'profiles');"]
    assert _parse(query[0].removesuffix(";")) is not None
