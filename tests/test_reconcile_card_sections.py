"""The card is a verdict, one thing to do, and three closed sections.

Sandeep ran five statements against a real warehouse and the card buried the answer. What is pinned
here is what the redesign owes him: a row knows which section it belongs to, a line nobody can act on
is gone, a summary names the exception when there is one, and the five-row sample is bounded at the
surface that publishes it rather than trusted from whatever built it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402
import render_reconcile_report as rr  # noqa: E402


def _row(key, state, section, family=None, **kw):
    row = {"key": key, "state": state, "section": section, "yours": kw.pop("yours", None),
           "agami": None, "note": None}
    if family:
        row["family"] = family
    row.update(kw)
    return row


# --- every row knows its section -----------------------------------------------------------------


def test_a_diff_row_carries_the_section_it_belongs_to():
    rec = {"status": "mismatch", "statement": "SELECT 1", "sql": "SELECT 1",
           "recorded": {"columns": ["n"], "row_count": 3}, "statement_recorded": {"columns": ["n"], "row_count": 3},
           "claims": {"claims": [{"name": "tables", "status": "agrees", "generated": ["t"], "golden": ["t"]}]},
           "ledger": {"rows": [{"part": "join:a-b", "verdict": "confirmed", "kind": None, "depends_on": [],
                                "evidence": {}, "note": ""},
                               {"part": "question_fit", "verdict": "confirmed", "kind": None, "depends_on": [],
                                "evidence": {"fit": "plausible"}, "note": ""}],
                      "verdict": "confirmed", "counts": {}}}
    rows, _ = reconcile._diff_rows(rec, None)
    by_key = {r["key"]: r for r in rows}
    assert by_key["answer"]["section"] == "data"
    assert by_key["tables read"]["section"] == "sql"
    assert by_key["join a to b"]["section"] == "checks"
    # the fit is a judgment about the statement against its question, so it sits with the queries
    assert by_key["answers the question"]["section"] == "sql"


# --- what a reader cannot act on is gone ---------------------------------------------------------


def test_a_wide_column_having_no_declared_value_list_earns_no_line():
    # The probe reads the whole column and ignores the statement's own filters, so a query pinned to
    # one city still reported "more than 25 distinct values". True, and no use to anybody.
    rows = [_row("value list for schools.city", "noted", "checks", "values_declared",
                 yours="too many values to list")]
    assert reconcile._condense(rows) == []


def test_a_value_list_that_is_actually_missing_is_kept():
    rows = [_row("value list for orders.state", "gap", "checks", "values_declared", yours="no value list")]
    assert len(reconcile._condense(rows)) == 1


def test_a_dropped_rows_count_earns_no_line_whatever_it_counted():
    """ACE-150. The count is taken over the whole table BEFORE the statement's own filters, so a
    query pinned to one city and one year reports that most of the table has no partner. That is
    arithmetic about the warehouse, not a fact about this query, and nobody can act on it. It stays
    in `ledger.json`, where the run states it; it is not on the card."""
    nothing = [_row("rows dropped by join a to b", "noted", "checks", "dropped_rows", yours="0 of 4,000 a rows")]
    some = [_row("rows dropped by join a to b", "noted", "checks", "dropped_rows", yours="63,931 of 269,158 a rows")]
    assert reconcile._condense(nothing) == []
    assert reconcile._condense(some) == []


def test_twelve_value_checks_that_all_passed_become_one_counted_line():
    rows = [_row(f"value t.c={n}", "held", "checks", "literal", yours="exists") for n in range(12)]
    out = reconcile._condense(rows)
    assert len(out) == 1 and out[0]["key"] == "12 values checked, all exist"
    assert out[0]["rolled"] == 12 and out[0]["state"] == "held"


def test_a_value_check_that_failed_keeps_every_member_visible():
    rows = [_row("value t.c=a", "held", "checks", "literal", yours="exists"),
            _row("value t.c=b", "defect", "checks", "literal", yours="matches no rows")]
    assert len(reconcile._condense(rows)) == 2


# --- the summaries -------------------------------------------------------------------------------


def test_a_summary_names_the_exception_and_counts_when_there_is_none():
    rows = [_row("values", "held", "data", yours="9 of 10 rows match."),
            _row("columns", "defect", "data", yours=["a", "b"], yours_hi=["b"]),
            _row("tables read", "differs", "sql"), _row("limit", "held", "sql"),
            _row("12 values checked, all exist", "held", "checks", "literal", rolled=12),
            _row("default filter on orders", "gap", "checks", "default_filter")]
    s = reconcile._summaries(rows, {"label": "different answer", "data": "differs"}, {})
    assert s["data"] == "9 of 10 rows match. Your query returns one more column."
    assert s["sql"] == "The two queries differ in tables read."
    # the roll-up stands for the checks it replaced, so the count is of checks, not of lines
    assert s["checks"] == "12 checks passed. One didn't: default filter on orders."


def test_a_doubtful_fit_leads_the_sql_summary_because_it_is_the_thing_worth_opening():
    rows = [_row("answers the question", "open", "sql", "question_fit", yours="doubtful"),
            _row("limit", "differs", "sql")]
    s = reconcile._summaries(rows, {"label": "different answer", "data": "differs"}, {})
    assert s["sql"].startswith("Your query might not answer the question.")


def test_a_row_that_could_not_be_compared_reads_its_label():
    rows = [_row("answer", "open", "data", yours="failed")]
    s = reconcile._summaries(rows, {"label": "different query, answer not compared", "data": "could_not_compare"}, {})
    assert s["data"] == "The two queries differ, and the answers weren't compared."


# --- the sample ----------------------------------------------------------------------------------


def _csvs(tmp_path, yours, agami):
    d = tmp_path / "rows" / "1"
    d.mkdir(parents=True)
    (d / "statement.csv").write_text(yours, encoding="utf-8")
    (d / "actual.csv").write_text(agami, encoding="utf-8")
    return d


def test_the_sample_puts_the_rows_that_disagree_first(tmp_path):
    d = _csvs(tmp_path,
              "name,n\na,1\nb,2\nc,3\nd,4\ne,5\nf,6\n",
              "name,n\na,1\nb,99\nc,3\nd,4\ne,5\nf,6\n")
    sample = reconcile._sample(d, {"column_pairs": [["name", "name"], ["n", "n"]]})
    assert sample["rows"][0]["yours"] == ["b", "2"] and sample["rows"][0]["same"] is False
    assert sample["shown"] == 5 and sample["total"] == 6 and sample["aligned"] is True


def test_the_sample_falls_back_to_names_when_the_comparator_reported_no_pairs(tmp_path):
    # Differing row counts make the comparator short-circuit before pairing anything, which is
    # exactly the row a reader wants to look at.
    d = _csvs(tmp_path, "name,n\na,1\nb,2\n", "name,n\na,1\n")
    sample = reconcile._sample(d, {"column_pairs": []})
    assert sample["by_name"] is True and sample["aligned"] is False
    assert sample["pairs"] == [["name", "name"], ["n", "n"]]
    assert all(r["agami"] is None for r in sample["rows"])


def test_no_sample_when_a_side_never_ran(tmp_path):
    d = _csvs(tmp_path, "name\na\n", "")
    assert reconcile._sample(d, {"column_pairs": [["name", "name"]]}) is None


# --- the page bounds the sample itself ------------------------------------------------------------

_ITEM = {"row": 1, "question": "q", "status": "mismatch", "diff": []}


def test_the_renderer_refuses_more_than_five_rows_whatever_built_them():
    six = {"pairs": [["a", "a"]], "rows": [{"yours": ["x"], "agami": ["x"], "same": True} for _ in range(6)]}
    with pytest.raises(ValueError, match="at most 5 rows"):
        rr._validate_item(dict(_ITEM, sample=six), 0)
    # On a one-query row `rows` is always empty and every value rides in `agami_rows`, so the cap
    # has to hold there too or the branch this card depends on publishes a whole result set.
    solo = {"pairs": [["a", "a"]], "rows": [], "agami_rows": [["x"] for _ in range(6)], "agami_total": 6}
    with pytest.raises(ValueError, match="at most 5 rows"):
        rr._validate_item(dict(_ITEM, sample=solo), 0)
    # And one type-checked field per field: `one_query` is the page's only new one.
    with pytest.raises(ValueError, match="'one_query' must be true or false"):
        rr._validate_item(dict(_ITEM, one_query="yes"), 0)


def test_the_renderer_refuses_a_sample_that_is_not_the_shape_it_expects():
    with pytest.raises(ValueError, match="two-name pairs"):
        rr._validate_item(dict(_ITEM, sample={"pairs": ["a"], "rows": []}), 0)
    with pytest.raises(ValueError, match="'yours' list"):
        rr._validate_item(dict(_ITEM, sample={"pairs": [["a", "a"]], "rows": [{"agami": ["x"]}]}), 0)


def test_a_five_row_sample_reaches_the_page(tmp_path):
    sample = {"pairs": [["name", "name"]], "rows": [{"yours": ["a"], "agami": ["a"], "same": True}],
              "shown": 1, "total": 1, "aligned": True, "by_name": False, "only_yours": [], "only_agami": []}
    html = rr.render(title="t", profile="p", run="r", items=[dict(_ITEM, sample=sample)])
    assert '"sample"' in html and "function grid(sample)" in html
    # and the page still refuses result rows everywhere else
    assert "result rows are never rendered" in Path(rr.__file__).read_text(encoding="utf-8")


# --- the page is in the person's order, not the checkpoint's ---------------------------------------


def test_cards_render_in_row_order_however_the_checkpoint_was_written(tmp_path):
    # `record` replaces a row by appending a new line, so a row re-run after a fix ends up last in
    # rows.jsonl. That is right for an append log and wrong for a page.
    run = tmp_path / "20260915-000000"
    (run / "rows").mkdir(parents=True)
    written = [1, 3, 4, 5, 2]
    (run / "rows.jsonl").write_text("".join(
        json.dumps({"row": n, "label": f"r{n}", "question": f"q{n}", "status": "match", "match": True,
                    "recorded": {"columns": ["n"], "rows": [[1]]},
                    "ledger": {"rows": [], "verdict": "confirmed", "counts": {}}, "ledger_verdict": "confirmed"}) + "\n"
        for n in written), encoding="utf-8")
    assert [i["row"] for i in reconcile.report_items(run)] == [1, 2, 3, 4, 5]


# --- the decision block ---------------------------------------------------------------------------


def test_the_decisions_are_a_named_group_not_five_anonymous_radios():
    # Five radios sharing a name are a group; a group with no legend has no accessible name, so a
    # reader is told five options and never what they decide.
    html = rr.render(title="t", profile="p", run="r", items=[dict(_ITEM, fix="query", owner="you")])
    assert '<fieldset class="grade opts"><legend>What to do</legend>' in html


def test_the_suggestion_is_selected_before_anyone_clicks_and_the_block_carries_it():
    html = rr.render(title="t", profile="p", run="r", items=[dict(_ITEM, fix="query", owner="you")])
    # one map, read by the radios and by the seeding, so the page and the pasted-back block agree
    assert "function suggestionFor(item)" in html
    assert "function seedDecisions()" in html and "seedDecisions();\n    renderItems();" in html
    # and it is still labelled, so a reader sees the tool chose it
    assert "' <span class=\"tag suggested\">suggested</span>'" in html


# --- never report an artefact of the method as a fact about the data --------------------------------


def _table_rec(golden_rows, generated_rows, **score):
    s = {"status": "scored", "accuracy": 0.0, "column_pairs": [], "reason": "the answer key has 5 rows and the generated result has 1",
         "golden_row_count": golden_rows, "generated_row_count": generated_rows}
    s.update(score)
    return {"status": "mismatch", "statement": "SELECT 1", "sql": "SELECT 1", "match": False,
            "recorded": {"columns": ["n"], "row_count": generated_rows},
            "statement_recorded": {"columns": ["n"], "row_count": golden_rows},
            "comparison": {"result_set": s},
            "ledger": {"rows": [], "verdict": "confirmed", "counts": {}}, "ledger_verdict": "confirmed"}


def test_differing_row_counts_never_read_as_a_percentage_of_values():
    # The comparator decides on the row counts BEFORE pairing anything, so its 0.0 means "not
    # compared", not "nothing matched". A school can sit in both results while this reads 0%.
    rows, _ = reconcile._diff_rows(_table_rec(5, 1), None)
    values = next(r for r in rows if r["key"] == "values")
    assert values["state"] == "open" and values["yours"] == "The results weren't compared."
    assert values["note"] == "Your query returned 5 rows and agami's returned 1 row, so they couldn't be lined up row by row."
    assert "%" not in str(values["yours"])
    # and the sentence says the same thing rather than naming columns
    assert reconcile._sentence(_table_rec(5, 1), rows).startswith("Your query returned 5 rows")


def test_one_row_is_a_row():
    assert reconcile._rows_word(1) == "1 row" and reconcile._rows_word(5) == "5 rows"
    assert reconcile._recorded_display({"columns": ["n"], "row_count": 1})[0] == "1 row"


def test_the_comparators_answer_key_vocabulary_does_not_reach_the_page():
    # comparator.py serves the golden run first, where the two sides are an answer key and a
    # generated result. A reader who never wrote an answer key cannot place those words.
    said = reconcile._in_our_words("the answer key has 4 rows and the generated result has 3")
    assert said == "your query has 4 rows and agami's result has 3"
    assert reconcile._in_our_words(None) is None


def test_agamis_own_rows_are_listed_when_the_two_cannot_be_lined_up(tmp_path):
    d = _csvs(tmp_path, "name\na\nb\nc\n", "name\nz\n")
    sample = reconcile._sample(d, {"column_pairs": []})
    assert sample["aligned"] is False and sample["agami_rows"] == [["z"]] and sample["agami_total"] == 1


def test_the_fit_reason_is_one_sentence_not_three_fragments():
    note = ("the statement may not answer the question: The question asks for the highest rated ELEMENTARY "
            "school, and the statement does not restrict to elementary schools; reword the question or the "
            "statement and re-run this row")
    said = reconcile._fit_reason(note)
    assert said.startswith("The question asks for the highest rated ELEMENTARY school")
    assert "reword the question or the statement" not in said and said.endswith(".")


def test_no_surface_of_the_card_titles_a_section_with_the_bare_word_model():
    # plain-language.md: the bare word can mean the semantic model, the AI, the prompt examples or
    # the database, so it "appears only inside a quotation of someone else's text". A section title
    # is not a quotation. This caught a title I had written as "Model checks".
    tpl = (REPO_ROOT / "plugins" / "agami" / "shared" / "reconcile-report-template.html").read_text(encoding="utf-8")
    titles = re.findall(r"section\(item, '[a-z]+', '([^']+)'\)", tpl)
    assert titles == ["Data", "SQL", "Checks"]
    for title in titles:
        assert title.lower() != "model" and not title.lower().startswith("model ")


def test_a_section_key_and_its_summary_key_are_the_same_word():
    # The page asks for summaries[key] with the same key it passes to section(), so a rename that
    # moved one and not the other would silently blank every summary.
    tpl = (REPO_ROOT / "plugins" / "agami" / "shared" / "reconcile-report-template.html").read_text(encoding="utf-8")
    keys = re.findall(r"section\(item, '([a-z]+)'", tpl)
    rows = [_row("answer", "held", k) for k in keys]
    assert set(reconcile._summaries(rows, {"label": "match", "data": "matches"}, {})) == set(keys)


def test_a_filter_chip_reads_label_first_then_its_count():
    # A leading numeral on a chip reads as an ordinal marker, and a filter row is scanned by its
    # labels. GitHub, Gmail and GOV.UK facets all trail the count for that reason.
    tpl = (REPO_ROOT / "plugins" / "agami" / "shared" / "reconcile-report-template.html").read_text(encoding="utf-8")
    for built in re.findall(r"'\"><(?:span|b)>.*?</button>'", tpl):
        assert built.index("<span>") < built.index("<b>"), built
    assert "'<b>' + n + '</b><span>'" not in tpl


def test_only_the_verdict_is_set_at_the_largest_size():
    # The chip counts were at the verdict's size and coloured while their labels were grey, so the
    # loudest thing on the page was a filter count.
    css = (REPO_ROOT / "plugins" / "agami" / "shared" / "reconcile-pages.css").read_text(encoding="utf-8")
    assert css.count("font-size: var(--t-verdict)") == 1
    assert ".card .vl { font-size: var(--t-verdict)" in css


def test_one_type_scale_reaches_every_page_that_shares_the_css():
    """ACE-150. The scale lives in the shared CSS so the report and the intake page cannot drift
    apart, and it only holds if no page sets a size of its own and nothing falls back to the
    browser's 16px default. `h2` and `body` were both off the scale by omission."""
    shared = REPO_ROOT / "plugins" / "agami" / "shared"
    css = (shared / "reconcile-pages.css").read_text(encoding="utf-8")

    tokens = set(re.findall(r"--t-([a-z]+):", css))
    assert tokens == {"micro", "body", "title", "verdict", "page", "data"}
    # Every size the CSS sets is one of those tokens; a literal px would be a seventh size.
    for declared in re.findall(r"font-size:\s*([^;]+);", css):
        assert declared.strip().startswith("var(--t-"), declared

    # The base size and the two headings are set, so nothing inherits 16px by accident.
    assert "body { margin: 0; font-size: var(--t-body);" in css
    assert "h1 { font-size: var(--t-page); margin: 0; }" in css
    assert "h2 { font-size: var(--t-title); margin: 0; }" in css

    # Neither page carries a size of its own: the scale is the CSS's to change, in one place.
    for name in ("reconcile-report-template.html", "reconcile-intake-template.html"):
        assert "font-size" not in (shared / name).read_text(encoding="utf-8"), name


