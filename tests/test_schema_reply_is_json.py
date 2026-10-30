"""A schema reply is one JSON document (#477).

The domain context (datasource.md narrative, derived summary, USER_MEMORY.md) used to follow the
JSON object as text. A client that offloaded a large reply to a file and parsed it as JSON failed
with "Extra data". It now rides inside, as `domain_context`. Synthetic, generic names only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "agami-core" / "src"))

import run_golden_eval as E  # noqa: E402
import tools  # noqa: E402
from semantic_model import introspect as I  # noqa: E402

from catalog_helpers import col, make_catalog_runner  # noqa: E402

_runner = make_catalog_runner(
    tables=["orders", "customers"],
    columns={
        "orders": [col("id", "integer", nullable=False), col("ordered_on", "date")],
        "customers": [col("id", "integer", nullable=False), col("region", "varchar")],
    },
)


@pytest.mark.parametrize(
    "args", [{}, {"mode": "summary"}, {"dataset_names": ["orders"]}, {"query": "orders by region"}]
)
def test_every_schema_reply_parses_as_one_document(tmp_path, monkeypatch, args):
    I.introspect("shop", "postgres", runner=_runner, artifacts_dir=tmp_path)
    (tmp_path / "shop" / "datasource.md").write_text("# About\n\nOrders and customers.\n")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    reply = json.loads(tools.tool_get_datasource_schema({"datasource": "shop", **args}))
    assert "Orders and customers." in reply["domain_context"]


def test_the_eval_reads_the_context_from_either_shape():
    document, prose = E._split_payload(
        json.dumps({"datasource": "shop", "domain_context": "rules"})
    )
    assert document == {"datasource": "shop"} and prose == "rules"
    document, prose = E._split_payload('{"datasource": "shop"}\n## Domain context\nrules')
    assert document == {"datasource": "shop"} and prose == "\n## Domain context\nrules"
