"""get_prompt_examples DB serving — scope + rank + cap, never the whole library (Slice D).

The fix that matters: a large library (e.g. accumulated corrections) returns a bounded, relevant
set. Default ranking is word-overlap (zero deps, zero egress); the embeddings tier stays off.
"""

from __future__ import annotations

import json
import sys

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


def test_self_referential_question_never_returns_a_real_identity_literal(tmp_path, monkeypatch):
    """ACE-118 review: the confidence shortcut this spec closes lives only in the local `sm
    examples` CLI path (`semantic_model.runtime.is_high_confidence`) — this hosted tool has no
    confidence-scored shortcut at all, it just ranks and returns matches, so a self-referential
    question can still surface another person's identity literal from a matched example's SQL. The
    fix has to redact at the tool boundary, unconditionally, not rely on the model reading the
    instructions sentence."""
    examples = [
        {
            "area": "sales",
            "question": "how many tickets are assigned to me",
            "sql": "SELECT COUNT(*) FROM tickets WHERE assigned_to = 'someone-else@example.com'",
        }
    ]
    url = _seed(tmp_path, examples)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    # `write_examples` seeds under `model_store.DEFAULT_ORG` ("local"); pin the resolved org to
    # match rather than let it fall through to whatever `~/.config/agami/path` points a
    # contributor's machine at (a real hazard `_isolate_active_profile` in conftest.py documents
    # for `.config` — this is the same class of leak one step further down the resolution chain).
    monkeypatch.setenv("AGAMI_ORG_ID", "local")

    out = json.loads(
        tools.tool_get_prompt_examples(
            {"datasource": "main", "query": "how many tickets are assigned to me"}
        )
    )
    assert out["examples"], "expected the near-identical example to match"
    for ex in out["examples"]:
        assert "someone-else@example.com" not in ex["sql"]
    assert "<RESOLVE_FROM_CALLER_IDENTITY>" in out["examples"][0]["sql"]


def test_redaction_catches_an_assignee_column_too(tmp_path, monkeypatch):
    """Copilot review on ACE-118: `assignee` is a real identity-bearing column name (this file's
    own self-reference fixtures use it) and was missing from the matcher — a hosted example
    naming it would have been returned verbatim for a self-referential question, defeating the
    redaction guarantee."""
    examples = [
        {
            "area": "sales",
            "question": "how many tickets are assigned to me",
            "sql": "SELECT COUNT(*) FROM tickets WHERE assignee = 'someone-else@example.com'",
        }
    ]
    url = _seed(tmp_path, examples)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ORG_ID", "local")

    out = json.loads(
        tools.tool_get_prompt_examples(
            {"datasource": "main", "query": "how many tickets are assigned to me"}
        )
    )
    assert "someone-else@example.com" not in out["examples"][0]["sql"]
    assert "<RESOLVE_FROM_CALLER_IDENTITY>" in out["examples"][0]["sql"]


def test_redaction_handles_a_doubled_single_quote_inside_the_literal(tmp_path, monkeypatch):
    """Copilot review on ACE-118: standard SQL escapes an apostrophe as `''`, not `\\'` — the
    dialect helper (`semantic_model/dialects.py`) emits exactly this shape for a value like
    O'Reilly. The old pattern stopped at the first `'`, leaving the remainder of the identity
    literal (and the SQL) unredacted and malformed."""
    examples = [
        {
            "area": "sales",
            "question": "how many tickets are assigned to me",
            "sql": "SELECT COUNT(*) FROM tickets WHERE assigned_to = 'o''reilly@example.com'",
        }
    ]
    url = _seed(tmp_path, examples)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ORG_ID", "local")

    out = json.loads(
        tools.tool_get_prompt_examples(
            {"datasource": "main", "query": "how many tickets are assigned to me"}
        )
    )
    sql = out["examples"][0]["sql"]
    assert "reilly@example.com" not in sql
    assert sql == "SELECT COUNT(*) FROM tickets WHERE assigned_to = '<RESOLVE_FROM_CALLER_IDENTITY>'"


