"""`sm export --target snowflake` (ACE-168): one Snowflake semantic view per subject area.

The view is checked by `validate_semantic_view` below, which is written from Snowflake's
documentation (the semantic view YAML specification, "How Snowflake validates semantic views" and
the verified query repository page, "Defining derived metrics" and "Snowflake-provided tags"), not
from what the writer emits: a validator derived from the
writer's output would pass whatever the writer does. The negative tests at the end prove each rule
it holds actually rejects a view that breaks it. Checking a view against an account (`verify_only`)
is manual and outside CI.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sqlglot = pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
PKG_SRC = REPO / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import cli  # noqa: E402
from semantic_model import export as X  # noqa: E402
from semantic_model import export_snowflake as S  # noqa: E402  (importing it registers the target)
from sqlglot import exp  # noqa: E402
from sqlglot.errors import ErrorLevel  # noqa: E402

SAMPLE = REPO / "plugins" / "agami" / "samples" / "store" / "model"
AREA = "sales"


# ---------------------------------------------------------------------------
# The validator, from Snowflake's documented rules
# ---------------------------------------------------------------------------

# Keys, from the YAML specification's syntax block (and `sample_values`, from its prose).
_KEYS = {
    "view": {"name", "description", "tables", "relationships", "variables", "metrics",
             "verified_queries", "custom_instructions", "module_custom_instructions",
             "max_staleness", "tags"},
    "table": {"name", "description", "synonyms", "base_table", "primary_key", "unique_keys",
              "dimensions", "time_dimensions", "facts", "metrics", "filters", "tags"},
    "dimensions": {"name", "synonyms", "description", "expr", "data_type", "sample_values",
                   "is_enum", "cortex_search_service", "labels", "tags"},
    "time_dimensions": {"name", "synonyms", "description", "expr", "data_type", "sample_values"},
    "facts": {"name", "synonyms", "description", "access_modifier", "expr", "data_type", "labels",
              "tags"},
    "metrics": {"name", "synonyms", "description", "access_modifier", "expr",
                "non_additive_dimensions", "using_relationships", "tags"},
    "filters": {"name", "synonyms", "description", "expr"},
    "relationship": {"name", "left_table", "right_table", "relationship_columns"},
    "relationship_column": {"left_column", "right_column", "type", "right_range"},
    "verified_query": {"name", "question", "verified_at", "verified_by",
                       "use_as_onboarding_question", "sql"},
    "module_custom_instructions": {"sql_generation", "question_categorization"},
    "non_additive_dimension": {"table", "dimension", "sort_direction", "null_order"},
    "view_metric": {"name", "synonyms", "description", "access_modifier", "expr", "tags"},
    "tag": {"name", "value"},
}
_MEMBERS = ("dimensions", "time_dimensions", "facts", "metrics")
# CERTIFICATION_STATUS's fixed values ("Snowflake-provided tags"); the tag value is a string of at
# most 256 characters (CREATE SEMANTIC VIEW), and an object holds at most 50 tags (tag quotas).
_CERTIFICATION = ("SNOWFLAKE", "TAGS", "CERTIFICATION_STATUS")
_CERTIFICATION_VALUES = {"DRAFT", "TO BE REVIEWED", "IN REVIEW", "CERTIFIED", "REJECTED", "DEPRECATED"}


def _parse(sql: str):
    return sqlglot.parse_one(sql, dialect="snowflake", error_level=ErrorLevel.RAISE)


def _keys(where: str, kind: str, item: dict, problems: list[str]) -> None:
    extra = set(item) - _KEYS[kind]
    if extra:
        problems.append(f"{where}: undocumented keys {sorted(extra)}")


def _check_tags(where: str, item: dict, problems: list[str]) -> None:
    """`tags`: a list of {name: {database, schema, tag}, value} (YAML specification, Tags)."""
    if "tags" not in item:
        return
    tags = item["tags"]
    if not isinstance(tags, list) or not tags or len(tags) > 50:
        problems.append(f"{where}: tags is a list of 1 to 50 tag references")
        return
    for t in tags:
        if not isinstance(t, dict) or set(t) != _KEYS["tag"]:
            problems.append(f"{where}: a tag reference holds exactly name and value")
            continue
        name, value = t["name"], t["value"]
        if not isinstance(name, dict) or set(name) != {"database", "schema", "tag"} or not all(
                isinstance(x, str) and x for x in name.values()):
            problems.append(f"{where}: a tag name is its database, schema and tag")
            continue
        if not isinstance(value, str) or len(value) > 256:
            problems.append(f"{where}: a tag value is a string of at most 256 characters")
        full = tuple(name[k].upper() for k in ("database", "schema", "tag"))
        if full == _CERTIFICATION and value not in _CERTIFICATION_VALUES:
            problems.append(f"{where}: {value!r} is not a CERTIFICATION_STATUS value")


def _physical(table: dict) -> set[str]:
    """Columns the table exposes as a plain column expression: all the validator can know of the
    base table, since it cannot see the warehouse."""
    out = set()
    for s in ("dimensions", "time_dimensions", "facts"):
        for it in table.get(s, []):
            try:
                node = _parse(it["expr"])
            except Exception:
                continue
            if isinstance(node, exp.Column) and not node.table:
                out.add(node.name.casefold())
    return out


def _has_cycle(edges: dict[str, set[str]]) -> bool:
    state: dict[str, int] = {}

    def visit(n: str) -> bool:
        state[n] = 1
        for m in edges.get(n, ()):
            if state.get(m) == 1 or (m not in state and visit(m)):
                return True
        state[n] = 2
        return False

    return any(n not in state and visit(n) for n in list(edges))


def _connected(edges: dict[str, set[str]], a: str, b: str) -> bool:
    und: dict[str, set[str]] = {}
    for x, ys in edges.items():
        for y in ys:
            und.setdefault(x, set()).add(y)
            und.setdefault(y, set()).add(x)
    seen, todo = set(), [a]
    while todo:
        n = todo.pop()
        if n == b:
            return True
        if n not in seen:
            seen.add(n)
            todo.extend(und.get(n, ()))
    return False


def _check_table(t: dict, problems: list[str]) -> None:
    where = f"table {t.get('name')}"
    _keys(where, "table", t, problems)
    _check_tags(where, t, problems)
    bt = t.get("base_table") or {}
    if not ({"database", "schema", "table"} <= set(bt) or set(bt) == {"definition"}):
        problems.append(f"{where}: base_table needs database, schema and table, or a definition")
    names = [it.get("name") for s in _MEMBERS for it in t.get(s, [])]
    if len(names) != len(set(names)):
        problems.append(f"{where}: duplicate member names")
    phys = _physical(t)
    keys = ([t["primary_key"]] if "primary_key" in t else []) + t.get("unique_keys", [])
    for k in keys:
        # Keys are physical base table columns.
        if not k.get("columns") or any(c.casefold() not in phys for c in k["columns"]):
            problems.append(f"{where}: a key names a column that is not a physical column")
    for s in _MEMBERS + ("filters",):
        for it in t.get(s, []):
            _keys(f"{where}.{it.get('name')}", s, it, problems)
            _check_tags(f"{where}.{it.get('name')}", it, problems)
            if not it.get("name") or not it.get("expr"):
                problems.append(f"{where}: an item lacks name or expr")
            labels = it.get("labels", [])
            if labels and (set(labels) != {"filter"} or it.get("data_type") != "BOOLEAN"):
                problems.append(f"{where}.{it['name']}: a filter label needs a BOOLEAN expression")
            if it.get("access_modifier", "public_access") not in ("public_access", "private_access"):
                problems.append(f"{where}.{it['name']}: unknown access_modifier")
            if "is_enum" in it and not isinstance(it["is_enum"], bool):
                problems.append(f"{where}.{it['name']}: is_enum is a boolean")


def _check_expr(view_tables: dict, edges, home: str, it: dict, metric: bool,
                problems: list[str]) -> None:
    where = f"{home}.{it.get('name')}"
    try:
        tree = _parse(it["expr"])
    except Exception:
        problems.append(f"{where}: expr does not parse in Snowflake's dialect")
        return
    if metric and not any(True for _ in tree.find_all(exp.AggFunc)):
        problems.append(f"{where}: a metric that is not derived must aggregate")
    for col in tree.find_all(exp.Column):
        owner = col.table.casefold() if col.table else home
        if owner not in view_tables:
            problems.append(f"{where}: names an undeclared table {col.table}")
            continue
        members = {i["name"].casefold() for s in _MEMBERS for i in view_tables[owner].get(s, [])}
        own = owner == home and col.name.casefold() in _physical(view_tables[owner])
        if not (own or col.name.casefold() in members):
            problems.append(f"{where}: {col.sql()} does not resolve")
        if owner != home and not _connected(edges, home, owner):
            problems.append(f"{where}: reads {owner}, which no relationship relates to {home}")


def validate_semantic_view(doc: dict) -> list[str]:
    """Every documented rule `doc` breaks; empty when it passes."""
    problems: list[str] = []
    _keys("view", "view", doc, problems)
    if not doc.get("name") or not doc.get("tables"):
        return problems + ["a view needs a name and at least one table"]
    tables = {t.get("name", "").casefold(): t for t in doc["tables"]}
    if len(tables) != len(doc["tables"]):
        problems.append("duplicate logical table names")
    for t in doc["tables"]:
        _check_table(t, problems)
    if not any(t.get(s) for t in doc["tables"] for s in _MEMBERS if s != "facts") \
            and not doc.get("metrics"):
        problems.append("a semantic view needs at least one dimension or metric")
    edges: dict[str, set[str]] = {}
    rel_names = [r.get("name") for r in doc.get("relationships", [])]
    if len(rel_names) != len(set(rel_names)):
        problems.append("duplicate relationship names")
    for r in doc.get("relationships", []):
        _keys(f"relationship {r.get('name')}", "relationship", r, problems)
        left, right = (r.get("left_table") or "").casefold(), (r.get("right_table") or "").casefold()
        if left not in tables or right not in tables:
            problems.append(f"relationship {r.get('name')}: names an undeclared table")
            continue
        if left == right:
            problems.append(f"relationship {r['name']}: a table cannot reference itself")
        cols = r.get("relationship_columns") or []
        if not cols:
            problems.append(f"relationship {r['name']}: no relationship_columns")
        for c in cols:
            _keys(f"relationship {r['name']}", "relationship_column", c, problems)
            if c.get("left_column", "").casefold() not in _physical(tables[left]):
                problems.append(f"relationship {r['name']}: left column is not a column of {left}")
        rt = tables[right]
        keys = [rt["primary_key"]] if "primary_key" in rt else []
        keys += rt.get("unique_keys", [])
        want = {c.get("right_column", "").casefold() for c in cols}
        if want not in [{x.casefold() for x in k["columns"]} for k in keys]:
            problems.append(f"relationship {r['name']}: right columns are not a key of {right}")
        edges.setdefault(left, set()).add(right)
    if _has_cycle(edges):
        problems.append("circular relationships")
    seen: set[str] = set()
    items = [*doc["tables"], *doc.get("metrics", []),
             *(i for t in doc["tables"] for s in (*_MEMBERS, "filters") for i in t.get(s, []))]
    for it in items:
        for syn in it.get("synonyms", []):
            if syn.casefold() in seen:
                problems.append(f"synonym {syn!r} is not unique across the view")
            seen.add(syn.casefold())
    for t in doc["tables"]:
        home = t["name"].casefold()
        for s in _MEMBERS:
            for it in t.get(s, []):
                _check_expr(tables, edges, home, it, s == "metrics", problems)
                for nad in it.get("non_additive_dimensions", []):
                    _keys(f"{home}.{it['name']}", "non_additive_dimension", nad, problems)
                    nt = tables.get(nad.get("table", "").casefold(), {})
                    dims = {d["name"] for s2 in ("dimensions", "time_dimensions")
                            for d in nt.get(s2, [])}
                    if nad.get("dimension") not in dims:
                        problems.append(f"{home}.{it['name']}: non-additive dimension unresolved")
                    if nad.get("sort_direction", "ascending") not in ("ascending", "descending"):
                        problems.append(f"{home}.{it['name']}: unknown sort_direction")
    _check_tags("view", doc, problems)
    _check_view_metrics(doc, tables, problems)
    _keys("module_custom_instructions", "module_custom_instructions",
          doc.get("module_custom_instructions", {}), problems)
    vq_names = [q.get("name") for q in doc.get("verified_queries", [])]
    if len(vq_names) != len(set(vq_names)):
        problems.append("duplicate verified query names")
    for q in doc.get("verified_queries", []):
        _keys(f"verified query {q.get('name')}", "verified_query", q, problems)
        if not (q.get("name") and q.get("question") and q.get("sql")):
            problems.append("a verified query needs name, question and sql")
            continue
        at = q.get("verified_at", 0)
        if isinstance(at, bool) or not isinstance(at, int) or at < 0:
            problems.append(f"verified query {q['name']}: verified_at is seconds since the epoch")
        if not isinstance(q.get("use_as_onboarding_question", False), bool):
            problems.append(f"verified query {q['name']}: use_as_onboarding_question is a boolean")
        try:
            tree = _parse(q["sql"])
        except Exception:
            problems.append(f"verified query {q['name']}: sql does not parse")
            continue
        ctes = {c.alias_or_name.casefold() for c in tree.find_all(exp.CTE)}
        for tb in tree.find_all(exp.Table):
            # Verified SQL reads logical tables, prefixed with two underscores.
            name = tb.name.casefold()
            if name not in ctes and not (name.startswith("__") and name[2:] in tables):
                problems.append(f"verified query {q['name']}: {tb.name} is not a logical table")
        _check_vq_columns(tables, q["name"], tree, problems)
    return problems


def _check_view_metrics(doc: dict, tables: dict, problems: list[str]) -> None:
    """Derived metrics: no table, a name no regular metric has, and an expression of "aggregations
    of dimensions and facts defined in any logical table", "scalar expressions of metrics" and
    other derived metrics (Defining derived metrics)."""
    regular = {m["name"].casefold() for t in doc["tables"] for m in t.get("metrics", [])}
    derived = [m.get("name", "") for m in doc.get("metrics", [])]
    if len(derived) != len(set(derived)):
        problems.append("duplicate derived metric names")
    for m in doc.get("metrics", []):
        where = f"derived metric {m.get('name')}"
        _keys(where, "view_metric", m, problems)
        _check_tags(where, m, problems)
        if m.get("name", "").casefold() in regular:
            problems.append(f"{where}: a derived metric and a regular metric share a name")
        try:
            tree = _parse(m.get("expr") or "")
        except Exception:
            problems.append(f"{where}: expr does not parse in Snowflake's dialect")
            continue
        for col in tree.find_all(exp.Column):
            if not col.table:
                if col.name not in derived:
                    problems.append(f"{where}: {col.name} names no derived metric")
                continue
            t = tables.get(col.table.casefold())
            sections = {s for s in _MEMBERS for i in (t or {}).get(s, [])
                        if i["name"].casefold() == col.name.casefold()}
            if not sections:
                problems.append(f"{where}: {col.sql()} does not resolve")
            elif sections != {"metrics"} and col.find_ancestor(exp.AggFunc) is None:
                problems.append(f"{where}: {col.sql()} is a dimension or fact read outside an "
                                "aggregate")


def _check_vq_columns(tables: dict, name: str, tree, problems: list[str]) -> None:
    """Verified SQL names the logical columns of the tables it reads, never a base table's own
    column, and qualifies a column only with a name its FROM lists (the VQR page)."""
    def members(t: str) -> set[str]:
        return {i["name"].casefold() for s in _MEMBERS for i in tables[t].get(s, [])}

    read: dict[str, str] = {}
    for tb in tree.find_all(exp.Table):
        n = tb.name.casefold()
        if n.startswith("__") and n[2:] in tables:
            read[tb.alias.casefold() if tb.alias else n] = n[2:]
    derived = {x.alias.casefold() for x in tree.find_all(exp.Subquery, exp.CTE) if x.alias}
    named = {a.alias.casefold() for a in tree.find_all(exp.Alias)}
    readable = set().union(*(members(t) for t in read.values())) if read else set()
    for col in tree.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            continue
        q, c = col.table.casefold(), col.name.casefold()
        if q in read and c not in members(read[q]):
            problems.append(f"verified query {name}: {col.table}.{col.name} is not a logical column")
        elif q and q not in read and q not in derived:
            problems.append(f"verified query {name}: {col.table} is no name its FROM lists")
        elif not q and c not in readable | named:
            problems.append(f"verified query {name}: {col.name} is not a logical column")


def emitted_sql(doc: dict) -> list[str]:
    """Every `expr` and verified query `sql` in the view."""
    out = [it["expr"] for t in doc["tables"] for s in (*_MEMBERS, "filters") for it in t.get(s, [])]
    out += [m["expr"] for m in doc.get("metrics", [])]
    return out + [q["sql"] for q in doc.get("verified_queries", [])]


# ---------------------------------------------------------------------------
# Running the export
# ---------------------------------------------------------------------------


def _sm(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["export", *argv])
    return rc, buf.getvalue()


def _export(root: Path, out: Path, *flags: str) -> dict:
    rc, text = _sm(str(root), "--target", "snowflake", "--location", "DEMO.SALES_DATA",
                   "--out", str(out), *flags)
    assert rc == 0, text
    return json.loads(text)


def _view(out: Path, stem: str) -> dict:
    return yaml.safe_load((out / "snowflake" / f"{stem}.semantic_view.yaml").read_text("utf-8"))


def _losses(report: dict) -> dict[tuple[str, str], list[tuple[str, str]]]:
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for x in report["targets"]["snowflake"]["losses"]:
        out.setdefault((x["kind"], x["name"]), []).append((x["outcome"], x["reason"]))
    return out


def _table(doc: dict, name: str) -> dict:
    return next(t for t in doc["tables"] if t["name"] == name)


def _member(doc: dict, table: str, name: str) -> dict:
    t = _table(doc, table)
    return next(i for s in _MEMBERS for i in t.get(s, []) if i["name"] == name)


# ---------------------------------------------------------------------------
# The bundled sample
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("sample")
    return out, _export(SAMPLE, out)


def test_the_sample_writes_one_view_file_and_one_sql_file(sample):
    out, report = sample
    files = sorted(p.name for p in (out / "snowflake").iterdir())
    # The agent beside them is ACE-169's.
    assert files == ["agami_example.agent.sql", "agami_example.semantic_view.yaml", "agami_example.sql"]
    assert report["targets"]["snowflake"]["files"] == [f"snowflake/{f}" for f in files]


def test_the_sample_view_passes_the_documented_rules(sample):
    out, _ = sample
    doc = _view(out, "agami_example")
    assert validate_semantic_view(doc) == []
    assert doc["name"] == S.semantic_view_name(_fake_view("agami-example")) == "agami_example"


def test_every_emitted_expression_parses_in_the_snowflake_dialect(sample):
    out, _ = sample
    sql = emitted_sql(_view(out, "agami_example"))
    assert len(sql) > 50
    for s in sql:
        _parse(s)


def test_the_samples_eight_metrics_carry_their_snowflake_bindings_expanded(sample):
    out, _ = sample
    doc = _view(out, "agami_example")
    metrics = {m["name"]: m["expr"] for t in doc["tables"] for m in t.get("metrics", [])}
    assert len(metrics) == 8
    assert metrics["average_order_value"] == "(SUM(orders.total_amount)) / (COUNT(DISTINCT orders.id))"
    assert "{" not in "".join(metrics.values())
    mrr = _member(doc, "subscriptions", "monthly_recurring_revenue")
    assert "plans.monthly_price" in mrr["expr"] and "MRR" in mrr["synonyms"]


def test_on_the_sample_a_default_filter_is_reported_not_written(sample):
    out, report = sample
    assert _losses(report)[("default_filter", "orders#1")] == [("dropped", "sql_not_for_target")]
    doc = _view(out, "agami_example")
    assert not any(i.get("labels") for t in doc["tables"] for i in t.get("dimensions", []))


def test_choice_fields_become_stored_sample_values_with_their_labels_described(sample):
    out, _ = sample
    channel = _member(_view(out, "agami_example"), "orders", "channel")
    assert channel["sample_values"] == ["web", "mobile", "store", "partner"]
    assert channel["is_enum"] is True and "store = In-store" in channel["description"]


def test_the_sql_file_creates_the_view_in_the_location_schema(sample):
    out, _ = sample
    sql = (out / "snowflake" / "agami_example.sql").read_text("utf-8")
    yml = (out / "snowflake" / "agami_example.semantic_view.yaml").read_text("utf-8")
    assert "CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML(\n  'DEMO.SALES_DATA',\n  $$\n" in sql
    assert sql.split("$$\n", 1)[1].rsplit("$$", 1)[0] == yml
    assert "DEMO.SALES_DATA.agami_example" in sql


def test_two_runs_are_byte_identical(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _export(SAMPLE, a), _export(SAMPLE, b)
    for name in ("agami_example.semantic_view.yaml", "agami_example.sql"):
        assert (a / "snowflake" / name).read_bytes() == (b / "snowflake" / name).read_bytes()


def test_without_a_location_the_export_is_refused(tmp_path):
    rc, text = _sm(str(SAMPLE), "--target", "snowflake", "--out", str(tmp_path / "o"))
    assert rc == 2 and "--location" in text and not (tmp_path / "o").exists()


# ---------------------------------------------------------------------------
# A synthetic Snowflake model: acme's SALES_DATA, with every rule broken once
# ---------------------------------------------------------------------------


def _col(name, type_="integer", **kw):
    return {"name": name, "type": type_, **kw}


def _metric(name, sql, **kw):
    return {"name": name, "calculation": f"The {name}.", "bindings": {"Snowflake": sql},
            "source_tables": kw.pop("source_tables", ["orders"]), "review_state": "approved", **kw}


def _rel(frm, to, fc=None, tc=None, **kw):
    r = {"from_table": frm, "to_table": to, "relationship": kw.pop("card", "many_to_one"),
         "review_state": "approved", **kw}
    if fc:
        r.update(from_column=fc, to_column=tc)
    return r


def _write_model(root: Path) -> Path:
    def put(rel, doc):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    put("datasource.yaml", {
        "datasource": "acme", "description": "Acme demo sales.",
        "storage_connections": [{"name": "acme_db", "storage_type": "Snowflake", "storage_config": {}}],
        "subject_areas": [f"subject_areas/{AREA}"],
        "key_terminology": {"GMV": "gross merchandise value"},
        "ambiguities": [{"name": "which amount", "when": "a question says amount",
                         "applies_to": ["orders"],
                         "options": [{"label": "net", "how": "use orders.amount", "default": True},
                                     {"label": "gross", "how": "add tax"}]}]})
    put(f"subject_areas/{AREA}/subject_area.yaml", {"name": AREA, "description": "Orders."})
    tables = {
        "orders": {"grain": ["id"], "description": "Orders; amounts in $$ (dollars).",
                   "default_filters": ["{alias}.status != 'void'"],
                   "columns": [_col("id"), _col("customer_id"), _col("rep_id"),
                               _col("amount", "decimal", aggregation="additive", unit="USD"),
                               _col("balance", "decimal", aggregation="additive"),
                               _col("status", "string", choice_field={"open": "Open", "void": "Void"}),
                               _col("placed_at", "timestamp"),
                               _col("raw_code", "string", value_transform="TRIM(raw_code)")]},
        "customers": {"grain": ["id"], "columns": [_col("id"), _col("region_id"), _col("name", "string")]},
        "regions": {"grain": ["id"], "columns": [_col("id"), _col("hq_customer_id"), _col("top_order_id")]},
        "employees": {"grain": ["id"], "columns": [_col("id"), _col("manager_id")]},
        "tags": {"grain": ["id"], "columns": [_col("id"), _col("tag", "string")]},
        "accounts": {"grain": ["id", "ssn"],
                     "columns": [_col("id"), _col("ssn", "string", sensitive=True)]},
    }
    for name, t in tables.items():
        put(f"subject_areas/{AREA}/tables/{name}.yaml", {"name": name, "schema": "SALES_DATA", **t})
    put(f"subject_areas/{AREA}/tables/reps.yaml", {
        "name": "reps", "schema": "SALES_DATA", "source_type": "sql", "grain": ["id"],
        "sql": "SELECT id, name FROM SALES_DATA.REPS_RAW",
        "columns": [_col("id"), _col("name", "string")]})
    put(f"subject_areas/{AREA}/relationships.yaml", [
        _rel("orders", "customers", "customer_id", "id"),
        _rel("customers", "regions", "region_id", "id"),
        _rel("regions", "customers", "hq_customer_id", "id"),    # closes a cycle directly
        _rel("regions", "orders", "top_order_id", "id"),         # closes one transitively
        _rel("employees", "employees", "manager_id", "id"),      # a self-join
        _rel("orders", "tags", "status", "tag"),                 # not the other table's key
        _rel("orders", "reps", on="orders.rep_id = reps.id"),
    ])
    put(f"subject_areas/{AREA}/entities/customer.yaml", {
        "name": "customer", "plural": "customers", "other_names": ["client"],
        "review_state": "approved", "maps_to": [{"table": "customers", "column": "id", "primary": True}]})
    for m in [
        _metric("revenue", "SUM(orders.amount)", other_names=["sales", "turnover"]),
        _metric("order count", "COUNT(DISTINCT orders.id)", other_names=["sales", "volume"]),
        _metric("average order value", "{revenue} / {order count}",
                base_metrics=["revenue", "order count"]),
        _metric("region count", "COUNT(DISTINCT regions.id)", source_tables=["regions"]),
        _metric("doubled amount", "orders.amount * 2"),
        _metric("staff and sales", "SUM(employees.id) + SUM(orders.amount)",
                source_tables=["employees", "orders"], other_names=["headcount and sales"]),
        _metric("staff plus amount", "SUM(employees.id) + orders.amount",
                source_tables=["employees", "orders"]),
        _metric("ghost total", "SUM(orders.nowhere)"),
        _metric("Amount", "SUM(orders.amount)"),  # collides with the fact
        _metric("closing balance", "SUM(orders.balance)", non_additive_dimensions=["placed_at"],
                semi_additive_agg="last"),
        _metric("average balance", "SUM(orders.balance)", non_additive_dimensions=["time"],
                semi_additive_agg="average"),
    ]:
        put(f"subject_areas/{AREA}/metrics/{m['name'].replace(' ', '_')}.yaml", m)
    put(f"prompt_examples/{AREA}/examples.yaml", [
        {"question": "Revenue by region?", "status": "confirmed",
         "sql": "SELECT r.id, SUM(o.amount) AS revenue FROM SALES_DATA.orders AS o JOIN customers AS c "
                "ON o.customer_id = c.id JOIN regions AS r ON c.region_id = r.id GROUP BY r.id"},
        {"question": "What changed?", "status": "confirmed"},
        {"question": "Audit rows?", "status": "confirmed", "sql": "SELECT COUNT(*) FROM audit_log"},
    ])
    return root


@pytest.fixture(scope="module")
def acme(tmp_path_factory) -> tuple[Path, dict, dict]:
    root = _write_model(tmp_path_factory.mktemp("art") / "acme")
    out = tmp_path_factory.mktemp("acme_out")
    report = _export(root, out)
    return out, report, _view(out, AREA)


def test_the_synthetic_view_passes_the_documented_rules_after_its_drops(acme):
    _, _, doc = acme
    assert validate_semantic_view(doc) == []
    for s in emitted_sql(doc):
        _parse(s)


@pytest.mark.parametrize("name,rule", [
    ("regions->customers", "circular"),
    ("regions->orders", "circular"),
    ("orders->tags", "primary or unique key"),
])
def test_each_broken_relationship_rule_is_a_target_rule_drop(acme, name, rule):
    _, report, doc = acme
    hit = [x for x in report["targets"]["snowflake"]["losses"]
           if x["kind"] == "relationship" and x["name"] == name]
    assert [(x["outcome"], x["reason"]) for x in hit] == [("dropped", "target_rule")]
    assert rule in hit[0]["detail"]
    kept = {(r["left_table"], r["right_table"]) for r in doc["relationships"]}
    # The self-join is kept through a role-playing table (see its own test).
    assert kept == {("orders", "customers"), ("customers", "regions"), ("orders", "reps"),
                    ("employees", "employees_manager")}


def test_an_on_join_of_column_equalities_becomes_relationship_columns(acme):
    _, _, doc = acme
    rel = next(r for r in doc["relationships"] if r["right_table"] == "reps")
    assert rel["relationship_columns"] == [{"left_column": "rep_id", "right_column": "id"}]
    assert _table(doc, "reps")["base_table"] == {"definition": "SELECT id, name FROM SALES_DATA.REPS_RAW"}


def test_metrics_that_break_a_rule_are_dropped_and_say_which(acme):
    _, report, doc = acme
    losses = {x["name"]: x for x in report["targets"]["snowflake"]["losses"] if x["kind"] == "metric"
              and x["outcome"] == "dropped"}
    assert losses["doubled amount"]["reason"] == "target_rule"
    assert "must aggregate" in losses["doubled amount"]["detail"]
    # A column read outside an aggregate cannot be a derived metric either, so it stays dropped.
    assert "related" in losses["staff plus amount"]["detail"]
    assert "staff and sales" not in losses   # a derived metric now: see its own test
    assert "orders.nowhere" in losses["ghost total"]["detail"]
    assert losses["Amount"]["reason"] == "name_collision"
    region = _member(doc, "regions", "region_count")
    assert region["expr"] == "COUNT(DISTINCT regions.id)"


def test_synonyms_stay_unique_across_the_view(acme):
    _, report, doc = acme
    # Items are taken in name order, so the earlier metric keeps the shared synonym.
    assert _member(doc, "orders", "order_count")["synonyms"] == ["sales", "volume"]
    assert _member(doc, "orders", "revenue")["synonyms"] == ["turnover"]
    assert ("degraded", "target_rule") in _losses(report)[("metric", "revenue")]
    assert _table(doc, "customers")["synonyms"] == ["customer", "client"]


def test_a_snowflake_default_filter_becomes_a_boolean_filter_dimension(acme):
    _, _, doc = acme
    f = _member(doc, "orders", "default_filter_1")
    assert f["expr"] == "orders.status != 'void'"
    assert f["data_type"] == "BOOLEAN" and f["labels"] == ["filter"]
    # Descriptive, as in agami, in the words every target uses: not "unless it says otherwise".
    assert f["description"] == X.DEFAULT_FILTER_NOTE


def test_columns_map_by_type_and_aggregation(acme):
    _, report, doc = acme
    orders = _table(doc, "orders")
    assert {i["name"] for i in orders["facts"]} == {"amount", "balance"}
    assert [i["name"] for i in orders["time_dimensions"]] == ["placed_at"]
    assert "Unit: USD." in _member(doc, "orders", "amount")["description"]
    assert ("degraded", "detail_lost") in _losses(report)[("column", "orders.raw_code")]


def test_non_additive_dimensions_map_and_what_has_no_counterpart_is_reported(acme):
    _, report, doc = acme
    assert _member(doc, "orders", "closing_balance")["non_additive_dimensions"] == [
        {"table": "orders", "dimension": "placed_at", "sort_direction": "ascending"}]
    avg = _member(doc, "orders", "average_balance")["non_additive_dimensions"]
    assert avg == [{"table": "orders", "dimension": "placed_at", "sort_direction": "ascending"}]
    assert ("degraded", "detail_lost") in _losses(report)[("metric", "average balance")]


def test_examples_become_verified_queries_over_logical_tables(acme):
    _, report, doc = acme
    (q,) = doc["verified_queries"]
    assert q["name"] == "revenue_by_region" and q["question"] == "Revenue by region?"
    assert "FROM __orders AS o JOIN __customers AS c" in q["sql"] and "verified_by" not in q
    # Their questions travel as the agent's sample questions (ACE-169): one without SQL loses
    # nothing, and one whose SQL cannot be a verified query loses only that.
    losses = _losses(report)
    assert ("example", "What changed?") not in losses
    assert losses[("example", "Audit rows?")] == [("degraded", "no_home_in_target")]


def test_ambiguities_terms_and_entities_become_text(acme):
    _, report, doc = acme
    mci = doc["module_custom_instructions"]
    assert "which amount" in mci["sql_generation"] and "net: use orders.amount" in mci["sql_generation"]
    assert "ask which is meant: net, gross" in mci["question_categorization"]
    assert "GMV: gross merchandise value" in doc["description"]
    assert "Entity customer" in _table(doc, "customers")["description"]
    assert _losses(report)[("ambiguity", "which amount")] == [("degraded", "carried_as_text")]


def test_part_of_a_composite_key_is_not_written_as_a_key(acme):
    _, _, doc = acme
    accounts = _table(doc, "accounts")
    assert "primary_key" not in accounts and accounts["dimensions"][0]["name"] == "id"


def test_a_view_holding_dollar_quotes_falls_back_to_an_escaped_literal(acme):
    out, _, _ = acme
    sql = (out / "snowflake" / f"{AREA}.sql").read_text("utf-8")
    yml = (out / "snowflake" / f"{AREA}.semantic_view.yaml").read_text("utf-8")
    literal = sql.split("',\n  '", 1)[1].rsplit("'\n);", 1)[0]
    assert literal.replace("''", "'").replace("\\\\", "\\") == yml


# ---------------------------------------------------------------------------
# Units and the seam ACE-169 builds on
# ---------------------------------------------------------------------------


def _fake_view(area: str, **kw) -> X.ExportView:
    base = dict(target="snowflake", engines=("Snowflake",), source_engine="Snowflake",
                sql_engine="Snowflake", datasource="acme", description="", area=area,
                area_description="", tables=(), metrics=(), relationships=(), entities=(),
                examples=(), golden=(), ambiguities=(), terminology=())
    return X.ExportView(**{**base, **kw})


@pytest.mark.parametrize("text,name", [("agami-example", "agami_example"), ("Order Count", "order_count"),
                                       ("2024 sales", "_2024_sales"), ("--", "unnamed")])
def test_the_naming_rule(text, name):
    assert S.snowflake_name(text) == name


def test_the_seam_names_the_view_and_its_place():
    opts = X.ExportOptions(targets=("snowflake",), location=X.Location("DEMO", "my schema"))
    view = _fake_view("Sales Ops")
    assert S.semantic_view_fqn(view, opts) == 'DEMO."my schema".sales_ops'
    assert S.file_stem(view) == "sales_ops"
    with pytest.raises(X.ExportRefused):
        S.view_location(X.ExportOptions(targets=("snowflake",)))


def test_a_view_with_nothing_to_hold_writes_no_file_and_says_why():
    opts = X.ExportOptions(targets=("snowflake",), location=X.Location("DEMO", "SALES_DATA"))
    out = S.write_snowflake(_fake_view("empty"), opts)
    assert out.files == {}
    assert [(x.kind, x.reason) for x in out.losses] == [("subject_area", "target_rule")]


def test_the_target_is_registered_for_the_snowflake_engine():
    assert X.TARGETS["snowflake"].engines == ("Snowflake",)
    assert X.TARGETS["snowflake"].write is S.write_snowflake


# ---------------------------------------------------------------------------
# The validator rejects what breaks each rule it holds
# ---------------------------------------------------------------------------


def _good() -> dict:
    return {"name": "v", "tables": [
        {"name": "a", "base_table": {"database": "D", "schema": "S", "table": "A"},
         "primary_key": {"columns": ["id"]},
         "dimensions": [{"name": "id", "expr": "id"}, {"name": "b_id", "expr": "b_id"}],
         "metrics": [{"name": "n", "expr": "COUNT(a.id)", "synonyms": ["count"]}]},
        {"name": "b", "base_table": {"database": "D", "schema": "S", "table": "B"},
         "primary_key": {"columns": ["id"]},
         "dimensions": [{"name": "id", "expr": "id"}, {"name": "a_id", "expr": "a_id"}]}],
        "relationships": [{"name": "a_to_b", "left_table": "a", "right_table": "b",
                           "relationship_columns": [{"left_column": "b_id", "right_column": "id"}]}]}


def _break(fn) -> list[str]:
    doc = _good()
    fn(doc)
    return validate_semantic_view(doc)


def test_the_validator_passes_a_documented_view():
    assert validate_semantic_view(_good()) == []


@pytest.mark.parametrize("fn,needle", [
    (lambda d: d["relationships"].append({"name": "b_to_a", "left_table": "b", "right_table": "a",
     "relationship_columns": [{"left_column": "a_id", "right_column": "id"}]}), "circular"),
    (lambda d: d["relationships"].__setitem__(0, {**d["relationships"][0], "right_table": "a"}),
     "itself"),
    (lambda d: d["relationships"][0]["relationship_columns"][0].update(right_column="a_id"), "key"),
    (lambda d: d["relationships"][0].update(left_table="c"), "undeclared"),
    (lambda d: d["tables"][1].update(synonyms=["COUNT"]), "unique"),
    (lambda d: d["tables"][0]["metrics"][0].update(expr="a.id + 1"), "aggregate"),
    (lambda d: d["tables"][0]["metrics"][0].update(expr="SUM(c.id)"), "undeclared"),
    (lambda d: d["tables"][0]["metrics"][0].update(expr="SUM(a.nope)"), "resolve"),
    (lambda d: d["tables"][0]["metrics"][0].update(expr="SUM(("), "parse"),
    (lambda d: d["tables"][0].update(primary_key={"columns": ["zz"]}), "physical"),
    (lambda d: d["tables"][0].update(joins=[]), "undocumented"),
    (lambda d: [t.pop(s, None) for t in d["tables"] for s in ("dimensions", "metrics")],
     "at least one"),
    (lambda d: d["tables"][0]["dimensions"][0].update(labels=["filter"]), "BOOLEAN"),
    (lambda d: d.update(verified_queries=[{"name": "q", "question": "q?", "sql": "SELECT 1 FROM A"}]),
     "logical table"),
    (lambda d: d.update(verified_queries=[{"name": "q", "question": "q?",
                                           "sql": "SELECT a.id FROM __a"}]), "FROM lists"),
    (lambda d: d.update(verified_queries=[{"name": "q", "question": "q?",
                                           "sql": "SELECT x.nope FROM __a AS x"}]), "logical column"),
])
def test_the_validator_rejects_each_broken_rule(fn, needle):
    assert any(needle in p for p in _break(fn)), _break(fn)


def test_the_validator_rejects_a_read_of_an_unrelated_table():
    def fn(d):
        d["relationships"] = []
        d["tables"][0]["metrics"][0]["expr"] = "SUM(b.id)"
    assert any("relates" in p for p in _break(fn))


# ---------------------------------------------------------------------------
# Edge shapes, straight from a hand-built view
# ---------------------------------------------------------------------------


def _edge_view(**kw) -> X.ExportView:
    from semantic_model import models as M

    def table(name, cols, **t):
        return X.ExportTable(M.Table(name=name, columns=[M.Column(**c) for c in cols], **t),
                             X.PhysicalName(None, "SALES_DATA", name))

    def rel(name, frm, to, card="many_to_one", **r):
        return X.ExportRelationship(name, M.Relationship(from_table=frm, to_table=to,
                                                         relationship=card, **r))

    def metric(name, sql, **m):
        return X.ExportMetric(M.Metric(name=name, calculation=f"The {name}.", **m), {"Snowflake": sql})

    opt = [M.AmbiguityOption(label="paid", how="status = 'paid'", default=True),
           M.AmbiguityOption(label="all", how="no filter")]
    return _fake_view(AREA, **{**dict(
        tables=(
            table("items", [_col("id"), _col("order_id"), _col("Total Amount", "decimal"),
                            _col("sku", "string")], grain=["id"], unique_keys=[["sku"]],
                  default_filters=["{alias}.id = ("]),
            table("orders", [_col("id"), _col("status", "string")], grain=["id"]),
            table("Orders", [_col("id")]),
            table("profiles", [_col("order_id"), _col("note", "string")]),
            table("staged", [_col("id")], source_type="sql", sql="SELECT 1 AS id"),
        ),
        relationships=(
            rel("orders->items", "orders", "items", "one_to_many", from_column="id",
                to_column="order_id"),
            rel("orders->items#2", "orders", "items", "one_to_many", on="orders.id > items.order_id"),
            rel("orders->items#3", "orders", "items", on="orders.id = other.id"),
            rel("orders->ghost", "orders", "ghost", from_column="id", to_column="id"),
            rel("items->orders", "items", "orders", from_column="nope", to_column="id"),
            rel("orders_to_items", "items", "orders", from_column="order_id", to_column="id"),
            rel("profiles->orders", "profiles", "orders", "one_to_one", from_column="order_id",
                to_column="id"),
        ),
        metrics=(
            metric("second order", "AVG(SUM(items.id))", inner_grain=["order_id"]),
            metric("plain", "SUM(sku)", primary_table="items", non_additive_dimensions=["nope", "time"]),
            metric("by status", "SUM(items.id)", primary_table="items",
                   non_additive_dimensions=["status"], semi_additive_agg="first"),
        ),
        entities=(M.Entity(name="ghost", maps_to=[M.EntityMapping(table="ghost", column="id")]),),
        examples=(X.ExportQuestion("example", "q", "q?", "SELECT FROM (("),
                  X.ExportQuestion("example", "r", "r?", "WITH x AS (SELECT 1 AS n) SELECT n FROM x"),
                  X.ExportQuestion("example", "R", "R?", "SELECT 1 FROM items")),
        ambiguities=(M.Ambiguity(name="which", when="a question says sales",
                                 behaviour="answer_and_offer", options=opt),),
    ), **kw})


def test_edge_shapes_each_take_their_documented_outcome():
    opts = X.ExportOptions(targets=("snowflake",), location=X.Location("DEMO", "SALES_DATA"))
    doc, losses = S.build_semantic_view(_edge_view(), opts)
    got = {(x.kind, x.name): (x.outcome, x.reason) for x in losses}
    assert validate_semantic_view(doc) == []
    assert got[("table", "Orders")] == ("dropped", "name_collision")
    assert got[("default_filter", "items#1")] == ("dropped", "unparseable_for_target")
    assert got[("relationship", "orders->items#2")] == ("dropped", "no_home_in_target")
    assert got[("relationship", "orders->items#3")] == ("dropped", "no_home_in_target")
    assert got[("relationship", "orders->ghost")] == ("dropped", "no_home_in_target")
    assert got[("relationship", "items->orders")] == ("dropped", "no_home_in_target")
    assert got[("relationship", "orders_to_items")] == ("dropped", "name_collision")
    assert got[("metric", "second order")] == ("dropped", "no_home_in_target")
    assert got[("metric", "plain")] == ("degraded", "detail_lost")
    assert got[("entity", "ghost")] == ("dropped", "no_home_in_target")
    assert got[("example", "q")] == ("degraded", "unparseable_for_target")
    assert got[("example", "R")] == ("degraded", "name_collision")
    rels = {r["name"]: r for r in doc["relationships"]}
    assert rels["orders_to_items"]["left_table"] == "items"          # one-to-many turns round
    assert rels["profiles_to_orders"]["left_table"] == "profiles"
    assert _table(doc, "items")["unique_keys"] == [{"columns": ["sku"]}]
    assert _member(doc, "items", "total_amount")["expr"] == '"Total Amount"'
    assert _member(doc, "items", "by_status")["non_additive_dimensions"] == [
        {"table": "orders", "dimension": "status", "sort_direction": "descending"}]
    assert [q["sql"] for q in doc["verified_queries"]] == ["WITH x AS (SELECT 1 AS n) SELECT n FROM x"]
    assert "answer with paid, say so" in doc["module_custom_instructions"]["question_categorization"]
    assert _table(doc, "staged")["base_table"] == {"definition": "SELECT 1 AS id"}


def test_a_query_defined_table_needs_the_snowflake_engine():
    opts = X.ExportOptions(targets=("snowflake",), location=X.Location("DEMO", "SALES_DATA"))
    _, losses = S.build_semantic_view(_edge_view(sql_engine=None, source_engine="SQLite"), opts)
    assert ("table", "staged", "sql_not_for_target") in {(x.kind, x.name, x.reason) for x in losses}


# ---------------------------------------------------------------------------
# Verified queries read the view's logical names, or travel as their question only
# ---------------------------------------------------------------------------


def _vq_view(sqls: dict[str, str]) -> X.ExportView:
    from semantic_model import models as M

    def table(name, cols):
        return X.ExportTable(M.Table(name=name, columns=[M.Column(**c) for c in cols], grain=["id"]),
                             X.PhysicalName(None, "SALES_DATA", name))

    orders = table("orders", [_col("id"), _col("customer_id"), _col("status", "string"),
                              _col("total_amount", "decimal", aggregation="additive"),
                              _col("Net Value", "decimal", aggregation="additive")])
    customers = table("customers", [_col("id"), _col("name", "string")])
    rel = X.ExportRelationship("orders->customers", M.Relationship(
        from_table="orders", to_table="customers", relationship="many_to_one", from_column="customer_id",
        to_column="id"))
    return _fake_view(AREA, tables=(customers, orders), relationships=(rel,),
                      examples=tuple(X.ExportQuestion("example", k, f"{k}?", v) for k, v in sqls.items()))


_VQ_REWRITES = {
    "qualified": ("SELECT orders.id, SUM(orders.total_amount) FROM orders GROUP BY orders.id",
                  "SELECT __orders.id, SUM(__orders.total_amount) FROM __orders GROUP BY __orders.id"),
    "schema qualified": ("SELECT SALES_DATA.orders.id FROM SALES_DATA.orders",
                         "SELECT __orders.id FROM __orders"),
    "aliased": ("SELECT o.id, c.name FROM orders o JOIN customers AS c ON o.customer_id = c.id",
                "SELECT o.id, c.name FROM __orders o JOIN __customers AS c ON o.customer_id = c.id"),
    "twice": ("SELECT a.id, b.id FROM orders a JOIN orders b ON a.customer_id = b.customer_id",
              "SELECT a.id, b.id FROM __orders a JOIN __orders b ON a.customer_id = b.customer_id"),
    "shadowed": ("WITH orders AS (SELECT id FROM customers) SELECT orders.id FROM orders",
                 "WITH orders AS (SELECT id FROM __customers) SELECT orders.id FROM orders"),
    "subquery": ("SELECT x.n FROM (SELECT o.id AS n FROM orders AS o) AS x",
                 "SELECT x.n FROM (SELECT o.id AS n FROM __orders AS o) AS x"),
    # Quoted in the case Snowflake stores it: `"orders"` would be another, lower-case table.
    "quoted": ('SELECT "ORDERS"."Net Value" FROM "ORDERS"', "SELECT __orders.net_value FROM __orders"),
    "renamed": ('SELECT status, SUM("Net Value") AS v FROM orders GROUP BY status ORDER BY v',
                "SELECT status, SUM(net_value) AS v FROM __orders GROUP BY status ORDER BY v"),
    "correlated": ("SELECT id FROM orders WHERE EXISTS (SELECT 1 FROM customers c "
                   "WHERE c.id = orders.customer_id)",
                   "SELECT id FROM __orders WHERE EXISTS (SELECT 1 FROM __customers c "
                   "WHERE c.id = __orders.customer_id)"),
}
_VQ_DEGRADED = {
    "ambiguous": ("SELECT id FROM orders JOIN customers ON customer_id = customers.id", "target_rule"),
    "missing column": ("SELECT orders.nope FROM orders", "no_home_in_target"),
    "star": ("SELECT * FROM orders", "target_rule"),
    "unknown qualifier": ("SELECT z.id FROM orders", "target_rule"),
    "renamed output": ('SELECT x."Net Value" FROM (SELECT "Net Value" FROM orders) AS x', "target_rule"),
}


def _vq_build():
    opts = X.ExportOptions(targets=("snowflake",), location=X.Location("DEMO", "SALES_DATA"))
    sqls = {k: v[0] for k, v in {**_VQ_REWRITES, **_VQ_DEGRADED}.items()}
    return S.build_semantic_view(_vq_view(sqls), opts)


@pytest.mark.parametrize("name", sorted(_VQ_REWRITES))
def test_a_verified_query_is_rewritten_onto_logical_tables_and_columns(name):
    doc, _ = _vq_build()
    got = {q["question"]: q["sql"] for q in doc["verified_queries"]}
    assert got[f"{name}?"] == _VQ_REWRITES[name][1]


@pytest.mark.parametrize("name", sorted(_VQ_DEGRADED))
def test_a_verified_query_that_cannot_be_rewritten_safely_keeps_its_question_only(name):
    doc, losses = _vq_build()
    assert f"{name}?" not in {q["question"] for q in doc["verified_queries"]}
    got = {(x.kind, x.name): (x.outcome, x.reason) for x in losses}
    assert got[("example", name)] == ("degraded", _VQ_DEGRADED[name][1])


def test_a_relationships_join_type_is_reported_as_detail_lost():
    # A semantic view relationship holds no join type, as a metric view and Ossie hold none.
    _, losses = _vq_build()
    (loss,) = [x for x in losses if (x.kind, x.name) == ("relationship", "orders->customers")]
    assert (loss.outcome, loss.reason) == ("degraded", "detail_lost") and "LEFT" in loss.detail


def test_two_areas_with_one_view_name_write_one_file_name():
    # `a-b` and `a_b` create the same view, so they write the same files and the core refuses the
    # run, rather than one view silently replacing the other when both are applied.
    import dataclasses

    opts = X.ExportOptions(targets=("snowflake",), location=X.Location("DEMO", "SALES_DATA"))
    view = _vq_view({})
    a, b = (S.write_snowflake(dataclasses.replace(view, area=n), opts) for n in ("a-b", "A_B"))
    assert set(a.files) == set(b.files) == {"a_b.semantic_view.yaml", "a_b.sql", "a_b.agent.sql"}


def test_every_rewritten_verified_query_passes_the_documented_rules():
    doc, _ = _vq_build()
    assert validate_semantic_view(doc) == []


# ---------------------------------------------------------------------------
# Keywords and case: what Snowflake reads as a name
# ---------------------------------------------------------------------------


_KEYWORD_COLUMNS = ("order", "group", "start", "rows", "values", "table", "to", "column", "schema",
                    "select", "case", "true", "current_date", "qualify", "sample", "left", "view")


@pytest.mark.parametrize("name", _KEYWORD_COLUMNS)
def test_a_keyword_column_is_quoted_in_the_case_it_is_stored_in(name):
    # The model spells an unquoted (upper-case) Snowflake column in lower case.
    assert S.snowflake_ident(name) == f'"{name.upper()}"'
    assert S.snowflake_ident(name.upper()) == f'"{name.upper()}"'
    assert isinstance(_parse(S.snowflake_ident(name)), exp.Column)


@pytest.mark.parametrize("name,ident", [("id", "id"), ("ID", "ID"), ("customer_id", "customer_id"),
                                        ("Total Amount", '"Total Amount"'), ("Order", '"Order"'),
                                        ("customerId", '"customerId"'), ('a"b', '"a""b"')])
def test_other_names_are_bare_only_where_an_unquoted_spelling_reaches_them(name, ident):
    assert S.snowflake_ident(name) == ident


def test_the_keyword_list_is_snowflakes_documented_one():
    for word in ("ORDER", "GROUP", "START", "ROWS", "VALUES", "TABLE", "TO", "COLUMN", "SCHEMA",
                 "QUALIFY", "MINUS", "ILIKE", "REGEXP", "TRY_CAST", "LOCALTIMESTAMP", "ASC", "DESC"):
        assert word in S.RESERVED_KEYWORDS
    assert "DATE" not in S.RESERVED_KEYWORDS and "NAME" not in S.RESERVED_KEYWORDS


def _keyword_view() -> X.ExportView:
    from semantic_model import models as M

    cols = [_col("id"), *(_col(n, "string") for n in ("order", "group", "start", "rows", "values")),
            _col("table", "decimal", aggregation="additive")]
    t = X.ExportTable(M.Table(name="order", columns=[M.Column(**c) for c in cols], grain=["id"]),
                      X.PhysicalName(None, "SALES_DATA", "order"))
    total = X.ExportMetric(M.Metric(name="order count", calculation="Orders."),
                           {"Snowflake": 'COUNT(DISTINCT "order".id) + COUNT(SALES_DATA.order.id)'})
    return _fake_view("select", tables=(t,), metrics=(total,), examples=(
        X.ExportQuestion("example", "q", "q?", 'SELECT "order", SUM("table") FROM "order" GROUP BY 1'),))


def test_keyword_columns_and_names_write_a_view_snowflake_can_parse():
    opts = X.ExportOptions(targets=("snowflake",), location=X.Location("DEMO", "group"))
    doc, losses = S.build_semantic_view(_keyword_view(), opts)
    assert validate_semantic_view(doc) == []
    t = _table(doc, "order_")
    assert doc["name"] == "select_"
    exprs = {i["name"]: i["expr"] for s in _MEMBERS for i in t.get(s, [])}
    metric = exprs.pop("order_count")
    assert exprs == {"id": "id", "order_": '"ORDER"', "group_": '"GROUP"', "start_": '"START"',
                     "rows_": '"ROWS"', "values_": '"VALUES"', "table_": '"TABLE"'}
    for e in exprs.values():
        assert isinstance(_parse(e), exp.Column)
    # A metric qualifies by the logical table, whatever the binding called the base table.
    assert metric == "COUNT(DISTINCT order_.id) + COUNT(order_.id)"
    (q,) = doc["verified_queries"]
    assert q["sql"] == "SELECT order_, SUM(table_) FROM __order_ GROUP BY 1"
    # The base table's own name is a keyword: written as stored, and said so.
    got = {(x.kind, x.name): (x.outcome, x.reason) for x in losses}
    assert got[("table", "order")] == ("degraded", "target_rule")
    assert S.semantic_view_fqn(_keyword_view(), opts) == 'DEMO."GROUP".select_'


# ---------------------------------------------------------------------------
# What agami holds beyond the model: review state, conventions and sign-off times
# ---------------------------------------------------------------------------


OPTS = X.ExportOptions(targets=("snowflake",), location=X.Location("DEMO", "SALES_DATA"))
CERTIFY = dataclasses.replace(OPTS, target_args={"snowflake_certification_tags": True})
_CERT = {"database": "SNOWFLAKE", "schema": "TAGS", "tag": "CERTIFICATION_STATUS"}


def _cert(value: str) -> list[dict]:
    return [{"name": dict(_CERT), "value": value}]


def _strip_tags(node):
    if isinstance(node, dict):
        return {k: _strip_tags(v) for k, v in node.items() if k != "tags"}
    if isinstance(node, list):
        return [_strip_tags(x) for x in node]
    return node


def test_without_the_flag_nothing_is_tagged_and_the_report_says_what_the_flag_does(sample):
    out, report = sample
    yml = (out / "snowflake" / "agami_example.semantic_view.yaml").read_text("utf-8")
    sql = (out / "snowflake" / "agami_example.sql").read_text("utf-8")
    assert "tags:" not in yml and "OOB_TAG_APPLY" not in sql
    (entry,) = [x for x in report["targets"]["snowflake"]["losses"] if x["kind"] == "subject_area"]
    assert (entry["outcome"], entry["reason"]) == ("degraded", "detail_lost")
    assert "--snowflake-certification-tags" in entry["detail"] and "OOB_TAG_APPLY" in entry["detail"]


def test_the_flag_certifies_the_approved_sample_and_changes_nothing_else(sample, tmp_path):
    plain_out, _ = sample
    report = _export(SAMPLE, tmp_path, "--snowflake-certification-tags")
    doc = _view(tmp_path, "agami_example")
    assert validate_semantic_view(doc) == []
    assert _strip_tags(doc) == _view(plain_out, "agami_example")
    assert doc["tags"] == _cert("CERTIFIED")
    for t in doc["tables"]:
        assert t["tags"] == _cert("CERTIFIED")
        for s in ("dimensions", "facts", "metrics"):
            assert all(i["tags"] == _cert("CERTIFIED") for i in t.get(s, [])), (t["name"], s)
        assert not any("tags" in i for i in t.get("time_dimensions", []))
    yml = (tmp_path / "snowflake" / "agami_example.semantic_view.yaml").read_text("utf-8")
    assert "SENSITIVITY" not in yml   # sensitive columns are not in the view, so never tagged
    sql = (tmp_path / "snowflake" / "agami_example.sql").read_text("utf-8")
    assert "SNOWFLAKE.OOB_TAG_APPLY" in sql.split("CALL", 1)[0]
    assert "CREATE OR ALTER semantics" in sql.split("CALL", 1)[0]
    assert sql.split("$$\n", 1)[1].rsplit("$$", 1)[0] == yml
    # Every item is tagged (the sample has no time dimension), so nothing is reported.
    assert not [x for x in report["targets"]["snowflake"]["losses"] if x["kind"] == "subject_area"]
    assert report["options"]["target_args"] == {"snowflake_certification_tags": True}


def test_an_item_carried_unreviewed_is_to_be_reviewed_and_so_is_the_view(tmp_path):
    root = _write_model(tmp_path / "acme")
    (root / f"subject_areas/{AREA}/metrics/draft_revenue.yaml").write_text(yaml.safe_dump(
        _metric("draft revenue", "SUM(orders.amount)", review_state="unreviewed")), encoding="utf-8")
    report = _export(root, tmp_path / "out", "--include-unreviewed", "--snowflake-certification-tags")
    doc = _view(tmp_path / "out", AREA)
    # A time dimension has no tags field, so `placed_at` goes untagged and the report says so.
    assert "tags" not in _member(doc, "orders", "placed_at")
    (entry,) = [x for x in report["targets"]["snowflake"]["losses"] if x["kind"] == "subject_area"]
    assert (entry["reason"], entry["detail"].split(" carry")[0]) == (
        "detail_lost", "its 1 time dimension(s)")
    assert validate_semantic_view(doc) == []
    assert _member(doc, "orders", "draft_revenue")["tags"] == _cert("TO BE REVIEWED")
    assert _member(doc, "orders", "revenue")["tags"] == _cert("CERTIFIED")
    assert _member(doc, "orders", "amount")["tags"] == _cert("CERTIFIED")
    assert doc["metrics"][0]["tags"] == _cert("CERTIFIED")             # a derived metric
    assert _table(doc, "employees_manager")["tags"] == _cert("CERTIFIED")  # a role-playing table
    assert doc["tags"] == _cert("TO BE REVIEWED")


@pytest.mark.parametrize("month,span", [(4, "April-March"), (2, "February-January"),
                                        (12, "December-November")])
def test_a_fiscal_year_and_a_default_time_window_become_sql_generation_text(month, span):
    view = dataclasses.replace(_vq_view({}), fiscal_year_start_month=month,
                               default_time_window=" last 90 days ")
    doc, losses = S.build_semantic_view(view, OPTS)
    start = span.split("-")[0]
    assert doc["module_custom_instructions"] == {"sql_generation": (
        f"Use fiscal year ({span}) when a question asks for a fiscal year or fiscal quarter: the "
        f"fiscal year starts in {start}.\n"
        "If no date filter is provided, apply this default time window: last 90 days.")}
    got = {(x.kind, x.name): (x.outcome, x.reason) for x in losses}
    assert got[("datasource", "acme")] == ("degraded", "carried_as_text")
    assert ("subject_area", AREA, "carried_as_text") in {(x.kind, x.name, x.reason) for x in losses}


def test_a_calendar_fiscal_year_and_no_time_window_write_no_instructions():
    doc, losses = S.build_semantic_view(_vq_view({}), OPTS)
    assert "module_custom_instructions" not in doc
    assert not [x for x in losses if x.reason == "carried_as_text"]


def test_conventions_follow_the_ambiguities_in_sql_generation():
    doc, _ = S.build_semantic_view(_edge_view(fiscal_year_start_month=7), OPTS)
    lines = doc["module_custom_instructions"]["sql_generation"].split("\n")
    assert lines[0].startswith("which: when a question says sales") and "(July-June)" in lines[1]


def test_a_verified_query_carries_its_sign_off_time_never_its_signer():
    sqls = {k: _VQ_REWRITES[k][0] for k in ("qualified", "aliased", "quoted")}
    view = dataclasses.replace(_vq_view(sqls), signed=frozenset({
        ("example", "qualified", "2026-06-15T00:00:00Z"), ("example", "aliased", "2026-06-15T02:00:00"),
        ("example", "quoted", "last Tuesday")}))
    doc, losses = S.build_semantic_view(view, OPTS)
    assert validate_semantic_view(doc) == []
    got = {q["name"]: q for q in doc["verified_queries"]}
    assert got["qualified"]["verified_at"] == 1781481600          # 2026-06-15T00:00:00Z
    assert got["aliased"]["verified_at"] == 1781481600 + 7200     # no offset: read as UTC
    assert list(got["qualified"]) == ["name", "question", "verified_at",
                                      "use_as_onboarding_question", "sql"]
    assert "verified_at" not in got["quoted"]
    assert not any("verified_by" in q for q in doc["verified_queries"])
    lost = {(x.kind, x.name): (x.outcome, x.reason) for x in losses}
    assert lost[("example", "quoted")] == ("degraded", "detail_lost")


def _flagged(doc: dict) -> list[str]:
    return [q["name"] for q in doc["verified_queries"] if q.get("use_as_onboarding_question")]


def test_the_first_five_approved_verified_queries_are_onboarding_questions():
    doc, _ = _vq_build()
    names = [q["name"] for q in doc["verified_queries"]]
    assert len(names) > S.ONBOARDING_QUESTIONS == 5
    assert _flagged(doc) == names[:5]
    assert all(q["use_as_onboarding_question"] is True for q in doc["verified_queries"][:5])
    # One carried unreviewed is never suggested to a new user; the next approved one is.
    sqls = {k: v[0] for k, v in {**_VQ_REWRITES, **_VQ_DEGRADED}.items()}
    view = dataclasses.replace(_vq_view(sqls), unreviewed=frozenset({("example", "qualified")}))
    doc, _ = S.build_semantic_view(view, OPTS)
    assert _flagged(doc) == names[1:6]


def _unrelated_view(*metrics) -> X.ExportView:
    from semantic_model import models as M

    def table(name, cols):
        return X.ExportTable(M.Table(name=name, columns=[M.Column(**c) for c in cols], grain=["id"]),
                             X.PhysicalName(None, "SALES_DATA", name))

    return _fake_view(AREA, tables=(
        table("Sales Orders", [_col("id"), _col("Net Value", "decimal", aggregation="additive"),
                               _col("placed_at", "timestamp")]),
        table("refunds", [_col("id"), _col("order", "string")])),
        metrics=tuple(X.ExportMetric(M.Metric(name=n, calculation=f"The {n}.", **kw), {"Snowflake": sql})
                      for n, sql, kw in metrics))


def test_a_metric_no_one_table_holds_becomes_a_derived_metric(acme):
    _, report, doc = acme
    assert doc["metrics"] == [{"name": "staff_and_sales", "description": "Calculation: The staff and sales.",
                               "expr": "SUM(employees.id) + SUM(orders.amount)",
                               "synonyms": ["headcount and sales"]}]
    assert ("metric", "staff and sales") not in _losses(report)


def test_a_derived_metric_reads_members_by_their_logical_names_and_keeps_the_rules():
    doc, losses = S.build_semantic_view(_unrelated_view(
        ("net", 'SUM("Sales Orders"."Net Value")', {}),
        ("net and refunds", 'SUM("Sales Orders"."Net Value") - COUNT(DISTINCT refunds."order")', {}),
        ("Net", 'SUM("Sales Orders"."Net Value") + COUNT(refunds.id)', {}),
        ("closing mix", 'SUM("Sales Orders"."Net Value") / COUNT(refunds.id)',
         {"non_additive_dimensions": ["time"], "semi_additive_agg": "last"}),
    ), OPTS)
    assert validate_semantic_view(doc) == []
    derived = {m["name"]: m["expr"] for m in doc["metrics"]}
    assert derived == {"net_and_refunds": "SUM(sales_orders.net_value) - COUNT(DISTINCT refunds.order_)",
                       "closing_mix": "SUM(sales_orders.net_value) / COUNT(refunds.id)"}
    for e in derived.values():
        _parse(e)
    got = {(x.kind, x.name): (x.outcome, x.reason) for x in losses}
    assert got[("metric", "Net")] == ("dropped", "name_collision")   # a regular metric is `net`
    assert got[("metric", "closing mix")] == ("degraded", "detail_lost")


def _self_join_view(*rels) -> X.ExportView:
    from semantic_model import models as M

    def table(name, cols):
        return X.ExportTable(M.Table(name=name, columns=[M.Column(**c) for c in cols], grain=["id"]),
                             X.PhysicalName(None, "SALES_DATA", name))

    return _fake_view(AREA, tables=(
        table("employees", [_col("id"), _col("manager_id"), _col("name", "string"),
                            _col("hired_at", "date")]),
        table("staff", [_col("id"), _col("lead_id")]),
        table("staff_lead", [_col("id")])),
        relationships=tuple(X.ExportRelationship(n, M.Relationship(
            from_table=f, to_table=t, relationship=card, from_column=fc, to_column=tc))
            for n, f, t, card, fc, tc in rels))


def test_a_self_join_references_a_role_playing_table_on_the_same_base_table(acme):
    out, report, doc = acme
    emp, mgr = _table(doc, "employees"), _table(doc, "employees_manager")
    assert mgr["base_table"] == emp["base_table"] and mgr["primary_key"] == emp["primary_key"]
    assert [i["name"] for i in mgr["dimensions"]] == [i["name"] for i in emp["dimensions"]]
    assert "synonyms" not in mgr
    assert {"name": "employees_to_employees", "left_table": "employees",
            "right_table": "employees_manager",
            "relationship_columns": [{"left_column": "manager_id", "right_column": "id"}]} \
        in doc["relationships"]
    assert ("degraded", "target_rule") in _losses(report)[("relationship", "employees->employees")]
    # A copy, not a shared object: PyYAML would write a shared one as an anchor and an alias.
    assert "&id" not in (out / "snowflake" / f"{AREA}.semantic_view.yaml").read_text("utf-8")


def test_a_self_join_turns_round_when_one_to_many_and_a_taken_role_name_is_a_collision():
    doc, losses = S.build_semantic_view(_self_join_view(
        ("employees->employees", "employees", "employees", "one_to_many", "id", "manager_id"),
        ("staff->staff", "staff", "staff", "many_to_one", "lead_id", "id"),
    ), OPTS)
    assert validate_semantic_view(doc) == []
    (rel,) = doc["relationships"]
    assert (rel["left_table"], rel["right_table"]) == ("employees", "employees_manager")
    assert rel["relationship_columns"] == [{"left_column": "manager_id", "right_column": "id"}]
    role = _table(doc, "employees_manager")
    assert [i["name"] for i in role["time_dimensions"]] == ["hired_at"]
    got = {(x.kind, x.name): (x.outcome, x.reason) for x in losses}
    assert got[("relationship", "staff->staff")] == ("dropped", "name_collision")


def test_nothing_is_written_private():
    # agami has no item that only feeds another, which is all `private_access` is for.
    doc, _ = S.build_semantic_view(_unrelated_view(("net", 'SUM("Sales Orders"."Net Value")', {})),
                                   CERTIFY)
    assert "access_modifier" not in json.dumps(doc)


def _good_with(**extra) -> dict:
    d = _good()
    d["metrics"] = [{"name": "d", "expr": "a.n + SUM(b.id)", "tags": _cert("CERTIFIED")}]
    d["tags"] = _cert("TO BE REVIEWED")
    d["tables"][0]["tags"] = [{"name": {"database": "D", "schema": "S", "tag": "owner"},
                               "value": "x" * 256}]
    d["verified_queries"] = [{"name": "q", "question": "q?", "verified_at": 1781481600,
                              "use_as_onboarding_question": True, "sql": "SELECT id FROM __a"}]
    d.update(extra)
    return d


def test_the_validator_passes_tags_derived_metrics_and_dated_onboarding_questions():
    assert validate_semantic_view(_good_with()) == []


@pytest.mark.parametrize("fn,needle", [
    (lambda d: d["tables"][0].update(
        time_dimensions=[{"name": "t", "expr": "t", "tags": _cert("CERTIFIED")}]), "undocumented"),
    (lambda d: d.update(tags=_cert("APPROVED")), "CERTIFICATION_STATUS value"),
    (lambda d: d["tables"][0]["tags"][0].update(value="x" * 257), "256"),
    (lambda d: d["tables"][0]["tags"][0].pop("value"), "exactly name and value"),
    (lambda d: d["tables"][0]["tags"][0]["name"].pop("schema"), "database, schema and tag"),
    (lambda d: d.update(tags=_cert("CERTIFIED") * 51), "1 to 50"),
    (lambda d: d["tables"][0]["dimensions"][0].update(tags={"x": 1}), "1 to 50"),
    (lambda d: d["verified_queries"][0].update(verified_at="2026-06-15"), "epoch"),
    (lambda d: d["verified_queries"][0].update(verified_at=True), "epoch"),
    (lambda d: d["verified_queries"][0].update(use_as_onboarding_question="yes"), "boolean"),
    (lambda d: d["metrics"][0].update(name="n"), "share a name"),
    (lambda d: d["metrics"][0].update(expr="a.n + SUM(b.nope)"), "resolve"),
    (lambda d: d["metrics"][0].update(expr="a.n + a.id"), "outside an aggregate"),
    (lambda d: d["metrics"][0].update(expr="a.n + other"), "no derived metric"),
    (lambda d: d["metrics"][0].update(table="a"), "undocumented"),
    (lambda d: d["metrics"].append(dict(d["metrics"][0])), "duplicate derived"),
])
def test_the_validator_rejects_a_bad_tag_derived_metric_or_verified_query_field(fn, needle):
    doc = _good_with()
    fn(doc)
    problems = validate_semantic_view(doc)
    assert any(needle in p for p in problems), problems


# ---------------------------------------------------------------------------
# Review-wave fixes: other schemas' tables, cross-table aggregates, certification strictness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    "SELECT amount FROM archive.orders",
    "SELECT SUM(total_amount) AS s FROM otherdb.otherschema.orders",
    'SELECT id FROM "orders"',                    # lower case, quoted: another Snowflake table
    "SELECT id FROM IDENTIFIER('orders')",
    "SELECT id, nope FROM orders",                # a bare output is no alias: `nope` is a column
])
def test_a_verified_query_on_another_table_is_never_pointed_at_the_views_table(sql):
    doc, losses = S.build_semantic_view(_vq_view({"other": sql, "fine": _VQ_REWRITES["qualified"][0]}),
                                        OPTS)
    assert [q["name"] for q in doc["verified_queries"]] == ["fine"]
    (loss,) = [x for x in losses if x.name == "other"]
    assert loss.outcome == "degraded" and loss.reason in ("no_home_in_target", "target_rule")


def test_a_metric_naming_another_schemas_table_is_dropped():
    from semantic_model import models as M

    view = _vq_view({})
    other = X.ExportMetric(M.Metric(name="archived", calculation="x."),
                           {"Snowflake": "SUM(ARCHIVE.orders.total_amount)"})
    own = X.ExportMetric(M.Metric(name="own", calculation="x."),
                         {"Snowflake": "SUM(SALES_DATA.orders.total_amount)"})
    doc, losses = S.build_semantic_view(dataclasses.replace(view, metrics=(other, own)), OPTS)
    assert _member(doc, "orders", "own")["expr"] == "SUM(orders.total_amount)"
    lost = {(x.kind, x.name): (x.outcome, x.reason) for x in losses}
    assert lost[("metric", "archived")] == ("dropped", "target_rule")


def test_one_aggregate_over_two_unrelated_tables_is_no_derived_metric():
    from semantic_model import models as M

    def table(name, col):
        return X.ExportTable(M.Table(name=name, columns=[
            M.Column(**_col("id")), M.Column(**_col(col, "decimal", aggregation="additive"))],
            grain=["id"]), X.PhysicalName(None, "SALES_DATA", name))

    def metric(name, sql):
        return X.ExportMetric(M.Metric(name=name, calculation="x."), {"Snowflake": sql})

    view = _fake_view(AREA, tables=(table("stores", "size"), table("regions", "area")), metrics=(
        metric("cross", "SUM(stores.size * regions.area)"),
        metric("ratio", "SUM(stores.size) / SUM(regions.area)")))
    doc, losses = S.build_semantic_view(view, OPTS)
    # Each aggregate within one logical table is a derived metric; one spanning two is not.
    assert [m["name"] for m in doc["metrics"]] == ["ratio"]
    lost = {(x.kind, x.name): (x.outcome, x.reason) for x in losses}
    assert lost[("metric", "cross")] == ("dropped", "target_rule")


def test_a_table_carrying_an_unreviewed_column_is_not_certified(tmp_path):
    root = _write_model(tmp_path / "acme")
    path = root / f"subject_areas/{AREA}/tables/orders.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["columns"][-1]["review_state"] = "unreviewed"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    report = _export(root, tmp_path / "out", "--include-unreviewed", "--snowflake-certification-tags")
    view = _view(tmp_path / "out", AREA)
    assert _table(view, "orders")["tags"] == _cert("TO BE REVIEWED")
    assert _table(view, "customers")["tags"] == _cert("CERTIFIED")
    assert ("degraded", "not_approved") in _losses(report)[("table", "orders")]


def test_a_view_carrying_key_terms_or_ambiguities_is_not_certified():
    from semantic_model import models as M

    view = dataclasses.replace(_vq_view({}), terminology=(("GMV", "gross merchandise value"),))
    doc, losses = S.build_semantic_view(view, CERTIFY)
    assert doc["tags"] == _cert("TO BE REVIEWED")
    assert _table(doc, "orders")["tags"] == _cert("CERTIFIED")
    (loss,) = [x for x in losses if x.kind == "subject_area" and x.reason == "not_approved"]
    assert "key terms" in loss.detail
    amb = M.Ambiguity(name="which", when="a question says sales", options=[
        M.AmbiguityOption(label="net", how="net", default=True), M.AmbiguityOption(label="g", how="g")])
    doc, _ = S.build_semantic_view(dataclasses.replace(_vq_view({}), ambiguities=(amb,)), CERTIFY)
    assert doc["tags"] == _cert("TO BE REVIEWED")
    doc, _ = S.build_semantic_view(_vq_view({}), CERTIFY)
    assert doc["tags"] == _cert("CERTIFIED")
