"""A person's free text reaches `sm` through a file, never on the command line.

A company name, a datasource line or an example's question can hold a quote, `$`, `;` or `|`. On the
command line the shell reads those, so the skill once told Claude to swap `'` for `’`, which changed
the person's words ("Macy's" stored with another character). `set-org`, `set-description` and
`remove-example` now take the text from a file the skill writes with its Write tool, used as written.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli, curate  # noqa: E402
from semantic_model import org_record as OR  # noqa: E402
from semantic_model.loader import list_prompt_examples, load_datasource  # noqa: E402

# Everything the shell or a quoting rule would touch, plus non-ASCII and an inner newline.
AWKWARD = "Macy's \"Best\" store; a|b $HOME `x` — Zürich 東京"


def _run(args: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(args)
    return rc, buf.getvalue()


def _model(root: Path) -> None:
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/sales"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    d = root / "subject_areas" / "sales"
    (d / "tables").mkdir(parents=True)
    (d / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sales", "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}]}))
    (d / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "orders",
        "columns": [{"name": "id", "type": "integer", "primary_key": True, "description": "the id"}]}))


def _file(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return str(path)


def test_set_org_reads_name_and_description_verbatim(tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    name = _file(tmp_path, "agami-name.txt", "Macy's\n")
    desc = _file(tmp_path, "agami-desc.txt", AWKWARD + "\nSecond line.\n")
    rc, out = _run(["set-org", str(art), "--name-file", name, "--description-file", desc])
    assert rc == 0, out
    rec = OR.load_org_record(art)
    assert rec.name == "Macy's"  # one trailing newline dropped, the apostrophe kept
    assert rec.description == AWKWARD + "\nSecond line."


def test_set_description_reads_the_line_verbatim(tmp_path):
    _model(tmp_path)
    path = _file(tmp_path, "agami-line.txt", AWKWARD + "\n")
    rc, out = _run(["set-description", str(tmp_path), "--description-file", path])
    assert rc == 0, out
    assert load_datasource(tmp_path).description == AWKWARD


def test_only_one_trailing_newline_is_dropped(tmp_path):
    assert cli._read_text_file(_file(tmp_path, "a.txt", "Orders.\n\n")) == "Orders.\n"
    assert cli._read_text_file(_file(tmp_path, "b.txt", "x\r\n")) == "x"
    assert cli._read_text_file(_file(tmp_path, "c.txt", "x")) == "x"


def test_remove_example_takes_questions_from_a_file(tmp_path):
    _model(tmp_path)
    awkward_q = "What's Macy's \"best\" order; a|b $1?"
    curate.add_examples(tmp_path, "sales", [
        {"question": awkward_q, "sql": "SELECT COUNT(*) AS n FROM orders"},
        {"question": "how many orders?", "sql": "SELECT COUNT(*) AS n FROM orders"}])
    path = _file(tmp_path, "agami-reject.txt", awkward_q + "\n")
    rc, out = _run(["remove-example", str(tmp_path), "--area", "sales", "--question-file", path])
    assert rc == 0, out
    assert [e["question"] for e in list_prompt_examples(tmp_path, "sales")] == ["how many orders?"]


@pytest.mark.parametrize("argv", [
    ["set-description", "ROOT", "--description", "a", "--description-file", "f"],
    ["set-description", "ROOT"],
    ["set-org", "ROOT", "--name", "a", "--name-file", "f"],
    ["remove-example", "ROOT", "--question", "q", "--question-file", "f"],
    ["remove-example", "ROOT"],
])
def test_exactly_one_of_the_inline_and_file_forms(argv):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)
    assert exc.value.code == 2


def test_a_missing_file_is_a_json_error(tmp_path):
    _model(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run(["set-description", str(tmp_path), "--description-file", str(tmp_path / "nope.txt")])
    assert exc.value.code == 2


@pytest.mark.parametrize("argv", [["set-description", "ROOT", "--description-file"],
                                  ["set-org", "ART", "--name-file"],
                                  ["remove-example", "ROOT", "--question-file"]])
def test_a_file_that_is_not_utf8_is_a_json_error(tmp_path, capsys, argv):
    _model(tmp_path)
    bad = tmp_path / "agami-latin1.txt"
    bad.write_bytes("Café".encode("latin-1"))
    args = [str(tmp_path) if a in ("ROOT", "ART") else a for a in argv] + [str(bad)]
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"].startswith(f"could not read text from {bad.resolve()}")
