"""`run_golden_eval.py --ask` answers one question the way a golden-run item is answered: the same
context assembly, the same client generator, the same fixed sentences; the reconcile skill's door to
agami's answer without its own context in play."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import run_golden_eval as rge  # noqa: E402
from semantic_model import golden_run as gr  # noqa: E402


class _Generator:
    """A stand-in for the client: records what it was given, answers what the test set."""

    made: list = []

    def __init__(self, schema, *, timeout_s):
        self.schema, self.timeout_s = schema, timeout_s
        _Generator.made.append(self)

    def generate(self, question, org, datasource):
        self.asked = (question, org, datasource, self.schema(question))
        return _Generator.answer


def _wire(monkeypatch, tmp_path):
    monkeypatch.setattr(rge, "GENERATOR", _Generator)
    monkeypatch.setattr(rge, "_fetch_context", lambda root, top_k, profile: {"root": root, "top_k": top_k, "profile": profile, "areas": ["store"], "org_context": "ctx"})
    monkeypatch.setattr(rge, "_model_context", lambda cached, question: f"schema for {question} with {cached['top_k']} examples")
    monkeypatch.setattr(rge.agami_paths, "profile_dir", lambda profile: tmp_path / profile)
    org = lambda: "local"  # noqa: E731
    org.cache_clear = lambda: None  # the session fixture clears the real resolver's cache at teardown
    monkeypatch.setattr(rge.tools, "resolved_org_id", org)
    _Generator.made.clear()


def test_ask_answers_one_question_with_the_golden_runs_generator_and_context(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    _Generator.answer = gr.GeneratedSql(sql="SELECT COUNT(*) AS n FROM orders", error=None)
    out = tmp_path / "rows" / "1" / "agami-answer.json"
    assert rge.main(["--profile", "demo", "--ask", "How many orders?", "--top-k", "3", "--timeout-s", "45", "--out", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"question": "How many orders?", "sql": "SELECT COUNT(*) AS n FROM orders", "error": None}
    assert json.loads(out.read_text()) == printed
    gen = _Generator.made[0]
    assert gen.timeout_s == 45.0 and gen.asked == ("How many orders?", "local", "demo", "schema for How many orders? with 3 examples")


def test_ask_with_no_statement_exits_3_with_the_fixed_sentence_and_never_a_statement(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    _Generator.answer = gr.GeneratedSql(sql="", error=gr._GENERATION_UNAVAILABLE)
    assert rge.main(["--profile", "demo", "--ask", "How many orders?"]) == rge._NO_STATEMENT == 3
    printed = json.loads(capsys.readouterr().out)
    assert printed["sql"] is None and printed["error"] == "the generator command could not be started on this machine"


def test_ask_refuses_a_dataset_beside_it_and_an_empty_question(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    assert rge.main(["--profile", "demo", "--ask", "q", "--dataset", "d"]) == rge._CANNOT_START
    assert rge.main(["--profile", "demo", "--ask", "   "]) == rge._CANNOT_START


def test_the_ask_door_and_the_golden_run_share_one_generator():
    assert rge.GENERATOR is gr.ClaudeCliGenerator


def test_ask_file_fetches_the_context_once_and_spawns_per_question_in_parallel(monkeypatch, tmp_path, capsys):
    import threading
    _wire(monkeypatch, tmp_path)
    fetched = []
    monkeypatch.setattr(rge, "_fetch_context", lambda root, top_k, profile: fetched.append(1) or {"root": root, "top_k": top_k, "profile": profile, "areas": ["store"], "org_context": "ctx"})
    seen_threads = set()

    class _Slow(_Generator):
        def generate(self, question, org, datasource):
            seen_threads.add(threading.get_ident())
            if question == "broken":
                return gr.GeneratedSql(sql="", error=gr._GENERATION_EXITED)
            return gr.GeneratedSql(sql=f"SELECT '{question}'", error=None)
    monkeypatch.setattr(rge, "GENERATOR", _Slow)
    chunk = tmp_path / "chunk.json"
    chunk.write_text(json.dumps({"chunk": [{"row": 7, "question": "How many orders?"}, {"row": 8, "question": "broken"}, {"row": 9, "question": "What was revenue?"}, {"row": 10}]}))
    out_dir = tmp_path / "run" / "rows"
    rc = rge.main(["--profile", "demo", "--ask-file", str(chunk), "--out-dir", str(out_dir), "--parallel", "3"])
    printed = json.loads(capsys.readouterr().out)
    assert rc == rge._NO_STATEMENT and printed["asked"] == 4 and printed["answered"] == 2 and printed["parallel"] == 3
    assert [a["row"] for a in printed["answers"]] == [7, 8, 9, 10]  # the file's order, whatever finished first
    assert printed["answers"][1]["error"] == "the generator exited without answering" and printed["answers"][3]["error"] == "the row carries no question"
    assert json.loads((out_dir / "7" / "agami-answer.json").read_text())["sql"] == "SELECT 'How many orders?'"
    assert len(fetched) == 1 and len(_Generator.made) == 1  # one context, one generator, for the whole batch
    # a bare list works too, and a file that is not a list of rows is refused with one line
    (tmp_path / "list.json").write_text(json.dumps([{"row": 1, "question": "q"}]))
    assert rge.main(["--profile", "demo", "--ask-file", str(tmp_path / "list.json")]) == 0
    (tmp_path / "bad.json").write_text(json.dumps({"rows": "nope"}))
    assert rge.main(["--profile", "demo", "--ask-file", str(tmp_path / "bad.json")]) == rge._CANNOT_START
    assert rge.main(["--profile", "demo", "--ask", "q", "--ask-file", str(chunk)]) == rge._CANNOT_START

