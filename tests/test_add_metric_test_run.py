"""`sm add --profile` runs each metric's SQL over zero rows before anything is written.

A live onboarding signed off two metrics whose SQL named a column the table doesn't have and
compared a text code to a number. Nothing ran the SQL before the sign-off, so neither was caught
until an example query failed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
yaml = pytest.importorskip("yaml")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from semantic_model import cli  # noqa: E402
from semantic_model import introspect as INTRO  # noqa: E402

from test_semantic_model_curate import _write_model  # noqa: E402


def _metric(name: str, binding: str, tables: list[str]) -> dict:
    return {"name": name, "description": name, "calculation": name,
            "bindings": {"PostgreSQL": binding}, "source_tables": tables}


def _add_archive_orders(root: Path) -> None:
    """A second `orders`, in schema `archive`, so a bare `orders` names two tables."""
    area = root / "subject_areas" / "sales"
    sa = yaml.safe_load((area / "subject_area.yaml").read_text())
    sa["tables"].append({"storage_connection": "c", "schema": "archive", "table": "orders"})
    (area / "subject_area.yaml").write_text(yaml.safe_dump(sa))
    (area / "tables" / "archive_orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "archive", "storage_connection": "c", "grain": ["id"],
        "description": "archived orders", "columns": [{"name": "id", "type": "integer", "primary_key": True}],
    }))


def _run(tmp_path: Path, monkeypatch, capsys, items: list[dict], profile: str | None = "demo",
         runner=None, archive: bool = False):
    _write_model(tmp_path, git=False)
    if archive:
        _add_archive_orders(tmp_path)
    seen: list[str] = []

    def default_runner(sql: str):
        if "missing_col" in sql:
            raise RuntimeError("column does not exist")
        return []

    def recording(sql: str):
        seen.append(sql)
        return (runner or default_runner)(sql)

    monkeypatch.setattr(INTRO, "make_execute_sql_runner", lambda *a, **k: recording)
    f = tmp_path / "metrics.json"
    f.write_text(json.dumps(items))
    argv = ["add", str(tmp_path), "--kind", "metric", "--area", "sales", "--file", str(f)]
    args = cli.build_parser().parse_args(argv + (["--profile", profile] if profile else []))
    rc = args.func(args)
    return rc, json.loads(capsys.readouterr().out), seen


def test_a_metric_whose_sql_fails_is_not_written(tmp_path, monkeypatch, capsys):
    rc, out, _ = _run(tmp_path, monkeypatch, capsys,
                      [_metric("good", "COUNT(*)", ["orders"]), _metric("bad", "SUM(missing_col)", ["orders"])])
    assert rc == 1 and out["applied"] == []
    assert out["test_run"]["good"] == {"ok": True} and "error" in out["test_run"]["bad"]
    assert not (tmp_path / "subject_areas" / "sales" / "metrics" / "good.yaml").exists()


def test_metrics_that_run_are_written_with_their_test_run(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(tmp_path, monkeypatch, capsys, [_metric("good", "COUNT(*)", ["orders"])])
    assert rc == 0 and out["test_run"] == {"good": {"ok": True}}
    assert len(seen) == 1 and "orders" in seen[0] and seen[0].endswith("WHERE 1 = 0")


def test_a_metric_over_several_tables_is_reported_not_test_run(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(tmp_path, monkeypatch, capsys, [_metric("two", "COUNT(*)", ["orders", "customers"])])
    assert rc == 0 and "not_test_run" in out["test_run"]["two"] and seen == []


def test_without_a_profile_nothing_runs(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(tmp_path, monkeypatch, capsys, [_metric("bad", "SUM(missing_col)", ["orders"])], profile=None)
    assert rc == 0 and "test_run" not in out and seen == []


def test_a_trailing_line_comment_cannot_swallow_the_from(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(tmp_path, monkeypatch, capsys, [_metric("c", "COUNT(*) -- all orders", ["orders"])])
    assert rc == 0 and out["test_run"] == {"c": {"ok": True}}
    from_line = next(line for line in seen[0].splitlines() if line.startswith("FROM"))
    assert "--" not in from_line and seen[0].splitlines()[-1] == "WHERE 1 = 0"


def _unreachable(sql: str):
    raise INTRO.ExecuteSqlFailure("could not connect to server: Connection refused", 10)


def test_an_unreachable_database_is_one_connection_error_not_a_bad_binding(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(tmp_path, monkeypatch, capsys,
                         [_metric("a", "COUNT(*)", ["orders"]), _metric("b", "SUM(id)", ["orders"])],
                         runner=_unreachable)
    assert rc == 1 and out["applied"] == []
    assert out["test_run"] == {"connection": {"error": "could not connect to server: Connection refused"}}
    assert len(out["errors"]) == 1 and "could not reach the database" in out["errors"][0]
    assert "fix the binding" not in out["errors"][0]
    assert len(seen) == 1  # stops at the first: every other metric would fail the same way
    assert not (tmp_path / "subject_areas" / "sales" / "metrics" / "a.yaml").exists()


def test_an_unclassified_failure_that_select_1_shares_is_a_connection_error(tmp_path, monkeypatch, capsys):
    def no_driver(sql: str):
        raise RuntimeError("No module named psycopg2")

    rc, out, seen = _run(tmp_path, monkeypatch, capsys, [_metric("a", "COUNT(*)", ["orders"])], runner=no_driver)
    assert rc == 1 and "connection" in out["test_run"] and seen[-1] == "SELECT 1"


def test_a_classified_sql_error_is_the_bindings_fault_without_a_select_1(tmp_path, monkeypatch, capsys):
    def bad_column(sql: str):
        raise INTRO.ExecuteSqlFailure("column \"nope\" does not exist", 7)

    rc, out, seen = _run(tmp_path, monkeypatch, capsys, [_metric("a", "SUM(nope)", ["orders"])], runner=bad_column)
    assert rc == 1 and "error" in out["test_run"]["a"] and "SELECT 1" not in seen
    assert "fix the binding" in out["errors"][0]


def test_the_source_table_resolves_by_schema(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(tmp_path, monkeypatch, capsys,
                         [_metric("arch", "COUNT(*)", ["archive.orders"]), _metric("pub", "COUNT(*)", ["public.orders"]),
                          _metric("bare", "COUNT(*)", ["orders"])], archive=True)
    assert out["test_run"]["arch"] == {"ok": True} and out["test_run"]["pub"] == {"ok": True}
    assert any('"archive"."orders"' in q for q in seen) and any('"public"."orders"' in q for q in seen)
    assert "several schemas" in out["test_run"]["bare"]["not_test_run"]


def test_the_runner_names_the_failure_kind_from_the_exit_code():
    assert INTRO.ExecuteSqlFailure("x", 4).kind == "auth"
    assert INTRO.ExecuteSqlFailure("x", 7).kind == "column_not_found"
    assert INTRO.ExecuteSqlFailure("x", 99).kind is None