def test_one_query_written_means_one_value_column_and_one_sql_pane():
    """ACE-150. The grid's two value columns are "yours" beside "agami". On a row where only agami
    wrote a query there is no "yours": filing agami's own facts under that heading told a reader
    their SQL answered the question when they wrote none, and left a dead column on every row."""
    tpl = (REPO_ROOT / "plugins" / "agami" / "shared" / "reconcile-report-template.html").read_text(encoding="utf-8")
    css = (REPO_ROOT / "plugins" / "agami" / "shared" / "reconcile-pages.css").read_text(encoding="utf-8")
    assert "const one = !!item.one_query;" in tpl
    assert '<div class="h">agami’s query</div>' in tpl
    assert ".dg.one-query { grid-template-columns: 22px minmax(140px, 230px) minmax(0, 1fr); }" in css
    # The value shown is the graded side, whichever field carried it.
    assert "cell(one ? (r.yours ?? r.agami) : r.yours" in tpl
    # A SQL pane reading "(none)" is a placeholder shown as content; one query renders one pane.
    assert "if (!item.sql_yours) return '<div class=\"sql1\">' + agami + '</div>';" in tpl
    assert "esc(item.sql_yours || '(none)')" not in tpl


def test_a_lone_sql_pane_wraps_and_scrolls_inside_its_own_box():
    """ACE-150. Every pane rule was scoped to `.sql2`, so the one-pane layout got the browser's
    `white-space: pre` and one long statement widened the PAGE to 3172px instead of scrolling in its
    own box. Both layouts share the pane's styling."""
    css = (REPO_ROOT / "plugins" / "agami" / "shared" / "reconcile-pages.css").read_text(encoding="utf-8")
    pane = re.search(r"\.sql1 pre, \.sql2 pre \{([^}]*)\}", css)
    assert pane, "the two layouts must share one pane rule"
    for prop in ("white-space: pre-wrap", "overflow-wrap: anywhere", "max-height: 260px", "overflow: auto"):
        assert prop in pane.group(1), prop
    assert ".sql1 b, .sql2 b {" in css
    # The grid is the two-pane layout's alone; the shared margin is not.
    assert ".sql2 { display: grid;" in css and ".sql1, .sql2 { margin-top: 6px; }" in css


