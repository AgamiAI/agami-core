"""A capability the server has and the surface never mentions is a capability no client uses.

Four things shipped working and undocumented, which is a worse failure than shipping them broken:
a broken feature gets reported, a silent one just never gets called.

  - `execute_sql` returns three statuses and the description named two. The third, `failed`, is the
    whole database-error channel, carrying a `kind` from a declared ten-value enum. An agent that
    has never been told the shape exists meets it for the first time in the one situation where it
    is least able to reason about it, and improvises instead of retrying.
  - `list_datasources` has always returned `database_type` and `table_count`, and described
    neither. So the dialect was guessed on every non-metric expression, and the schema call was
    made blind to how big the answer would be.
  - `get_datasource_schema` ships each table's declared `default_filters` in its payload and its
    description named none of them. A declared filter was therefore first MET on the receipt —
    after the statement it belonged in had already run.

The tests derive from what the code emits rather than restating the strings, for the reason
`test_hosted_instruction_truth` gives about `binding`: a literal-vs-literal assertion passes right
up until someone renames the thing, and then it passes anyway. Add a `FailureKind` and this file is
what tells you the surface still lists nine.
"""

from __future__ import annotations

import ast
import inspect
from typing import get_args

import pytest

pytest.importorskip("pydantic")

import guardrail  # noqa: E402
import tools  # noqa: E402


def _emits_key(source: str, key: str) -> bool:
    """Does `source` build a dict entry under `key`?

    Quote-agnostic on purpose. Matching only `"key"` would make a no-op restyle to single quotes
    fail a test about payload SHAPE, which is not what any of these are asserting. `ruff format`
    would put the double quotes back, but a test that depends on the formatter to stay true is
    testing the formatter.
    """
    return f'"{key}"' in source or f"'{key}'" in source


def _signoff_keys() -> list[str]:
    """The relationship keys every join item carries, read off the assembler's own literal.

    `assemble_receipt` seeds them as `{key: None for key in (...)}` precisely so both branches
    carry an identical key set, which makes that tuple the one true statement of the join shape.
    Parsed rather than copied: copying it here would just create the second place to drift that
    this whole change is about closing.
    """
    from semantic_model import runtime

    tree = ast.parse(inspect.getsource(runtime.assemble_receipt))
    for node in ast.walk(tree):
        if not isinstance(node, ast.DictComp) or not isinstance(node.value, ast.Constant):
            continue
        if node.value.value is not None:
            continue
        source = node.generators[0].iter
        if isinstance(source, ast.Tuple):
            return [e.value for e in source.elts if isinstance(e, ast.Constant)]
    raise AssertionError("the join sign-off key set is no longer a literal tuple — re-derive it")


def _instruction_variants(monkeypatch) -> dict[str, str]:
    """Both deployment wordings. A rule that survives only one of them is a rule half the
    installs do not have."""
    out = {}
    for label, hosted in (("hosted", True), ("local", False)):
        for var in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
            monkeypatch.delenv(var, raising=False)
        if hosted:
            monkeypatch.setenv("AGAMI_DB_URL", "sqlite:///tmp/does-not-need-to-exist.db")
        out[label] = tools.server_instructions()
    return out


# --- the failure channel ----------------------------------------------------


def test_every_failure_kind_the_server_can_return_is_named_on_the_tool():
    """Derived from the enum, so a new kind fails here rather than reaching a client unannounced.

    Four of the ten are declared-but-unreachable today (`guardrail.FailureKind` says which and
    why). They are named anyway: the point of documenting a closed set is that a client can branch
    exhaustively on it, and a set that grows silently under the reader is not closed.
    """
    described = tools.TOOLS["execute_sql"]["description"]
    missing = [kind for kind in get_args(guardrail.FailureKind) if kind not in described]
    assert missing == [], f"execute_sql never tells a client it can return: {', '.join(missing)}"


