"""`seed-examples`, `seed-validate`, `remove-example` and the examples dashboard take several
subject areas per call, or every area when no `--area` is given (#544). One `--area` prints
exactly what it printed before."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from semantic_model import cli, curate, introspect  # noqa: E402
from semantic_model.loader import list_prompt_examples  # noqa: E402

AREAS = {"sales": "orders", "ops": "shipments"}


def _model(root: Path) -> None:
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": [f"subject_areas/{a}" for a in AREAS]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    for area, table in AREAS.items():
        d = root / "subject_areas" / area
        (d / "tables").mkdir(parents=True)
        (d / "subject_area.yaml").write_text(yaml.safe_dump({
            "name": area, "tables": [{"storage_connection": "c", "schema": "public", "table": table}]}))
        (d / "tables" / f"{table}.yaml").write_text(yaml.safe_dump({
            "name": table, "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": table,
            "columns": [{"name": "id", "type": "integer", "primary_key": True, "description": "the id"},
                        {"name": "total", "type": "decimal", "description": "the total"}]}))


def _examples(root: Path) -> None:
    curate.add_examples(root, "sales", [
        {"question": "how many rows?", "sql": "SELECT COUNT(*) AS n FROM orders"},
        {"question": "total billed?", "sql": "SELECT SUM(total) AS total FROM orders"}])
    curate.add_examples(root, "ops", [
        {"question": "how many rows?", "sql": "SELECT COUNT(*) AS n FROM shipments"},
        {"question": "broken seed", "sql": "SELECT id FROM does_not_exist"}])


def _run(args: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(args)
    return rc, buf.getvalue()


@pytest.fixture
def fake_db(monkeypatch):
    """Stand in for execute_sql.py: a statement on `does_not_exist` errors, any other returns 42."""
    calls = []

    def fake_run(cmd, capture_output, text, env, encoding=None):
        calls.append(cmd)
        sql = cmd[cmd.index("--sql") + 1]
        r = subprocess.CompletedProcess(cmd, 0, "n\n42\n", "")
        if "does_not_exist" in sql:
            r.returncode, r.stdout, r.stderr = 1, "", "relation does_not_exist does not exist"
        return r

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_seed_validate_with_no_area_reports_every_area_together(tmp_path, fake_db):
    _model(tmp_path)
    _examples(tmp_path)
    rc, out = _run(["seed-validate", str(tmp_path), "--profile", "p"])
    assert rc == 0
    d = json.loads(out)
    assert d["areas"] == [{"area": "sales", "examples": 2, "errored": 0},
                          {"area": "ops", "examples": 2, "errored": 1}]
    assert d["totals"] == {"areas": 2, "examples": 4, "errored": 1}
    assert [(it["area"], it["n"], it["question"]) for it in d["items"]] == [
        ("sales", 1, "how many rows?"), ("sales", 2, "total billed?"),
        ("ops", 1, "how many rows?"), ("ops", 2, "broken seed")]
    # each seed still runs through execute_sql scoped to its own area, as a single-area call does
    assert [c[c.index("--area") + 1] for c in fake_db] == ["sales", "sales", "ops", "ops"]


def test_seed_validate_takes_area_repeated(tmp_path, fake_db):
    _model(tmp_path)
    _examples(tmp_path)
    rc, out = _run(["seed-validate", str(tmp_path), "--profile", "p", "--area", "ops", "--area", "sales"])
    d = json.loads(out)
    assert rc == 0 and [a["area"] for a in d["areas"]] == ["ops", "sales"]
    assert d["totals"]["examples"] == 4


def test_one_area_prints_what_it_did_before(tmp_path, fake_db):
    """One `--area` is byte-for-byte the pre-#544 output, and its items are what the combined
    call carries for that area, less the `area` label."""
    _model(tmp_path)
    _examples(tmp_path)
    rc, single = _run(["seed-validate", str(tmp_path), "--profile", "p", "--area", "sales"])
    assert rc == 0
    expected = [{"n": 1, "question": "how many rows?", "sql": "SELECT COUNT(*) AS n FROM orders",
                 "state": "unreviewed", "row_headers": ["n"], "row_preview": [["42"]], "row_count": 1},
                {"n": 2, "question": "total billed?", "sql": "SELECT SUM(total) AS total FROM orders",
                 "state": "unreviewed", "row_headers": ["n"], "row_preview": [["42"]], "row_count": 1}]
    assert single == json.dumps(expected, indent=2) + "\n"
    _, both = _run(["seed-validate", str(tmp_path), "--profile", "p", "--area", "sales", "--area", "ops"])
    sales = [{k: v for k, v in it.items() if k != "area"}
             for it in json.loads(both)["items"] if it["area"] == "sales"]
    assert sales == expected


@pytest.mark.parametrize("cmd", [
    ["seed-validate", "--profile", "p"],
    ["remove-example", "--question", "how many rows?"],
    ["seed-examples", "--profile", "p", "--file", "unused.json"],
])
def test_an_unknown_area_errors_and_lists_the_valid_ones(tmp_path, cmd):
    _model(tmp_path)
    _examples(tmp_path)
    rc, out = _run([cmd[0], str(tmp_path), *cmd[1:], "--area", "sales", "--area", "nope"])
    d = json.loads(out)
    assert rc == 1 and d["error"] == "unknown_area"
    assert d["unknown"] == ["nope"] and d["valid_areas"] == ["sales", "ops"]
    assert "sales, ops" in d["message"]
    # one unknown area is refused too, instead of writing a stray prompt_examples/<area>/
    rc, out = _run([cmd[0], str(tmp_path), *cmd[1:], "--area", "nope"])
    assert rc == 1 and json.loads(out)["unknown"] == ["nope"]
    assert not (tmp_path / "prompt_examples" / "nope").exists()


def test_remove_example_refuses_a_question_in_several_areas(tmp_path):
    _model(tmp_path)
    _examples(tmp_path)
    rc, out = _run(["remove-example", str(tmp_path), "--question", "how many rows?",
                    "--question", "total billed?"])
    d = json.loads(out)
    assert rc == 1 and d["refused"] == "ambiguous_question"
    assert d["ambiguous"] == [{"question": "how many rows?", "areas": ["sales", "ops"]}]
    # nothing was rejected, not even the unambiguous one
    assert len(list_prompt_examples(tmp_path, "sales")) == 2
    assert len(list_prompt_examples(tmp_path, "ops")) == 2


def test_remove_example_across_areas_by_unique_match_or_area_prefix(tmp_path):
    _model(tmp_path)
    _examples(tmp_path)
    rc, out = _run(["remove-example", str(tmp_path), "--question", "ops/how many rows?",
                    "--question", "total billed?", "--question", "no such q?"])
    d = json.loads(out)
    assert rc == 0
    assert d["rejected"] == ["example (rejected) sales/total billed?",
                             "example (rejected) ops/how many rows?"]
    assert d["examples"] == [{"area": "sales", "question": "total billed?", "status": "rejected"},
                             {"area": "ops", "question": "how many rows?", "status": "rejected"}]
    assert d["skipped"] == [{"item": "no such q?",
                             "reason": "no example with that question in any of the areas"}]
    assert [a["area"] for a in d["areas"]] == ["sales", "ops"]
    assert d["totals"] == {"areas": 2, "rejected": 2, "skipped": 1}
    assert [e["question"] for e in list_prompt_examples(tmp_path, "sales")] == ["how many rows?"]
    assert [e["question"] for e in list_prompt_examples(tmp_path, "ops")] == ["broken seed"]


def test_seed_examples_writes_each_seed_to_its_area(tmp_path, monkeypatch):
    _model(tmp_path)
    monkeypatch.setattr(introspect, "make_execute_sql_runner", lambda profile: (lambda sql: []))
    seeds = tmp_path / "seeds.json"
    seeds.write_text(json.dumps([
        {"area": "sales", "question": "orders?", "sql": "SELECT COUNT(*) FROM orders"},
        {"area": "ops", "question": "shipments?", "sql": "SELECT COUNT(*) FROM shipments"},
        {"area": "ops", "question": "no sql"}]))
    rc, out = _run(["seed-examples", str(tmp_path), "--profile", "p", "--file", str(seeds)])
    d = json.loads(out)
    assert rc == 0 and d["written"]
    assert d["added"] == ["example sales/orders?", "example ops/shipments?"]
    assert d["rejected"] == [{"area": "ops", "question": "no sql", "error": "question and sql are required"}]
    assert d["totals"] == {"areas": 2, "added": 2, "rejected": 1}
    sales = list_prompt_examples(tmp_path, "sales")
    assert [e["question"] for e in sales] == ["orders?"] and "area" not in sales[0]
    assert [e["question"] for e in list_prompt_examples(tmp_path, "ops")] == ["shipments?"]


def test_seed_examples_across_areas_needs_each_seed_to_name_its_area(tmp_path, monkeypatch):
    _model(tmp_path)
    monkeypatch.setattr(introspect, "make_execute_sql_runner", lambda profile: (lambda sql: []))
    seeds = tmp_path / "seeds.json"
    seeds.write_text(json.dumps([
        {"area": "sales", "question": "orders?", "sql": "SELECT COUNT(*) FROM orders"},
        {"question": "where?", "sql": "SELECT COUNT(*) FROM shipments"}]))
    rc, out = _run(["seed-examples", str(tmp_path), "--profile", "p", "--file", str(seeds),
                    "--area", "sales", "--area", "ops"])
    d = json.loads(out)
    assert rc == 2 and d["error"] == "seed_area_missing"
    assert d["seeds"] == [{"question": "where?", "area": None}]
    assert list_prompt_examples(tmp_path, "sales") == []   # nothing written


def test_dashboard_renders_several_areas(tmp_path, fake_db):
    import render_examples_validation as R

    _model(tmp_path)
    _examples(tmp_path)
    _, out = _run(["seed-validate", str(tmp_path), "--profile", "p"])
    combined = tmp_path / "items.json"
    combined.write_text(out)
    _, one = _run(["seed-validate", str(tmp_path), "--profile", "p", "--area", "sales"])
    single = tmp_path / "sales.json"
    single.write_text(one)
    html = tmp_path / "out.html"
    argv = ["render", "--title", "t", "--profile", "p", "--items-file", str(combined),
            "--items-file", str(single), "--out", str(html)]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "argv", argv)
        assert R.main() == 0
    page = html.read_text(encoding="utf-8")
    assert '"area": "ops"' in page and "broken seed" in page
    # one numbering across both files, written back in each file's own shape
    d = json.loads(combined.read_text())
    assert [it["n"] for it in d["items"]] == [1, 2, 3, 4] and d["totals"]["examples"] == 4
    assert [it["n"] for it in json.loads(single.read_text())] == [5, 6]


def test_seed_examples_across_areas_stops_at_the_model_wide_gate(tmp_path, monkeypatch):
    _model(tmp_path)
    curate.write_items(tmp_path, "sales", "metric", [
        {"name": "rev", "calculation": "sum", "bindings": {"PostgreSQL": "SUM(orders.total)"},
         "source_tables": ["orders"], "confidence": "inferred", "review_state": "unreviewed"}])
    monkeypatch.setattr(introspect, "make_execute_sql_runner", lambda profile: (lambda sql: []))
    seeds = tmp_path / "seeds.json"
    seeds.write_text(json.dumps([
        {"area": "sales", "question": "orders?", "sql": "SELECT COUNT(*) FROM orders"},
        {"area": "ops", "question": "shipments?", "sql": "SELECT COUNT(*) FROM shipments"}]))
    rc, out = _run(["seed-examples", str(tmp_path), "--profile", "p", "--file", str(seeds)])
    assert rc == 2 and json.loads(out)["refused"] == "preseed_review_pending"
    assert list_prompt_examples(tmp_path, "sales") == list_prompt_examples(tmp_path, "ops") == []


def test_one_area_still_runs_on_a_model_that_does_not_load(tmp_path, fake_db):
    _model(tmp_path)
    _examples(tmp_path)
    sa = tmp_path / "subject_areas" / "ops" / "subject_area.yaml"
    sa.write_text(yaml.safe_dump({**yaml.safe_load(sa.read_text()), "tables": "not a list"}))
    rc, out = _run(["seed-validate", str(tmp_path), "--profile", "p", "--area", "sales"])
    assert rc == 0 and [it["question"] for it in json.loads(out)] == ["how many rows?", "total billed?"]
    with pytest.raises(Exception):
        _run(["seed-validate", str(tmp_path), "--profile", "p"])


def test_dashboard_refuses_an_items_file_without_items(tmp_path, capsys):
    import render_examples_validation as R

    bad = tmp_path / "items.json"
    bad.write_text(json.dumps({"areas": []}))
    argv = ["render", "--title", "t", "--profile", "p", "--items-file", str(bad),
            "--out", str(tmp_path / "out.html")]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "argv", argv)
        assert R.main() == 1
    assert "'items' array" in capsys.readouterr().err
    assert not (tmp_path / "out.html").exists()
