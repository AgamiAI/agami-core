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
    assert not tools & {"Write", "Edit", "NotebookEdit", "AskUserQuestion", "Agent"}, tools
    assert "never asks the person anything" in meta["description"]
    assert "**Never ask the person anything.**" in body
    assert "One JSON object, nothing else after it" in body


def test_the_doc_reader_cannot_run_commands_and_leaves_parsed_files_alone():
    meta, body = _agent("agami-doc-reader")
    assert "Bash" not in meta["tools"]
    assert "**Only what the document says.**" in body
    assert '"skipped": "column dictionary"' in body  # exact parsers apply those, not a reader
    assert "at most 30 files or 250 KB" in body


def test_the_enricher_never_writes_and_reads_the_shared_rules():
    _, body = _agent("agami-enricher")
    assert "1. **Never write.**" in body
    assert "skills/agami-connect/references/enrich.md" in body  # one set of rules, not a copy
    assert "never run two queries at once" in body
    assert "--build-profile" in body


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
    assert f"--allow {ENRICH_FIELDS} --area <area>" in text
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