def test_the_cards_text_blocks_all_end_at_the_same_edge():
    """ACE-150. Three different reading measures (lede 72ch, verdict sentence 68ch, its action 60ch)
    inside one 1040px column left each block stranded at its own width, which reads as three
    accidents rather than one decision. These are short instructions, so the column is the measure."""
    css = (REPO_ROOT / "plugins" / "agami" / "shared" / "reconcile-pages.css").read_text(encoding="utf-8")
    # `.wrap` is the only place a width is capped; no prose block sets one of its own.
    caps = re.findall(r"^\s*(\.[\w.-]+) \{[^}]*max-width:", css, re.M)
    assert caps == [".wrap"], caps


def test_one_query_is_decided_in_one_place_and_survives_a_row_with_no_ledger_yet():
    """The predicate was computed three times from two definitions, and the narrow one let a card
    drop its second column while the sample still built two sides. One definition, and it holds
    before Phase 2.5 has run: a question-only row has one query whether or not it has been graded."""
    ungraded_no_ledger = {"status": "ungraded", "statement": None, "ledger": None}
    graded = {"status": "ungraded", "statement": None, "ledger": {"rows": [], "verdict": "confirmed"}}
    two_sided = {"status": "mismatch", "statement": "SELECT 1", "ledger": {"rows": [], "verdict": "confirmed"}}
    assert reconcile._one_query(ungraded_no_ledger) is True
    assert reconcile._one_query(graded) is True
    assert reconcile._one_query(two_sided) is False


