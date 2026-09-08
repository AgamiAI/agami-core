"""ACE-113 — the client names the model it is running.

Over MCP the model belongs to the client and the server never sees it, so the log could say who
called and what they ran but never what was driving. An operator reading a run of unusually poor
statements could not tell whether the client was on a different model that week.

**The value is a claim, and every test here treats it as one.** It is bounded at the writer, stored
unvalidated, marked self-reported where it renders, and — the property that matters most — consulted
by nothing. A client-controlled value that could change what the server does would be a much worse
thing than an empty column, so `test_nothing_in_the_package_branches_on_the_model` asserts against
the source that no such reader exists rather than trusting that today's code happens not to have one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pydantic")

import admin  # noqa: E402
import tools  # noqa: E402
from model_store import _TOOL_CALL_COLS  # noqa: E402
from store import Store  # noqa: E402

_SRC = Path(__file__).resolve().parents[1] / "packages" / "agami-core" / "src"

A_MODEL = "a-model-id-1-2"


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = "sqlite://" + str(tmp_path / "calls.db")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    s = Store.connect(url)
    s.run_migrations()
    s.close()
    return url


def _rows(url):
    s = Store.connect(url)
    rows = s.query("SELECT * FROM tool_calls ORDER BY ts")
    s.close()
    return rows


def _record(client_model=None, **extra):
    args = {"datasource": "SALES_DATA", "sql": "SELECT 1", **extra}
    if client_model is not None:
        args["client_model"] = client_model
    tools.record_tool_call(
        name="execute_sql",
        arguments=args,
        result_text='{"row_count": 1}',
        execution_ms=4,
        actor="jordan@example.com",
    )


# --- written, and read back ------------------------------------------------------------------------


def test_a_reported_model_is_recorded(db):
    _record(A_MODEL)

    (row,) = _rows(db)
    assert row["client_model"] == A_MODEL


def test_a_call_reporting_nothing_records_null(db):
    """The ordinary case. A client that says nothing is behaving correctly, and so is one built
    before the column existed."""
    _record()

    (row,) = _rows(db)
    assert row["client_model"] is None


def test_the_stored_model_reaches_the_reader(db):
    """**The half that is easy to miss.** The SELECT list is narrower than the INSERT — `org_id` and
    `audit_id` are written and read by nobody — so a column added to one and not the other is
    recorded faithfully and reaches no reader at all. Every DB round-trip test would still pass."""
    assert "client_model" in _TOOL_CALL_COLS


def test_sending_a_model_perturbs_no_other_column(db):
    """An optional self-report must not change anything else about the row it rides on."""
    _record()
    _record(A_MODEL)

    without, with_model = _rows(db)
    assert set(without) == set(with_model)
    differing = {k for k in without if without[k] != with_model[k]}
    assert differing <= {"id", "ts", "client_model"}, differing


# --- bounded at the writer -------------------------------------------------------------------------


def test_an_over_long_model_is_cut_by_the_writer(db):
    """Bounded here, not by the caller — a bound the caller applies is not a bound."""
    _record("m" * (tools.CLIENT_MODEL_MAX_CHARS + 500))

    (row,) = _rows(db)
    assert len(row["client_model"]) == tools.CLIENT_MODEL_MAX_CHARS


def test_the_cap_is_far_above_any_honest_model_id():
    """Why there is no truncation flag. The cap is many times the longest real model id, so a cut
    means a caller already did something pathological rather than that a real value was too long —
    and a flag would be a column false on every row ever written."""
    assert tools.CLIENT_MODEL_MAX_CHARS >= 5 * len(A_MODEL)


@pytest.mark.parametrize(
    "junk",
    [
        pytest.param(7, id="an integer"),
        pytest.param(1.5, id="a float"),
        pytest.param(True, id="a boolean"),
        pytest.param(["a", "b"], id="a list"),
        pytest.param({"name": "x"}, id="an object"),
        pytest.param("", id="an empty string"),
    ],
)
def test_a_non_string_model_is_stored_as_null(db, junk):
    """JSON hands back whatever the client sent. Without the isinstance guard a `7` would be stored
    as the Python repr of an int and read later as though somebody had reported a model called "7"
    — a fabricated claim, which is worse than the absence it replaced."""
    _record(junk)

    (row,) = _rows(db)
    assert row["client_model"] is None


# --- the tool schema -------------------------------------------------------------------------------


def _schemas():
    return {name: spec["inputSchema"] for name, spec in tools.TOOLS.items()}


def test_the_prop_is_declared_optional_on_every_tool():
    """Declared on all four, required by none. Declaring it is what makes it SENDABLE: every schema
    is `additionalProperties: False`, so an undeclared property is refused rather than ignored."""
    for name, schema in _schemas().items():
        assert "client_model" in schema["properties"], name
        assert "client_model" not in schema.get("required", []), name
        assert schema.get("additionalProperties") is False, name


def test_the_schema_constrains_nothing_the_sdk_could_refuse_the_call_over():
    """**The bound lives in the writer, never in the schema.** The MCP SDK validates `inputSchema`
    before the handler runs, so a `maxLength` or an `enum` here would refuse the whole query over an
    optional note rather than trim it."""
    for name, schema in _schemas().items():
        prop = schema["properties"]["client_model"]
        assert set(prop) <= {"type", "description"}, (name, prop)
        assert prop["type"] == "string", name


# --- rendered, marked, and harmless ----------------------------------------------------------------


def _card(**call):
    base = {
        "ts": "2026-09-07T00:00:00Z",
        "tool_name": "execute_sql",
        "success": 1,
        "sql": "SELECT 1",
        "datasource": "SALES_DATA",
    }
    return admin._call_card({**base, **call})


def test_the_card_marks_the_model_self_reported():
    """Shown, and shown as a claim. An operator must never read it as something the server saw."""
    html = _card(client_model=A_MODEL)

    assert A_MODEL in html
    assert "· self-reported" in html


def test_a_call_reporting_no_model_renders_nothing_about_it():
    """No dash, no placeholder, no line. Most calls report none, and a note on every one of them
    would be noise on the surface an operator opened to read something else."""
    html = _card()

    assert "self-reported" not in html


def test_the_rendered_model_is_escaped():
    """A self-reported value is attacker-influenceable by definition."""
    html = _card(client_model='<script>alert("x")</script>&')

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html


@pytest.mark.parametrize(
    "junk",
    [pytest.param(7, id="an integer"), pytest.param(["x"], id="a list"), pytest.param({}, id="a dict")],
)
def test_a_malformed_model_does_not_take_the_page_down(junk):
    """A column written by something other than this process — an older embedder, a hand-edited row
    — must not cost the operator the whole log. The blast radius is the page rather than the card:
    `_session_drawer` joins every card into one string, so one raise loses all of them."""
    html = _card(client_model=junk)

    assert "self-reported" not in html
    assert "SELECT 1" in html, "the rest of the card still rendered"


# --- the rule that keeps it a claim -----------------------------------------------------------------


def test_nothing_in_the_package_branches_on_the_model():
    """**A client-controlled value must not be able to change what the server does**, and the way to
    guarantee that is for nothing to consult it. Asserted against the source rather than against
    behaviour: the property is the absence of a reader, not the correctness of today's readers.

    The four sites that may name it are the record, the writer, the SELECT list and the render. A
    fifth is a decision somebody has to make deliberately, in front of this test.
    """
    allowed = {"contracts.py", "model_store.py", "tools.py", "admin.py"}
    offenders = sorted(
        str(path.relative_to(_SRC))
        for path in _SRC.rglob("*.py")
        if "client_model" in path.read_text(encoding="utf-8") and path.name not in allowed
    )

    assert offenders == [], f"a new reader of the self-reported model appeared: {offenders}"


def test_the_record_defaults_the_field_so_an_older_caller_still_writes(db):
    """The `getattr` tolerance every column after `correlation_id` already has: an embedder on an
    older record shape writes NULL rather than raising."""
    from contracts import ToolCallRecord
    from model_store import DbActivitySink

    s = Store.connect(db)
    DbActivitySink(s).record_tool_call(
        ToolCallRecord(ts="2026-09-07T00:00:00Z", tool_name="execute_sql", source="mcp_server")
    )
    rows = s.query("SELECT client_model FROM tool_calls", ())
    s.close()

    assert [r["client_model"] for r in rows] == [None]
