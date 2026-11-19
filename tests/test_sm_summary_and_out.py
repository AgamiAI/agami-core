"""`sm summary` (the closing summary as markdown, built only from engine state, so the skill posts
it as printed) and `--out` (a file written atomically instead of a shell redirect, which made every
call a compound command no permission rule matches). `--out` writes only one of agami's scratch
files or a model's own datasource.md: a pre-approved call pointed anywhere else could overwrite any
file the person owns."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli, say_text, summary  # noqa: E402

from skill_docs import PLUGIN  # noqa: E402

SAMPLE = PLUGIN / "samples" / "store" / "model"

TABLE = (
    "| Subject area | Tables | Described | Metrics | Joins | Examples |\n"
    "|---|---|---|---|---|---|\n"
    "| Sample store | 11 | 40% | 8 | 12 | 14 |"
)


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
        "**Sample store: 11 tables, 12 joins, 8 metrics, 14 examples.**\n\n" + TABLE + "\n"
    )


def test_summary_adds_the_set_up_line(model, capsys):
    _timeline(
        model,
        [
            ("connect", "in_progress", "10:00:00"),
            ("build", "done", "10:03:00"),
            ("first_question", "in_progress", "10:06:40"),
        ],
    )
    assert cli.main(["summary", str(model)]) == 0
    assert capsys.readouterr().out == (
        "**Sample store: 11 tables, 12 joins, 8 metrics, 14 examples.**\n\n"
        + TABLE
        + "\n\nSet up in 6 min 40 s\n"
    )


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


@pytest.mark.parametrize(
    "n, line",
    [
        (2, "2 definitions are waiting for sign-off: 2 metrics."),
        (1, "1 definition is waiting for sign-off: 1 metric."),
    ],
)
def test_summary_says_what_waits_for_sign_off(model, n, line):
    _unsign_metrics(model, n)
    text = summary.onboarding_summary(model)["markdown"]
    assert summary.onboarding_summary(model)["totals"]["needs_signoff"] == n
    assert text.endswith(TABLE + "\n\n" + line + "\n")


def _awaiting(root: Path, metrics: int, entities: int) -> None:
    """Leave `metrics` metrics and `entities` entities awaiting sign-off, copying the sample's
    files under new names when it has fewer, as a fresh build of a larger database leaves them."""
    area = root / "subject_areas" / "agami-example"
    for folder, n in (("metrics", metrics), ("entities", entities)):
        files = sorted((area / folder).glob("*.yaml"))
        docs = [yaml.safe_load(f.read_text()) for f in files]
        for f in files:
            f.unlink()
        for i in range(n):
            doc = dict(docs[i % len(docs)])
            doc["name"] = f"{doc['name']} {i}"
            doc["review_state"] = "unreviewed"
            for k in ("signed_off_by", "signed_off_at", "signed_off_role"):
                doc.pop(k, None)
            (area / folder / f"copy_{i:02d}.yaml").write_text(yaml.safe_dump(doc))


def _unreviewed_join(root: Path) -> None:
    """Leave the sample's first join awaiting confirmation, as a join guessed from names is."""
    path = root / "subject_areas" / "agami-example" / "relationships.yaml"
    doc = yaml.safe_load(path.read_text())
    rels = doc["relationships"] if isinstance(doc, dict) else doc
    rels[0]["review_state"] = "unreviewed"
    for k in ("signed_off_by", "signed_off_at", "signed_off_role"):
        rels[0].pop(k, None)
    path.write_text(yaml.safe_dump(doc))


def test_summary_counts_what_the_sign_off_card_counts(model, capsys):
    # A live run's summary said 11 (metrics alone) where the card said 23. The card's total is
    # `review-items --scope preseed`: metrics, named filters and entities. A join is one line and
    # no card, so an unreviewed join must not raise the count either.
    _awaiting(model, metrics=11, entities=12)
    _unreviewed_join(model)
    assert cli.main(["review-items", str(model), "--scope", "preseed"]) == 0
    card = json.loads(capsys.readouterr().out)
    assert len(card) == 23
    from semantic_model import curate

    assert curate.model_signoff_counts(model)["waiting"] == len(card)
    assert curate.model_signoff_counts(model)["joins"] == 1
    text = summary.onboarding_summary(model)["markdown"]
    assert text.endswith("\n\n23 definitions are waiting for sign-off: 11 metrics, 12 entities.\n")
    assert text.startswith("**Sample store: 11 tables, 12 joins, 11 metrics, 14 examples.**")


def test_summary_counts_the_review_queue_with_a_hidden_table(model, capsys):
    from test_onboarding_steps import _hide_invoices_with_everything_unsigned

    _hide_invoices_with_everything_unsigned(model)
    assert cli.main(["review-queue", str(model)]) == 0
    counts = json.loads(capsys.readouterr().out)["counts"]
    assert (counts["rule_1"], counts["rule_2"]) == (7, 7)
    text = summary.onboarding_summary(model)["markdown"]
    assert text.endswith("\n\n14 definitions are waiting for sign-off: 7 metrics, 7 entities.\n")


def test_the_sign_off_line_leaves_joins_out():
    # Joins are not on the sign-off card, so the line that repeats the card's count names none.
    from semantic_model import summary as S

    assert S._signoff_line(
        {"metrics": 0, "entities": 1, "joins": 2, "waiting": 1, "signed": 0}
    ) == ("1 definition is waiting for sign-off: 1 entity.")
    assert S._signoff_line({"metrics": 0, "entities": 0, "joins": 2, "waiting": 0}) is None


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
    assert rows[1].startswith("| Sample store | 11 |")
    assert rows[2].startswith("| Agami extra | 11 |")
    assert rows[3].startswith("| **Total** | 22 | ")


