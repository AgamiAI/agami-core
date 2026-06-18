"""Phase B — the agami-connect determinism scripts: prune-block parser + curate-gate.

parse_prune_block.py turns the pasted prune block into a shell-safe tables file
(killing the zsh word-split that built one garbage table). `sm curate-gate`
returns the Phase-4 open-the-explorer decision in one call instead of the LLM
running two commands and branching.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import parse_prune_block as P  # noqa: E402

BLOCK = """AGAMI PRUNE  (paste back to Claude)
keep tables: 3 of 5
sales.orders
sales.customers
sales.order_items
exclude columns:
sales.customers.ssn
sales.orders.internal_notes
done
"""


def test_prune_parse_tables_and_columns():
    tables, excluded, meta, anomalies = P.parse(BLOCK)
    assert tables == ["sales.orders", "sales.customers", "sales.order_items"]
    assert excluded == ["sales.customers.ssn", "sales.orders.internal_notes"]
    assert meta["declared_keep"] == 3 and meta["declared_total"] == 5
    assert anomalies == []


def test_prune_writes_shell_safe_file_one_per_line(tmp_path):
    out = tmp_path / "keep.txt"
    rc = P.main(["--block-file", _write(tmp_path / "b.txt", BLOCK), "--tables-out", str(out)])
    assert rc == 0
    lines = out.read_text().splitlines()
    assert lines == ["sales.orders", "sales.customers", "sales.order_items"]  # newline-separated, no blob


def test_prune_malformed_line_is_anomaly_not_dropped():
    tables, _, _, anomalies = P.parse("keep tables: 2 of 2\nsales.orders\nnope not a table\n")
    assert tables == ["sales.orders"]
    assert any(a["kind"] == "bad_table_line" for a in anomalies)


def test_prune_kept_everything_flag(tmp_path):
    out = tmp_path / "k.txt"
    block = "keep tables: 2 of 2\na.x\na.y\ndone\n"
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        P.main(["--block-file", _write(tmp_path / "b.txt", block), "--tables-out", str(out)])
    assert json.loads(buf.getvalue())["data"]["kept_everything"] is True


def test_curate_gate_counts_pii(tmp_path, capsys):
    from catalog_helpers import col, make_catalog_runner
    from semantic_model import cli
    from semantic_model import introspect as I
    runner = make_catalog_runner(
        tables=["customers"],
        columns={"customers": [col("id", "integer", nullable=False), col("email", "varchar")]}, fks=[])
    I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path)
    cli.cmd_curate_gate(types.SimpleNamespace(root=str(tmp_path / "shop")))
    gate = json.loads(capsys.readouterr().out)
    assert gate["pii_count"] >= 1  # email flagged
    assert gate["should_open_explorer"] is True


def _write(p: Path, text: str) -> str:
    p.write_text(text, encoding="utf-8")
    return str(p)
