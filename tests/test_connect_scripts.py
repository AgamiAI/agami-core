"""Phase B — the agami-connect determinism scripts: curate-gate.

`sm curate-gate` returns the Phase-4 decision in one call instead of the LLM running two
commands and branching. (The prune-block parser went with the prune page; tables are picked on a
card, and `inventory_summary.py` writes the tables file.)
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

def test_curate_gate_counts_pii(tmp_path, capsys):
    from semantic_model import cli
    from semantic_model import introspect as I

    from catalog_helpers import col, make_catalog_runner
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
