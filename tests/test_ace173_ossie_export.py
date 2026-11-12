"""`sm export --target ossie` (ACE-173): each subject area as one Apache Ossie semantic model.

Output is validated against Ossie's own published JSON Schemas, vendored under
`tests/fixtures/ossie/` (see the NOTICE there): 0.1.1 at its release tag, 0.3.0.dev0 at the pinned
`main` commit named below. A schema checks keys and enum values but not references or SQL, so the
tests also check every reference by hand and parse every expression with sqlglot in the dialect it
is tagged with.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import importlib.util
import io
import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")
jsonschema = pytest.importorskip("jsonschema")

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402
from sqlglot.errors import ErrorLevel  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PKG_SRC = REPO / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import cli  # noqa: E402
from semantic_model import export as X  # noqa: E402
from semantic_model import export_ossie as O  # noqa: E402  (importing it registers `ossie`)
from semantic_model.loader import load_datasource  # noqa: E402

SAMPLE = REPO / "plugins" / "agami" / "samples" / "store" / "model"
FIXTURES = REPO / "tests" / "fixtures" / "ossie"
# The 0.3.0.dev0 schema is a development version, so it is pinned to the apache/ossie commit it was copied from.
OSSIE_03_COMMIT = "e6ad94ee87a53fd8f8b6dbf0de33fab59e406c0c"
SCHEMAS = {
    "0.1.1": FIXTURES / "osi-schema-0.1.1.json",
    "0.3": FIXTURES / "ossie-schema-0.3.0.dev0.json",
}
AREA = "sales"
SECRET = "tax_ref"
# A field expression is a column reference; ANSI_SQL and Ossie's portable SQL read in the generic
# grammar, which is ANSI. The empty string names sqlglot's base dialect explicitly.
SQLGLOT = {
    "SNOWFLAKE": "snowflake",
    "DATABRICKS": "databricks",
    "ANSI_SQL": "",
    "OSSIE_SQL_2026": "",
}


# ---------------------------------------------------------------------------
# A synthetic model: acme's SALES_DATA, written for Snowflake
# ---------------------------------------------------------------------------


def _col(name, type_="string", **kw):
    return {"name": name, "type": type_, **kw}


def _metric(name, bindings, **kw):
    return {
        "name": name,
        "calculation": f"The {name}.",
        "bindings": bindings,
        "source_tables": kw.pop("source_tables", ["orders"]),
        "review_state": "approved",
        **kw,
    }


def _all(sql):
    return {"SQLite": sql, "Snowflake": sql, "Databricks": sql}


def _write_model(root: Path) -> Path:
    def put(rel, doc):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    put(
        "datasource.yaml",
        {
            "datasource": "acme",
            "description": "Acme demo sales.",
            "storage_connections": [
                {"name": "acme_wh", "storage_type": "Snowflake", "storage_config": {}}
            ],
            "subject_areas": [f"subject_areas/{AREA}"],
            "key_terminology": {"GMV": "gross merchandise value"},
            "ambiguities": [
                {
                    "name": "which amount",
                    "when": "a question says amount",
                    "behaviour": "answer_and_offer",
                    "options": [
                        {"label": "net", "how": "use orders.amount", "default": True},
                        {"label": "gross", "how": "add tax"},
                    ],
                }
            ],
        },
    )
    put(
        f"subject_areas/{AREA}/subject_area.yaml",
        {"name": AREA, "description": "Orders and customers."},
    )
    put(
        f"subject_areas/{AREA}/tables/orders.yaml",
        {
            "name": "orders",
            "schema": "SALES_DATA",
            "grain": ["id"],
            "description": "One row per order.",
            "default_filters": ["{alias}.status != 'void'"],
            "caveats": ["Amounts are net of tax."],
            "signed_off_by": "you@example.com",
            "signed_off_at": "2026-01-01T00:00:00Z",
            "columns": [
                _col("id", "integer", primary_key=True, aggregation="dimension"),
                _col("customer_id", "integer", aggregation="dimension"),
                _col("amount", "decimal", aggregation="additive"),
                _col("status", aggregation="dimension", choice_field={"v": "void", "p": "paid"}),
                _col("placed at", "timestamp"),
                _col("payload", "json"),
            ],
        },
    )
    put(
        f"subject_areas/{AREA}/tables/customers.yaml",
        {
            "name": "customers",
            "schema": "SALES_DATA",
            "unique_keys": [["region", "id"]],
            "columns": [
                _col("id", "integer", primary_key=True),
                _col("region"),
                _col(SECRET, sensitive=True),
            ],
        },
    )
    put(
        f"subject_areas/{AREA}/tables/big_orders.yaml",
        {
            "name": "big_orders",
            "source_type": "sql",
            "sql": "SELECT id, amount FROM SALES_DATA.orders WHERE amount > 100",
            "columns": [_col("id", "integer"), _col("amount", "decimal")],
        },
    )
    put(
        f"subject_areas/{AREA}/relationships.yaml",
        [
            {
                "from_table": "orders",
                "to_table": "customers",
                "from_column": "customer_id",
                "to_column": "id",
                "relationship": "many_to_one",
                "join_type": "INNER",
                "review_state": "approved",
            },
            {
                "from_table": "customers",
                "to_table": "orders",
                "from_column": "id",
                "to_column": "customer_id",
                "relationship": "one_to_many",
                "review_state": "approved",
            },
            {
                "from_table": "orders",
                "to_table": "big_orders",
                "from_column": "id",
                "to_column": "id",
                "relationship": "one_to_one",
                "review_state": "approved",
            },
            {
                "from_table": "orders",
                "to_table": "customers",
                "on": "orders.customer_id = customers.id",
                "relationship": "many_to_one",
                "review_state": "approved",
            },
        ],
    )
    put(
        f"subject_areas/{AREA}/entities/customer.yaml",
        {
            "name": "customer",
            "other_names": ["client"],
            "review_state": "approved",
            "maps_to": [{"table": "customers", "column": "id", "primary": True}],
        },
    )
    put(f"subject_areas/{AREA}/entities/deal.yaml", {"name": "deal", "review_state": "approved"})
    metrics = [
        _metric(
            "revenue",
            _all("SUM(orders.amount)"),
            other_names=["sales"],
            signed_off_by="you@example.com",
            signed_off_at="2026-02-01T00:00:00Z",
        ),
        _metric("order count", _all("COUNT(DISTINCT orders.id)")),
        _metric(
            "average order value",
            _all("{revenue} / {order count}"),
            base_metrics=["revenue", "order count"],
        ),
        _metric(
            "open balance",
            {"Snowflake": "SUM(orders.amount)"},
            non_additive_dimensions=["placed at"],
            semi_additive_agg="last",
        ),
        _metric("Order-Count", {"Databricks": "COUNT(*)"}),
        _metric("postgres only", {"PostgreSQL": "SUM(orders.amount)"}),
    ]
    for m in metrics:
        put(f"subject_areas/{AREA}/metrics/{m['name'].replace(' ', '_')}.yaml", m)
    put(
        f"prompt_examples/{AREA}/examples.yaml",
        [
            {
                "question": "What is revenue?",
                "sql": "SELECT SUM(amount) FROM orders",
                "status": "confirmed",
            },
            {"question": "How many regions?", "status": "confirmed"},
        ],
    )
    put(
        "golden_datasets/core.yaml",
        {
            "test_cases": [
                {
                    "id": "g1",
                    "query": "Total revenue",
                    "expected": {"sql": "SELECT SUM(amount) FROM orders", "sql_confirmed": True},
                }
            ]
        },
    )
    return root


@pytest.fixture()
def model(tmp_path: Path) -> Path:
    return _write_model(tmp_path / "art" / "acme")


# ---------------------------------------------------------------------------
# Running and reading an export
# ---------------------------------------------------------------------------


def _sm(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["export", *argv])
    return rc, buf.getvalue()


def _export(root: Path, out: Path, version: str = "0.1.1", *flags: str) -> dict:
    rc, text = _sm(
        str(root), "--target", "ossie", "--out", str(out), "--ossie-version", version, *flags
    )
    assert rc == 0, text
    return json.loads(text)


def _sample(out: Path, version: str = "0.1.1") -> dict:
    return _export(SAMPLE, out, version, "--location", "DEMO.SALES_DATA")


def _docs(out: Path) -> dict[str, dict]:
    return {
        p.name: yaml.safe_load(p.read_text(encoding="utf-8"))
        for p in sorted((out / "ossie").glob("*.ossie.yaml"))
    }


def _model(doc: dict) -> dict:
    return doc["semantic_model"][0] if doc["version"] == "0.1.1" else doc


def _losses(report: dict) -> list[dict]:
    return report["targets"]["ossie"]["losses"]


def _loss(report: dict, kind: str, name: str) -> list[tuple[str, str]]:
    return [
        (x["outcome"], x["reason"])
        for x in _losses(report)
        if (x["kind"], x["name"]) == (kind, name)
    ]


def _validate(doc: dict, version: str) -> None:
    schema = json.loads(SCHEMAS[version].read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    # The schema is JSON, so the document is checked as the JSON a YAML loader would hand it.
    jsonschema.Draft202012Validator(schema).validate(json.loads(json.dumps(doc)))


def _check_references(model: dict) -> None:
    """What the schema cannot see: names unique, keys and relationship ends naming real fields."""
    datasets = {d["name"]: d for d in model["datasets"]}
    assert len(datasets) == len(model["datasets"])
    fields = {}
    for d in model["datasets"]:
        names = [f["name"] for f in d.get("fields", [])]
        assert len(names) == len(set(names)), d["name"]
        fields[d["name"]] = set(names)
        assert set(d.get("primary_key", [])) <= fields[d["name"]]
        assert all(set(k) <= fields[d["name"]] for k in d.get("unique_keys", []))
    rels = model.get("relationships", [])
    assert len({r["name"] for r in rels}) == len(rels)
    for r in rels:
        assert r["from"] in datasets and r["to"] in datasets, r["name"]
        assert len(r["from_columns"]) == len(r["to_columns"])
        assert (
            set(r["from_columns"]) <= fields[r["from"]] and set(r["to_columns"]) <= fields[r["to"]]
        )
    metrics = [m["name"] for m in model.get("metrics", [])]
    assert len(metrics) == len(set(metrics))


def _expressions(model: dict):
    """(where, dialect, SQL) for every expression in the model."""
    items = [
        (f"{d['name']}.{f['name']}", f["expression"])
        for d in model["datasets"]
        for f in d["fields"]
    ]
    items += [(m["name"], m["expression"]) for m in model.get("metrics", [])]
    for where, expression in items:
        if "sql" in expression:
            yield where, "OSSIE_SQL_2026", expression["sql"]
        for e in expression.get("dialects", []):
            yield where, e["dialect"], e["expression"]


def _parse(sql: str, dialect: str):
    return sqlglot.parse_one(sql, dialect=SQLGLOT[dialect], error_level=ErrorLevel.RAISE)


# ---------------------------------------------------------------------------
# Success criteria
# ---------------------------------------------------------------------------


def test_the_sample_writes_one_0_1_1_file_per_area_that_validates(tmp_path):
    report = _sample(tmp_path / "out")
    areas = [sa.name for sa in load_datasource(SAMPLE).subject_areas]
    docs = _docs(tmp_path / "out")
    # Named by the model's own Ossie name, not the raw area name (`agami-example`).
    assert sorted(docs) == sorted(f"{O.ossie_name(a)}.ossie.yaml" for a in areas)
    assert report["targets"]["ossie"]["files"] == sorted(
        f"ossie/{O.ossie_name(a)}.ossie.yaml" for a in areas
    )
    for doc in docs.values():
        assert doc["version"] == "0.1.1"
        _validate(doc, "0.1.1")
        _check_references(_model(doc))
        assert len(_model(doc)["metrics"]) == 8 and len(_model(doc)["datasets"]) == 11


def test_the_0_3_flag_validates_against_the_pinned_0_3_0_dev0_schema(model, tmp_path):
    notice = (FIXTURES / "NOTICE").read_text(encoding="utf-8")
    assert OSSIE_03_COMMIT in notice and "Apache License" in (FIXTURES / "LICENSE").read_text()
    for version_flag in ("0.3", "0.3.0.dev0"):
        _sample(tmp_path / version_flag, version_flag)
        _export(model, tmp_path / f"m{version_flag}", version_flag)
        for out in (tmp_path / version_flag, tmp_path / f"m{version_flag}"):
            for doc in _docs(out).values():
                assert doc["version"] == "0.3.0.dev0" and "semantic_model" not in doc
                _validate(doc, "0.3")
                _check_references(doc)


def test_the_synthetic_model_validates_at_0_1_1_too(model, tmp_path):
    _export(model, tmp_path / "out")
    (doc,) = _docs(tmp_path / "out").values()
    _validate(doc, "0.1.1")
    _check_references(_model(doc))


@pytest.mark.parametrize("version", ["0.1.1", "0.3"])
def test_every_expression_parses_in_the_dialect_it_is_tagged_with(model, tmp_path, version):
    _sample(tmp_path / "s", version)
    _export(model, tmp_path / "m", version)
    seen = set()
    for out in (tmp_path / "s", tmp_path / "m"):
        for doc in _docs(out).values():
            for where, dialect, sql in _expressions(_model(doc)):
                seen.add(dialect)
                assert _parse(sql, dialect) is not None, (where, dialect)
    assert {"SNOWFLAKE", "DATABRICKS"} <= seen


def test_a_metric_bound_for_three_engines_carries_each_ossie_dialect_and_reports_the_rest(
    model, tmp_path
):
    report = _export(model, tmp_path / "out")
    (doc,) = _docs(tmp_path / "out").values()
    revenue = next(m for m in _model(doc)["metrics"] if m["name"] == "revenue")
    assert revenue["expression"] == {
        "dialects": [
            {"dialect": "DATABRICKS", "expression": "SUM(orders.amount)"},
            {"dialect": "SNOWFLAKE", "expression": "SUM(orders.amount)"},
        ]
    }
    # The SQLite binding is not the view's to report: the target carries no SQLite engine, so the
    # core never resolved it, and the writer reads only what the core resolved.
    assert _loss(report, "metric", "revenue") == []
    # One dialect short is a weaker metric; no dialect at all is no metric.
    open_balance = next(m for m in _model(doc)["metrics"] if m["name"] == "open_balance")
    assert [d["dialect"] for d in open_balance["expression"]["dialects"]] == ["SNOWFLAKE"]
    assert ("degraded", "no_binding_for_target") in _loss(report, "metric", "open balance")
    # Ossie names two engines; a metric bound for neither is still one entry.
    assert _loss(report, "metric", "postgres only") == [("dropped", "no_binding_for_target")]
    assert "postgres_only" not in {m["name"] for m in _model(doc)["metrics"]}

    sample = _sample(tmp_path / "sample")
    for m in _model(next(iter(_docs(tmp_path / "sample").values())))["metrics"]:
        assert {d["dialect"] for d in m["expression"]["dialects"]} == {"DATABRICKS", "SNOWFLAKE"}
    assert not [x for x in _losses(sample) if x["kind"] == "metric"]


@pytest.mark.parametrize("version", ["0.1.1", "0.3"])
def test_no_metric_expression_references_another_metric(model, tmp_path, version):
    _sample(tmp_path / "s", version)
    _export(model, tmp_path / "m", version)
    for out in (tmp_path / "s", tmp_path / "m"):
        for doc in _docs(out).values():
            metrics = _model(doc)["metrics"]
            names = {m["name"] for m in metrics}  # an expanded metric would leave its name behind
            for where, dialect, sql in _expressions({"datasets": [], "metrics": metrics}):
                assert "{" not in sql and "MEASURE(" not in sql.upper(), where
                found = {i.name.casefold() for i in _parse(sql, dialect).find_all(exp.Identifier)}
                assert not found & names, (where, found & names)
    (doc,) = _docs(tmp_path / "m").values()
    aov = next(m for m in _model(doc)["metrics"] if m["name"] == "average_order_value")
    assert "SUM(orders.amount)" in aov["expression"]["dialects"][0]["expression"]


@pytest.mark.parametrize("version", ["0.1.1", "0.3"])
def test_every_relationship_carried_is_reported_once_as_detail_lost_naming_cardinality(
    model, tmp_path, version
):
    for out, report in (
        (tmp_path / "s", _sample(tmp_path / "s", version)),
        (tmp_path / "m", _export(model, tmp_path / "m", version)),
    ):
        carried = [r for d in _docs(out).values() for r in _model(d).get("relationships", [])]
        lost = [
            x
            for x in _losses(report)
            if x["kind"] == "relationship" and x["reason"] == "detail_lost"
        ]
        assert carried and len(lost) == len(carried)
        assert sorted(O.ossie_name(x["name"]) for x in lost) == sorted(r["name"] for r in carried)
        assert len({x["name"] for x in lost}) == len(lost)
        for x in lost:
            assert x["outcome"] == "degraded"
            assert re.search(r"cardinality (many_to_one|one_to_many|one_to_one)", x["detail"])
            assert "join type" in x["detail"]


@pytest.mark.parametrize("version", ["0.1.1", "0.3"])
def test_two_runs_produce_byte_identical_files(model, tmp_path, version):
    def tree(out: Path) -> dict[str, bytes]:
        return {
            str(p.relative_to(out)): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()
        }

    for root, flags in ((SAMPLE, ("--location", "DEMO.SALES_DATA")), (model, ())):
        _export(root, tmp_path / "a", version, *flags)
        _export(root, tmp_path / "b", version, *flags)
        a, b = tree(tmp_path / "a"), tree(tmp_path / "b")
        assert a and a == b
        assert hashlib.sha256(b"".join(a.values())).hexdigest()


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


def test_relationships_put_the_many_side_first_and_drop_sql_joins(model, tmp_path):
    report = _export(model, tmp_path / "out")
    (doc,) = _docs(tmp_path / "out").values()
    rels = {r["name"]: r for r in _model(doc)["relationships"]}
    assert rels["orders_to_customers"] == {
        "name": "orders_to_customers",
        "from": "orders",
        "to": "customers",
        "from_columns": ["customer_id"],
        "to_columns": ["id"],
    }
    # one_to_many is turned round so `from` stays the many side.
    assert (rels["customers_to_orders"]["from"], rels["customers_to_orders"]["to"]) == (
        "orders",
        "customers",
    )
    assert "orders_to_big_orders" in rels
    assert _loss(report, "relationship", "orders->customers#2") == [
        ("dropped", "no_home_in_target")
    ]


def test_datasets_and_fields(model, tmp_path):
    _export(model, tmp_path / "a", "0.1.1")
    _export(model, tmp_path / "b", "0.3")
    (old,) = _docs(tmp_path / "a").values()
    (new,) = _docs(tmp_path / "b").values()
    orders = next(d for d in _model(old)["datasets"] if d["name"] == "orders")
    assert orders["source"] == "SALES_DATA.orders" and orders["primary_key"] == ["id"]
    # 0.1.1 writes AI context as text; 0.3 keeps the object.
    assert orders["ai_context"] == "Amounts are net of tax."
    fields = {f["name"]: f for f in orders["fields"]}
    assert fields["placed at"]["expression"]["dialects"] == [
        {"dialect": "ANSI_SQL", "expression": '"placed at"'}
    ]
    assert fields["placed at"]["dimension"] == {"is_time": True}
    assert "dimension" not in fields["amount"] and "datatype" not in fields["amount"]
    assert "void" in fields["status"]["ai_context"]
    big = next(d for d in _model(old)["datasets"] if d["name"] == "big_orders")
    assert big["source"].startswith("SELECT id, amount")
    customers = next(d for d in _model(old)["datasets"] if d["name"] == "customers")
    assert customers["unique_keys"] == [["region", "id"]]
    assert customers["ai_context"] == "Synonyms:\n- customer\n- client"
    assert SECRET not in json.dumps(old) + json.dumps(new)

    new_datasets = {d["name"]: d for d in new["datasets"]}
    assert new_datasets["orders"]["ai_context"] == {"instructions": "Amounts are net of tax."}
    assert new_datasets["customers"]["ai_context"]["synonyms"] == ["customer", "client"]

    new_orders = {
        f["name"]: f for f in next(d for d in new["datasets"] if d["name"] == "orders")["fields"]
    }
    assert new_orders["amount"]["expression"] == {"sql": "amount"}
    assert (new_orders["amount"]["datatype"], new_orders["placed at"]["datatype"]) == (
        "Decimal",
        "DateTime",
    )
    assert new_orders["payload"]["datatype"] == "Opaque"


def test_ai_context_carries_ambiguities_terminology_and_example_questions_only(model, tmp_path):
    # Read at 0.3, where AI context is still an object; 0.1.1's text form is checked against it below.
    report = _export(model, tmp_path / "out", "0.3")
    (doc,) = _docs(tmp_path / "out").values()
    context = _model(doc)["ai_context"]
    assert (
        "which amount: when a question says amount, answer with the default"
        in context["instructions"]
    )
    assert "net (default): use orders.amount" in context["instructions"]
    assert context["synonyms"] == ["GMV: gross merchandise value", "deal"]
    assert context["examples"] == ["How many regions?", "What is revenue?"]
    assert "SELECT" not in json.dumps(context)
    assert _loss(report, "example", "What is revenue?") == [("degraded", "no_home_in_target")]
    assert _loss(report, "example", "How many regions?") == []
    assert _loss(report, "golden", "core:g1") == [("dropped", "no_home_in_target")]
    assert report["targets"]["ossie"]["carried"]["golden"] == 0
    assert _loss(report, "entity", "customer") == [("degraded", "detail_lost")]


def test_metric_names_follow_one_rule_and_a_collision_drops_the_later(model, tmp_path):
    report = _export(model, tmp_path / "out", "0.3")
    (doc,) = _docs(tmp_path / "out").values()
    metrics = {m["name"]: m for m in _model(doc)["metrics"]}
    assert set(metrics) == {"average_order_value", "open_balance", "order_count", "revenue"}
    # Items go in name order, so "Order-Count" claims the name and "order count" collides.
    assert metrics["order_count"]["ai_context"]["synonyms"] == ["Order-Count"]
    assert metrics["revenue"]["ai_context"]["synonyms"] == ["sales"]
    assert _loss(report, "metric", "order count") == [("dropped", "name_collision")]
    assert (
        "Do not sum across placed at, take the last value."
        in metrics["open_balance"]["ai_context"]["instructions"]
    )
    assert ("degraded", "carried_as_text") in _loss(report, "metric", "open balance")


def _contexts(node, path=()) -> dict[tuple, object]:
    """Every `ai_context` in a model, keyed by the names on the way to it."""
    out = {}
    if isinstance(node, dict):
        if "ai_context" in node:
            out[path] = node["ai_context"]
        for key, value in node.items():
            for item in value if isinstance(value, list) else ():
                if isinstance(item, dict):
                    out.update(_contexts(item, (*path, key, item.get("name"))))
    return out


def _context_lines(text: str) -> tuple[str, list[str], list[str]]:
    """Text back into instructions, synonyms and examples, by its labelled lines."""
    head, lists, current = [], {"Synonyms:": [], "Example questions:": []}, None
    for line in text.split("\n"):
        if line in lists:
            current = lists[line]
        elif current is not None:
            assert line.startswith("- "), line
            current.append(line[2:])
        else:
            head.append(line)
    return "\n".join(head), lists["Synonyms:"], lists["Example questions:"]


def test_at_0_1_1_no_ai_context_is_a_mapping_and_its_text_keeps_everything_the_object_held(
    model, tmp_path
):
    # At 0.1.1 every `ai_context` is written as one string, at every level.
    for name, root, flags in (("s", SAMPLE, ("--location", "DEMO.SALES_DATA")), ("m", model, ())):
        old_report = _export(root, tmp_path / f"{name}a", "0.1.1", *flags)
        _export(root, tmp_path / f"{name}b", "0.1.1", *flags)
        new_report = _export(root, tmp_path / f"{name}3", "0.3", *flags)
        for doc_name, old in _docs(tmp_path / f"{name}a").items():
            again = (tmp_path / f"{name}b" / "ossie" / doc_name).read_bytes()
            assert (tmp_path / f"{name}a" / "ossie" / doc_name).read_bytes() == again
            new = _docs(tmp_path / f"{name}3")[doc_name]
            texts, objects = _contexts(_model(old)), _contexts(new)
            assert texts and set(texts) == set(objects)
            for where, text in texts.items():
                assert isinstance(text, str), where
                instructions, synonyms, examples = _context_lines(text)
                obj = objects[where]
                assert instructions == obj.get("instructions", ""), where
                assert synonyms == obj.get("synonyms", []), where
                assert examples == obj.get("examples", []), where
        area = "agami-example" if name == "s" else AREA
        # Reported once per file, not once per item, and only where the text form is written.
        assert _loss(old_report, "ai_context", area) == [("degraded", "carried_as_text")]
        files = len(_docs(tmp_path / f"{name}a"))
        assert len([x for x in _losses(old_report) if x["kind"] == "ai_context"]) == files
        assert _loss(new_report, "ai_context", area) == []


def _agami_block(doc: dict) -> dict:
    if doc["version"] == "0.1.1":
        (ext,) = _model(doc)["custom_extensions"]
        assert ext["vendor_name"] == "COMMON" and isinstance(ext["data"], str)
        return json.loads(ext["data"])["agami"]
    (ext,) = doc["nonportable_extensions"]
    assert ext["vendor_name"] == "AGAMI" and isinstance(ext["data"], dict)
    return ext["data"]


@pytest.mark.parametrize("version", ["0.1.1", "0.3"])
def test_default_filters_and_sign_off_travel_in_the_extension_block_as_text(
    model, tmp_path, version
):
    out = tmp_path / "out"
    report = _export(model, out, version)
    (doc,) = _docs(out).values()
    block = _agami_block(doc)
    assert block["default_filters"] == {
        "engine": "Snowflake",
        "usage": X.DEFAULT_FILTER_NOTE,
        "by_dataset": {"orders": ["orders.status != 'void'"]},
    }
    assert _loss(report, "default_filter", "orders#1") == [("degraded", "carried_as_text")]
    sign = {(s["kind"], s["name"]): s for s in block["sign_off"]}
    assert sign[("metric", "revenue")]["signed_off_at"] == "2026-02-01T00:00:00Z"
    assert sign[("table", "orders")]["review_state"] == "approved"
    assert sign[("metric", "revenue")]["id"] == X.export_id(
        "ossie", "metric", X.qualified_name(AREA, "metric", "revenue")
    )
    assert ("relationship", "orders->customers#2") not in sign  # it did not carry over
    assert _loss(report, "sign_off", AREA) == [("degraded", "carried_as_text")]
    text = "".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file())
    assert "you@example.com" not in text


def test_sqlite_model_sql_stays_home(tmp_path):
    report = _sample(tmp_path / "out")
    (doc,) = _docs(tmp_path / "out").values()
    assert "default_filters" not in _agami_block(doc)
    assert ("dropped", "sql_not_for_target") in _loss(report, "default_filter", "orders#1")
    assert all(d["source"].startswith("DEMO.SALES_DATA.") for d in _model(doc)["datasets"])


def test_a_table_defined_by_another_engines_sql_and_an_empty_area(model):
    org = load_datasource(model)
    target = X.TARGETS["ossie"]
    view, _ = X.build_view(
        org, org.subject_area(AREA), target, X.ExportOptions(("ossie",)), "Snowflake", [], []
    )
    elsewhere = dataclasses.replace(view, sql_engine=None)
    doc, losses = O.build_model(elsewhere, O.V011)
    assert "big_orders" not in {d["name"] for d in doc["datasets"]}
    by = {(x.kind, x.name): x.reason for x in losses}
    assert by[("table", "big_orders")] == "sql_not_for_target"
    assert by[("relationship", "orders->big_orders")] == "depends_on_excluded"

    out = O.write_ossie(dataclasses.replace(view, tables=()), X.ExportOptions(("ossie",)))
    assert out.files == {} and [(x.kind, x.reason) for x in out.losses][-1] == (
        "subject_area",
        "target_rule",
    )


def test_a_relationship_name_collision_drops_the_later(model):
    org = load_datasource(model)
    options = X.ExportOptions(("ossie",))
    view, _ = X.build_view(
        org, org.subject_area(AREA), X.TARGETS["ossie"], options, "Snowflake", [], []
    )
    first = view.relationships[0]
    twin = X.ExportRelationship(first.name.upper(), first.relationship)
    doc, losses = O.build_model(dataclasses.replace(view, relationships=(first, twin)), O.V03)
    assert [r["name"] for r in doc["relationships"]] == [O.ossie_name(first.name)]
    collided = [x.name for x in losses if (x.kind, x.reason) == ("relationship", "name_collision")]
    assert collided == [twin.name]


def test_ossie_version_is_checked(model, tmp_path):
    with pytest.raises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
        _sm(
            str(model),
            "--target",
            "ossie",
            "--out",
            str(tmp_path / "out"),
            "--ossie-version",
            "0.2",
        )
    with pytest.raises(X.ExportRefused, match="--ossie-version"):
        O.ossie_version(X.ExportOptions(("ossie",), target_args={"ossie_version": "9"}))
    assert O.ossie_version(X.ExportOptions(("ossie",))) == O.V011
    report = _export(model, tmp_path / "out", "0.3")
    assert report["options"]["target_args"] == {"ossie_version": "0.3"}
    # Not given, the flag stays out of the report, so another target's run does not name it.
    rc, text = _sm(str(model), "--target", "ossie", "--out", str(tmp_path / "plain"))
    assert rc == 0 and json.loads(text)["options"]["target_args"] == {}
    assert next(iter(_docs(tmp_path / "plain").values()))["version"] == "0.1.1"


def test_names_and_column_references():
    assert O.ossie_name("orders->customers#2") == "orders_to_customers_2"
    assert O.ossie_name("Average Order-Value") == "average_order_value"
    assert O.ossie_name("!!") == "unnamed"
    assert O.column_ref("amount") == "amount" and O.column_ref('a "b"') == '"a ""b"""'


