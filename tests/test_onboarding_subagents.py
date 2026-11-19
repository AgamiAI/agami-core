"""agami-connect describes larger models and drafts their examples in parallel subagents.

On a model of more than 12 tables, an agami-enricher per chunk of at most 12 tables describes it and
an agami-example-writer per subject area drafts its example questions. Subagents can't show question
cards and don't get the plugin hook's view of the person, so they only read files the main chat
prepared with agami's own commands and write `agami-` result files under the scratch folder. The main
chat applies each file through existing verbs, one at a time; an enricher's edits go through
`sm curate --allow … --area … --tables …`, which refuses a batch that does anything but edit the
allowed fields of the chunk's own tables. At 12 tables or fewer, or with no Agent tool, the phases
run inline as before.
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
CONNECT = (SKILLS_DIR / "agami-connect" / "SKILL.md").read_text(encoding="utf-8")
PARALLEL = (PLUGIN / "shared" / "parallel-work.md").read_text(encoding="utf-8")
ENRICH_FIELDS = ",".join(cli.ENRICH_FIELDS)
AGENT_NAMES = ["agami-enricher", "agami-example-writer"]


def _agent(name: str) -> tuple[dict, str]:
    text = (AGENTS / f"{name}.md").read_text(encoding="utf-8")
    _, head, body = text.split("---\n", 2)
    return yaml.safe_load(head), body


# --- the agent files ---


@pytest.mark.parametrize("name", AGENT_NAMES)
def test_each_agent_reads_and_writes_files_only_and_never_asks(name):
    meta, body = _agent(name)
    assert meta["name"] == name
    # `tools` only narrows what the subagent can use; nothing pre-approves a command
    # (tests/test_skills_declare_no_allowed_tools.py holds the skills to the same).
    assert set(meta) == {"name", "description", "tools"}, meta
    assert {t.strip() for t in meta["tools"].split(",")} == {"Read", "Write"}
    assert "never runs a command, never asks the person anything" in meta["description"]
    assert len(meta["description"]) < 1024
    assert "You run no commands" in body and "You have no Bash" in body
    assert "**Never ask the person anything.**" in body
    assert "Never the model" in body
    assert "<artifacts_dir>/local/tmp/" in body  # results are scratch files the main chat applies
    assert "reply with only the" in body


def test_the_enricher_describes_only_its_chunk_and_proposes_no_join():
    meta, body = _agent("agami-enricher")
    assert "one per chunk" in meta["description"] and "more than 12 tables" in meta["description"]
    assert "**Describe only the tables in `tables`.**" in body
    assert "every table it rests" in body and "on is in your chunk" in body
    assert "Never propose a\n   join edit" in body
    for field in cli.ENRICH_FIELDS:
        assert f"`{field}" in body, field
    assert "Write `<result_stem>.json` last" in body


def test_the_example_writer_never_projects_a_sensitive_column_or_a_star():
    _, body = _agent("agami-example-writer")
    assert "**Never write a star projection**" in body
    assert "**Never project a column marked sensitive**" in body
    assert (
        '"area": "<area>"' in body
    )  # seed-examples refuses a seed without one when --area is left out


@pytest.mark.parametrize(
    "name,section",
    [
        ("agami-enricher", "### 2a — Descriptions"),
        ("agami-enricher", "### 2b — Entities"),
        ("agami-example-writer", "## Phase 5: Seed prompt examples"),
    ],
)
def test_each_agent_follows_the_skills_own_rules_not_a_copy(name, section):
    _, body = _agent(name)
    assert "<plugin_root>/skills/agami-connect/SKILL.md" in body
    assert section in CONNECT
    title = section.lstrip("# ").split(":")[0].split(" (")[0]
    assert title.split(" — ")[0] in body


# --- the skill and the shared rules ---


def test_both_phases_send_large_models_to_the_shared_rules():
    assert CONNECT.count("[shared/parallel-work.md](../../shared/parallel-work.md)") == 2
    describe = CONNECT[CONNECT.index("### 2a — Descriptions") : CONNECT.index("### 2b — Entities")]
    assert "**More than 12 tables: describe them in parallel.**" in describe
    examples = CONNECT[CONNECT.index("## Phase 5:") : CONNECT.index("## Phase 6:")]
    assert "**More than 12 tables: draft each area's examples in parallel.**" in examples


def test_small_models_and_no_agent_tool_keep_the_inline_path():
    describe = CONNECT[CONNECT.index("### 2a — Descriptions") : CONNECT.index("### 2b — Entities")]
    assert (
        "At 12 tables or fewer, or with no Agent tool, describe them yourself, one table after "
        "another, as written below."
    ) in describe
    examples = CONNECT[CONNECT.index("## Phase 5:") : CONNECT.index("## Phase 6:")]
    assert "Otherwise, generate them yourself, as written below." in examples
    when = PARALLEL[PARALLEL.index("## 0. When") : PARALLEL.index("## 1. Chunks")]
    assert "the model has **more than 12 tables**" in when
    assert "an **Agent tool**" in when and "**at least two** subagents" in when
    assert "do the phase **inline, exactly as written**" in when
    # the inline path itself is still all there: the sequential describe and seed commands
    assert (
        'scripts/sm" curate "<root>" --ops-file "<artifacts_dir>/local/tmp/agami-descriptions.json"'
        in CONNECT
    )
    assert (
        'scripts/sm" seed-examples "<root>" --profile <profile> --file "<artifacts_dir>/local/tmp/agami-seeds.json"'
        in CONNECT
    )


def test_results_are_collected_from_returned_calls_with_no_wait_verb():
    assert "wait-files" not in PARALLEL and "wait-files" not in CONNECT
    collect = PARALLEL[PARALLEL.index("## 3. Collect") : PARALLEL.index("## 4. Never")]
    assert "with no `run_in_background`" in PARALLEL
    assert (
        "Each Agent call returns when its subagent is done, so there is nothing to poll." in collect
    )
    assert "give it up in one line and do that piece\n  yourself, inline" in collect
    assert "never two at once" in collect
    assert "never\nedited, merged or split by you" in collect


def test_the_enricher_is_applied_through_curate_allow_held_to_its_chunk():
    assert (
        f'scripts/sm" curate "<root>" --ops-file "<artifacts_dir>/local/tmp/agami-enrich-<batch>-<chunk>'
        f'.ops.json" --allow {ENRICH_FIELDS} --area <area> --tables <t1,t2>'
    ) in PARALLEL
    assert 'scripts/sm" add "<root>" --kind entity --area <area>' in PARALLEL
    assert 'scripts/sm" batch-id' in PARALLEL
    assert "<plugin_root>/agents/<agent>.md" in PARALLEL  # the fallback names the real files
    for name in AGENT_NAMES:
        assert f"agami-core:{name}" in PARALLEL
        assert (AGENTS / f"{name}.md").is_file()


# --- sm curate --allow ---


@pytest.fixture
def model(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    shutil.copytree(PLUGIN / "samples" / "store" / "model", root)
    return root


def _curate(model: Path, tmp_path: Path, ops, capsys, *extra: str) -> tuple[int, dict]:
    f = tmp_path / "agami-ops.json"
    f.write_text(json.dumps(ops))
    rc = cli.main(["curate", str(model), "--ops-file", str(f), *extra])
    return rc, json.loads(capsys.readouterr().out)


def _edit(area: str = "agami-example", field: str = "description", name: str = "orders") -> dict:
    return {
        "op": "edit",
        "kind": "table",
        "area": area,
        "name": name,
        "field": field,
        "value": "One row per customer order.",
        "source": "ai",
    }


def _yaml(model: Path) -> list[str]:
    return sorted(p.read_text() for p in model.rglob("*.yaml"))


def test_allow_applies_a_batch_of_allowed_edits(model, tmp_path, capsys):
    rc, out = _curate(
        model,
        tmp_path,
        [_edit()],
        capsys,
        "--allow",
        ENRICH_FIELDS,
        "--area",
        "agami-example",
        "--tables",
        "orders,customers",
    )
    assert rc == 0 and out["validated"] is True, out
    assert "One row per customer order." in "".join(_yaml(model))


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "approve", "kind": "metric", "area": "agami-example", "name": "revenue"},
        {"op": "exclude", "kind": "table", "area": "agami-example", "name": "orders"},
        _edit(field="review_state"),
        _edit(area="another-area"),
        _edit(name="products"),
        _edit(name="../tables/orders"),
        {
            "op": "edit",
            "kind": "relationship",
            "area": "agami-example",
            "name": "orders_customers",
            "field": "description",
            "value": "x",
        },
        "not an op",
    ],
    ids=[
        "approve",
        "exclude",
        "other-field",
        "other-area",
        "other-table",
        "path-name",
        "join",
        "string",
    ],
)
def test_allow_refuses_the_whole_batch_for_one_op_outside_it(model, tmp_path, capsys, bad):
    before = _yaml(model)
    rc, out = _curate(
        model,
        tmp_path,
        [_edit(), bad],
        capsys,
        "--allow",
        ENRICH_FIELDS,
        "--area",
        "agami-example",
        "--tables",
        "orders",
    )
    assert rc == 1 and out["applied"] == 0 and len(out["refused"]) == 1, out
    assert _yaml(model) == before  # nothing written


def test_allow_refuses_a_file_that_is_not_a_list(model, tmp_path, capsys):
    rc, out = _curate(model, tmp_path, {"ops": "x"}, capsys, "--allow", ENRICH_FIELDS)
    assert rc == 1 and out["refused"] == ["the ops file is not a list of ops"]


def test_area_and_tables_only_narrow_allow(model, tmp_path, capsys):
    before = _yaml(model)
    rc, out = _curate(model, tmp_path, [_edit()], capsys, "--tables", "orders")
    assert rc == 2 and "--allow" in out["error"]
    assert _yaml(model) == before


def test_curate_without_allow_is_unchanged(model, tmp_path, capsys):
    rc, out = _curate(model, tmp_path, [_edit()], capsys)
    assert rc == 0 and out["validated"] is True, out


# --- sm profile --out ---


def test_profile_writes_its_output_to_a_scratch_file(model, tmp_path, capsys, monkeypatch):
    intro = model / ".introspect"
    intro.mkdir()
    (intro / "profile.json").write_text(json.dumps({"tables": {}, "joins": []}))
    scratch = tmp_path / "local" / "tmp"
    scratch.mkdir(parents=True)
    out_file = scratch / "agami-enrich-b1-agami-example-1.profile.json"
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    rc = cli.main(["profile", str(model), "--table", "orders", "customers", "--out", str(out_file)])
    printed = json.loads(capsys.readouterr().out)
    assert rc == 0, printed
    assert printed["out"] == str(out_file)
    assert json.loads(out_file.read_text()) == {"tables": {}, "joins": []}


def test_the_enricher_writes_entities_in_the_canonical_shape_and_a_skip_is_redrafted():
    _, body = _agent("agami-enricher")
    assert "`<plugin_root>/shared/metric-entity-shape.md`" in body
    assert (PLUGIN / "shared" / "metric-entity-shape.md").is_file()
    assert "is not fixed in the file: draft it\n  yourself as 2b says" in PARALLEL
