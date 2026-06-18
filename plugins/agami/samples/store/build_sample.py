#!/usr/bin/env python3
"""Build the agami sample SQLite database from seed.sql — stdlib only.

The sample dataset is shipped as text (seed.sql); the materialized .db is built
locally so no binary blob lives in git. This builder is the deterministic, zero-
dependency way to do that on a bare Windows/Mac:

  * Preferred path: pipe seed.sql through the `sqlite3` CLI if it's on PATH.
  * Fallback path:  Python's stdlib `sqlite3` via executescript() — always
    available, since `sqlite3` ships with CPython.

Both produce the same database (seed.sql is 100% deterministic — no random()),
so the result is byte-reproducible and idempotent (rebuild overwrites).

Usage:
  python3 build_sample.py --out /path/to/store.db
  python3 build_sample.py                      # defaults to ./store.db next to this file
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = HERE / "seed.sql"


def build(out_path: Path, prefer_cli: bool = True) -> str:
    """Build the sample DB at out_path from seed.sql. Returns the method used."""
    if not SEED.exists():
        raise FileNotFoundError(f"seed.sql not found next to builder: {SEED}")

    out_path = out_path.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Idempotent: start clean so a rebuild can't accrete onto a stale file.
    if out_path.exists():
        out_path.unlink()

    sql = SEED.read_text(encoding="utf-8")

    cli = shutil.which("sqlite3") if prefer_cli else None
    if cli:
        # Feed the script on stdin so we never put anything on the command line.
        proc = subprocess.run(
            [cli, str(out_path)],
            input=sql,
            text=True,
            capture_output=True,
        )
        if proc.returncode == 0:
            return "sqlite3-cli"
        # CLI present but failed (e.g. ancient build) — fall through to stdlib.
        sys.stderr.write(
            f"agami: sqlite3 CLI build failed ({proc.stderr.strip()}); "
            "falling back to the stdlib builder.\n"
        )
        if out_path.exists():
            out_path.unlink()

    # Stdlib fallback — always works (sqlite3 is in the Python stdlib).
    conn = sqlite3.connect(str(out_path))
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()
    return "stdlib"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the agami sample SQLite database.")
    ap.add_argument(
        "--out",
        default=str(HERE / "store.db"),
        help="output .db path (default: ./store.db next to this script)",
    )
    ap.add_argument(
        "--no-cli",
        action="store_true",
        help="skip the sqlite3 CLI and force the stdlib builder (for testing the fallback)",
    )
    args = ap.parse_args(argv)

    out = Path(args.out)
    method = build(out, prefer_cli=not args.no_cli)
    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"built {out} ({size_mb:.1f} MB) via {method}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
