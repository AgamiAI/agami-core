"""The chart drawn in the chat: text bars from the same sections file the HTML chart reads."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from text_chart import MAX_ROWS, WIDTH, draw_section, main  # noqa: E402


def _section(labels, data, shown=None, **extra):
    return {
        "title": "Revenue by category",
        "labels": labels,
        "datasets": [{"label": "Revenue", "data": data}],
        "table_headers": ["category", "Revenue"],
        "table_rows": [[lab, s] for lab, s in zip(labels, shown or [f"{v:,}" for v in data])],
        **extra,
    }


def _bars(text: str) -> list[str]:
    block = text.split("```text\n", 1)[1].split("\n```", 1)[0]
    return block.splitlines()


def test_the_longest_bar_is_full_width_and_values_come_from_the_table():
    out = draw_section(_section(["Home", "Books"], [200.0, 100.0], ["$200.00", "$100.00"]))
    home, books = _bars(out)
    assert home.count("█") == WIDTH and home.endswith("$200.00")
    assert books.count("█") == WIDTH // 2 and books.endswith("$100.00")
    assert out.startswith("**Revenue by category**")


def test_values_line_up_on_the_right():
    rows = _bars(draw_section(_section(["a", "b"], [1000.0, 5.0], ["1,000", "5"])))
    assert len({len(r) for r in rows}) == 1


def test_more_rows_than_the_chat_limit_are_counted_not_drawn():
    n = MAX_ROWS + 3
    out = draw_section(_section([f"r{i}" for i in range(n)], [float(n - i) for i in range(n)]))
    assert len(_bars(out)) == MAX_ROWS
    assert f"Showing {MAX_ROWS} of {n}; 3 more in the table." in out


def test_negative_and_empty_sections_say_why_there_are_no_bars():
    assert "negative" in draw_section(_section(["a", "b"], [5.0, -1.0]))
    assert "nothing to chart" in draw_section({"title": "Empty", "labels": [], "datasets": []})


def test_labels_with_no_data_say_there_is_nothing_to_chart():
    # Used to crash with "max() iterable argument is empty".
    assert "nothing to chart" in draw_section(_section(["a", "b"], []))
    assert "nothing to chart" in draw_section({"title": "T", "labels": ["a"], "datasets": [{"label": "x"}]})
    assert "nothing to chart" in draw_section(_section(["a"], [None], [""]))


def test_nan_and_null_values_are_missing_not_a_crash():
    rows = _bars(draw_section(_section(["a", "b", "c"], [10.0, float("nan"), None], ["10", "nan", ""])))
    assert rows[0].count("█") == WIDTH
    for row in rows[1:]:
        assert "█" not in row and row.rstrip().endswith("—")


def test_missing_values_without_table_rows_show_a_dash():
    s = {"title": "T", "labels": ["a", "b"], "datasets": [{"label": "x", "data": [4, float("inf")]}]}
    rows = _bars(draw_section(s))
    assert rows[0].rstrip().endswith("4") and rows[1].rstrip().endswith("—")


def test_several_value_columns_bar_the_first_and_say_so():
    s = _section(["a"], [3.0])
    s["datasets"].append({"label": "Orders", "data": [7]})
    assert "Bars show Revenue" in draw_section(s)


def test_cli_prints_every_section(tmp_path, capsys):
    f = tmp_path / "sections.json"
    f.write_text(json.dumps([_section(["a"], [1.0]), {**_section(["b"], [2.0]), "title": "Second"}]))
    assert main(["--sections-file", str(f)]) == 0
    out = capsys.readouterr().out
    assert "**Revenue by category**" in out and "**Second**" in out


def test_cli_refuses_a_file_that_is_not_a_list(tmp_path):
    f = tmp_path / "sections.json"
    f.write_text("{}")
    assert main(["--sections-file", str(f)]) == 2


def test_the_query_skill_draws_the_chart_in_the_chat():
    skill = (REPO_ROOT / "plugins" / "agami" / "skills" / "agami-query" / "SKILL.md").read_text()
    assert "text_chart.py --sections-file" in skill
    assert "a bar chart drawn here in the chat" in skill


def test_the_chart_is_posted_in_the_final_message():
    skill = (REPO_ROOT / "plugins" / "agami" / "skills" / "agami-query" / "SKILL.md").read_text()
    assert "**Post it in the turn's final message**" in skill
