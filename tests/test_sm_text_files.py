"""A person's free text reaches `sm` through a file, never on the command line.

A company name, a datasource line or an example's question can hold a quote, `$`, `;` or `|`. On the
command line the shell reads those, so the skill once told Claude to swap `'` for `’`, which changed
the person's words ("Macy's" stored with another character). `set-org`, `set-description` and
`remove-example` now take the text from a file the skill writes with its Write tool, used as written.

That file is one of agami's scratch files and nothing else (`<artifacts_dir>/local/tmp/agami-*`, a
plain file, not linked): the text is saved into the org record and datasource.yaml, so an option that
read any path copied a credentials file there when a call was pointed at one.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
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
AWKWARD = 'Macy\'s "Best" store; a|b $HOME `x` — Zürich 東京'


def _run(args: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(args)
    return rc, buf.getvalue()


def _model(root: Path) -> None:
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "p",
                "version": 1,
                "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
                "subject_areas": ["subject_areas/sales"],
            }
        )
    )
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"})
    )
    d = root / "subject_areas" / "sales"
    (d / "tables").mkdir(parents=True)
    (d / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "sales",
                "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}],
            }
        )
    )
    (d / "tables" / "orders.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "orders",
                "schema": "public",
                "storage_connection": "c",
                "grain": ["id"],
                "description": "orders",
                "columns": [
                    {"name": "id", "type": "integer", "primary_key": True, "description": "the id"}
                ],
            }
        )
    )


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch) -> Path:
    """An artifacts folder with its scratch folder, as `sm` and `py` leave it."""
    tmp = tmp_path / "art" / "local" / "tmp"
    tmp.mkdir(parents=True)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "art"))
    return tmp


def _file(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / "art" / "local" / "tmp" / name
    path.write_bytes(text.encode("utf-8"))
    return str(path)


def test_set_org_reads_name_and_description_verbatim(tmp_path):
    art = tmp_path / "art"
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
    assert cli._read_text_file(_file(tmp_path, "agami-a.txt", "Orders.\n\n")) == "Orders.\n"
    assert cli._read_text_file(_file(tmp_path, "agami-b.txt", "x\r\n")) == "x"
    assert cli._read_text_file(_file(tmp_path, "agami-c.txt", "x")) == "x"


def test_remove_example_takes_questions_from_a_file(tmp_path):
    _model(tmp_path)
    awkward_q = "What's Macy's \"best\" order; a|b $1?"
    curate.add_examples(
        tmp_path,
        "sales",
        [
            {"question": awkward_q, "sql": "SELECT COUNT(*) AS n FROM orders"},
            {"question": "how many orders?", "sql": "SELECT COUNT(*) AS n FROM orders"},
        ],
    )
    path = _file(tmp_path, "agami-reject.txt", awkward_q + "\n")
    rc, out = _run(["remove-example", str(tmp_path), "--area", "sales", "--question-file", path])
    assert rc == 0, out
    assert [e["question"] for e in list_prompt_examples(tmp_path, "sales")] == ["how many orders?"]


@pytest.mark.parametrize(
    "argv",
    [
        ["set-description", "ROOT", "--description", "a", "--description-file", "f"],
        ["set-description", "ROOT"],
        ["set-org", "ROOT", "--name", "a", "--name-file", "f"],
        ["remove-example", "ROOT", "--question", "q", "--question-file", "f"],
        ["remove-example", "ROOT"],
    ],
)
def test_exactly_one_of_the_inline_and_file_forms(argv):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)
    assert exc.value.code == 2


def test_a_missing_file_is_a_json_error(tmp_path):
    _model(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run(["set-description", str(tmp_path), "--description-file",
              str(tmp_path / "art" / "local" / "tmp" / "agami-nope.txt")])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["set-description", "ROOT", "--description-file"],
        ["set-org", "ART", "--name-file"],
        ["remove-example", "ROOT", "--question-file"],
    ],
)
def test_a_file_that_is_not_utf8_is_a_json_error(tmp_path, capsys, argv):
    _model(tmp_path)
    bad = tmp_path / "art" / "local" / "tmp" / "agami-latin1.txt"
    bad.write_bytes("Café".encode("latin-1"))
    args = [str(tmp_path) if a in ("ROOT", "ART") else a for a in argv] + [str(bad)]
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"].startswith(f"could not read text from {bad.resolve()}")


# --- only agami's own scratch files ------------------------------------------------------------


def _refused(tmp_path, capsys, path: str) -> str:
    """set-org pointed at `path`: refused with exit 2 and a plain message, nothing saved."""
    art = tmp_path / "art"
    with pytest.raises(SystemExit) as exc:
        cli.main(["set-org", str(art), "--description-file", path])
    assert exc.value.code == 2
    assert OR.load_org_record(art) is None  # nothing saved
    return json.loads(capsys.readouterr().out)["error"]


def test_a_credentials_file_is_never_read_into_the_org_record(tmp_path, capsys):
    secret = tmp_path / "art" / "local" / "credentials"
    secret.write_text("[acme]\npassword = not-a-real-one\n")
    assert "not one of agami's scratch files" in _refused(tmp_path, capsys, str(secret))


def test_a_file_outside_the_scratch_folder_is_refused(tmp_path, capsys):
    outside = tmp_path / "agami-desc.txt"  # the right name, the wrong folder
    outside.write_text("x")
    assert "not directly inside" in _refused(tmp_path, capsys, str(outside))


def test_a_subfolder_of_scratch_is_refused(tmp_path, capsys, scratch):
    (scratch / "sub").mkdir()
    (scratch / "sub" / "agami-desc.txt").write_text("x")
    assert "not directly inside" in _refused(tmp_path, capsys, str(scratch / "sub" / "agami-desc.txt"))


def test_dot_dot_is_refused(tmp_path, capsys, scratch):
    (scratch / "agami-desc.txt").write_text("x")
    path = f"{scratch}/../tmp/agami-desc.txt"
    assert "without ~, . or .." in _refused(tmp_path, capsys, path)


def test_a_symlink_is_refused(tmp_path, capsys, scratch):
    secret = tmp_path / "secret.txt"
    secret.write_text("not-a-real-secret")
    (scratch / "agami-desc.txt").symlink_to(secret)
    assert "a link is refused" in _refused(tmp_path, capsys, str(scratch / "agami-desc.txt"))


def test_a_hard_link_is_refused(tmp_path, capsys, scratch):
    secret = tmp_path / "secret.txt"
    secret.write_text("not-a-real-secret")
    os.link(secret, scratch / "agami-desc.txt")
    assert "hard link" in _refused(tmp_path, capsys, str(scratch / "agami-desc.txt"))


def test_a_linked_scratch_folder_is_refused(tmp_path, capsys, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "local" / "tmp").mkdir(parents=True)
    (elsewhere / "local" / "tmp" / "agami-desc.txt").write_text("x")
    art = tmp_path / "art2"
    (art / "local").mkdir(parents=True)
    (art / "local" / "tmp").symlink_to(elsewhere / "local" / "tmp")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    with pytest.raises(SystemExit):
        cli.main(["set-org", str(art), "--description-file", str(art / "local" / "tmp" / "agami-desc.txt")])
    assert "is a link" in json.loads(capsys.readouterr().out)["error"]


def test_an_oversized_file_is_refused(tmp_path, capsys, scratch):
    big = scratch / "agami-desc.txt"
    big.write_bytes(b"x" * (1024 * 1024 + 1))
    assert "larger than" in _refused(tmp_path, capsys, str(big))


def test_a_folder_named_like_a_scratch_file_is_refused(tmp_path, capsys, scratch):
    (scratch / "agami-desc.txt").mkdir()
    assert "agami-desc.txt" in _refused(tmp_path, capsys, str(scratch / "agami-desc.txt"))
