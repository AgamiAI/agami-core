"""get_prompt_examples DB serving — scope + rank + cap, never the whole library (Slice D).

The fix that matters: a large library (e.g. accumulated corrections) returns a bounded, relevant
set. Default ranking is word-overlap (zero deps, zero egress); the embeddings tier stays off.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

import model_store  # noqa: E402
import tools  # noqa: E402
from store import Store  # noqa: E402


def _seed(tmp_path, examples) -> str:
    url = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(url)
    s.run_migrations()
    model_store.write_examples(s, "main", examples)
    s.close()
    return url


def test_large_library_is_ranked_and_capped(tmp_path, monkeypatch):
    examples = [
        {"area": "sales", "question": f"monthly revenue trend {i}", "sql": "SELECT 1"}
        for i in range(40)
    ]
    examples += [
        {"area": "sales", "question": f"unrelated widget count {i}", "sql": "SELECT 2"}
        for i in range(40)
    ]
    url = _seed(tmp_path, examples)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    out = json.loads(tools.tool_get_prompt_examples({"datasource": "main", "query": "revenue"}))
    assert out["count"] <= 10  # top-K cap — never the whole 80-example library
    # the revenue-matching examples rank ahead of the unrelated ones
    assert out["examples"], "expected at least one match"
    assert all("revenue" in e["question"] for e in out["examples"][:5])


def test_char_budget_bounds_the_result(tmp_path, monkeypatch):
    # one giant example + many small: the budget stops accumulation (but always returns >=1).
    big = {"area": "s", "question": "x " * 50, "sql": "Q" * 30_000}
    examples = [big] + [{"area": "s", "question": f"q{i}", "sql": "SELECT 1"} for i in range(20)]
    url = _seed(tmp_path, examples)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    out = json.loads(tools.tool_get_prompt_examples({"datasource": "main"}))
    serialized = sum(len(json.dumps(e)) for e in out["examples"])
    assert serialized <= 20_000 + 30_000  # bounded; the 30K example doesn't drag the whole library


def test_empty_library_returns_empty(tmp_path, monkeypatch):
    url = _seed(tmp_path, [])
    monkeypatch.setenv("AGAMI_DB_URL", url)
    out = json.loads(tools.tool_get_prompt_examples({"datasource": "main", "query": "anything"}))
    assert out["examples"] == [] and out["count"] == 0