def test_summary_without_a_model_is_no_model(tmp_path, capsys):
    assert cli.main(["summary", str(tmp_path / "missing")]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "no_model"


def test_a_pipe_in_an_area_name_stays_in_its_cell():
    assert say_text.cell("a|b") == "a\\|b"


@pytest.mark.parametrize("raw, want", [(r"a\| b | c", r"a\\\| b \| c"), (r"a\|b", r"a\\\|b")])
def test_a_backslash_cannot_undo_an_escaped_pipe(raw, want):
    # `a\|` escaped as `a\\|` would read as an escaped backslash, then a column break
    assert say_text.cell(raw) == want


# --- --out -----------------------------------------------------------------------------------


@pytest.fixture
def scratch(tmp_path, monkeypatch) -> Path:
    """`tmp_path` as the artifacts folder, with its scratch folder."""
    (tmp_path / "local" / "tmp").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    return tmp_path / "local" / "tmp"


@pytest.mark.parametrize(
    "argv", [["model-tree"], ["bundle", "--area", "agami-example"], ["org-draft"]]
)
def test_out_writes_the_same_bytes_as_stdout(model, tmp_path, capsys, scratch, argv):
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


def test_out_replaces_an_earlier_file(model, tmp_path, capsys, scratch):
    target = scratch / "agami-tree.json"
    target.write_text("old")
    assert cli.main(["model-tree", str(model), "--out", str(target)]) == 0
    assert json.loads(target.read_text())["datasource"] == "agami-example"


def test_out_keeps_the_exit_code_and_writes_what_was_printed(tmp_path, capsys, scratch):
    import argparse

    def refuses(args):
        print('{"error": "nope"}')
        return 2

    target = scratch / "agami-x.json"
    assert cli._run_to_out(argparse.Namespace(func=refuses, out_file=str(target))) == 2
    assert json.loads(target.read_text()) == {
        "error": "nope"
    }  # where the redirect would have put it


def test_out_writes_nothing_when_the_verb_raises(tmp_path, capsys, scratch):
    target = scratch / "agami-tree.json"
    assert cli.main(["model-tree", str(tmp_path / "missing"), "--out", str(target)]) == 3
    assert not target.exists()
    assert json.loads(capsys.readouterr().out)["error"] == "no_model"


@pytest.mark.parametrize("where", ["outside", "credentials", "subfolder", "dotdot", "other-name"])
def test_out_refuses_anywhere_but_scratch(model, tmp_path, capsys, scratch, where):
    target = {
        "outside": tmp_path / "agami-tree.json",
        "credentials": tmp_path / "local" / "credentials",
        "subfolder": scratch / "x" / "agami-tree.json",
        "dotdot": f"{scratch}/../tmp/agami-tree.json",
        "other-name": scratch / "tree.json",
    }[where]
    assert cli.main(["model-tree", str(model), "--out", str(target)]) == 2
    assert "error" in json.loads(capsys.readouterr().out)
    assert not Path(str(target)).exists()


def test_out_writes_a_models_own_datasource_md(tmp_path, capsys, scratch):
    root = tmp_path / "acme"
    shutil.copytree(SAMPLE, root)
    assert cli.main(["org-draft", str(root), "--out", str(root / "datasource.md")]) == 0
    assert (root / "datasource.md").read_text()
    elsewhere = tmp_path / "x" / "acme"
    elsewhere.mkdir(parents=True)
    assert cli.main(["org-draft", str(root), "--out", str(elsewhere / "datasource.md")]) == 2
    assert not (elsewhere / "datasource.md").exists()


def test_write_atomically_cleans_up_on_failure(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        cli.write_atomically(tmp_path / "x.json", b"{}")
    assert list(tmp_path.iterdir()) == []


def test_the_verbs_whose_output_a_skill_keeps_take_out():
    sub = cli.build_parser()._subparsers._group_actions[0].choices
    for verb in ("bundle", "model-tree", "org-draft", "open-questions"):
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
    assert first == "**Sample store: 1 table, 1 join, 1 metric, 1 example.**"


def test_a_name_with_a_line_break_or_pipe_stays_on_its_line():
    assert say_text.cell("acme\n| evil") == "acme \\| evil"
    assert say_text.cell("a\r\nb\tc") == "a b c"


def test_the_datasource_name_is_escaped(model):
    ds = yaml.safe_load((model / "datasource.yaml").read_text())
    ds["datasource"] = "acme|x\n**y"
    (model / "datasource.yaml").write_text(yaml.safe_dump(ds))
    first = summary.onboarding_summary(model)["markdown"].split("\n\n")[0]
    assert "\n" not in first and first.startswith("**acme\\|x \\*\\*y: ")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_out_file_mode_follows_the_umask_like_a_redirect(model, tmp_path, capsys, scratch):
    import os
    import stat

    old = os.umask(0o022)
    try:
        target = scratch / "agami-tree.json"
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


# --- batch-id ----------------------------------------------------------------------------------

BATCH = r"^\d{8}-\d{6}-[0-9a-f]{4}$"


def test_batch_id_is_a_utc_stamp_with_four_hex(capsys):
    import re

    assert cli.main(["batch-id"]) == 0
    first = json.loads(capsys.readouterr().out)["batch"]
    assert re.match(BATCH, first), first
    assert cli.main(["batch-id"]) == 0
    second = json.loads(capsys.readouterr().out)["batch"]
    assert re.match(BATCH, second) and second != first  # two batches never share file names
