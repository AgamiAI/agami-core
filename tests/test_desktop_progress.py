"""Onboarding shows its progress in the Desktop app, and its overview as one card.

Long steps run in the background with a progress line drawn from the engine's own counts; the step
list carries live counts; the overview card's numbers come from one tested command, `sm overview`,
so a card never adds up three commands' output; and the skills pre-approve their own scripts so a
permission prompt doesn't land between two cards.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from skill_docs import PLUGIN, SKILLS_DIR, skill_text

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli, curate  # noqa: E402
from semantic_model import loader as L  # noqa: E402

SAMPLE = PLUGIN / "samples" / "store" / "model"
REFS = SKILLS_DIR / "agami-connect" / "references"


@pytest.fixture
def model(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    shutil.copytree(SAMPLE, root)
    return root


def _run(argv: list[str], capsys) -> dict:
    assert cli.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_overview_matches_the_commands_it_replaces(model, capsys):
    ov = _run(["overview", str(model)], capsys)
    org = L.load_datasource(model, include_rejected=True)
    cov = curate.column_coverage(org)
    tree = curate.model_tree(org)
    live_tables = sum(
        1 for a in tree["subject_areas"] for t in a["tables"] if t["review_state"] != "rejected"
    )
    assert ov["totals"]["tables"] == live_tables
    assert ov["totals"]["columns"] == sum(t["columns"] for t in cov["tables"])
    described = sum(t["described"] for t in cov["tables"])
    assert ov["totals"]["described_pct"] == round(100 * described / ov["totals"]["columns"])
    assert ov["totals"]["examples"] == sum(
        len(L.list_prompt_examples(model, a["area"])) for a in ov["areas"]
    )
    area = ov["areas"][0]
    assert area["plain_name"] == "Agami example"  # named in words for the tile
    assert set(area) >= {"tables", "described_pct", "metrics", "metrics_waiting", "joins",
                         "joins_waiting", "examples"}


def test_overview_counts_what_waits_for_sign_off_and_leaves_hidden_things_out(model, capsys):
    metric = next((model / "subject_areas").glob("*/metrics/*.yaml"))
    doc = yaml.safe_load(metric.read_text())
    doc["review_state"] = "unreviewed"
    metric.write_text(yaml.safe_dump(doc))
    table = next((model / "subject_areas").glob("*/tables/*.yaml"))
    t = yaml.safe_load(table.read_text())
    t["review_state"] = "rejected"
    table.write_text(yaml.safe_dump(t))

    ov = _run(["overview", str(model)], capsys)
    assert ov["totals"]["needs_signoff"] == 1
    assert ov["areas"][0]["metrics_waiting"] == 1
    assert ov["totals"]["tables"] == 10  # the sample's 11, less the hidden one


def test_overview_names_two_areas_that_read_the_same_apart():
    items = curate.distinct_labels(
        [{"area": "sales_eu", "plain_name": "Orders"}, {"area": "sales_us", "plain_name": "Orders"}],
        "plain_name",
    )
    assert items[0]["plain_name"] != items[1]["plain_name"]


def test_the_build_runs_in_the_background_with_a_bar_from_the_log():
    build = (REFS / "build.md").read_text(encoding="utf-8")
    assert "on every server database (anything but SQLite and DuckDB" in build
    assert "`run_in_background: true`, and a `description` the person can read" in build
    assert 'scripts/sm" progress "<artifacts_dir>/<profile>" --wait 20' in build
    # a turn that ends loses the skill's pre-approvals (live run), so it loops on a pre-approved call
    assert "Use this, not `tail` or a watcher of your own: it is pre-approved" in build
    # a live run ended its turn mid-build twice: the app wakes it, but the turn must say so
    assert "If you do end your turn while it runs, say so in that message" in build
    # the old fallback stays for hosts without background commands
    assert "**Where background commands aren't available, a batched build**" in build


def test_the_examples_run_in_the_background_with_no_invented_bar():
    examples = (REFS / "examples.md").read_text(encoding="utf-8")
    assert "**On a server database, run it in the background**" in examples
    assert "so post no bar" in examples


def test_a_long_step_carries_its_live_count_on_the_step_list():
    text = skill_text("agami-connect")
    assert "**While a long step runs, its label carries the live count too**" in text


def test_the_overview_card_takes_every_number_from_sm_overview():
    card = (PLUGIN / "shared" / "overview-card.md").read_text(encoding="utf-8")
    assert 'scripts/sm" overview' in card
    assert "never added up by you" in card
    assert "## The fallback table" in card
    for skill_doc in (SKILLS_DIR / "agami-model" / "SKILL.md", REFS / "summary.md"):
        assert "overview-card.md" in skill_doc.read_text(encoding="utf-8"), skill_doc


@pytest.mark.parametrize("skill", ["agami-connect", "agami-model", "agami-query"])
def test_the_skill_pre_approves_its_own_scripts(skill):
    head = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8").split("\n---\n", 1)[0]
    rules = yaml.safe_load(head.removeprefix("---\n"))["allowed-tools"]
    assert 'Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" *)' in rules
    # an allow rule doesn't match past an env assignment Claude Code doesn't know is safe
    assert 'Bash(AGAMI_SQL_MAX_ROWS=* bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" *)' in rules
    # a live run dropped the quotes, ran the script without `bash`, and read phase files: each form
    # that missed a rule stopped for permission
    assert 'Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/sm *)' in rules
    assert 'Bash("${CLAUDE_PLUGIN_ROOT}/scripts/sm" *)' in rules
    assert "Read(/${CLAUDE_PLUGIN_ROOT}/**)" in rules
    assert all("${CLAUDE_PLUGIN_ROOT}/" in r for r in rules)  # nothing outside the plugin


def test_no_skill_runs_execute_sql_through_the_bare_interpreter():
    """The interpreter's path differs per machine, so no allowed-tools rule can name it: a live run
    stopped for permission on every sample query until the reads went through agami's own launcher."""
    from skill_docs import skill_docs
    offenders = [str(p) for p in [*skill_docs(), *(PLUGIN / "shared").glob("*.md"),
                                  *(PLUGIN / "agents").glob("*.md")]
                 if '"$PY" -m execute_sql' in p.read_text(encoding="utf-8")
                 or '"<py>" -m execute_sql' in p.read_text(encoding="utf-8")]
    assert not offenders, offenders


