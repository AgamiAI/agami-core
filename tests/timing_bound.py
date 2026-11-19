"""A wall-clock bound that scales with the machine it runs on (shared by the ACE-177/178 bounds).

An absolute "under one second" measures the runner: the same assembly takes 0.3-0.6 s on a laptop
and 1.0-1.7 s on a shared CI runner. What those bounds exist to catch is an ALGORITHMIC blow-up on a
long statement, so the assembly is timed against a reference workload on the same machine in the
same process: parsing the same statement and walking its whole tree, the work the receipt starts
with. A slow runner slows both; a quadratic assembly slows only one.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import sqlglot

# Far above any honest run, so a pathological hang still fails whatever the ratio says.
BACKSTOP_SECONDS = 10.0


def _best_of(work: Callable[[], object], repeats: int) -> float:
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        work()
        best = min(best, time.perf_counter() - started)
    return best


def reference_seconds(sql: str, dialect: str, repeats: int) -> float:
    """Best-of-`repeats` time to parse `sql` and visit every node of the tree."""
    def parse_and_walk() -> int:
        return sum(1 for _ in sqlglot.parse_one(sql, read=dialect).walk())

    return _best_of(parse_and_walk, repeats)


def assert_assembly_within(assemble: Callable[[], object], sql: str, dialect: str,
                           repeats: int, ratio: float) -> tuple[float, float]:
    """Assert best-of-`repeats` assembly < `ratio` x the reference, and under the backstop."""
    reference = reference_seconds(sql, dialect, repeats)
    assembly = _best_of(assemble, repeats)
    assert assembly < BACKSTOP_SECONDS, (assembly, reference)
    assert assembly < ratio * reference, (
        f"assembly {assembly:.3f}s vs reference {reference:.3f}s "
        f"= {assembly / reference:.1f}x, bound {ratio}x")
    return assembly, reference
