"""`sm summary` (the closing summary as markdown, built only from engine state, so the skill posts
it as printed) and `--out` (a file written atomically instead of a shell redirect, which made every
call a compound command no permission rule matches)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli, summary  # noqa: E402

from skill_docs import PLUGIN  # noqa: E402

SAMPLE = PLUGIN / "samples" / "store" / "model"

TABLE = ("| Subject area | Tables | Described | Metrics | Joins | Examples |\n"
         "|---|---|---|---|---|---|\n"
         "| Agami example | 11 | 40% | 8 | 12 | 14 |")


@pytest.fixture
def model(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    shutil.copytree(SAMPLE, root)
    return root


def _timeline(root: Path, marks: list[tuple[str, str, str]]) -> None:
    path = root / ".introspect" / "timeline.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for i, s, t in marks:
            fh.write(json.dumps({"id": i, "status": s, "at": f"2026-11-12T{t}+00:00"}) + "\n")


def test_summary_of_the_sample(model, capsys):
    assert cli.main(["summary", str(model)]) == 0
    assert capsys.readouterr().out == (
        "**agami-example: 11 tables, 12 joins, 8 metrics, 14 examples.**\n\n" + TABLE + "\n")


def test_summary_adds_the_set_up_line(model, capsys):
    _timeline(model, [("connect", "in_progress", "10:00:00"), ("build", "done", "10:03:00"),
                      ("first_question", "in_progress", "10:06:40")])
    assert cli.main(["summary", str(model)]) == 0
    assert capsys.readouterr().out == (
        "**agami-example: 11 tables, 12 joins, 8 metrics, 14 examples.**\n\n" + TABLE
        + "\n\nSet up in 6 min 40 s\n")


def test_summary_json_wraps_the_same_text(model, capsys):
    assert cli.main(["summary", str(model), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["markdown"] == summary.onboarding_summary(model)["markdown"]
    assert out["totals"]["tables"] == 11 and out["totals"]["needs_signoff"] == 0


def _unsign_metrics(root: Path, n: int) -> None:
    """Send the first `n` metric files back to review, as a fresh build leaves them."""
    files = sorted((root / "subject_areas").rglob("metrics/*.yaml"))
    assert len(files) >= n
    for f in files[:n]:
        doc = yaml.safe_load(f.read_text())
        doc["review_state"] = "unreviewed"
        doc.pop("reviewed_by", None)
        f.write_text(yaml.safe_dump(doc))


@pytest.mark.parametrize("n, line", [(2, "2 definitions are waiting for sign-off."),
                                     (1, "1 definition is waiting for sign-off.")])
def test_summary_says_what_waits_for_sign_off(model, n, line):
    _unsign_metrics(model, n)
    text = summary.onboarding_summary(model)["markdown"]
    assert summary.onboarding_summary(model)["totals"]["needs_signoff"] == n
    assert text.endswith(TABLE + "\n\n" + line + "\n")


def test_summary_has_a_totals_row_with_two_areas(model):
    second = model / "subject_areas" / "agami-extra"
    shutil.copytree(model / "subject_areas" / "agami-example", second)
    sa = yaml.safe_load((second / "subject_area.yaml").read_text())
    sa["name"] = "agami-extra"
    (second / "subject_area.yaml").write_text(yaml.safe_dump(sa))
    ds = yaml.safe_load((model / "datasource.yaml").read_text())
    ds["subject_areas"].append("subject_areas/agami-extra")
    (model / "datasource.yaml").write_text(yaml.safe_dump(ds))
    text = summary.onboarding_summary(model)["markdown"]
    rows = [ln for ln in text.splitlines() if ln.startswith("| ")]
    assert rows[1].startswith("| Agami example | 11 |")
    assert rows[2].startswith("| Agami extra | 11 |")
    assert rows[3].startswith("| **Total** | 22 | ")


def test_summary_without_a_model_is_no_model(tmp_path, capsys):
    assert cli.main(["summary", str(tmp_path / "missing")]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "no_model"


def test_a_pipe_in_an_area_name_stays_in_its_cell():
    assert summary._cell("a|b") == "a\\|b"


# --- --out -----------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["model-tree"], ["bundle", "--area", "agami-example"],
                                  ["org-draft"]])
def test_out_writes_the_same_bytes_as_stdout(model, tmp_path, capsys, argv):
    verb, *rest = argv
    assert cli.main([verb, str(model), *rest]) == 0
    printed = capsys.readouterr().out
    target = tmp_path / "local" / "tmp" / "agami-out.json"
    assert cli.main([verb, str(model), *rest, "--out", str(target)]) == 0
    line = capsys.readouterr().out
    assert line.count("\n") == 1  # one line
    assert json.loads(line) == {"out": str(target), "bytes": len(printed.encode("utf-8"))}
    assert target.read_bytes() == printed.encode("utf-8")
    assert [p.name for p in target.parent.iterdir()] == [target.name]  # no temp file left


def test_out_replaces_an_earlier_file(model, tmp_path, capsys):
    target = tmp_path / "agami-tree.json"
    target.write_text("old")
    assert cli.main(["model-tree", str(model), "--out", str(target)]) == 0
    assert json.loads(target.read_text())["datasource"] == "agami-example"


def test_out_keeps_the_exit_code_and_writes_what_was_printed(model, tmp_path, capsys):
    target = tmp_path / "agami-claims.json"
    rc = cli.main(["claims", str(model), "--sql-file", str(tmp_path / "nope.sql"),
                   "--against-sql-file", str(tmp_path / "nope2.sql"), "--out", str(target)])
    assert rc == 2
    assert json.loads(target.read_text())  # the error, where the redirect would have put it


def test_out_writes_nothing_when_the_verb_raises(tmp_path, capsys):
    target = tmp_path / "agami-tree.json"
    assert cli.main(["model-tree", str(tmp_path / "missing"), "--out", str(target)]) == 3
    assert not target.exists()
    assert json.loads(capsys.readouterr().out)["error"] == "no_model"


def test_write_atomically_cleans_up_on_failure(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        cli.write_atomically(tmp_path / "x.json", b"{}")
    assert list(tmp_path.iterdir()) == []


def test_every_verb_the_skills_redirected_takes_out():
    sub = cli.build_parser()._subparsers._group_actions[0].choices
    for verb in ("seed-validate", "bundle", "model-tree", "org-draft", "claims"):
        assert "--out" in sub[verb].format_help(), verb



def test_one_of_each_reads_singular(model, monkeypatch):
    from semantic_model import curate
    real = curate.overview

    def one_of_each(org, root):
        view = real(org, root)
        view["totals"].update(tables=1, joins=1, metrics=1, examples=1)
        return view

    monkeypatch.setattr(curate, "overview", one_of_each)
    first = summary.onboarding_summary(model)["markdown"].splitlines()[0]
    assert first == "**agami-example: 1 table, 1 join, 1 metric, 1 example.**"


def test_a_name_with_a_line_break_or_pipe_stays_on_its_line():
    assert summary._cell("acme\n| evil") == "acme \\| evil"
    assert summary._cell("a\r\nb\tc") == "a b c"


def test_the_datasource_name_is_escaped(model):
    ds = yaml.safe_load((model / "datasource.yaml").read_text())
    ds["datasource"] = "acme|x\n**y"
    (model / "datasource.yaml").write_text(yaml.safe_dump(ds))
    first = summary.onboarding_summary(model)["markdown"].split("\n\n")[0]
    assert "\n" not in first and first.startswith("**acme\\|x **y: ")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_out_file_mode_follows_the_umask_like_a_redirect(model, tmp_path, capsys):
    import os
    import stat
    old = os.umask(0o022)
    try:
        target = tmp_path / "agami-tree.json"
        assert cli.main(["model-tree", str(model), "--out", str(target)]) == 0
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
    finally:
        os.umask(old)


def _every_parser(parser):
    import argparse
    yield parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                yield from _every_parser(child)


def test_no_parser_accepts_an_abbreviated_option():
    parsers = list(_every_parser(cli.build_parser()))
    assert len(parsers) > 50
    assert all(p.allow_abbrev is False for p in parsers)


@pytest.mark.parametrize("abbrev", ["--o", "--ou", "--o=x.json", "--ou=x.json"])
@pytest.mark.parametrize("verb", ["model-tree", "bundle", "org-draft"])
def test_the_cli_refuses_an_abbreviated_out(model, tmp_path, capsys, abbrev, verb):
    target = tmp_path / "x.json"
    extra = ["--area", "agami-example"] if verb == "bundle" else []
    argv = [verb, str(model), *extra, abbrev] + ([] if "=" in abbrev else [str(target)])
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert not target.exists() and not (Path.cwd() / "x.json").exists()