def test_all_three_statuses_are_documented_not_just_the_two_that_were():
    """`ok` and `refused` were described; `failed` was not, and it is the one a client cannot
    guess the shape of from the other two — `refusal` names its own fix and `failure` cannot."""
    described = tools.TOOLS["execute_sql"]["description"]
    for status in ("ok", "refused", "failed"):
        assert f"status:'{status}'" in described, f"the {status} outcome is undescribed"
    # The distinction that decides whether an agent should retry at all.
    assert "failure:{kind, message}" in described


def test_the_tool_says_which_failures_are_worth_retrying():
    """A taxonomy with no policy attached just moves the guesswork one level down. The split is
    the useful part: a statement the schema can repair, versus deployment configuration that no
    number of retries will fix."""
    described = tools.TOOLS["execute_sql"]["description"]
    assert "retry SILENTLY" in described
    for repairable in ("syntax", "column_not_found", "table_not_found"):
        assert repairable in described
    for unfixable in ("auth", "dsn", "driver_missing"):
        assert unfixable in described


# --- fields that always shipped and were never described ---------------------


def test_list_datasources_describes_the_fields_it_actually_returns():
    """Both backends behind that tool build the same keys; the description named only one of
    them. Sourced from the handler so a renamed key breaks this rather than silently making the
    description wrong again."""
    handler_src = inspect.getsource(tools.tool_list_datasources)
    described = tools.TOOLS["list_datasources"]["description"]
    for key in ("database_type", "table_count"):
        assert _emits_key(handler_src, key), f"handler no longer emits {key}; fix the description"
        assert f"`{key}`" in described, f"{key} ships on every call and is described nowhere"


def test_the_one_conditional_field_is_described_as_conditional():
    """`description` is the only entry here a client must not assume.

    The served path emits it only when the model declares one and the local path never does, so
    "each entry carries a description" was the same class of untruth this file exists to catch —
    and the cost is specific: routing without a schema call is the tool's whole purpose, and an
    agent that reads a missing key as an error re-adds the call it was meant to avoid.
    """
    described = tools.TOOLS["list_datasources"]["description"]
    assert "WHEN it declares one" in described
    assert "still routes on its name" in described


def test_the_dialect_rule_points_at_the_field_that_carries_it(monkeypatch):
    """The instructions used to be silent on dialect entirely, on the theory that a metric's
    `binding` arrives pre-resolved. True, and it covers only the metrics — date arithmetic, string
    functions and casts are the agent's to write, and it had nothing to write them from."""
    for label, text in _instruction_variants(monkeypatch).items():
        assert "database_type" in text, f"{label}: no dialect source named"
        assert "never assume" in text, f"{label}: no instruction not to guess"
        # And it must not undo the reason core could stay silent before.
        assert "`binding`" in text, f"{label}: lost the copy-the-binding rule"


def test_the_schema_tool_names_the_table_context_it_ships():
    """`_table_contexts` requests these four and the description mentioned none, so the only place
    a declared filter appeared was the receipt — which the agent reads after the query ran."""
    context_src = inspect.getsource(tools._table_contexts)
    described = tools.TOOLS["get_datasource_schema"]["description"]
    for block in ("default_filters", "relationships", "caveats", "value_transforms"):
        assert _emits_key(context_src, block), f"{block} is no longer requested; fix the wording"
        assert f"`{block}`" in described, f"{block} ships in the payload and is described nowhere"


def test_the_join_item_shape_is_described_as_the_assembler_builds_it():
    """The declared shape said `{predicate, from_to, scope, status}` and stopped there, while the
    next two sentences referenced `name` and `review_state` — and the instructions tell an agent to
    filter joins ON `review_state`. So the surface named a field in its rule that its own shape
    said did not exist, which is the exact failure mode of the four undocumented capabilities this
    change is about, one layer in.

    Derived from `assemble_receipt`'s key tuple: add a sign-off field and this says so.
    """
    described = tools.TOOLS["execute_sql"]["description"]
    missing = [key for key in _signoff_keys() if key not in described]
    assert missing == [], f"join items carry these and the shape omits them: {', '.join(missing)}"
    # Present-but-null is the part a consumer cannot guess, and it is what makes "review_state is
    # null when status is not 'declared'" a readable rule rather than a contradiction.
    assert "always PRESENT" in described


