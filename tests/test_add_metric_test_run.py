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
    return {
        "name": name,
        "description": name,
        "calculation": name,
        "bindings": {"PostgreSQL": binding},
        "source_tables": tables,
    }


def _add_archive_orders(root: Path) -> None:
    """A second `orders`, in schema `archive`, so a bare `orders` names two tables."""
    area = root / "subject_areas" / "sales"
    sa = yaml.safe_load((area / "subject_area.yaml").read_text())
    sa["tables"].append({"storage_connection": "c", "schema": "archive", "table": "orders"})
    (area / "subject_area.yaml").write_text(yaml.safe_dump(sa))
    (area / "tables" / "archive_orders.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "orders",
                "schema": "archive",
                "storage_connection": "c",
                "grain": ["id"],
                "description": "archived orders",
                "columns": [{"name": "id", "type": "integer", "primary_key": True}],
            }
        )
    )


def _run(
    tmp_path: Path,
    monkeypatch,
    capsys,
    items: list[dict],
    profile: str | None = "demo",
    runner=None,
    archive: bool = False,
):
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
    rc, out, _ = _run(
        tmp_path,
        monkeypatch,
        capsys,
        [_metric("good", "COUNT(*)", ["orders"]), _metric("bad", "SUM(missing_col)", ["orders"])],
    )
    assert rc == 1 and out["applied"] == []
    assert out["test_run"]["good"] == {"ok": True} and "error" in out["test_run"]["bad"]
    assert not (tmp_path / "subject_areas" / "sales" / "metrics" / "good.yaml").exists()


