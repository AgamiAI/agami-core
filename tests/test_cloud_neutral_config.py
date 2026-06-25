"""Cloud-neutral config hardening — lock in the no-GCP-lock-in invariants.

agami-core is self-hostable on any cloud (VM + Postgres, or a stateless platform + managed
Postgres). These tests pin the config contract so it can't silently regress:

  1. the DB env var (canonical `AGAMI_DB_URL`, alias `APP_DATABASE_URL`) opens the same store;
  2. the server boots with NO required GCP platform dependency (opt-in datasource drivers excepted);
  3. the single-tenant org resolver is wired into the HTTP server;
  4. a DB-backed instance serves with no local-disk artifacts (stateless-platform-safe).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))


# --- 1. DB env var: canonical + alias resolve to the same store --------------


def test_app_database_url_is_accepted_as_an_alias(tmp_path, monkeypatch):
    from store import Store

    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.setenv("APP_DATABASE_URL", "sqlite://" + str(tmp_path / "a.db"))
    s = Store.from_env()
    assert s is not None and s.dialect == "sqlite"
    s.close()


def test_canonical_agami_db_url_wins_over_alias(tmp_path, monkeypatch):
    # Both set → the canonical name is the unambiguous override.
    from store import Store

    monkeypatch.setenv("AGAMI_DB_URL", "sqlite://" + str(tmp_path / "canonical.db"))
    monkeypatch.setenv("APP_DATABASE_URL", "postgresql://should-not-be-used/db")
    s = Store.from_env()
    assert s is not None and s.dialect == "sqlite"  # picked canonical, not the postgres alias
    s.close()


def test_no_db_env_means_local_file_path(monkeypatch):
    from store import Store

    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    assert Store.from_env() is None  # unset ⇒ the local file path is used (unchanged)