def test_a_non_self_referential_question_keeps_the_examples_sql_verbatim(tmp_path, monkeypatch):
    """The additive guarantee: redaction is gated on self-reference, not applied blindly to every
    example — a question with no 'my'/'me'/'I'/'mine' must see the real SQL unchanged."""
    examples = [
        {
            "area": "sales",
            "question": "how many tickets are assigned to alex",
            "sql": "SELECT COUNT(*) FROM tickets WHERE assigned_to = 'alex@example.com'",
        }
    ]
    url = _seed(tmp_path, examples)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ORG_ID", "local")

    out = json.loads(
        tools.tool_get_prompt_examples(
            {"datasource": "main", "query": "how many tickets are assigned to alex"}
        )
    )
    assert out["examples"][0]["sql"] == examples[0]["sql"]


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


def test_area_narrows_to_that_area_plus_the_cross_area_bucket(tmp_path, monkeypatch):
    """`select_examples` has always implemented this correctly — `area = ? OR area IS NULL`, which
    keeps genuinely cross-area examples while dropping other areas'. It was simply unreachable:
    `area` was missing from the tool's inputSchema, which sets `additionalProperties: false`, so a
    compliant client could not send it and the branch was dead on every MCP call.

    The pairing is the point: an area-scoped call must NOT silently lose the cross-area examples,
    which is why this asserts both halves.
    """
    examples = [
        {"area": "sales", "question": "how many orders by region", "sql": "SELECT 1"},
        {"area": "assets", "question": "how many assets by install status", "sql": "SELECT 2"},
        {"area": None, "question": "how many rows overall", "sql": "SELECT 3"},
    ]
    url = _seed(tmp_path, examples)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    out = json.loads(tools.tool_get_prompt_examples({"datasource": "main", "area": "sales"}))
    got = {e["question"] for e in out["examples"]}
    assert "how many orders by region" in got          # the named area
    assert "how many rows overall" in got              # the cross-area bucket, not lost
    assert "how many assets by install status" not in got   # another area, dropped


def test_the_area_parameter_is_advertised_so_a_client_can_send_it(tmp_path):
    """The handler reading an argument is not enough — `additionalProperties: false` means an
    undeclared key is rejected at the transport, so the schema is what makes it reachable."""
    schema = tools.TOOLS["get_prompt_examples"]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert "area" in schema["properties"]


def test_every_served_example_carries_its_id(tmp_path, monkeypatch):
    """The id column was never selected, so it reached nobody even when it held something. A caller
    can now name the example it used rather than quoting it back (ACE-109).

    Seeded under the org the tool will resolve to rather than the write default: on a machine that
    has a deployment record, those differ, and the read would find nothing.
    """
    examples = [
        {"area": "sales", "question": f"monthly revenue trend {i}", "sql": f"SELECT {i}"}
        for i in range(3)
    ]
    url = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(url)
    s.run_migrations()
    model_store.write_examples(s, "main", examples, tools.current_org_id())
    s.close()
    monkeypatch.setenv("AGAMI_DB_URL", url)

    out = json.loads(tools.tool_get_prompt_examples({"datasource": "main", "query": "revenue"}))
    assert out["examples"], "expected at least one match"
    assert all(e["id"] == model_store.example_id(e) for e in out["examples"])


# --- the local (file) path, which had no test at all -----------------------------------------


@pytest.fixture()
def local_library(tmp_path, monkeypatch):
    """A local install with a two-area curated library on disk."""
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    ex = tmp_path / "main" / "prompt_examples"
    for area, q in (("sales", "how many orders"), ("assets", "how many assets")):
        (ex / area).mkdir(parents=True)
        (ex / area / "examples.yaml").write_text(f"- question: {q}\n  sql: SELECT 1\n")
    tools.bootstrap_paths()
    return "main"


def test_the_local_path_returns_every_area_when_none_is_named(local_library):
    out = tools.tool_get_prompt_examples({"datasource": local_library})
    assert "subject area: sales" in out and "subject area: assets" in out


