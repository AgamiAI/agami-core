"""agami-connect reads documents and describes larger models in parallel subagents.

A doc reader per document runs while the database is read; on a model with 2+ areas and more than
30 tables an enricher per area describes it. Subagents can't show question cards, so they only read
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


@pytest.mark.parametrize("name", ["agami-doc-reader", "agami-enricher"])
def test_each_agent_is_read_only_and_never_asks(name):
    meta, body = _agent(name)
    assert meta["name"] == name
    tools = {t.strip() for t in meta["tools"].split(",")}
    assert not tools & {"Edit", "NotebookEdit", "AskUserQuestion", "Agent"}, tools
    # the reader writes nothing; the enricher writes only its result file (Write, not a heredoc,
    # which a live run showed is easy to get wrong), and never the model
    assert ("Write" in tools) == (name == "agami-enricher")
    # neither runs commands: a subagent's commands each stop for permission (live run, Manual mode)
    assert "Bash" not in tools
    assert "never asks the person anything" in meta["description"]
    assert "**Never ask the person anything.**" in body
    # the reader returns its JSON; the enricher writes it to a file the engine applies whole
    assert ("One JSON object, nothing else after it" in body if name == "agami-doc-reader"
            else "Write one JSON object to `result_path`" in body)


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


def test_2a_uses_enrichers_only_on_a_larger_model_and_applies_with_allow():
    text = (REFS / "enrich.md").read_text(encoding="utf-8")
    assert "**2 or more subject areas and more than 30 tables**" in text
    assert 'scripts/sm" apply-enrichment "$ROOT" --area <area>' in text
    assert 'scripts/sm" enrich-input "$ROOT" --area <area> --profile <profile>' in text
    assert "**Launch the enrichers in one message, in the foreground**" in text
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