def test_progress_turns_the_engines_log_into_a_persons_line_with_a_rounded_down_bar():
    line = cli._progress_line
    # rounded down: a live run showed round() fill the bar at 33/34, which reads as done
    assert line("columns+grain 33/34")["line"] == "Reading columns ▰▰▰▰▰▰▰▱ 33/34 tables"
    assert line("columns+grain 34/34")["line"] == "Reading columns ▰▰▰▰▰▰▰▰ 34/34 tables"
    assert line("columns+grain 3/34")["line"] == "Reading columns ▱▱▱▱▱▱▱▱ 3/34 tables"
    assert line("relationships: declared FK 80/187")["line"].startswith("Checking joins ▰▰▰")
    assert line("done: 34 tables, 7 relationships") == {"line": "Done: 34 tables, 7 joins", "done": True,
                                                        "failed": False}
    assert line("columns+grain 3/34")["failed"] is False
    assert "▰" not in line("building relationships (34 tables; FK + value-overlap probes)…")["line"]


def test_progress_waits_for_the_log_to_move_on(tmp_path, capsys):
    import threading
    log = tmp_path / "acme" / ".introspect" / "progress.log"
    log.parent.mkdir(parents=True)
    log.write_text("columns+grain 3/34\n")
    threading.Timer(0.6, lambda: log.write_text("columns+grain 3/34\ncolumns+grain 6/34\n")).start()
    out = _run(["progress", str(tmp_path / "acme"), "--wait", "5"], capsys)
    assert out["changed"] is True and out["n"] == 6
    missing = _run(["progress", str(tmp_path / "missing")], capsys)
    assert missing == {"line": "", "done": False, "failed": False, "started": False}


def test_progress_wait_returns_at_once_on_a_failed_log(tmp_path, capsys):
    # a skill looping on --wait needs an end signal for a failed build, not a 30 s stall per call
    import time
    log = tmp_path / "acme" / ".introspect" / "progress.log"
    log.parent.mkdir(parents=True)
    log.write_text("columns+grain 3/34\nfailed: permission denied for schema demo\n")
    started = time.monotonic()
    out = _run(["progress", str(tmp_path / "acme"), "--wait", "30"], capsys)
    assert time.monotonic() - started < 2
    assert out["failed"] is True and out["done"] is False


def test_enrich_input_samples_only_what_queries_may_read(model, tmp_path, capsys, monkeypatch):
    from semantic_model import introspect
    sent: list = []

    def runner(profile, python=None, *, building_the_model=False, artifacts_dir=None):
        return lambda sql: sent.append(sql) or [{"id": "1"}]

    monkeypatch.setattr(introspect, "make_execute_sql_runner", runner)
    table = next((model / "subject_areas").glob("*/tables/customers.yaml"))
    t = yaml.safe_load(table.read_text())
    hidden = t["columns"][1]["name"]
    t["columns"][1]["sensitive"] = True
    table.write_text(yaml.safe_dump(t))
    out_file = tmp_path / "in.json"
    out = _run(["enrich-input", str(model), "--area", "agami-example", "--profile", "acme",
                "--out", str(out_file)], capsys)
    assert out["tables"] == out["sampled"] > 0 and not out["errors"]
    assert sent and all("*" not in q and "LIMIT 5" in q for q in sent)
    assert not any(hidden in q for q in sent if "customers" in q)  # never read a sensitive column
    assert set(json.loads(out_file.read_text())) == {"area", "bundle", "tables"}
    assert cli.main(["enrich-input", str(model), "--area", "nope", "--profile", "acme",
                     "--out", str(out_file)]) == 2