def test_the_local_path_also_redacts_a_self_referential_identity_literal(tmp_path, monkeypatch):
    """ACE-118: the DB path's redaction (`_redact_self_referential_identity`) never ran here — this
    branch returns a whole area's YAML as one text block, not a list of example dicts. But
    `_redact_identity_literals` is a plain string transform keyed on the SQL shape, not on that
    structure, so it applies just as well to the raw text before it's parsed.

    Real-world relevance: this is the plain local file mode (Claude Code / Claude Desktop, no
    database, no login) — normally single-user by design, so there is no "someone else" to leak
    to. It still matters when the model files are shared, which this project's own onboarding
    explicitly supports (pointing `<artifacts_dir>` at a git repo so a team shares one tuned
    model) — a colleague's old example can carry a real identity literal into a shared library.
    """
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    ex = tmp_path / "main" / "prompt_examples" / "sales"
    ex.mkdir(parents=True)
    ex.joinpath("examples.yaml").write_text(
        "- question: how many tickets are assigned to me\n"
        "  sql: SELECT COUNT(*) FROM tickets WHERE assigned_to = 'someone-else@example.com'\n"
    )
    tools.bootstrap_paths()

    out = tools.tool_get_prompt_examples(
        {"datasource": "main", "query": "how many tickets are assigned to me"}
    )
    assert "someone-else@example.com" not in out
    assert "<RESOLVE_FROM_CALLER_IDENTITY>" in out


def test_the_local_path_leaves_a_non_self_referential_examples_yaml_verbatim(tmp_path, monkeypatch):
    """The additive guarantee, same as the DB path's sibling test: a question with no self-
    reference marker must see the real SQL unchanged, byte for byte."""
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    ex = tmp_path / "main" / "prompt_examples" / "sales"
    ex.mkdir(parents=True)
    sql_line = "  sql: SELECT COUNT(*) FROM tickets WHERE assigned_to = 'alex@example.com'\n"
    ex.joinpath("examples.yaml").write_text(
        "- question: how many tickets are assigned to alex\n" + sql_line
    )
    tools.bootstrap_paths()

    out = tools.tool_get_prompt_examples(
        {"datasource": "main", "query": "how many tickets are assigned to alex"}
    )
    assert sql_line in out


def test_the_local_path_serves_examples_without_the_model_deps_installed(local_library, monkeypatch):
    """Copilot review on ACE-118: this branch historically needed no model deps at all — a bare
    `agami-core` install (no `[model]` extra) can still serve raw YAML. `semantic_model.runtime`
    pulls in `semantic_model.models`, which needs pydantic; importing it unconditionally would turn
    a working call into an unhandled `ModuleNotFoundError`. The guard must degrade (skip
    redaction), never crash the whole call — same convention `_resolve_units` already uses."""
    monkeypatch.setitem(sys.modules, "semantic_model", None)

    out = tools.tool_get_prompt_examples({"datasource": local_library, "query": "how many orders"})
    assert "subject area: sales" in out


def test_the_local_path_honours_area_too(local_library):
    """One schema, one behaviour. Advertising `area` while only the served path honoured it would
    have been the same defect this batch is about, one layer down: a parameter a client can send
    that silently does nothing on half the deployments."""
    out = tools.tool_get_prompt_examples({"datasource": local_library, "area": "sales"})
    assert "subject area: sales" in out
    assert "subject area: assets" not in out
    assert "how many assets" not in out


def test_an_area_with_no_examples_on_disk_gives_the_empty_note(local_library):
    out = json.loads(tools.tool_get_prompt_examples(
        {"datasource": local_library, "area": "nonexistent"}))
    assert out["examples"] == []
    assert "prompt_examples" in out["note"]


@pytest.mark.parametrize("bad", [True, 1, 0, [], {}, 3.5])
def test_a_non_string_area_does_not_crash_the_local_path(local_library, bad):
    """`(x or "").strip()` raises on any TRUTHY non-string, and this handler is reachable outside
    a schema-validating transport — tests and embedders call it directly.

    It is a regression risk specific to this change: before `area` was honoured on the local path
    the argument was ignored entirely, so no input could crash it. Treated as "no scope" rather
    than refused — this path returns the curated library and has no vocabulary for an input error.
    """
    out = tools.tool_get_prompt_examples({"datasource": local_library, "area": bad})
    assert "subject area: sales" in out and "subject area: assets" in out


def test_a_whitespace_only_area_is_no_scope_not_an_empty_scope(local_library):
    """`"  "` is not the name of an area. Stripping to empty must read as "no scope given",
    not as "an area named nothing", which would match no directory and return the empty note."""
    out = tools.tool_get_prompt_examples({"datasource": local_library, "area": "   "})
    assert "subject area: sales" in out and "subject area: assets" in out
