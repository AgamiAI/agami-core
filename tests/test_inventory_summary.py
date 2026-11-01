"""inventory_summary.py: summarise a discover inventory and write the kept-tables file."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "agami"
    / "scripts"
    / "inventory_summary.py"
)
sys.path.insert(0, str(SCRIPT.parent))

import inventory_summary  # noqa: E402


def _inventory(spec: dict[str, list[str]]) -> dict:
    return {
        "schemas": list(spec),
        "tables": [
            {"schema": s, "table": t, "columns": [{"name": "id", "type": "int"}]}
            for s, ts in spec.items()
            for t in ts
        ],
    }


def _run(tmp_path: Path, inv: dict, *args: str) -> tuple[int, dict]:
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inv), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--inventory", str(path), *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, json.loads(proc.stdout)


@pytest.mark.parametrize(
    "name",
    [
        "stg_orders",
        "orders_bak",
        "orders_2023",
        "OrdersBackup",
        "tmp.orders",
        "orders-archive",
        "orders_20230115",
        "orders_2023_01",
        "orders_old",
        "test_orders",
        "temp_x",
    ],
)
def test_noise_names_are_flagged(name):
    assert inventory_summary.noise_reason(name)


@pytest.mark.parametrize(
    "name",
    [
        "customers",
        "testimonials",
        "stage_name",
        "orders",
        "oldest_orders",
        "tempest",
        "order_items_v2",
        "h3_cells",
    ],
)
def test_ordinary_names_are_not_flagged(name):
    assert inventory_summary.noise_reason(name) is None


def test_summary_shape(tmp_path):
    inv = _inventory({"sales": ["orders", "customers", "stg_orders"], "ops": ["orders_bak"]})
    code, out = _run(tmp_path, inv)
    assert code == 0
    assert out["tables"] == 4
    assert {s["name"]: s["tables"] for s in out["schemas"]} == {"sales": 3, "ops": 1}
    assert {n["table"] for n in out["likely_noise"]} == {"sales.stg_orders", "ops.orders_bak"}
    assert all(n["why"] for n in out["likely_noise"])
    assert out["groups"]["noise_by_pattern"]


def test_keep_everything_and_drop_noise(tmp_path):
    inv = _inventory({"sales": ["orders", "customers", "stg_orders"], "ops": ["orders_bak"]})
    out_file = tmp_path / "keep.txt"
    code, out = _run(tmp_path, inv, "--keep", "drop_noise", "--tables-out", str(out_file))
    assert code == 0 and out == {"tables_file": str(out_file), "kept": 2}
    assert out_file.read_text().splitlines() == ["sales.orders", "sales.customers"]
    code, out = _run(tmp_path, inv, "--keep", "all", "--tables-out", str(out_file))
    assert out["kept"] == 4


def test_keep_schemas(tmp_path):
    inv = _inventory({"sales": ["orders"], "ops": ["jobs", "runs"], "hr": ["staff"]})
    out_file = tmp_path / "keep.txt"
    code, out = _run(tmp_path, inv, "--keep", "schemas:ops,hr", "--tables-out", str(out_file))
    assert code == 0 and out["kept"] == 3
    assert out_file.read_text().splitlines() == ["ops.jobs", "ops.runs", "hr.staff"]


def test_unknown_schema_is_reported_not_guessed(tmp_path):
    inv = _inventory({"sales": ["orders"]})
    code, out = _run(
        tmp_path, inv, "--keep", "schemas:sales,nope", "--tables-out", str(tmp_path / "k")
    )
    assert code == 1 and out["unmatched"] == ["nope"]
    assert not (tmp_path / "k").exists()


def test_keep_named_tables(tmp_path):
    inv = _inventory({"sales": ["orders", "customers"], "ops": ["orders"]})
    names = tmp_path / "names.txt"
    names.write_text("sales.orders\ncustomers\n", encoding="utf-8")
    out_file = tmp_path / "keep.txt"
    code, out = _run(tmp_path, inv, "--keep", f"names:{names}", "--tables-out", str(out_file))
    assert code == 0 and out["kept"] == 2
    assert out_file.read_text().splitlines() == ["sales.orders", "sales.customers"]


def test_named_tables_that_match_nothing_or_are_ambiguous_are_reported(tmp_path):
    inv = _inventory({"sales": ["orders", "customers"], "ops": ["orders"]})
    names = tmp_path / "names.txt"
    names.write_text("orders\nwidgets\ncustomers\n", encoding="utf-8")
    code, out = _run(tmp_path, inv, "--keep", f"names:{names}", "--tables-out", str(tmp_path / "k"))
    assert code == 1
    assert out["unmatched"] == ["widgets"] and out["ambiguous"] == {
        "orders": ["sales.orders", "ops.orders"]
    }
    assert not (tmp_path / "k").exists()


def test_large_inventory_is_summarised_to_15_rows(tmp_path):
    inv = _inventory({f"s{i:02d}": [f"t{j}" for j in range(15)] for i in range(40)})
    assert len(inv["tables"]) == 600
    code, out = _run(tmp_path, inv)
    assert out["tables"] == 600
    assert len(out["schemas"]) <= 15
    assert out["schemas_more"] == {"schemas": 25, "tables": 375}
    groups = out["groups"]["schema_groups"]
    assert len(groups) <= 4
    assert sorted(s for g in groups for s in g["schemas"]) == sorted(f"s{i:02d}" for i in range(40))


def test_few_schemas_have_no_overflow_or_groups(tmp_path):
    code, out = _run(tmp_path, _inventory({"a": ["x"], "b": ["y"]}))
    assert out["schemas_more"] is None and out["groups"]["schema_groups"] == []


def test_every_table_in_a_noise_named_schema_is_flagged():
    for schema in ("staging", "Scratch", "tmp"):
        assert "schema" in inventory_summary.noise_reason("orders", schema)
    assert inventory_summary.noise_reason("orders", "sales") is None


@pytest.mark.parametrize("name", ["orders_2023_q1", "orders_2023_v2", "orders_20230115_v3"])
def test_a_date_before_a_version_or_quarter_is_flagged(name):
    assert inventory_summary.noise_reason(name)


def test_a_version_alone_is_not_noise():
    assert inventory_summary.noise_reason("order_items_v2") is None


def test_summary_flags_the_tables_of_a_noise_schema(tmp_path):
    code, out = _run(tmp_path, _inventory({"staging": ["orders"], "sales": ["orders"]}))
    assert [n["table"] for n in out["likely_noise"]] == ["staging.orders"]
    assert "staging" in out["likely_noise"][0]["why"]


def test_keeping_nothing_is_an_error_and_writes_nothing(tmp_path):
    out_file = tmp_path / "k"
    inv = _inventory({"staging": ["a", "b"]})
    code, out = _run(tmp_path, inv, "--keep", "drop_noise", "--tables-out", str(out_file))
    assert code == 1 and out == {"error": "no tables kept"} and not out_file.exists()
    names = tmp_path / "names.txt"
    names.write_text("\n", encoding="utf-8")
    code, out = _run(tmp_path, inv, "--keep", f"names:{names}", "--tables-out", str(out_file))
    assert code == 1 and out == {"error": "no tables kept"} and not out_file.exists()
    code, out = _run(
        tmp_path, _inventory({"a": ["x"]}), "--keep", "schemas:", "--tables-out", str(out_file)
    )
    assert code == 1 and out == {"error": "no tables kept"} and not out_file.exists()


def test_names_match_case_insensitively_and_strip_quotes(tmp_path):
    inv = _inventory({"sales": ["Orders", "customers"]})
    names = tmp_path / "names.txt"
    names.write_text('"SALES.orders"\n`Customers`,\n', encoding="utf-8")
    out_file = tmp_path / "keep.txt"
    code, out = _run(tmp_path, inv, "--keep", f"names:{names}", "--tables-out", str(out_file))
    assert code == 0 and out["kept"] == 2
    assert out_file.read_text().splitlines() == ["sales.Orders", "sales.customers"]


def test_schema_groups_carry_schema_and_table_counts(tmp_path):
    inv = _inventory({f"s{i}": ["t", "u"] for i in range(6)})
    _, out = _run(tmp_path, inv)
    for g in out["groups"]["schema_groups"]:
        assert g["schema_count"] == len(g["schemas"]) and g["tables"] == 2 * g["schema_count"]
