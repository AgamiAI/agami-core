"""Every verdict a person reads is a sentence, and the sentences live in one place.

The tokens stay on the wire: `rows.jsonl`, the keep gate and the `status` verb's choices are pinned
to them, and churning that would be risk without a reader benefit. What is pinned here is that no
surface a person looks at ever shows one. `expected_doubtful` is why the rule exists: it is the row
where the analyst's own query is the thing in doubt, the most delicate claim the tool makes, and a
reader who has to look the word up will not trust it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402

SKILL = (REPO_ROOT / "plugins" / "agami" / "skills" / "agami-reconcile" / "SKILL.md").read_text()
TEMPLATE = (REPO_ROOT / "plugins" / "agami" / "shared" / "reconcile-report-template.html").read_text()

# The words the machinery uses among itself. None of them belongs on a page or in a chat line.
TOKENS = ("match_unverified", "expected_doubtful", "query_defect", "model_gap", "fan_out", "values_declared",
          "question_fit", "dropped_rows", "not_comparable", "could_not_compare")


def test_every_status_has_a_sentence_and_no_sentence_is_a_token():
    assert set(reconcile._STATUS_WORDS) == {reconcile.MATCH, reconcile.MATCH_UNVERIFIED, reconcile.MISMATCH,
                                            reconcile.EXPECTED_DOUBTFUL, reconcile.ERROR}
    for token, words in reconcile._STATUS_WORDS.items():
        assert "_" not in words, (token, words)
        assert token not in words, (token, words)
        assert words == words.lower(), words


def test_the_two_statuses_about_the_person_s_query_say_so_in_words():
    # The whole point: a reader learns what happened without a glossary, and learns it is about
    # THEIR query rather than about agami's.
    assert reconcile._STATUS_WORDS[reconcile.EXPECTED_DOUBTFUL] == "different answer, and your query has a problem"
    assert "your query" in reconcile._STATUS_WORDS[reconcile.MATCH_UNVERIFIED]
    # and the two that are only about the answers do not blame anyone
    assert reconcile._STATUS_WORDS[reconcile.MATCH] == "same answer"
    assert reconcile._STATUS_WORDS[reconcile.MISMATCH] == "different answer"


def test_a_status_nobody_planned_for_never_reads_as_its_token():
    assert reconcile.status_words("some_status_added_later") == "could not compare"
    assert reconcile.status_words(None) == "could not compare"
    assert reconcile.status_words("") == "could not compare"


def test_the_page_takes_the_words_from_the_data_and_keeps_no_copy():
    # Two glossaries drift, and then the card, the filter chip and the chat call one row three
    # things. So the page is handed its legend and writes no status vocabulary of its own: the
    # compound status tokens do not appear on it at all, not even as the key of a colour map.
    assert "item.status_words" in TEMPLATE
    assert "DATA.status.cls" in TEMPLATE and "DATA.status.chips" in TEMPLATE
    for token in ("match_unverified", "expected_doubtful", "mismatch"):
        assert token not in TEMPLATE, token
    assert "match by luck" not in TEMPLATE


def test_the_legend_the_page_is_handed_covers_every_status():
    legend = reconcile.status_legend()
    assert set(legend["cls"]) == set(reconcile._STATUS_WORDS)
    # every colour a status can take has chip words, or a filter chip would render its own class name
    assert set(legend["cls"].values()) <= set(legend["chips"])
    # the chips are a coarser grouping on purpose: the two statuses about the person's query share one
    assert legend["cls"][reconcile.MATCH_UNVERIFIED] == legend["cls"][reconcile.EXPECTED_DOUBTFUL]
    assert "your query" in legend["chips"][legend["cls"][reconcile.EXPECTED_DOUBTFUL]]


def test_every_item_carries_its_status_in_words(tmp_path):
    run = tmp_path / "20260914-090000"
    (run / "rows").mkdir(parents=True)
    records = [
        {"row": 1, "label": "Q3 revenue", "question": "What was revenue in Q3?", "expected": 10.0, "actual": 10.0,
         "match": True, "status": "match", "recorded": {"columns": ["revenue"], "rows": [[10.0]]},
         "ledger": {"rows": [], "verdict": "confirmed", "counts": {}}, "ledger_verdict": "confirmed"},
        {"row": 2, "label": "Open items", "question": "How many items are open?", "expected": 4.0, "actual": 9.0,
         "match": False, "status": "expected_doubtful", "recorded": {"columns": ["n"], "rows": [[9.0]]},
         "statement": "SELECT COUNT(*) AS n FROM items WHERE state = 'Open'",
         "ledger": {"rows": [{"part": "literal:items.state='Open'", "verdict": "query_defect", "kind": None,
                              "depends_on": [], "evidence": {}, "note": "not a value the column holds"}],
                    "verdict": "query_defect", "counts": {}}, "ledger_verdict": "query_defect"},
    ]
    (run / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    items = reconcile.report_items(run)
    assert [i["status_words"] for i in items] == ["same answer", "different answer, and your query has a problem"]
    for item in items:
        assert item["status_words"] == reconcile._STATUS_WORDS[item["status"]]


def test_no_token_reaches_a_person_in_what_phase_3_says():
    # The fenced blocks under Phase 3 are the lines the skill puts in front of the person verbatim.
    phase3 = SKILL[SKILL.index("## Phase 3"):]
    for block in re.findall(r"```[a-z]*\n(.*?)```", phase3, re.S):
        for token in TOKENS:
            assert token not in block, (token, block.strip()[:160])


def test_a_status_the_skill_spells_out_is_the_table_s_sentence_verbatim():
    """Banning the token is half the rule; the other half is that the words replacing it come from
    `_STATUS_WORDS` and not from a paraphrase. Phase 3's summary line quoted both sentences and then
    appended a fragment from an older template, so the line a person read ended "and your query has
    a problem in them" — no token, and still not the sentence the table gives.
    """
    phase3 = SKILL[SKILL.index("## Phase 3"):]
    for status in (reconcile.MATCH_UNVERIFIED, reconcile.EXPECTED_DOUBTFUL):
        words = reconcile._STATUS_WORDS[status]
        for block in re.findall(r"```[a-z]*\n(.*?)```", phase3, re.S):
            if words in block:
                # Whatever follows the sentence must start a new clause, never continue it.
                tail = block.split(words, 1)[1][:1]
                assert tail in ("", ".", ";", ",", "\n", ")"), (status, block.split(words, 1)[1][:60])


def test_the_card_never_labels_a_check_with_the_machinery_s_word():
    labels = " ".join(reconcile._PART_KEYS.values())
    assert "fan-out" not in labels and "fan_out" not in labels
    # the skill forbids the word in prose; the code that renders the label must agree with it
    assert 'never "fan-out"' in SKILL
    assert reconcile._PART_KEYS["fan_out"] == "double counting in {x}"