def test_the_privacy_scan_covers_the_ossie_module():
    spec = importlib.util.spec_from_file_location(
        "privacy", REPO / "tests" / "test_privacy_no_network.py"
    )
    privacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(privacy)
    module = PKG_SRC / "semantic_model" / "export_ossie.py"
    assert module in privacy.SCRIPTS
    assert not any(rx.search(module.read_text(encoding="utf-8")) for rx in privacy._FORBIDDEN_RE)


@pytest.mark.parametrize(
    "area,stem",
    [("sales/eu", "sales_eu"), ("../../escape", "escape"), ("Sales EU", "sales_eu"), ("..", "unnamed")],
)
def test_the_file_is_named_by_the_models_own_ossie_name(model, area, stem):
    org = load_datasource(model)
    options = X.ExportOptions(("ossie",))
    view, _ = X.build_view(
        org, org.subject_area(AREA), X.TARGETS["ossie"], options, "Snowflake", [], []
    )
    out = O.write_ossie(dataclasses.replace(view, area=area), options)
    (path,) = out.files
    # One file directly under ossie/, which the core accepts as a writer's own path.
    assert path == f"{stem}.ossie.yaml" and X._safe_path(path) == path
    assert _model(yaml.safe_load(out.files[path]))["name"] == stem


def test_writers_read_the_view_not_the_raw_model_fields_it_resolved():
    """A writer reads metric SQL from `ExportMetric.sql` and never a raw binding or sensitive flag:
    the core already resolved and filtered those, and reports what they cost. (A copy's
    `review_state` is the state the core admitted it on, so the Ossie sign-off block may read it.)"""
    import ast

    raw = {"bindings", "sensitive"}
    offenders = []
    for path in sorted((PKG_SRC / "semantic_model").glob("export_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [
            f"{path.name}:{n.lineno} .{n.attr}"
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr in raw
        ]
    assert offenders == []


def test_a_binding_the_view_did_not_resolve_is_never_reported_by_the_writer(model):
    org = load_datasource(model)
    options = X.ExportOptions(("ossie",))
    view, _ = X.build_view(
        org, org.subject_area(AREA), X.TARGETS["ossie"], options, "Snowflake", [], []
    )
    _, losses = O.build_model(view, O.V011)
    assert not [x for x in losses if x.kind == "metric" and x.reason == "no_home_in_target"]
