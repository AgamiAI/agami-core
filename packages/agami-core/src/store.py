"""Backend-portable store — the thin DB layer the hosted server serves from.

A shared/multi-instance server can't keep state in local files, so the model + prompt examples are
served from a database and query logs/feedback are written to it. The backend is **Postgres in
production** (cloud-neutral, networked, multi-instance-safe), but the schema + queries are kept
**portable** so the same code runs on **SQLite** — which is what the test suite uses (no DB service
needed in CI) and is fine for a small single-instance self-host.

This module is the only place that knows a backend dialect. Everything above it (the model loader,
the activity sink, example serving) writes one set of SQL with `?` placeholders and uses dict rows,
and `Store` adapts to whichever backend `AGAMI_DB_URL` selects:

    sqlite://                      → in-memory (tests)
    sqlite:///abs/path/agami.db    → a file (self-host)
    postgresql://user:pw@host/db   → Postgres (production; needs the [server] extra)

Migrations are ordered `migrations/core/NNN_*.sql` applied idempotently via a `schema_migrations`
tracking table — re-running only applies new files.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The repo's migration home (OCR-028 created the skeleton); resolved relative to this file so it
# works from an installed package or a checkout.
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations" / "core"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """A DB-API connection + its dialect, with portable execute/query helpers.

    SQL is authored once with `?` placeholders and adapted per dialect; rows come back as plain
    dicts on every backend (built from cursor.description), so callers never branch on the backend.
    """

    def __init__(self, conn: Any, dialect: str) -> None:
        self.conn = conn
        self.dialect = dialect  # "sqlite" | "postgres"

    # --- construction -------------------------------------------------------

    @classmethod
    def connect(cls, url: str) -> Store:
        if url.startswith("sqlite://"):
            import sqlite3

            rest = url[len("sqlite://") :]
            path = ":memory:" if rest in ("", ":memory:") else rest
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA foreign_keys = ON")
            return cls(conn, "sqlite")
        if url.startswith(("postgresql://", "postgres://")):
            import psycopg2  # in the [server] extra; only needed for the Postgres backend

            return cls(psycopg2.connect(url), "postgres")
        raise ValueError(
            f"Unsupported AGAMI_DB_URL scheme: {url.split('://', 1)[0]!r} "
            "(expected sqlite:// or postgresql://)"
        )

    @classmethod
    def from_env(cls) -> Store | None:
        """Open the store named by AGAMI_DB_URL, or None when unset (the local file path is used)."""
        import os

        url = os.environ.get("AGAMI_DB_URL", "").strip()
        return cls.connect(url) if url else None

    # --- portable SQL -------------------------------------------------------

    def _adapt(self, sql: str) -> str:
        # Our SQL never contains a literal '?'; Postgres wants %s placeholders.
        return sql if self.dialect == "sqlite" else sql.replace("?", "%s")

    def execute(self, sql: str, params: tuple = ()) -> Any:
        cur = self.conn.cursor()
        cur.execute(self._adapt(sql), params)
        return cur

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = self.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- migrations ---------------------------------------------------------

    def run_migrations(self, migrations_dir: Path | None = None) -> list[str]:
        """Apply un-applied `NNN_*.sql` in order; return the filenames newly applied. Idempotent."""
        migrations_dir = migrations_dir or MIGRATIONS_DIR
        self.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT)"
        )
        self.commit()
        applied = {r["id"] for r in self.query("SELECT id FROM schema_migrations")}
        ran: list[str] = []
        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in applied:
                continue
            self._run_script(path.read_text())
            self.execute(
                "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
                (path.name, _now_iso()),
            )
            self.commit()
            ran.append(path.name)
        return ran

    def _run_script(self, sql: str) -> None:
        """Run a multi-statement SQL script. SQLite needs executescript; psycopg2 runs a multi-
        statement string in one execute."""
        if self.dialect == "sqlite":
            self.conn.executescript(sql)
        else:
            self.conn.cursor().execute(sql)