def test_a_failing_check_on_agamis_query_names_agami_not_the_person():
    """`query_defect` on a row the person wrote nothing for means agami wrote the mistake. The two
    words that name a writer swap; a gap in the semantic model is a gap whoever tripped on it."""
    assert reconcile._STATE_WORDS["defect"] == "a mistake in your query"
    assert reconcile._STATE_WORDS_AGAMI["defect"] == "a mistake in agami's query"
    unchanged = set(reconcile._STATE_WORDS) - {"defect"}
    assert all(reconcile._STATE_WORDS_AGAMI[k] == reconcile._STATE_WORDS[k] for k in unchanged)
    assert reconcile._STATE_WORDS_AGAMI["gap"] == "a gap in the semantic model"


def test_every_fix_the_items_verb_can_emit_has_a_colour_family_on_the_page():
    """`FIX_CLASS` in the template is the display mirror of `_FIX_OWNER`; when only one side gained
    the new fix, the action pill rendered as `class="pill "` with no family at all."""
    tpl = (REPO_ROOT / "plugins" / "agami" / "shared" / "reconcile-report-template.html").read_text(encoding="utf-8")
    declared = re.search(r"const FIX_CLASS = \{([^}]*)\}", tpl).group(1)
    for fix in reconcile._FIX_WORDS:
        assert re.search(rf"\b{re.escape(fix)}:", declared), f"FIX_CLASS has no entry for {fix!r}"
