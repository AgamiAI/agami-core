#!/usr/bin/env python3
"""TPC-DS benchmark runner (Tier 3b, opt-in).

Reads benchmarks/tpcds/questions.json, asks Claude to generate SQL for
each NL prompt (with the OSI model as context), executes against the
configured DB, and asserts on shape (row count band, ordering, aggregate
tolerance) — never on exact rows or SQL bytes.

The with-vs-without-semantic-model delta from --baseline is the primary
signal of interest, not the absolute pass rate.

Gated by ``LITEBI_BENCHMARK=tpcds``. Skips with a clear message otherwise.

See ``benchmarks/tpcds/README.md`` for setup.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BENCHMARK_FLAG = "LITEBI_BENCHMARK"
BENCHMARK_VALUE = "tpcds"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
QUESTIONS_FILE = Path(__file__).resolve().parent / "questions.json"


def _enabled() -> bool:
    return os.environ.get(BENCHMARK_FLAG) == BENCHMARK_VALUE


def _load_questions() -> list[dict]:
    return json.loads(QUESTIONS_FILE.read_text())["questions"]


def _check_shape(rows: list[tuple], expected: dict) -> tuple[bool, str]:
    n = len(rows)
    lo, hi = expected.get("row_count_min", 0), expected.get("row_count_max", 10**9)
    if not (lo <= n <= hi):
        return False, f"row count {n} outside [{lo}, {hi}]"
    top_n = expected.get("top_n")
    if top_n is not None and n != top_n:
        return False, f"expected top-{top_n}, got {n} rows"
    return True, f"shape OK (n={n})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", action="store_true",
                    help="also run each question WITHOUT the semantic model loaded")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompts that would be sent; do not call the API")
    args = ap.parse_args()

    if not _enabled():
        sys.exit(
            f"{BENCHMARK_FLAG}={BENCHMARK_VALUE} not set; benchmark is opt-in.\n"
            "See benchmarks/tpcds/README.md for setup."
        )

    questions = _load_questions()
    print(f"loaded {len(questions)} questions from {QUESTIONS_FILE.name}")

    if args.dry_run:
        for q in questions:
            print(f"  [{q['id']}] {q['tpcds_ref']}: {q['prompt']}")
        return 0

    # Full implementation requires:
    #   - a configured agami profile (AGAMI_PROFILE, AGAMI_DB_URL)
    #   - the OSI model produced by agami-connect against that profile
    #   - psycopg2 / pymysql / duckdb driver matching the loaded DB
    #   - the anthropic SDK with ANTHROPIC_API_KEY
    # Wiring those up is contributor-specific (each setup has different
    # creds + DB choice); this scaffold focuses on the harness contract
    # and the shape-assertion helpers above.
    sys.exit(
        "Runner not yet wired up. The harness contract is documented in this\n"
        "file and benchmarks/tpcds/README.md. Contributors completing this\n"
        "benchmark wire it to their local TPC-DS instance + agami profile,\n"
        "reuse tools/run_skill_evals.py for the Claude call pattern, and\n"
        "use _check_shape() above for assertions. Use --dry-run to inspect\n"
        "the question set."
    )


if __name__ == "__main__":
    sys.exit(main())
