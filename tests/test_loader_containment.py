"""A model folder can't make the loader, or the read verbs the permission hook approves, read or
print a file outside it.

A model folder can come from anyone (a cloned repository, a shared drive). The loader opened every
path the model named (`ref: ../../x`, an absolute subject-area path, a symlinked table file)
unchecked, and a validation error echoed the values it read, so `sm areas <root>` printed parts of an
outside YAML file. Now every such path must resolve inside the model folder, and a load error names
the file and field, never a value.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli, steps  # noqa: E402
from semantic_model import loader as L  # noqa: E402

from skill_docs import PLUGIN  # noqa: E402

SAMPLE = PLUGIN / "samples" / "store" / "model"
SECRET = "s3cr3t-do-not-print"
SYMLINKS = pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32",
                              reason="symlinks")


@pytest.fixture
def model(tmp_path: Path) -> Path:
    root = tmp_path / "art" / "acme"
    shutil.copytree(SAMPLE, root)
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    f = out / "hosts.yml"
    f.write_text(yaml.safe_dump({"github.com": {"oauth_token": SECRET, "user": SECRET},
                                 "name": SECRET, "storage_type": SECRET}))
    return f


def _datasource(model: Path) -> dict:
    return yaml.safe_load((model / "datasource.yaml").read_text())


def _write_datasource(model: Path, doc: dict) -> None:
    (model / "datasource.yaml").write_text(yaml.safe_dump(doc))


def _run(argv: list[str], capsys) -> tuple[object, str]:
    try:
        rc = cli.main(argv)
    except Exception as exc:  # noqa: BLE001 — the message is what a traceback would print
        rc = exc
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    out = capsys.readouterr()
    return rc, out.out + out.err


@pytest.mark.parametrize("ref", ["../../out/hosts.yml", "ABSOLUTE"])
def test_a_storage_ref_outside_the_model_is_refused_without_its_content(model, outside, capsys, ref):
    ref = str(outside) if ref == "ABSOLUTE" else ref
    doc = _datasource(model)
    doc["storage_connections"] = [{"ref": ref}]
    _write_datasource(model, doc)
    with pytest.raises(L.ModelFileError) as exc:
        L.load_datasource(model)
    assert "storage_connections[0].ref" in str(exc.value) and "outside the model folder" in str(exc.value)
    assert SECRET not in str(exc.value)
    for verb in ("areas", "overview", "coverage", "review-queue", "review-items", "model-tree",
                 "summary"):
        rc, printed = _run([verb, str(model)], capsys)
        assert rc != 0, verb
        assert SECRET not in printed and "oauth_token" not in printed and "github.com" not in printed, verb


def test_steps_problem_line_carries_nothing_of_the_outside_file(model, outside, capsys):
    doc = _datasource(model)
    doc["storage_connections"] = [{"ref": "../../out/hosts.yml"}]
    _write_datasource(model, doc)
    rc, printed = _run(["steps", str(model)], capsys)
    assert rc == 0
    problem = json.loads(printed)["problem"]
    assert "outside the model folder" in problem
    assert SECRET not in printed and "oauth_token" not in printed


def test_a_subject_area_path_outside_the_model_is_refused(model, tmp_path):
    area = tmp_path / "elsewhere"
    shutil.copytree(model / "subject_areas" / "agami-example", area)
    doc = _datasource(model)
    doc["subject_areas"] = [str(area)]
    _write_datasource(model, doc)
    with pytest.raises(L.ModelFileError, match="subject_areas\\[0\\]"):
        L.load_datasource(model)
    doc["subject_areas"] = ["../../elsewhere"]
    _write_datasource(model, doc)
    with pytest.raises(L.ModelFileError, match="outside the model folder"):
        L.load_datasource(model)


@SYMLINKS
def test_a_symlinked_table_file_pointing_outside_is_refused(model, outside, capsys):
    tables = model / "subject_areas" / "agami-example" / "tables"
    (tables / "zz_link.yaml").symlink_to(outside)
    with pytest.raises(L.ModelFileError, match="zz_link.yaml.*outside the model folder"):
        L.load_datasource(model)
    rc, printed = _run(["areas", str(model)], capsys)
    assert rc != 0 and SECRET not in printed and "oauth_token" not in printed


@SYMLINKS
def test_a_symlinked_tables_folder_pointing_outside_is_refused(model, tmp_path):
    tables = model / "subject_areas" / "agami-example" / "tables"
    copy = tmp_path / "tables-elsewhere"
    shutil.move(str(tables), copy)
    tables.symlink_to(copy)
    with pytest.raises(L.ModelFileError, match="outside the model folder"):
        L.load_datasource(model)


@SYMLINKS
def test_a_link_inside_the_model_to_another_model_file_still_loads(model):
    tables = model / "subject_areas" / "agami-example" / "tables"
    first = sorted(tables.glob("*.yaml"))[0]
    moved = model / "kept" / first.name
    moved.parent.mkdir()
    shutil.move(str(first), moved)
    first.symlink_to(moved)
    assert L.load_datasource(model).subject_areas


def test_an_ordinary_validation_error_names_the_file_and_field_without_values(model, capsys):
    tables = model / "subject_areas" / "agami-example" / "tables"
    target = sorted(tables.glob("*.yaml"))[0]
    doc = yaml.safe_load(target.read_text())
    doc["columns"][0]["sensitive"] = {"nested": SECRET}
    target.write_text(yaml.safe_dump(doc))
    with pytest.raises(L.ModelFileError) as exc:
        L.load_datasource(model)
    message = str(exc.value)
    assert message.startswith(f"subject_areas/agami-example/tables/{target.name}: ")
    assert "columns.0.sensitive" in message
    assert SECRET not in message
    rc, printed = _run(["areas", str(model)], capsys)
    assert rc != 0 and target.name in printed and SECRET not in printed


def test_a_non_mapping_file_is_reported_by_name(model):
    tables = model / "subject_areas" / "agami-example" / "tables"
    target = sorted(tables.glob("*.yaml"))[0]
    target.write_text(yaml.safe_dump([SECRET]))
    with pytest.raises(L.ModelFileError, match=f"{target.name}: expected a mapping") as exc:
        L.load_datasource(model)
    assert SECRET not in str(exc.value)


def test_a_datasource_level_error_reports_no_value(model):
    doc = _datasource(model)
    doc["fiscal_year_start_month"] = SECRET
    _write_datasource(model, doc)
    with pytest.raises(L.ModelFileError) as exc:
        L.load_datasource(model)
    assert str(exc.value).startswith("datasource.yaml: fiscal_year_start_month")
    assert SECRET not in str(exc.value)


def test_an_area_named_to_climb_out_is_refused_for_its_examples(model, tmp_path):
    with pytest.raises(L.ModelFileError, match="outside the model folder"):
        out = tmp_path / "x" / "examples.yaml"
        out.parent.mkdir()
        out.write_text(yaml.safe_dump({"examples": [{"question": SECRET}]}))
        L.list_prompt_examples(model, "../../../x")


@SYMLINKS
def test_model_logs_that_link_out_are_neither_read_nor_written(model, tmp_path, capsys):
    secret = tmp_path / "id_rsa"
    secret.write_text(f"{SECRET}\n")
    introspect = model / ".introspect"
    introspect.mkdir(exist_ok=True)
    (introspect / "progress.log").symlink_to(secret)
    rc, printed = _run(["progress", str(model)], capsys)
    assert rc == 0 and SECRET not in printed

    target = tmp_path / "zshrc"
    target.write_text("")
    (introspect / "timeline.jsonl").symlink_to(target)
    rc, printed = _run(["steps", str(model), "--mark", "build=done"], capsys)
    assert rc == 2 and "out of the model folder" in printed
    assert target.read_text() == ""

    (model / "curation_log.jsonl").unlink(missing_ok=True)
    (model / "curation_log.jsonl").symlink_to(secret)
    from semantic_model import curate
    assert curate.history(model) == []


@SYMLINKS
def test_an_introspect_folder_that_links_out_gets_no_mark(model, tmp_path, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(model / ".introspect", ignore_errors=True)
    (model / ".introspect").symlink_to(elsewhere)
    with pytest.raises(ValueError, match="out of the model folder"):
        steps.mark(model, "build", "done")
    assert not list(elsewhere.iterdir())