# --- where each kind of guidance lives ---------------------------------------


def test_the_receipt_vocabulary_lives_on_the_tool_that_returns_it():
    """The field-by-field definitions moved off the always-on preamble and onto `execute_sql`,
    where a host renders them beside the result they describe. Read at initialize, four steps
    before the payload exists, they were the least-situated text on the surface."""
    described = tools.TOOLS["execute_sql"]["description"]
    for section in ("columns", "tables", "joins", "aggregates", "assumptions"):
        assert section in described
    # The status vocabularies — the part a reader cannot infer and must be able to look up.
    for vocabulary in ("undeclarable", "not_multiplied", "unmatched", "'reference'"):
        assert vocabulary in described, f"{vocabulary} is defined nowhere a client can reach"


def test_the_instructions_keep_the_rules_that_outlive_any_one_field(monkeypatch):
    """What stayed behind is deliberately not a summary of what moved. These are decisions —
    show it, don't refuse over it, don't read silence as clean — and they hold whatever the
    payload's field names become."""
    for label, text in _instruction_variants(monkeypatch).items():
        assert "NOT CHECKED" in text, f"{label}: lost the not-checked-vs-clean rule"
        assert "answer and warn" in text, f"{label}: lost the don't-refuse rule"
        assert "review_state" in text, f"{label}: lost the show-the-unapproved rule"


def test_the_two_grounding_calls_are_declared_independent(monkeypatch):
    """A numbered flow reads as a sequence, so a compliant agent serialized two round trips that
    share no state. Stated as a prohibition rather than a permission: an affordance can be
    declined, a rule gets checked against."""
    for label, text in _instruction_variants(monkeypatch).items():
        assert "INDEPENDENT" in text, f"{label}: the two grounding calls still read as ordered"
        assert "Never serialize what is independent" in text, f"{label}: stated too weakly"


def test_the_instructions_say_which_columns_may_be_queried(monkeypatch):
    """The rule existed only in the refusal a statement got for breaking it, so an agent learned it
    by being refused. Five refusals on one deployment were plausible-but-undeclared names, which is
    the shape of a rule nobody was told: the names were reasonable, they were just not in the
    model."""
    for label, text in _instruction_variants(monkeypatch).items():
        assert "Columns:" in text, f"{label}: no rule about which columns may be queried"
        assert "columns the schema returned" in text, f"{label}: does not name the source of truth"
        assert "plausible name is not a declared one" in text, f"{label}: states it too weakly"
        # The rule is about the table's own entry, not about the response as a whole: a response
        # sized down to `summary` or `index` carries no columns, and a rule keyed on "did you read
        # it" would tell that agent every column it needs is out of scope. The sizes are derived
        # so a renamed mode breaks this rather than leaving the escape hatch naming nothing.
        for mode in tools._SCHEMA_MODE_DOWNGRADE:
            if mode != "full":
                assert f"`{mode}`" in text, f"{label}: the no-columns case for {mode} is unstated"
        assert "dataset_names" in text, f"{label}: names no way to get the columns"


def test_a_scope_refusal_is_documented_as_repairable(monkeypatch):
    """The two channels taught opposite lessons for one mistake. A column the DATABASE lacks said
    "correct the statement and retry SILENTLY"; a column the MODEL does not declare said relay the
    remediation, which reads as a dead end and hands the user an instruction to go and edit the
    model. Same error, same repair, and the agent already holds the schema that makes it."""
    described = tools.TOOLS["execute_sql"]["description"]
    assert "rewrite with declared names and retry" in described
    # Whose fix it is has to be stated per rule class. "It always names its fix: relay the
    # remediation" as an unconditional opener, with the repair as a later qualifier, resolves to
    # the opener: an absolute followed by a hedge is read as the absolute.
    assert "always names its fix: relay" not in described, "the old blanket default is back"
    for label, text in _instruction_variants(monkeypatch).items():
        # Derived from the rule constants: rename one and this says the guidance names a rule that
        # no longer exists, which a literal would not.
        assert guardrail.RULE_COLUMN_SCOPE in text, f"{label}: the column rule is never named"
        assert guardrail.RULE_TABLE_SCOPE in text, f"{label}: the table rule is never named"
        assert "repair, not a dead end" in text, f"{label}: the refusal still reads as terminal"


