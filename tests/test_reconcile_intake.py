"""`reconcile.py intake` — any of the four input shapes a person brings, read into evidence rows.

The skill used to read one shape: a label and a number. A third CSV column was glued onto the label
as text, so a spreadsheet of tile, number and the SQL behind the tile arrived as a mangled label; a
list of questions, or SQL pasted on its own, was not recognised at all. `intake` reads all four and
says which it saw. The number path is unchanged: `parse`, `diff` and `band` are pinned elsewhere, and
a two-column CSV still goes through `parse_csv`.

Shapes, as the contract names them: (a) questions only, (b) questions with the SQL the person
expects, (c) labels with numbers, (d) labels with numbers and the SQL behind each. Synthetic fixtures
throughout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402
from reconcile import intake  # noqa: E402


def _file(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# --- the four shapes ------------------------------------------------------


def test_a_two_column_csv_is_the_number_shape_and_still_goes_through_parse_csv(tmp_path):
    p = _file(tmp_path, "tiles.csv", "Label,Value\nQ3 Revenue,$4.2M\nActive customers,12450\n")
    d = intake([p])
    assert d["shape"] == "c"
    assert [r["label"] for r in d["rows"]] == ["Q3 Revenue", "Active customers"]
    assert d["rows"][0]["expected"] == 4_200_000.0 and d["rows"][0]["raw_value"] == "$4.2M"
    assert d["rows"][0]["statement"] is None and d["rows"][0]["question"] is None


def test_a_third_column_of_sql_becomes_the_statement_and_the_label_is_not_extended(tmp_path):
    p = _file(tmp_path, "tiles.csv",
              'Label,Value,SQL\nQ3 Revenue,$4.2M,"SELECT SUM(total) FROM orders WHERE q = 3"\n')
    d = intake([p])
    assert d["shape"] == "d"
    (row,) = d["rows"]
    # Today `parse_csv` would have read the label as `Q3 Revenue (SELECT SUM(total) ...)`.
    assert row["label"] == "Q3 Revenue"
    assert row["statement"] == "SELECT SUM(total) FROM orders WHERE q = 3"
    assert row["expected"] == 4_200_000.0


def test_a_third_column_that_is_not_sql_is_still_glued_onto_the_label_as_today(tmp_path):
    # The one pinned three-column case from `tests/test_reconcile.py`: context, not a statement.
    p = _file(tmp_path, "tiles.csv", "Metric,Value,Quarter\nRevenue,4.2M,Q3 2025\n")
    d = intake([p])
    (row,) = d["rows"]
    assert d["shape"] == "c" and row["label"] == "Revenue (Q3 2025)" and row["statement"] is None


def test_plain_lines_are_questions(tmp_path):
    p = _file(tmp_path, "questions.txt",
              "How many orders did we ship in August?\nWhat was revenue by region last quarter?\n")
    d = intake([p])
    assert d["shape"] == "a"
    assert [r["question"] for r in d["rows"]] == [
        "How many orders did we ship in August?", "What was revenue by region last quarter?"]
    assert all(r["statement"] is None and r["expected"] is None for r in d["rows"])


def test_question_and_sql_pairs_are_the_trusted_sql_shape(tmp_path):
    p = _file(tmp_path, "pairs.csv",
              'question,sql\nHow many paid orders?,"SELECT COUNT(*) FROM orders WHERE status = \'paid\'"\n')
    d = intake([p])
    assert d["shape"] == "b"
    (row,) = d["rows"]
    assert row["question"] == "How many paid orders?"
    assert row["statement"] == "SELECT COUNT(*) FROM orders WHERE status = 'paid'"
    assert row["expected"] is None   # produced later, by running the statement


def test_sql_pasted_on_its_own_is_a_statement_row_with_no_question_yet(tmp_path):
    p = _file(tmp_path, "trusted.sql",
              "WITH x AS (SELECT 1 AS n) SELECT SUM(n) FROM x;\n")
    d = intake([p])
    assert d["shape"] == "b"
    (row,) = d["rows"]
    assert row["statement"].startswith("WITH x AS") and row["question"] is None


def test_a_json_list_reads_the_same_as_the_csv(tmp_path):
    p = _file(tmp_path, "rows.json", json.dumps([
        {"question": "How many paid orders?", "sql": "SELECT COUNT(*) FROM orders WHERE status = 'paid'"},
        {"label": "Q3 Revenue", "value": "$4.2M"},
        "What was revenue by region last quarter?",
    ]))
    d = intake([p])
    shapes = [r["provenance"]["shape"] for r in d["rows"]]
    assert shapes == ["b", "c", "a"]
    assert d["rows"][1]["expected"] == 4_200_000.0


def test_the_header_row_is_recognised_by_name_and_never_becomes_a_row(tmp_path):
    p = _file(tmp_path, "pairs.csv", "Question,Statement\nHow many?,SELECT COUNT(*) FROM orders\n")
    d = intake([p])
    assert len(d["rows"]) == 1 and d["rows"][0]["statement"] == "SELECT COUNT(*) FROM orders"


# --- mixed input ------------------------------------------------------------


def test_a_statement_whose_label_matches_a_tile_joins_that_tiles_row(tmp_path):
    tiles = _file(tmp_path, "tiles.csv", "Label,Value\nQ3 Revenue,$4.2M\nActive customers,12450\n")
    sql = _file(tmp_path, "sql.csv",
                'label,sql\nq3 revenue,"SELECT SUM(total) FROM orders WHERE q = 3"\n'
                'Refund rate,"SELECT AVG(refunded) FROM orders"\n')
    d = intake([tiles, sql])
    by_label = {r["label"]: r for r in d["rows"]}
    # Matched under the case-and-whitespace fold: the SQL lands on the tile's row.
    assert by_label["Q3 Revenue"]["statement"].startswith("SELECT SUM(total)")
    assert by_label["Q3 Revenue"]["expected"] == 4_200_000.0
    # An unmatched tile stays a number-only row; an unmatched statement gets its own row.
    assert by_label["Active customers"]["statement"] is None
    assert by_label["Refund rate"]["expected"] is None and by_label["Refund rate"]["statement"]
    assert len(d["rows"]) == 3


def test_provenance_names_the_file_the_line_and_the_persons_words(tmp_path):
    p = _file(tmp_path, "tiles.csv", "Label,Value\nQ3 Revenue,$4.2M\n")
    d = intake([p], source="the finance dashboard")
    (row,) = d["rows"]
    assert row["provenance"] == {
        "shape": "c", "source": "the finance dashboard", "file": "tiles.csv", "line": 2,
        "graded": None,
    }


# --- what is skipped or refused ----------------------------------------------


def test_a_row_with_nothing_usable_is_skipped_with_a_reason(tmp_path):
    p = _file(tmp_path, "tiles.csv", "Label,Value\nStatus,active\nRevenue,100\n")
    d = intake([p])
    assert [r["label"] for r in d["rows"]] == ["Revenue"]
    (skipped,) = d["skipped"]
    assert skipped["line"] == 2 and "number" in skipped["reason"]


def test_a_file_with_nothing_usable_exits_four(tmp_path, capsys):
    p = _file(tmp_path, "tiles.csv", "Label,Value\nStatus,active\n")
    assert reconcile.main(["intake", "--file", str(p)]) == 4
    assert "nothing usable" in capsys.readouterr().err


def test_a_missing_file_exits_two(tmp_path, capsys):
    assert reconcile.main(["intake", "--file", str(tmp_path / "missing.csv")]) == 2
    assert "not found" in capsys.readouterr().err.lower()


def test_the_verb_prints_the_same_json_the_function_returns(tmp_path, capsys):
    p = _file(tmp_path, "pairs.csv", 'question,sql\nHow many?,"SELECT COUNT(*) FROM orders"\n')
    assert reconcile.main(["intake", "--file", str(p), "--source", "analyst"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == intake([p], source="analyst")
