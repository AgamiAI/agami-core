"""Introspection reports the joins it tested on the data but didn't write (#512).

A column no join covers is tested against every other table's one-column key when its name or its
type matches, with a bounded match-rate probe. What the data bears out is listed in the report and in
`.introspect/join_candidates.json` as a ready `add` op; nothing is written to the model. Synthetic,
generic names only.
"""

from __future__ import annotations

import json
import sqlite3
import sys
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
from semantic_model import introspect as I  # noqa: E402

# orders.cust_ref holds customers' ids with no foreign key; orders.product_code mostly misses
# products.sku; orders.dealer_id names `dealers` but none of its values are there (sparse data).
_SCHEMA = (
    "CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);"
    "CREATE TABLE products(sku TEXT PRIMARY KEY, label TEXT);"
    "CREATE TABLE dealers(id INTEGER PRIMARY KEY, name TEXT);"
    "CREATE TABLE orders(id INTEGER PRIMARY KEY, cust_ref INTEGER, product_code TEXT, dealer_id INTEGER,"
    " amount REAL);"
    "INSERT INTO customers VALUES (1, 'a'), (2, 'b'), (3, 'c');"
    "INSERT INTO products VALUES ('p1', 'x'), ('p2', 'y');"
    "INSERT INTO dealers VALUES (100, 'd');"
    "INSERT INTO orders VALUES (1, 1, 'zz', NULL, 2.0), (2, 2, 'qq', NULL, 3.0), (3, 3, 'p1', NULL, 1.0),"
    " (4, 1, 'yy', NULL, 5.0);"
)
_FK_SCHEMA = (
    "CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);"
    "CREATE TABLE stores(id INTEGER PRIMARY KEY, city TEXT);"
    "CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),"
    " store_ref INTEGER);"
    "INSERT INTO customers VALUES (1, 'a'), (2, 'b');"
    "INSERT INTO stores VALUES (7, 'x'), (8, 'y');"
    "INSERT INTO orders VALUES (1, 1, 7), (2, 2, 8);"
)
_CUST_REF = "main.orders.cust_ref->main.customers.id"


def _db(tmp_path: Path, schema: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "shop.db"
    con = sqlite3.connect(db)
    con.executescript(schema)
    con.commit()
    con.close()
    return db


def _runner(db: Path, seen: list[str] | None = None):
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


def _build(tmp_path: Path, schema: str = _SCHEMA, **kw):
    db = _db(tmp_path, schema) if not (tmp_path / "shop.db").exists() else tmp_path / "shop.db"
    out = tmp_path / "demo"
    org, report = I.introspect("demo", "sqlite", runner=_runner(db, kw.pop("seen", None)),
                               artifacts_dir=tmp_path, out_dir=out, **kw)
    return org, report, out


def _joins(org) -> set[tuple]:
    return {(r.from_table, r.from_column, r.to_table, r.to_column)
            for sa in org.subject_areas for r in sa.relationships} | {
        (r.from_table, r.from_column, r.to_table, r.to_column) for r in org.cross_subject_area_relationships}


def _candidates(out: Path) -> dict:
    return json.loads((out / ".introspect" / "join_candidates.json").read_text())


def test_an_undeclared_matching_column_is_a_candidate_and_is_not_written(tmp_path):
    org, report, out = _build(tmp_path)
    assert ("orders", "cust_ref", "customers", "id") not in _joins(org)
    doc = _candidates(out)
    [cand] = [c for c in doc["candidates"] if c["id"] == _CUST_REF]
    assert cand["op"] == {"op": "add", "kind": "relationship", "from_table": "orders", "from_column": "cust_ref",
                          "to_table": "customers", "to_column": "id", "relationship": "many_to_one",
                          "from_schema": "main", "to_schema": "main",
                          "description": "found by introspection: " + cand["evidence"]["summary"]}
    assert cand["evidence"]["found_by"] == "type"
    assert (cand["evidence"]["matched"], cand["evidence"]["sampled"], cand["evidence"]["match_rate"]) == (3, 3, 1.0)
    text = report.render()
    assert "## join candidates" in text
    assert f"+ {_CUST_REF}: 100.0% of sampled orders.cust_ref values found in customers.id" in text
    assert "--candidate <id>" in text and str(out) in doc["how_to_accept"]


def test_a_low_overlap_pair_is_reported_as_rejected_not_offered(tmp_path):
    _, report, out = _build(tmp_path)
    doc = _candidates(out)
    low = {c["id"]: c for c in doc["low_overlap"]}
    rejected = "main.orders.product_code->main.products.sku"
    assert rejected in low and rejected not in {c["id"] for c in doc["candidates"]}
    assert low[rejected]["evidence"]["match_rate"] == 0.25
    assert f"- {rejected} (low overlap, not offered): 25.0%" in report.render()


def test_the_name_guessed_join_is_written_as_before_and_not_listed(tmp_path):
    # 4A: `dealer_id` beside `dealers` is still written unreviewed on the name alone.
    org, _, out = _build(tmp_path)
    [r] = [r for sa in org.subject_areas for r in sa.relationships if r.from_column == "dealer_id"]
    assert (r.to_table, r.review_state, r.confidence) == ("dealers", "unreviewed", "inferred")
    doc = _candidates(out)
    assert not any("dealer_id" in c["id"] for c in doc["candidates"] + doc["low_overlap"])


def test_the_scan_leaves_the_written_model_as_it_was(tmp_path, monkeypatch):
    _build(tmp_path / "a")
    monkeypatch.setattr(I, "_scan_join_candidates", lambda *a, **k: None)
    _build(tmp_path / "b")

    def files(root: Path) -> dict:
        return {p.relative_to(root): p.read_text() for p in sorted(root.rglob("*.yaml"))
                if ".introspect" not in p.parts and ".snapshots" not in p.parts}
    a, b = files(tmp_path / "a" / "demo"), files(tmp_path / "b" / "demo")
    assert a.keys() == b.keys()
    stamp = ("signed_off_at", "generated_at", "introspected_at", "org_id")   # time- or randomly-set
    strip = lambda t: "\n".join(ln for ln in t.splitlines() if not ln.strip().startswith(stamp))  # noqa: E731
    assert {k: strip(v) for k, v in a.items()} == {k: strip(v) for k, v in b.items()}


def test_candidates_are_found_beside_declared_foreign_keys(tmp_path):
    org, report, out = _build(tmp_path, _FK_SCHEMA)
    assert report.mode_per_capability["relationships"] == "catalog"
    assert ("orders", "customer_id", "customers", "id") in _joins(org)
    ids = {c["id"] for c in _candidates(out)["candidates"]}
    assert "main.orders.store_ref->main.stores.id" in ids
    assert not any("customer_id" in i for i in ids)   # the declared FK covers it


def test_the_probe_cap_stops_the_scan_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "JOIN_CANDIDATE_PROBE_CAP", 1)
    _, report, out = _build(tmp_path)
    doc = _candidates(out)
    assert doc["cap_hit"] is True and doc["probes"] == 1
    assert report.join_candidate_cap_hit
    assert "stopped after 1 probes" in report.render()


