"""`get_datasource_schema` says when a datasource has stored examples, and never sends them (#301).

Clients skipped `get_prompt_examples` because the only instruction to call it lived in the server
instructions, which a host may weight below its own. The schema response is the call every client
makes, so it carries a count and a one-line reminder. Never the examples: ranking them stays
`get_prompt_examples`' job, and the two calls stay independent.
"""

from __future__ import annotations

import inspect
import json

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import model_store  # noqa: E402
import tools  # noqa: E402
from semantic_model import build  # noqa: E402
from semantic_model.models import Datasource, SubjectArea  # noqa: E402
from store import Store  # noqa: E402


@pytest.fixture()
def local_model(tmp_path, monkeypatch):
    """A local install serving `crm`, with two subject areas and no examples yet."""
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE", "AGAMI_ORG_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    tools.resolved_org_id.cache_clear()
    org = Datasource(
        datasource="crm",
        subject_areas=[
            SubjectArea(name="Sales", description="Sales area"),
            SubjectArea(name="Support", description="Support area"),
        ],
    )
    build.write_tree(org, tmp_path / "crm")
    tools.bootstrap_paths()
    return tmp_path


def _examples(root, area: str, questions: list[str]) -> None:
    directory = root / "crm" / "prompt_examples" / area
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "examples.yaml").write_text(
        "".join(f"- question: {q}\n  sql: SELECT 1\n" for q in questions)
    )


def _head(out: str) -> dict:
    head, _end = json.JSONDecoder().raw_decode(out)
    return head


def test_the_schema_counts_the_stored_examples_and_says_to_fetch_them(local_model):
    _examples(local_model, "Sales", ["which deals closed last quarter", "top reps by revenue"])
    _examples(local_model, "Support", ["tickets assigned to me"])

    out = tools.tool_get_datasource_schema({"datasource": "crm"})

    assert _head(out)["prompt_examples"] == {"stored": 3, "next": tools._EXAMPLES_REMINDER}
    # A pointer, never the library: no example's text reaches this response.
    assert "tickets assigned to me" not in out and "top reps by revenue" not in out


def test_an_area_scoped_call_still_counts_every_area(local_model):
    """The issue's third chat: an `area` the client guessed filtered the one relevant example out.
    A count scoped the same way would say there was nothing to fetch."""
    _examples(local_model, "Sales", ["top reps by revenue"])
    _examples(local_model, "Support", ["tickets assigned to me", "open incidents by priority"])

    out = tools.tool_get_datasource_schema({"datasource": "crm", "area": "Sales"})

    assert _head(out)["scope"]["area"] == "Sales"
    assert _head(out)["prompt_examples"]["stored"] == 3


def test_a_datasource_with_no_examples_is_not_told_to_fetch_any(local_model):
    out = tools.tool_get_datasource_schema({"datasource": "crm"})

    assert "prompt_examples" not in _head(out)


def test_the_served_count_is_this_datasource_and_this_org_only(tmp_path, monkeypatch):
    for var in ("APP_DATABASE_URL", "AGAMI_ORG_ID"):
        monkeypatch.delenv(var, raising=False)
    tools.resolved_org_id.cache_clear()
    org = tools._current_org_id()
    url = "sqlite://" + str(tmp_path / "agami.db")
    store = Store.connect(url)
    store.run_migrations()
    ours = [
        {"area": a, "question": f"q{i}", "sql": "SELECT 1"} for i, a in enumerate(["s", "t", None])
    ]
    model_store.write_examples(store, "main", ours, org)
    model_store.write_examples(store, "other", ours[:1], org)
    model_store.write_examples(store, "main", ours * 2, "someone-else")
    assert model_store.count_examples(store, "main", org_id=org) == 3
    store.close()
    monkeypatch.setenv("AGAMI_DB_URL", url)

    assert tools._context_sources("main", org)[4] == 3


def test_the_surface_names_the_pointer_and_keeps_the_calls_independent(monkeypatch):
    assert "prompt_examples" in inspect.getsource(tools.tool_get_datasource_schema)
    assert "`prompt_examples`" in tools.TOOLS["get_datasource_schema"]["description"]
    assert "leave `area` out" in tools.TOOLS["get_prompt_examples"]["description"]
    for hosted in (True, False):
        for var in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
            monkeypatch.delenv(var, raising=False)
        if hosted:
            monkeypatch.setenv("AGAMI_DB_URL", "sqlite:///tmp/does-not-need-to-exist.db")
        text = tools.server_instructions()
        assert "`prompt_examples`" in text
        # The fallback does not undo the decision to issue both grounding calls in one turn.
        assert "INDEPENDENT" in text
