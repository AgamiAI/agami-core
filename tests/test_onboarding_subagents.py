"""agami-connect reads documents and describes larger models in parallel subagents.

A doc reader per document runs while the database is read; on a model of 2 or more chunks an
enricher per chunk describes it, and an example writer per area drafts example questions during
sign-off. Subagents can't show question cards, so they only read
and return data: the main chat writes, one batch at a time, and an enricher's edits go through
`sm curate --allow`, which refuses a batch that does anything but edit the allowed fields.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from skill_docs import PLUGIN, SKILLS_DIR

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli  # noqa: E402

AGENTS = PLUGIN / "agents"
REFS = SKILLS_DIR / "agami-connect" / "references"
ENRICH_FIELDS = "description,description_source,choice_field,column_groups,column_group_descriptions"


def _agent(name: str) -> tuple[dict, str]:
    text = (AGENTS / f"{name}.md").read_text(encoding="utf-8")
    _, head, body = text.split("---\n", 2)
    return yaml.safe_load(head), body


@pytest.mark.parametrize("name", ["agami-doc-reader", "agami-enricher", "agami-example-writer"])
def test_each_agent_is_read_only_and_never_asks(name):
    meta, body = _agent(name)
    assert meta["name"] == name
    tools = {t.strip() for t in meta["tools"].split(",")}
    assert not tools & {"Edit", "NotebookEdit", "AskUserQuestion", "Agent"}, tools
    # the reader writes nothing; the enricher writes only its result file (Write, not a heredoc,
    # which a live run showed is easy to get wrong), and never the model
    assert ("Write" in tools) == (name != "agami-doc-reader")
    if name != "agami-doc-reader":
        assert tools == {"Read", "Write"}, tools
    # neither runs commands: a subagent's commands each stop for permission (live run, Manual mode)
    assert "Bash" not in tools
    assert "never asks the person anything" in meta["description"]
    assert "**Never ask the person anything.**" in body
    # the reader returns its JSON; the enricher writes it to a file the engine applies whole
    assert ("One JSON object, nothing else after it" in body if name == "agami-doc-reader"
            else "to `result_path`" in body)
    if name == "agami-example-writer":
        assert "never writes the model, never runs a command, never asks the person" in meta["description"]
        assert len(meta["description"]) < 1024
        assert "You run no commands" in body and "Never the model" in body
        assert "skills/agami-connect/references/examples.md" in body  # one set of rules


def test_the_doc_reader_cannot_run_commands_and_leaves_parsed_files_alone():
    meta, body = _agent("agami-doc-reader")
    assert "Bash" not in meta["tools"]
    assert "**Only what the document says.**" in body
    assert '"skipped": "column dictionary"' in body  # exact parsers apply those, not a reader
    assert "at most 30 files or 250 KB" in body


def test_the_enricher_never_writes_and_reads_the_shared_rules():
    _, body = _agent("agami-enricher")
    assert "2. **Write only `result_path`.** Never the model, never anything else." in body
    assert "You run no commands" in body and "input_path" in body
    assert "skills/agami-connect/references/enrich.md" in body  # one set of rules, not a copy



def test_1_5_gives_each_document_its_own_reader_in_the_background():
    text = (REFS / "context-and-tables.md").read_text(encoding="utf-8")
    assert "**Each document gets its own reader, started the moment its path is known.**" in text
    assert "`agami-core:agami-doc-reader` subagent, one per document" in text
    assert "a project is one document" in text
    assert "At most 4 run at once" in text
    assert "<plugin_root>/agents/agami-doc-reader.md" in text  # the fallback names the real file


def test_2a_uses_enrichers_from_two_chunks_and_applies_with_tables():
    text = (REFS / "enrich.md").read_text(encoding="utf-8")
    assert "**2 or more chunks**" in text
    assert 'scripts/sm" apply-enrichment "$ROOT" --area <area> --tables <t1,t2,…>' in text
    assert 'scripts/sm" enrich-input "$ROOT" --area <area> --tables <t1,t2,…> --profile <profile>' in text
    assert "parallel-work.md" in text
    assert "never split the file yourself" in text  # no glue script between agent and engine
    assert "never two writes at once" in text
    assert "<plugin_root>/agents/agami-enricher.md" in text


@pytest.fixture
def model(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    shutil.copytree(PLUGIN / "samples" / "store" / "model", root)
    return root


def _curate(model: Path, tmp_path: Path, ops: list[dict], capsys, *extra: str) -> tuple[int, dict]:
    f = tmp_path / "ops.json"
    f.write_text(json.dumps(ops))
    rc = cli.main(["curate", str(model), "--ops-file", str(f), *extra])
    return rc, json.loads(capsys.readouterr().out)


def _edit(area: str, field: str = "description") -> dict:
    return {"op": "edit", "kind": "table", "area": area, "name": "orders", "field": field,
            "value": "One row per customer order."}


def test_allow_applies_a_batch_of_allowed_edits(model, tmp_path, capsys):
    rc, out = _curate(model, tmp_path, [_edit("agami-example")], capsys,
                      "--allow", ENRICH_FIELDS, "--area", "agami-example")
    assert rc == 0 and out["validated"] is True


@pytest.mark.parametrize("bad", [
    {"op": "approve", "kind": "metric", "area": "agami-example", "name": "revenue"},
    {"op": "exclude", "kind": "table", "area": "agami-example", "name": "orders"},
    _edit("agami-example", field="review_state"),
    _edit("another-area"),
])
def test_allow_refuses_the_whole_batch_for_one_op_outside_it(model, tmp_path, capsys, bad):
    before = sorted(p.read_text() for p in model.rglob("*.yaml"))
    rc, out = _curate(model, tmp_path, [_edit("agami-example"), bad], capsys,
                      "--allow", ENRICH_FIELDS, "--area", "agami-example")
    assert rc == 1 and out["applied"] == 0 and len(out["refused"]) == 1
    assert sorted(p.read_text() for p in model.rglob("*.yaml")) == before  # nothing written


def test_allow_still_takes_the_ops_file_as_a_positional(model, tmp_path, capsys):
    f = tmp_path / "ops.json"
    f.write_text(json.dumps([_edit("agami-example")]))
    rc = cli.main(["curate", str(model), "--allow", ENRICH_FIELDS, str(f)])
    assert rc == 0, capsys.readouterr().out


def _result(area: str, **over) -> dict:
    base = {
        "area": area,
        "column_descriptions": f"{area}.orders.status\tOrder status: 0 pending, 1 paid (handbook).\n",
        "curate_ops": [_edit(area)],
        "entities": [{"name": "buyer", "plural": "buyers", "other_names": [],
                      "maps_to": [{"table": "customers", "column": "id", "primary": True}],
                      "confidence": "inferred", "review_state": "unreviewed"}],
        "metric_ideas": [{"name": "refund rate", "definition": "refunded ÷ paid"}],
        "questions": ["What does status 3 mean?"],
    }
    return {**base, **over}


def _apply(model: Path, tmp_path: Path, result: dict, area: str, capsys) -> tuple[int, dict]:
    f = tmp_path / "result.json"
    f.write_text(json.dumps(result))
    rc = cli.main(["apply-enrichment", str(model), "--file", str(f), "--area", area])
    return rc, json.loads(capsys.readouterr().out)


def test_apply_enrichment_applies_one_area_and_passes_the_rest_back(model, tmp_path, capsys):
    rc, out = _apply(model, tmp_path, _result("agami-example"), "agami-example", capsys)
    assert rc == 0 and out["applied"] is True, out
    assert (out["described"], out["edited"], out["entities"]) == (1, 1, 1)
    assert out["pass_on"]["questions"] == ["What does status 3 mean?"]
    assert out["pass_on"]["metric_ideas"][0]["name"] == "refund rate"


@pytest.mark.parametrize("bad", [
    {"curate_ops": [_edit("agami-example"),
                    {"op": "approve", "kind": "metric", "area": "agami-example", "name": "revenue"}]},
    {"curate_ops": [_edit("agami-example", field="review_state")]},
    {"entities": {"name": "not a list"}},
])
def test_apply_enrichment_checks_everything_before_writing(model, tmp_path, capsys, bad):
    before = sorted(p.read_text() for p in model.rglob("*.yaml"))
    rc, out = _apply(model, tmp_path, _result("agami-example", **bad), "agami-example", capsys)
    assert rc != 0 and out["applied"] is False
    assert sorted(p.read_text() for p in model.rglob("*.yaml")) == before


def test_apply_enrichment_refuses_a_file_for_another_area(model, tmp_path, capsys):
    rc, out = _apply(model, tmp_path, _result("support"), "agami-example", capsys)
    assert rc == 2 and "not an enricher result for area" in out["error"]


def test_a_description_for_an_unknown_area_is_reported_not_written(model, tmp_path, capsys):
    result = _result("agami-example", column_descriptions="another-area.orders.status\tsomething\n")
    rc, out = _apply(model, tmp_path, result, "agami-example", capsys)
    assert rc == 0 and out["described"] == 0 and out["unmatched"]


# --- chunks: any model is described in parallel, at most CHUNK_MAX_TABLES tables per enricher ---

def _synthetic(tmp_path: Path, areas: dict[str, int], hidden: tuple[str, ...] = ()) -> Path:
    """A model with `areas` (name -> table count), tables t01, t02, … per area; `hidden` tables are
    excluded the way a curator excludes them (`review_state: rejected`)."""
    root = tmp_path / "acme"
    conn = root / "datasources" / "demo_sqlite"
    conn.mkdir(parents=True)
    (conn / "storage.yaml").write_text(yaml.safe_dump(
        {"name": "demo_sqlite", "storage_type": "SQLite",
         "storage_config": {"profile": "acme", "credentials_ref": "<artifacts_dir>/local/credentials"}}))
    for area, n in areas.items():
        tdir = root / "subject_areas" / area / "tables"
        tdir.mkdir(parents=True)
        names = [f"{area}_t{i:02d}" for i in range(1, n + 1)]
        (root / "subject_areas" / area / "subject_area.yaml").write_text(yaml.safe_dump(
            {"name": area, "description": "", "tables": [
                {"storage_connection": "demo_sqlite", "schema": "main", "table": t} for t in names]}))
        for t in names:
            doc = {"name": t, "schema": "main", "storage_connection": "demo_sqlite",
                   "columns": [{"name": "id", "type": "integer"}]}
            if t in hidden:
                doc["review_state"] = "rejected"
            (tdir / f"{t}.yaml").write_text(yaml.safe_dump(doc))
    (root / "datasource.yaml").write_text(yaml.safe_dump(
        {"datasource": "acme", "version": 1,
         "storage_connections": [{"name": "demo_sqlite", "ref": "datasources/demo_sqlite/storage.yaml"}],
         "subject_areas": [f"subject_areas/{a}" for a in areas]}))
    return root


def _chunks(root: Path, capsys, *extra: str) -> dict:
    assert cli.main(["enrich-input", str(root), "--chunks", *extra]) == 0
    return json.loads(capsys.readouterr().out)


def test_chunks_split_an_area_into_runs_of_at_most_twelve(tmp_path, capsys):
    out = _chunks(_synthetic(tmp_path, {"demo": 14}), capsys)
    assert cli.CHUNK_MAX_TABLES == 12
    assert [c["chunk"] for c in out["chunks"]] == ["demo-1", "demo-2"]
    assert [len(c["tables"]) for c in out["chunks"]] == [12, 2]
    assert out["chunks"][0]["tables"][0] == "demo_t01" and out["chunks"][1]["tables"] == [
        "demo_t13", "demo_t14"]  # the model's order, consecutive
    assert (out["tables"], out["max_tables"]) == (14, 12)
    assert {c["area"] for c in out["chunks"]} == {"demo"}


def test_a_small_model_is_one_chunk(tmp_path, capsys):
    out = _chunks(_synthetic(tmp_path, {"demo": 6}), capsys)
    assert [(c["chunk"], len(c["tables"])) for c in out["chunks"]] == [("demo-1", 6)]


def test_chunks_follow_the_areas_in_the_models_order(tmp_path, capsys):
    out = _chunks(_synthetic(tmp_path, {"acme": 5, "demo": 13}), capsys)
    assert [(c["chunk"], c["area"], len(c["tables"])) for c in out["chunks"]] == [
        ("acme-1", "acme", 5), ("demo-1", "demo", 12), ("demo-2", "demo", 1)]
    assert out["tables"] == 18


def test_chunks_leave_hidden_tables_out(tmp_path, capsys):
    out = _chunks(_synthetic(tmp_path, {"demo": 13}, hidden=("demo_t02",)), capsys)
    listed = [t for c in out["chunks"] for t in c["tables"]]
    assert "demo_t02" not in listed and len(listed) == out["tables"] == 12
    assert len(out["chunks"]) == 1


def test_chunks_honour_max_tables(tmp_path, capsys):
    out = _chunks(_synthetic(tmp_path, {"demo": 11}), capsys, "--max-tables", "5")
    assert [len(c["tables"]) for c in out["chunks"]] == [5, 5, 1] and out["max_tables"] == 5


def test_chunks_need_no_database(tmp_path, capsys, monkeypatch):
    from semantic_model import introspect
    monkeypatch.setattr(introspect, "make_execute_sql_runner",
                        lambda *a, **k: pytest.fail("--chunks read the database"))
    _chunks(_synthetic(tmp_path, {"demo": 3}), capsys)


def test_chunks_take_no_area_or_out(tmp_path, capsys):
    assert cli.main(["enrich-input", str(_synthetic(tmp_path, {"demo": 3})), "--chunks",
                     "--area", "demo"]) == 2
    assert "--chunks" in json.loads(capsys.readouterr().out)["error"]


def test_chunks_refuse_a_max_below_one(tmp_path, capsys):
    assert cli.main(["enrich-input", str(_synthetic(tmp_path, {"demo": 3})), "--chunks",
                     "--max-tables", "0"]) == 2


def test_enrich_input_without_chunks_still_needs_area_profile_and_out(model, capsys):
    assert cli.main(["enrich-input", str(model), "--area", "agami-example"]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


# --- enrich-input --tables: one chunk's input file ---

@pytest.fixture
def no_db(monkeypatch):
    from semantic_model import introspect
    monkeypatch.setattr(introspect, "make_execute_sql_runner",
                        lambda *a, **k: (lambda sql: [{"id": "1"}]))


def test_enrich_input_tables_writes_only_that_chunk(model, tmp_path, capsys, no_db):
    out_file = tmp_path / "in.json"
    assert cli.main(["enrich-input", str(model), "--area", "agami-example", "--profile", "acme",
                     "--out", str(out_file), "--tables", "orders, customers"]) == 0
    out = json.loads(capsys.readouterr().out)
    written = json.loads(out_file.read_text())
    assert sorted(t["table"] for t in written["tables"]) == ["customers", "orders"]
    assert written["chunk_tables"] == ["orders", "customers"] and out["tables"] == 2
    # the bundle is the whole area's, so an enricher still sees the joins out of its chunk
    assert written["bundle"] == json.loads(json.dumps(
        cli.L.get_subject_area_bundle(cli.L.load_datasource(model), "agami-example"), default=str))


def test_enrich_input_refuses_a_table_outside_the_area(model, tmp_path, capsys, no_db):
    out_file = tmp_path / "in.json"
    assert cli.main(["enrich-input", str(model), "--area", "agami-example", "--profile", "acme",
                     "--out", str(out_file), "--tables", "orders,nope"]) == 2
    assert json.loads(capsys.readouterr().out) == {"error": "unknown_table", "tables": ["nope"]}
    assert not out_file.exists()


# --- apply-enrichment --tables: a chunk's enricher edits only its own tables ---

def _apply_tables(model: Path, tmp_path: Path, result: dict, tables: str, capsys) -> tuple[int, dict]:
    f = tmp_path / "result.json"
    f.write_text(json.dumps(result))
    rc = cli.main(["apply-enrichment", str(model), "--file", str(f), "--area", "agami-example",
                   "--tables", tables])
    return rc, json.loads(capsys.readouterr().out)


def _stray() -> dict:
    return {"op": "edit", "kind": "table", "area": "agami-example", "name": "customers",
            "field": "description", "value": "One row per customer."}


@pytest.mark.parametrize("stray", [
    {"curate_ops": [_edit("agami-example"), _stray()]},
    {"column_descriptions": "agami-example.orders.status\tOrder status.\n"
                            "agami-example.customers.email\tContact address.\n"},
])
def test_apply_enrichment_tables_refuses_an_edit_outside_the_chunk(model, tmp_path, capsys, stray):
    before = sorted(p.read_text() for p in model.rglob("*.yaml"))
    rc, out = _apply_tables(model, tmp_path, _result("agami-example", **stray), "orders,products",
                            capsys)
    assert rc == 1 and out["applied"] is False
    assert len(out["refused"]) == 1 and "customers" in out["refused"][0]
    assert "orders, products" in out["error"]
    assert sorted(p.read_text() for p in model.rglob("*.yaml")) == before


def test_apply_enrichment_tables_applies_edits_inside_the_chunk(model, tmp_path, capsys):
    # the entity maps to customers, outside the chunk: entities are area-level and stay allowed
    rc, out = _apply_tables(model, tmp_path, _result("agami-example"), "orders", capsys)
    assert rc == 0 and out["applied"] is True, out
    assert (out["described"], out["edited"], out["entities"]) == (1, 1, 1)


@pytest.mark.parametrize("tables", ["", " , "])
def test_an_empty_tables_list_is_refused_not_read_as_the_whole_area(model, tmp_path, capsys, tables,
                                                                   no_db):
    # an unset variable in the skill must not lift a chunk's limit to the whole area
    before = sorted(p.read_text() for p in model.rglob("*.yaml"))
    rc, out = _apply_tables(model, tmp_path, _result("agami-example", curate_ops=[_stray()]), tables,
                            capsys)
    assert rc == 2 and out["applied"] is False
    assert sorted(p.read_text() for p in model.rglob("*.yaml")) == before
    out_file = tmp_path / "in.json"
    assert cli.main(["enrich-input", str(model), "--area", "agami-example", "--profile", "acme",
                     "--out", str(out_file), "--tables", tables]) == 2
    assert "--tables" in json.loads(capsys.readouterr().out)["error"] and not out_file.exists()


def test_apply_enrichment_tables_match_the_name_exactly(model, tmp_path, capsys):
    dotted = {**_edit("agami-example"), "name": "main.orders"}
    before = sorted(p.read_text() for p in model.rglob("*.yaml"))
    rc, out = _apply_tables(model, tmp_path, _result("agami-example", curate_ops=[dotted]), "orders",
                            capsys)
    assert rc == 1 and "main.orders" in out["refused"][0]
    assert sorted(p.read_text() for p in model.rglob("*.yaml")) == before


@pytest.mark.parametrize("over", [{"name": "../tables/customers"}, {"name": "../../other/tables/t"},
                                  {"name": "..\\x"}, {"name": 7}, {"area": "../other"}])
def test_a_path_shaped_name_is_refused_by_both_commands(model, tmp_path, capsys, over):
    # curate joins area and a table's name into a file path: `..` would reach past the area check
    bad = {**_edit("agami-example"), **over}
    before = sorted(p.read_text() for p in model.rglob("*.yaml"))
    rc, out = _apply(model, tmp_path, _result("agami-example", curate_ops=[bad]), "agami-example",
                     capsys)
    assert rc == 1 and out["applied"] is False
    if "name" in over:  # a bad area is already refused as another area, before the name check
        assert "not a plain name" in out["refused"][0]
    rc, out = _curate(model, tmp_path, [bad], capsys, "--allow", ENRICH_FIELDS)
    assert rc == 1 and out["applied"] == 0 and "not a plain name" in out["refused"][0]
    assert sorted(p.read_text() for p in model.rglob("*.yaml")) == before


def test_curate_allow_without_area_refuses_a_path_shaped_area(model, tmp_path, capsys):
    bad = {**_edit("agami-example"), "area": "agami-example/../agami-example"}
    rc, out = _curate(model, tmp_path, [bad], capsys, "--allow", ENRICH_FIELDS)
    assert rc == 1 and "area" in out["refused"][0]


def _with_column(model: Path, table: str, column: str) -> None:
    f = next((model / "subject_areas").glob(f"*/tables/{table}.yaml"))
    doc = yaml.safe_load(f.read_text())
    doc["columns"].append({"name": column, "type": "string", "description": ""})
    f.write_text(yaml.safe_dump(doc, sort_keys=False))


@pytest.mark.parametrize("column", ["price/unit", "in..out", "a\\b"])
def test_a_column_name_with_a_slash_or_dots_is_described(model, tmp_path, capsys, column):
    # a column is only compared, never part of a path, so a real name like `price/unit` is fine
    _with_column(model, "orders", column)
    op = {**_edit("agami-example"), "column": column, "value": "Price per unit, in dollars."}
    # the TSV addresses a column as area.table.column, so a dotted column name can only come as an op
    tsv = "" if "." in column else f"agami-example.orders.{column}\tPrice per unit, in dollars.\n"
    rc, out = _apply_tables(model, tmp_path,
                            _result("agami-example", column_descriptions=tsv, curate_ops=[op]),
                            "orders", capsys)
    assert rc == 0 and out["applied"] is True and out["edited"] == 1, out
    assert out["described"] == (0 if "." in column else 1)
    doc = yaml.safe_load(next((model / "subject_areas").glob("*/tables/orders.yaml")).read_text())
    assert next(c for c in doc["columns"] if c["name"] == column)["description"] == (
        "Price per unit, in dollars.")


def test_a_metric_named_with_a_slash_passes_allow(model, tmp_path, capsys):
    # entity and metric names are slugged before they reach a path
    f = next((model / "subject_areas").glob("*/metrics/total_refunds.yaml"))
    doc = yaml.safe_load(f.read_text())
    doc["name"] = "active subscriptions / order"
    f.unlink()
    (f.parent / "active_subscriptions_order.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    op = {"op": "edit", "kind": "metric", "area": "agami-example", "name": "active subscriptions / order",
          "field": "description", "value": "Active subscriptions per order."}
    rc, out = _curate(model, tmp_path, [op], capsys, "--allow", "description", "--area", "agami-example")
    assert rc == 0 and out["validated"] is True, out


def _with_other_area_join(model: Path) -> None:
    """A second area holding its own join, and a cross-area join in datasource.yaml."""
    src = next((model / "subject_areas").glob("*/relationships.yaml"))
    other = model / "subject_areas" / "other"
    other.mkdir()
    (other / "relationships.yaml").write_text(src.read_text())
    ds = yaml.safe_load((model / "datasource.yaml").read_text())
    rel = yaml.safe_load(src.read_text())["relationships"][0]
    ds["cross_subject_area_relationships"] = [
        {**rel, "from_subject_area": "agami-example", "to_subject_area": "other"}]
    (model / "datasource.yaml").write_text(yaml.safe_dump(ds, sort_keys=False))


@pytest.mark.parametrize("join", ["invoices->subscriptions", "order_items->products"])
def test_a_join_edit_is_refused_when_an_area_is_given(model, tmp_path, capsys, join):
    # curate falls back to every area and the cross-area joins when the named area lacks the join
    _with_other_area_join(model)
    op = {"op": "edit", "kind": "relationship", "area": "agami-example", "name": join,
          "field": "description", "value": "Each invoice bills one subscription."}
    files = [model / "datasource.yaml", model / "subject_areas" / "other" / "relationships.yaml"]
    before = [f.read_bytes() for f in files] + [sorted(p.read_text() for p in model.rglob("*.yaml"))]
    rc, out = _apply(model, tmp_path, _result("agami-example", curate_ops=[op]), "agami-example",
                     capsys)
    assert rc == 1 and out["applied"] is False and "a join" in out["refused"][0]
    rc, out = _curate(model, tmp_path, [op], capsys, "--allow", "description", "--area",
                      "agami-example")
    assert rc == 1 and out["applied"] == 0 and "a join" in out["refused"][0]
    after = [f.read_bytes() for f in files] + [sorted(p.read_text() for p in model.rglob("*.yaml"))]
    assert after == before


@pytest.mark.parametrize("kind", [["table"], {"t": 1}, 3])
def test_an_odd_kind_is_refused_not_a_crash(model, tmp_path, capsys, kind):
    rc, out = _curate(model, tmp_path, [{**_edit("agami-example"), "kind": kind}], capsys,
                      "--allow", ENRICH_FIELDS, "--area", "agami-example")
    assert rc == 1 and out["applied"] == 0 and len(out["refused"]) == 1