def test_metrics_that_run_are_written_with_their_test_run(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(tmp_path, monkeypatch, capsys, [_metric("good", "COUNT(*)", ["orders"])])
    assert rc == 0 and out["test_run"] == {"good": {"ok": True}}
    assert len(seen) == 1 and "orders" in seen[0] and seen[0].endswith("WHERE 1 = 0")


def test_a_metric_over_several_tables_is_reported_not_test_run(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(
        tmp_path, monkeypatch, capsys, [_metric("two", "COUNT(*)", ["orders", "customers"])]
    )
    assert rc == 0 and "not_test_run" in out["test_run"]["two"] and seen == []


def test_without_a_profile_nothing_runs(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(
        tmp_path,
        monkeypatch,
        capsys,
        [_metric("bad", "SUM(missing_col)", ["orders"])],
        profile=None,
    )
    assert rc == 0 and "test_run" not in out and seen == []


def test_a_trailing_line_comment_cannot_swallow_the_from(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(
        tmp_path, monkeypatch, capsys, [_metric("c", "COUNT(*) -- all orders", ["orders"])]
    )
    assert rc == 0 and out["test_run"] == {"c": {"ok": True}}
    from_line = next(line for line in seen[0].splitlines() if line.startswith("FROM"))
    assert "--" not in from_line and seen[0].splitlines()[-1] == "WHERE 1 = 0"


def _unreachable(sql: str):
    raise INTRO.ExecuteSqlFailure("could not connect to server: Connection refused", 10)


def test_an_unreachable_database_is_one_connection_error_not_a_bad_binding(
    tmp_path, monkeypatch, capsys
):
    rc, out, seen = _run(
        tmp_path,
        monkeypatch,
        capsys,
        [_metric("a", "COUNT(*)", ["orders"]), _metric("b", "SUM(id)", ["orders"])],
        runner=_unreachable,
    )
    assert rc == 1 and out["applied"] == []
    assert out["test_run"] == {
        "connection": {"error": "could not connect to server: Connection refused"}
    }
    assert len(out["errors"]) == 1 and "could not reach the database" in out["errors"][0]
    assert "fix the binding" not in out["errors"][0]
    assert len(seen) == 1  # stops at the first: every other metric would fail the same way
    assert not (tmp_path / "subject_areas" / "sales" / "metrics" / "a.yaml").exists()


def test_an_unclassified_failure_that_select_1_shares_is_a_connection_error(
    tmp_path, monkeypatch, capsys
):
    def no_driver(sql: str):
        raise RuntimeError("No module named psycopg2")

    rc, out, seen = _run(
        tmp_path, monkeypatch, capsys, [_metric("a", "COUNT(*)", ["orders"])], runner=no_driver
    )
    assert rc == 1 and "connection" in out["test_run"] and seen[-1] == "SELECT 1"


def test_a_classified_sql_error_is_the_bindings_fault_without_a_select_1(
    tmp_path, monkeypatch, capsys
):
    def bad_column(sql: str):
        raise INTRO.ExecuteSqlFailure('column "nope" does not exist', 7)

    rc, out, seen = _run(
        tmp_path, monkeypatch, capsys, [_metric("a", "SUM(nope)", ["orders"])], runner=bad_column
    )
    assert rc == 1 and "error" in out["test_run"]["a"] and "SELECT 1" not in seen
    assert "fix the binding" in out["errors"][0]


def test_the_source_table_resolves_by_schema(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(
        tmp_path,
        monkeypatch,
        capsys,
        [
            _metric("arch", "COUNT(*)", ["archive.orders"]),
            _metric("pub", "COUNT(*)", ["public.orders"]),
            _metric("bare", "COUNT(*)", ["orders"]),
        ],
        archive=True,
    )
    assert out["test_run"]["arch"] == {"ok": True} and out["test_run"]["pub"] == {"ok": True}
    assert any('"archive"."orders"' in q for q in seen) and any(
        '"public"."orders"' in q for q in seen
    )
    assert "several schemas" in out["test_run"]["bare"]["not_test_run"]


def test_the_runner_names_the_failure_kind_from_the_exit_code():
    assert INTRO.ExecuteSqlFailure("x", 4).kind == "auth"
    assert INTRO.ExecuteSqlFailure("x", 7).kind == "column_not_found"
    assert INTRO.ExecuteSqlFailure("x", 99).kind is None


# A live sample run added "average order value" as `{revenue} / {order count}`. The test run sent
# the braces to SQLite as written, which answered with its generic "unexpected error on the server",
# so the metric could never be added, with or without its bases in the same file.


def _derived(name: str, binding: str, bases: list[str], tables: list[str] | None = None) -> dict:
    return {**_metric(name, binding, tables if tables is not None else ["orders"]), "base_metrics": bases}


def test_a_derived_metric_over_a_model_metric_runs_its_expanded_sql(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(
        tmp_path,
        monkeypatch,
        capsys,
        [_derived("orders per order", "{order_count} / {order_count}", ["order_count"])],
    )
    assert rc == 0 and out["test_run"] == {"orders per order": {"ok": True}}
    assert "{" not in seen[0] and "(COUNT(*)) / (COUNT(*))" in seen[0]
    assert (tmp_path / "subject_areas" / "sales" / "metrics" / "orders_per_order.yaml").exists()


def test_a_derived_metric_and_its_bases_can_be_added_in_one_file(tmp_path, monkeypatch, capsys):
    # The derived metric comes first in the file: its bases are taken from the batch whatever the order.
    rc, out, seen = _run(
        tmp_path,
        monkeypatch,
        capsys,
        [
            _derived("average order value", "{revenue} / {order count}", ["revenue", "order count"]),
            _metric("revenue", "SUM(amount)", ["orders"]),
            _metric("order count", "COUNT(DISTINCT id)", ["orders"]),
        ],
    )
    assert rc == 0, out
    assert all(r == {"ok": True} for r in out["test_run"].values())
    aov = next(q for q in seen if "/" in q)
    assert "(SUM(amount)) / (COUNT(DISTINCT id))" in aov and "{" not in aov
    written = {p.name for p in (tmp_path / "subject_areas" / "sales" / "metrics").iterdir()}
    assert {"average_order_value.yaml", "revenue.yaml", "order_count.yaml"} <= written


def test_a_derived_metric_with_no_source_tables_runs_over_its_bases_table(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(
        tmp_path,
        monkeypatch,
        capsys,
        [_derived("double count", "2 * {order_count}", ["order_count"], tables=[])],
    )
    assert out["test_run"] == {"double count": {"ok": True}}
    assert '"public"."orders"' in seen[0]


def test_a_base_that_is_nowhere_is_said_plainly_and_nothing_runs(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(
        tmp_path,
        monkeypatch,
        capsys,
        [_derived("aov", "{revenue} / {order_count}", ["revenue", "order_count"])],
    )
    assert rc == 1 and seen == [] and out["applied"] == []
    reason = out["test_run"]["aov"]["error"]
    assert "revenue" in reason and "same file" in reason
    assert "server" not in reason


def test_an_unexpected_database_error_says_the_databases_own_words(tmp_path, monkeypatch, capsys):
    def generic(sql: str):
        if sql == "SELECT 1":
            return []
        raise INTRO.ExecuteSqlFailure(
            "The statement could not be run because of an unexpected error on the server. "
            "The details are in the server log.\n"
            'Database error: SQLite execution error: unrecognized token: "#"',
            6,
        )

    rc, out, _ = _run(tmp_path, monkeypatch, capsys, [_metric("a", "SUM(#)", ["orders"])], runner=generic)
    reason = out["test_run"]["a"]["error"]
    assert "unrecognized token" in reason and "server log" not in reason


@pytest.mark.parametrize("kind,folder", [("metric", "metrics"), ("entity", "entities")])
def test_an_add_never_signs_itself_off(tmp_path, kind, folder):
    # As a join add never approves (test_curate_add_join): a file item copied from a signed-off
    # definition carries its sign-off, and the step list then counted it as the person's.
    from semantic_model import curate

    _write_model(tmp_path, git=False)
    item = {"name": "copied", "description": "d", "review_state": "approved",
            "signed_off_by": "you@example.com", "signed_off_at": "2026-01-01T00:00:00Z",
            "signed_off_role": "maintainer"}
    item.update(_metric("copied", "COUNT(*)", ["orders"]) if kind == "metric"
                else {"maps_to": [{"table": "orders", "column": "id"}]})
    res = curate.write_items(tmp_path, "sales", kind, [item], signer="you@example.com", role="data_lead")
    assert res.validated, res.errors
    doc = yaml.safe_load((tmp_path / "subject_areas" / "sales" / folder / "copied.yaml").read_text())
    assert doc["review_state"] == "unreviewed"
    assert not {"signed_off_by", "signed_off_at", "signed_off_role"} & set(doc)


def test_a_derived_metric_in_a_cycle_is_said_and_nothing_runs(tmp_path, monkeypatch, capsys):
    rc, out, seen = _run(
        tmp_path,
        monkeypatch,
        capsys,
        [_derived("a", "{b} + 1", ["b"]), _derived("b", "{a} + 1", ["a"])],
    )
    assert rc == 1 and seen == []
    assert "cycle" in out["test_run"]["a"]["error"]


def test_a_file_item_the_model_refuses_still_expands_for_the_test_run(tmp_path, monkeypatch, capsys):
    # An unknown key makes the item fail the model's own check; the test run still expands it, and
    # the write reports the key.
    item = {**_derived("twice", "2 * {order_count}", ["order_count"]), "colour": "blue"}
    rc, out, seen = _run(tmp_path, monkeypatch, capsys, [item])
    assert out["test_run"] == {"twice": {"ok": True}}
    assert "(COUNT(*))" in seen[0]


def test_a_failure_with_no_words_is_named_by_its_kind(tmp_path, monkeypatch, capsys):
    def silent(sql: str):
        if sql == "SELECT 1":
            return []
        raise ValueError()

    rc, out, _ = _run(tmp_path, monkeypatch, capsys, [_metric("a", "SUM(id)", ["orders"])], runner=silent)
    assert out["test_run"]["a"] == {"error": "ValueError"}