def test_the_probes_are_bounded_reads(tmp_path):
    seen: list[str] = []
    _build(tmp_path, seen=seen)
    probes = [s for s in seen if "AS sampled" in s]
    assert probes
    assert all(s.lstrip().upper().startswith("SELECT") and f"LIMIT {I._CANDIDATE_SAMPLE}" in s for s in probes)


def test_a_dry_run_writes_no_candidates_file(tmp_path):
    _, report, out = _build(tmp_path, dry_run=True)
    assert report.join_candidates
    assert not (out / ".introspect" / "join_candidates.json").exists()


def test_accepting_a_candidate_takes_one_curate_call_and_a_refresh_stops_listing_it(tmp_path, capsys):
    _, _, out = _build(tmp_path)
    path = out / ".introspect" / "join_candidates.json"
    assert cli.main(["curate", str(out), "--ops-file", str(path), "--candidate", _CUST_REF]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["applied"] and res["validated"]
    rel = yaml.safe_load((out / "subject_areas" / "demo" / "relationships.yaml").read_text())["relationships"]
    [added] = [r for r in rel if r.get("from_column") == "cust_ref"]
    assert added["review_state"] == "unreviewed"
    assert curate.history(out)[0]["op"] == "add"

    # the refresh keeps it and tests that column no more
    _build(tmp_path, append=True)
    assert _CUST_REF not in {c["id"] for c in _candidates(out)["candidates"]}


def test_a_candidates_file_needs_a_named_candidate(tmp_path, capsys):
    _, _, out = _build(tmp_path)
    path = out / ".introspect" / "join_candidates.json"
    with pytest.raises(SystemExit):
        cli.main(["curate", str(out), "--ops-file", str(path)])
    assert _CUST_REF in json.loads(capsys.readouterr().out)["candidates"]
    with pytest.raises(SystemExit):
        cli.main(["curate", str(out), "--ops-file", str(path), "--candidate", "nope"])


def test_a_pair_is_offered_once_and_a_small_number_is_not_taken_for_a_reference(tmp_path):
    schema = (
        "CREATE TABLE accounts(id INTEGER PRIMARY KEY, region_code TEXT UNIQUE, age INTEGER);"
        "CREATE TABLE regions(code TEXT PRIMARY KEY, label TEXT);"
        "INSERT INTO accounts VALUES (1, 'north', 2), (2, 'south', 1);"
        "INSERT INTO regions VALUES ('north', 'n'), ('south', 's');"
    )
    _, _, out = _build(tmp_path, schema)
    doc = _candidates(out)
    ids = [c["id"] for c in doc["candidates"]]
    assert ids == ["main.accounts.region_code->main.regions.code"]   # not also regions.code->accounts
    assert not any(".age->" in c["id"] for c in doc["candidates"] + doc["low_overlap"])


# Personal columns at either end: `orders.contact_email` holds the same text as `invites.code` (so a
# type match with full overlap), and `users.email` is a one-column key `accounts.owner_email` names.
_PERSONAL_SCHEMA = (
    "CREATE TABLE invites(code TEXT PRIMARY KEY, label TEXT);"
    "CREATE TABLE orders(id INTEGER PRIMARY KEY, contact_email TEXT);"
    "CREATE TABLE users(id INTEGER PRIMARY KEY, email TEXT UNIQUE);"
    "CREATE TABLE accounts(id INTEGER PRIMARY KEY, owner_email TEXT);"
    "INSERT INTO invites VALUES ('ann@example.com', 'a'), ('bob@example.com', 'b');"
    "INSERT INTO orders VALUES (1, 'ann@example.com'), (2, 'bob@example.com');"
    "INSERT INTO users VALUES (1, 'ann@example.com'), (2, 'bob@example.com');"
    "INSERT INTO accounts VALUES (1, 'ann@example.com'), (2, 'bob@example.com');"
)


def test_a_sensitive_column_is_never_probed_or_listed(tmp_path):
    seen: list[str] = []
    org, report, out = _build(tmp_path, _PERSONAL_SCHEMA, seen=seen)
    cols = {(n, c.name): c for sa in org.subject_areas for n in ("orders", "users")
            if sa.defined_table(n) for c in sa.defined_table(n).columns}
    assert cols[("orders", "contact_email")].sensitive and cols[("users", "email")].sensitive
    probes = [s for s in seen if "AS sampled" in s]
    assert not any("contact_email" in s or "email" in s for s in probes)
    doc = _candidates(out)
    assert not any("email" in c["id"] for c in doc["candidates"] + doc["low_overlap"])
    assert "email" not in "\n".join(report._render_join_candidates())


def test_the_candidates_file_holds_counts_and_names_never_sampled_values(tmp_path):
    schema = (
        "CREATE TABLE customers(code TEXT PRIMARY KEY, name TEXT);"
        "CREATE TABLE orders(id INTEGER PRIMARY KEY, cust_code TEXT);"
        "INSERT INTO customers VALUES ('CX-7781', 'a'), ('CX-7782', 'b'), ('CX-7783', 'c');"
        "INSERT INTO orders VALUES (1, 'CX-7781'), (2, 'CX-7782'), (3, 'QZ-0001');"
    )
    _, report, out = _build(tmp_path, schema)
    doc = _candidates(out)
    assert {c["id"] for c in doc["candidates"] + doc["low_overlap"]} == {"main.orders.cust_code->main.customers.code"}
    text = (out / ".introspect" / "join_candidates.json").read_text() + report.render()
    assert not any(v in text for v in ("CX-77", "QZ-0001"))


def test_a_reference_with_a_value_list_read_from_the_data_is_still_a_candidate(tmp_path):
    # 20 customers: `cust_ref` has few enough distinct values to get a value list, yet it is a reference.
    schema = ("CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);"
              "CREATE TABLE orders(id INTEGER PRIMARY KEY, cust_ref INTEGER, amount REAL);"
              + "".join(f"INSERT INTO customers VALUES ({i}, 'n{i}');" for i in range(1, 21))
              + "".join(f"INSERT INTO orders VALUES ({i}, {i % 20 + 1}, 1.0);" for i in range(1, 61)))
    org, _, out = _build(tmp_path, schema)
    orders = next(sa.defined_table("orders") for sa in org.subject_areas if sa.defined_table("orders"))
    assert next(c for c in orders.columns if c.name == "cust_ref").choice_field
    assert _CUST_REF in {c["id"] for c in _candidates(out)["candidates"]}


def test_a_member_of_a_composite_key_is_scanned_when_its_name_backs_it(tmp_path):
    schema = ("CREATE TABLE orders(id INTEGER PRIMARY KEY, placed TEXT);"
              "CREATE TABLE order_lines(order_ref INTEGER, line_no INTEGER, qty INTEGER,"
              " PRIMARY KEY(order_ref, line_no));"
              "INSERT INTO orders VALUES (1, 'a'), (2, 'b'), (3, 'c');"
              "INSERT INTO order_lines VALUES (1, 1, 5), (1, 2, 5), (2, 1, 5), (3, 1, 5);")
    _, _, out = _build(tmp_path, schema)
    doc = _candidates(out)
    assert "main.order_lines.order_ref->main.orders.id" in {c["id"] for c in doc["candidates"]}
    # `line_no` falls inside any id range: a key member needs its name to back it, not its type alone
    assert not any(".line_no->" in c["id"] for c in doc["candidates"] + doc["low_overlap"])


def _t(name: str, cols: list, schema: str | None = None, **kw):
    from semantic_model.models import Column, Table
    return Table(name=name, schema_name=schema, columns=[Column(**c) for c in cols], **kw)


_ID = {"name": "id", "type": "integer", "primary_key": True}


def test_a_join_naming_its_schema_in_the_table_covers_only_that_schema():
    from semantic_model.models import Relationship
    tables = [_t("orders", [_ID, {"name": "cust_ref", "type": "integer"}], "crm", grain=["id"]),
              _t("orders", [_ID, {"name": "cust_ref", "type": "integer"}], "billing", grain=["id"]),
              _t("customers", [_ID], "crm", grain=["id"])]
    rels = [Relationship(from_table="crm.orders", from_column="cust_ref", to_table="crm.customers",
                         to_column="id", relationship="many_to_one")]
    froms = {(t.schema_name, t.name, c.name) for _, t, c, _, _ in I._candidate_pairs(rels, tables, "sqlite")}
    assert ("billing", "orders", "cust_ref") in froms and ("crm", "orders", "cust_ref") not in froms
    # a join that states no schema still covers the column in every schema
    bare = [Relationship(from_table="orders", from_column="cust_ref", to_table="customers", to_column="id",
                         relationship="many_to_one")]
    assert not any(c.name == "cust_ref" for _, _, c, _, _ in I._candidate_pairs(bare, tables, "sqlite"))


@pytest.mark.parametrize("state", ["rejected", "stale"])
def test_an_unserved_key_or_column_is_never_probed(state):
    code = {"name": "code", "type": "string"}
    tables = [_t("customers", [_ID, {**code, "review_state": state}], grain=["id"], unique_keys=[["code"]]),
              _t("orders", [_ID, {"name": "cust_code", "type": "string"},
                            {"name": "cust_id", "type": "integer", "review_state": state}], grain=["id"])]
    pairs = [(t.name, c.name, u.name, k) for _, t, c, u, k in I._candidate_pairs([], tables, None)]
    assert ("orders", "cust_code", "customers", "code") not in pairs
    assert not any(c == "cust_id" for _, c, _, _ in pairs)
    assert ("orders", "cust_code", "customers", "code") in [
        (t.name, c.name, u.name, k) for _, t, c, u, k in I._candidate_pairs(
            [], [_t("customers", [_ID, code], grain=["id"], unique_keys=[["code"]]), tables[1]], None)]


def test_candidate_pairs_come_lazily_in_a_fixed_priority_order():
    import inspect
    import itertools
    tables = [_t(f"t{i}", [_ID, {"name": f"t{(i + 1) % 6}_id", "type": "integer"},
                           {"name": "ref", "type": "integer"}, {"name": "tag", "type": "string"}],
                 grain=["id"], unique_keys=[["tag"]]) for i in range(6)]
    gen = I._candidate_pairs([], tables, None)
    assert inspect.isgenerator(gen)
    pairs = list(gen)
    key = lambda p: (p[0], p[1].schema_name or "", p[1].name, p[2].name, p[3].schema_name or "",  # noqa: E731
                     p[3].name, p[4])
    assert [key(p) for p in pairs] == sorted(key(p) for p in pairs)
    assert pairs[0][0] == 0 and pairs[-1][0] == 2
    first = [key(p) for p in itertools.islice(I._candidate_pairs([], tables, None), 5)]
    assert first == [key(p) for p in pairs[:5]]


def test_a_refresh_tells_filtered_joins_apart_by_their_literals():
    from semantic_model.models import Relationship
    on = "orders.cust_ref = customers.id AND customers.flag = '{}'"
    rel = lambda text: Relationship(from_table="orders", to_table="customers", on=text,  # noqa: E731
                                    relationship="many_to_one")
    assert not I._same_join(rel(on.format("Y")), rel(on.format("y")))
    assert I._same_join(rel(on.format("Y")), rel("  " + on.format("Y").upper().replace(" AND ", "  and ")))
