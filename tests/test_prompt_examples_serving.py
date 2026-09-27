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


@pytest.mark.parametrize("empty", [None, "", []])
def test_an_empty_area_is_no_scope(local_library, empty):
    out = tools.tool_get_prompt_examples({"datasource": local_library, "area": empty})
    assert "subject area: sales" in out and "subject area: assets" in out


def test_an_area_sent_as_a_list_of_one_still_narrows(local_library):
    """`["sales"]` was read as no scope: every area's examples, to a caller that believed it had
    narrowed to one."""
    out = tools.tool_get_prompt_examples({"datasource": local_library, "area": ["sales"]})
    assert "subject area: sales" in out and "subject area: assets" not in out


@pytest.mark.parametrize("bad", [True, 1, 0, {}, 3.5, ["sales", "assets"]])
def test_an_area_of_no_usable_shape_is_refused_not_widened(local_library, bad):
    """This path used to treat any non-string as "no scope" — no crash, but the caller got every
    area while believing it had asked for one. It is now refused, naming what `area` takes, the
    same answer `get_datasource_schema` gives."""
    err = json.loads(tools.tool_get_prompt_examples({"datasource": local_library, "area": bad}))[
        "error"
    ]
    assert err["kind"] == "invalid_argument" and "`area`" in err["remediation"]


def test_a_whitespace_only_area_is_no_scope_not_an_empty_scope(local_library):
    """`"  "` is not the name of an area. Stripping to empty must read as "no scope given",
    not as "an area named nothing", which would match no directory and return the empty note."""
    out = tools.tool_get_prompt_examples({"datasource": local_library, "area": "   "})
    assert "subject area: sales" in out and "subject area: assets" in out


def test_an_example_is_read_as_utf8_whatever_the_platform_default(
    local_library, tmp_path, monkeypatch
):
    """#236. `Path.read_text()` with no encoding decodes as cp1252 on Windows, turning an em dash
    into `â€”` on the way to the agent."""
    import io

    path = tmp_path / "main" / "prompt_examples" / "sales" / "examples.yaml"
    path.write_bytes("- question: revenue — by month\n  sql: SELECT 1\n".encode("utf-8"))
    real = io.text_encoding
    monkeypatch.setattr(
        io, "text_encoding", lambda enc, stacklevel=2: "cp1252" if enc is None else real(enc)
    )

    out = tools.tool_get_prompt_examples({"datasource": local_library, "area": "sales"})

    assert "revenue — by month" in out


def test_one_undecodable_area_does_not_take_down_the_others(local_library, tmp_path):
    """#236. The loop reads every area into one response, so a decode error in one file used to
    escape and fail the whole call — dropping the few-shot step for areas that were fine."""
    bad = tmp_path / "main" / "prompt_examples" / "assets" / "examples.yaml"
    bad.write_bytes(b"- question: \xff\n  sql: SELECT 1\n")

    out = tools.tool_get_prompt_examples({"datasource": local_library})

    assert "subject area: sales" in out
    assert "subject area: assets" not in out


# --- an area the model does not have -------------------------------------------------------------


@pytest.fixture()
def local_model(tmp_path, monkeypatch):
    """A local install with a semantic model (areas `sales`, `assets`) and examples for `sales`."""
    import yaml

    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    root = tmp_path / "main"
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"})
    )
    for area, table in (("sales", "orders"), ("assets", "devices")):
        adir = root / "subject_areas" / area
        (adir / "tables").mkdir(parents=True)
        (adir / "tables" / f"{table}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": table,
                    "schema": "public",
                    "storage_connection": "c",
                    "grain": ["id"],
                    "columns": [{"name": "id", "type": "integer", "primary_key": True}],
                }
            )
        )
        (adir / "subject_area.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": area,
                    "tables": [{"storage_connection": "c", "schema": "public", "table": table}],
                }
            )
        )
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "main",
                "version": 1,
                "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
                "subject_areas": ["subject_areas/sales", "subject_areas/assets"],
            }
        )
    )
    (root / "prompt_examples" / "sales").mkdir(parents=True)
    (root / "prompt_examples" / "sales" / "examples.yaml").write_text(
        "- question: how many orders\n  sql: SELECT 1\n"
    )
    tools.bootstrap_paths()
    return "main"


def test_an_unknown_area_is_refused_with_the_real_ones_locally(local_model):
    """It answered as a real area with no examples — the "no examples" note — which reads to an
    agent as "this area has none" and is a reason to write SQL without them."""
    err = json.loads(tools.tool_get_prompt_examples({"datasource": local_model, "area": "salez"}))[
        "error"
    ]
    assert err["kind"] == "not_found"
    assert err["did_you_mean"] == ["sales"]
    assert "Known areas: assets, sales." in err["remediation"]


def test_a_table_sent_as_an_area_says_which_parameter_it_belongs_in(local_model):
    err = json.loads(tools.tool_get_prompt_examples({"datasource": local_model, "area": "orders"}))[
        "error"
    ]
    assert "not an area" in err["hint"] and "'sales'" in err["hint"]


def test_a_real_area_with_no_examples_still_answers_empty(local_model):
    """Only an area the MODEL lacks is refused; one it has, with no examples yet, is real."""
    out = json.loads(tools.tool_get_prompt_examples({"datasource": local_model, "area": "assets"}))
    assert out["examples"] == []


def test_an_unknown_area_is_refused_on_a_served_deployment(tmp_path, monkeypatch, local_model):
    """Served, the query is `area = ? OR area IS NULL`, so an unknown area returned the cross-area
    examples alone — a plausible, non-empty answer to a question nobody asked."""
    from semantic_model import loader

    org = loader.load_datasource(tmp_path / "main")
    url = _seed(tmp_path, [{"area": None, "question": "how many rows overall", "sql": "SELECT 3"}])
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setattr(tools, "get_cached_org", lambda _p: org)

    err = json.loads(tools.tool_get_prompt_examples({"datasource": "main", "area": "salez"}))
    assert err["error"]["did_you_mean"] == ["sales"]
    ok = json.loads(tools.tool_get_prompt_examples({"datasource": "main", "area": "sales"}))
    assert [e["question"] for e in ok["examples"]] == ["how many rows overall"]
