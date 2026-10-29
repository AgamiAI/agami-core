"""An example can be tagged with several metrics, and is found under each (#546).

The tag was one name (`metric: a`), so an example computing two metrics could be linked to only one.
It is now a list (`metrics: [a, b]`); a single `metric:` and a string `metrics:` still read. Ranking
by metric (`metrics=` / `metric_names` / `sm examples --metric`) puts the examples tagged with more of
the asked metrics first, and asking for none leaves the order exactly as it was.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
PKG_SRC = REPO / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import model_store  # noqa: E402
import tools  # noqa: E402
from semantic_model import cli, curate  # noqa: E402
from semantic_model import runtime as RT  # noqa: E402
from semantic_model.loader import list_prompt_examples  # noqa: E402
from semantic_model.models import example_metrics  # noqa: E402
from store import Store  # noqa: E402

SAMPLE = REPO / "plugins" / "agami" / "samples" / "store" / "model"
AREA = "agami-example"
BOTH = {"question": "Weekly takings and basket tally", "area": AREA,
        "sql": "SELECT SUM(total_amount) AS revenue, COUNT(*) AS order_count FROM orders",
        "tables": ["orders"], "metrics": ["revenue", "order count"]}


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    dst = tmp_path / "art" / "demo"
    shutil.copytree(SAMPLE, dst)
    return dst


def _examples_file(root: Path) -> Path:
    return root / "prompt_examples" / AREA / "examples.yaml"


def _run(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, json.loads(buf.getvalue())


@pytest.mark.parametrize("ex, want", [
    ({"metric": "revenue"}, ["revenue"]),
    ({"metrics": "revenue"}, ["revenue"]),
    ({"metrics": ["revenue", "order count"]}, ["revenue", "order count"]),
    ({"metrics": ["revenue", " Revenue "], "metric": "order count"}, ["revenue", "order count"]),
    ({"metrics": [None, ""], "metric": None}, []),
    ({}, []),
])
def test_every_spelling_of_the_tag_reads_as_a_list(ex, want):
    assert example_metrics(ex) == want


def test_runtime_ranks_a_two_metric_example_for_either_metric():
    lib = [{"question": f"something else {i}", "metric": "units sold"} for i in range(5)] + [BOTH]
    for metric in ("revenue", "order count", "Order  Count"):
        top = RT.get_prompt_examples("unrelated words", lib, top_k=1, metrics=[metric])
        assert top[0].example is BOTH
    # Asking for no metric keeps the similarity order exactly.
    assert ([m.example for m in RT.get_prompt_examples("revenue", lib, top_k=6)]
            == [m.example for m in RT.get_prompt_examples("revenue", lib, top_k=6, metrics=[])])


def test_sm_examples_takes_metric(root):
    assert curate.add_examples(root, AREA, [dict(BOTH)]).applied
    for metric in ("revenue", "order count"):
        rc, out = _run(["examples", str(root), "--area", AREA, "--query", "basket",
                        "--metric", metric, "--top-k", "1"])
        assert rc == 0 and out["matches"][0]["example"]["question"] == BOTH["question"]


def test_writer_keeps_one_metric_as_metric_and_several_as_a_list(root):
    one = {"question": "Takings only", "sql": "SELECT SUM(total_amount) FROM orders",
           "metrics": ["revenue"]}
    res = curate.add_examples(root, AREA, [dict(BOTH), one])
    assert len(res.applied) == 2 and not res.skipped
    by_q = {e["question"]: e for e in list_prompt_examples(root, AREA)}
    assert by_q[BOTH["question"]]["metrics"] == ["revenue", "order count"]
    assert "metric" not in by_q[BOTH["question"]]
    # One metric is written the way every release reads it.
    assert by_q["Takings only"]["metric"] == "revenue" and "metrics" not in by_q["Takings only"]


def test_untouched_metric_entries_survive_an_unrelated_edit(root):
    before = list_prompt_examples(root, AREA, include_rejected=True)
    assert any("metric" in e for e in before)
    assert curate.add_examples(root, AREA, [dict(BOTH)]).applied
    after = list_prompt_examples(root, AREA, include_rejected=True)
    assert after[: len(before)] == before
    assert not any("metrics" in e for e in after[: len(before)])
    assert [example_metrics(e) for e in after[: len(before)]] == [
        [e["metric"]] if e.get("metric") else [] for e in before]
    # And a reject of another example leaves them too.
    curate.remove_examples(root, AREA, [BOTH["question"]])
    assert list_prompt_examples(root, AREA, include_rejected=True)[: len(before)] == before


def test_resaving_with_a_list_replaces_the_single_tag(root):
    first = list_prompt_examples(root, AREA)[0]
    assert first["metric"] == "order count"
    resave = {"question": first["question"], "sql": first["sql"],
              "metrics": ["order count", "revenue"]}
    assert curate.add_examples(root, AREA, [resave]).applied
    got = list_prompt_examples(root, AREA)[0]
    assert got["metrics"] == ["order count", "revenue"] and "metric" not in got
    assert got["status"] == first["status"] and list(got)[:3] == list(first)[:3]


def test_an_unknown_metric_in_the_list_refuses_the_example(root):
    text = _examples_file(root).read_text()
    bad = {**BOTH, "metrics": ["revenue", "gross margin"]}
    res = curate.add_examples(root, AREA, [bad, {**BOTH, "metrics": [], "metric": "nope"}])
    assert not res.applied
    assert [s["reason"] for s in res.skipped] == ["metric(s) not in the model: gross margin",
                                                 "metric(s) not in the model: nope"]
    assert _examples_file(root).read_text() == text


def test_seed_examples_reports_a_refused_tag(root, monkeypatch, tmp_path):
    from semantic_model import introspect

    monkeypatch.setattr(introspect, "make_execute_sql_runner", lambda profile: (lambda sql: []))
    seeds = tmp_path / "seeds.json"
    seeds.write_text(json.dumps([BOTH, {**BOTH, "question": "q2", "metrics": ["gross margin"]}]))
    rc, out = _run(["seed-examples", str(root), "--area", AREA, "--profile", "demo",
                    "--file", str(seeds), "--after-review"])
    assert rc == 0 and len(out["added"]) == 1
    assert out["rejected"] == [{"question": "q2", "error": "metric(s) not in the model: gross margin"}]


def _seed_store(tmp_path: Path, examples: list[dict]) -> str:
    url = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(url)
    s.run_migrations()
    model_store.write_examples(s, "main", examples)
    s.close()
    return url


FILLER = [{"area": AREA, "question": f"basket filler {i}", "sql": f"SELECT {i}",
           "metric": "units sold"} for i in range(12)]


def test_hosted_store_keeps_the_list_and_ranks_by_either_metric(tmp_path):
    url = _seed_store(tmp_path, FILLER + [BOTH])
    s = Store.connect(url)
    try:
        for metric in ("revenue", "order count"):
            top = model_store.select_examples(s, "main", query="basket", top_k=1, metrics=[metric])
            assert top[0]["question"] == BOTH["question"]
            assert top[0]["metrics"] == ["revenue", "order count"]
        plain = model_store.select_examples(s, "main", query="basket", top_k=13)
        assert plain == model_store.select_examples(s, "main", query="basket", top_k=13, metrics=[])
    finally:
        s.close()


def test_served_get_prompt_examples_returns_it_for_either_metric(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_DB_URL", _seed_store(tmp_path, FILLER + [BOTH]))
    for metric in ("revenue", "order count"):
        out = json.loads(tools.tool_get_prompt_examples(
            {"datasource": "main", "query": "basket", "top_k": 1, "metric_names": metric}))
        assert out["examples"][0]["question"] == BOTH["question"]


def test_local_get_prompt_examples_returns_it_for_either_metric(root, monkeypatch):
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(root.parent))
    monkeypatch.setenv("AGAMI_PROFILE", "demo")
    assert curate.add_examples(root, AREA, [dict(BOTH)]).applied
    for metric in ("revenue", "order count"):
        out = json.loads(tools.tool_get_prompt_examples(
            {"datasource": "demo", "query": "basket", "top_k": 1, "metric_names": [metric]}))
        assert out["examples"][0]["question"] == BOTH["question"]
    # A non-list is refused the way get_datasource_schema refuses it.
    bad = json.loads(tools.tool_get_prompt_examples({"datasource": "demo", "metric_names": 3}))
    assert bad["error"]["kind"] == "invalid_argument"


def test_the_tool_advertises_metric_names():
    schema = tools.TOOLS["get_prompt_examples"]["inputSchema"]["properties"]
    assert schema["metric_names"]["type"] == "array"


def test_without_a_readable_model_tags_are_not_checked(tmp_path):
    res = curate.add_examples(tmp_path, "s", [{"question": "q", "sql": "SELECT 1",
                                                "metrics": ["anything"], "metric": None}])
    assert res.applied and not res.skipped
    assert list_prompt_examples(tmp_path, "s") == [{"question": "q", "sql": "SELECT 1",
                                                     "metric": "anything"}]
    curate.add_examples(tmp_path, "s", [{"question": "q2", "sql": "SELECT 2", "metric": None}])
    assert list_prompt_examples(tmp_path, "s")[1] == {"question": "q2", "sql": "SELECT 2"}