def test_the_two_scope_refusals_that_a_rewrite_does_not_repair_are_named(monkeypatch):
    """`table_scope` refuses ambiguity as well as absence: a name declared in two schemas is
    declared twice, so "rewrite with declared names" repairs nothing and the agent retries the same
    bare name. And `check_column_scope` treats a column the author excluded exactly like one that
    was hallucinated, byte for byte, because the refusal is echo-only. Told a refusal is repairable
    and given no warning, an agent substitutes the nearest declared column and answers a different
    question than the one asked, which is worse than refusing."""
    for label, text in _instruction_variants(monkeypatch).items():
        assert "schema.table" in text, f"{label}: an ambiguous table name has no stated repair"
        assert "ON PURPOSE" in text, f"{label}: a deliberately undeclared column reads as a typo"
        assert "near-neighbour" in text, f"{label}: nothing forbids answering with a proxy column"


def test_the_surface_admits_it_cannot_save_a_correction(monkeypatch):
    """Absence and omission are indistinguishable to a reader. There is no save-a-correction tool
    here on purpose — that is a skill operation — but an agent told nothing about it will claim to
    have remembered something, which is the one failure worse than not offering the feature."""
    for label, text in _instruction_variants(monkeypatch).items():
        assert "Corrections:" in text, f"{label}: the absence reads as an oversight"
        assert "not persisted" in text, f"{label}: does not say the correction is not saved"


def test_the_star_ban_is_stated_before_a_client_meets_it():
    """A rule the gate ENFORCES has to be stated where a client reads before writing SQL (#387).

    This is the lesson #360 already paid for once: "only columns declared on the model's tables may
    be queried" existed solely inside the refusal a statement received for breaking it, so an agent
    learned it by being refused — five in a row on one deployment. The star ban was in the same
    position. It is the strictest rule this surface has, it applies in places a caller does not
    expect (a CTE body, a subquery, `t.*`), and a client that has never been told meets it for the
    first time in the one situation where it is least able to reason about it.

    Asserted against the rule the gate actually carries rather than a copy of the sentence: delete
    `RULE_SELECT_STAR` and this test goes with it, which is the coupling that keeps the instruction
    honest if the gate is ever relaxed.
    """
    import guardrail
    import tools

    assert hasattr(guardrail, "RULE_SELECT_STAR"), (
        "no star rule to state — if the gate went, the instruction should go with it"
    )
    # **Both spellings of the instructions**, which is the hazard `test_hosted_instruction_truth`
    # exists for: `SERVER_INSTRUCTIONS` is the back-compat LOCAL constant, and what a hosted
    # deployment actually serves comes from `server_instructions()`. A rule added to one and not
    # the other reaches half the surfaces, and the half it misses is the paid one.
    for name, instructions in (
        ("SERVER_INSTRUCTIONS", tools.SERVER_INSTRUCTIONS),
        ("server_instructions()", tools.server_instructions()),
    ):
        assert "`*`" in instructions or "SELECT *" in instructions, (
            f"{name}: the gate refuses every projected star and this text never mentions it, so a "
            "client meets the rule for the first time as a refusal"
        )
        # The places a caller is least likely to expect it, which is where the refusal surprises.
        for where in ("CTE", "subquery"):
            assert where in instructions, f"{name}: the star ban does not say it reaches a {where}"
        # **And the exemption, stated as loudly as the ban.** `COUNT(*)` is allowed — the star sits
        # inside the call — and an instruction that reads as "never write a star" would steer a
        # client off the commonest aggregate there is. Asserted because the first draft of this
        # rule said "refused wherever it appears", which is false and would have done exactly that.
        assert "COUNT(*)" in instructions, (
            f"{name}: the ban does not say that an aggregate over a star is fine, so it reads as "
            "wider than the gate actually is"
        )
