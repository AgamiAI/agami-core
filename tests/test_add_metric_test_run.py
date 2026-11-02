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


def _run(tmp_path: Path, monkeypatch, capsys, items: list[dict], profile: str | None = "demo"):
    _write_model(tmp_path, git=False)
    seen: list[str] = []

    def runner(sql: str):
        seen.append(sql)
        if "missing_col" in sql:
            raise RuntimeError("column does not exist")
        return []

    monkeypatch.setattr(INTRO, "make_execute_sql_runner", lambda *a, **k: runner)
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
