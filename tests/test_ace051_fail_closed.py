"""ACE-051 — the hosted safety guard resolves the model from the DB and FAILS CLOSED when no model
can be found: a served query never runs with the fan/chasm/scope/PII guards silently off. Locally
(no DB configured) a not-yet-built model is still a no-op.
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
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
from semantic_model import models as m  # noqa: E402


def _org() -> m.Organization:
    """A model declaring exactly two tables: orders, customers."""
    def _t(name):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name, columns=[m.Column(name="id", type="integer")])
    return m.Organization(
        organization="Shop",
        subject_areas=[m.SubjectArea(name="sales", tables_defined=[_t("orders"), _t("customers")])],
    )


def _seed_db(url: str, ds: str = "acme") -> None:
    import model_store
    from store import Store

    s = Store.connect(url)
    s.run_migrations()
    model_store.write_organization(s, ds, _org())
    s.close()


def _write_disk(root: Path) -> None:
    import yaml

    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "org.yaml").write_text(
        yaml.safe_dump({"organization": "Shop", "version": 1, "subject_areas": ["subject_areas/sales"]})
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": "sales", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"},
            {"storage_connection": "c", "schema": "public", "table": "customers"}]})
    )
    for t in ("orders", "customers"):
        (root / "subject_areas" / "sales" / "tables" / f"{t}.yaml").write_text(
            yaml.safe_dump({"name": t, "schema": "public", "storage_connection": "c", "grain": ["id"],
                            "description": t,
                            "columns": [{"name": "id", "type": "integer", "primary_key": True}]})
        )


def test_hosted_fail_closed_refuses_when_no_model(tmp_path, monkeypatch, capsys):
    # Hosted (DB configured) but NO model resolvable (DB migrated-but-empty + no disk) → refuse,
    # never run the query with the guards silently off.
    from store import Store

    url = "sqlite://" + str(tmp_path / "empty.db")
    Store.connect(url).run_migrations()
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_artifacts"))

    _, code = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert code == 1  # refused, not run
    assert json.loads(capsys.readouterr().err.strip())["error"]["kind"] == "model_unavailable"


def test_local_missing_model_is_noop(tmp_path, monkeypatch):
    # No DB configured → local path: a not-yet-built model legitimately means "no model" → no-op.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "empty"))

    sql, code = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert code is None and sql == "SELECT id FROM orders"  # unchanged, guards inert


def test_db_sourced_model_enforces_guards(tmp_path, monkeypatch, capsys):
    # Model in the DB, NOTHING on disk → the guards run off the DB-sourced model.
    url = "sqlite://" + str(tmp_path / "model.db")
    _seed_db(url, "acme")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))

    _, code = execute_sql._model_safety("SELECT id FROM sqlite_master", "acme", None)
    assert code == 1  # undeclared table refused by the table-scope guard, sourced from the DB model
    assert json.loads(capsys.readouterr().err.strip())["error"]["kind"] == "table_out_of_scope"

    sql, code = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert code is None  # a declared table with a named projection passes


def test_disk_db_verdict_parity(tmp_path, monkeypatch, capsys):
    # The same model sourced from disk vs the DB must yield identical guard verdicts.
    _write_disk(tmp_path / "art" / "acme")
    url = "sqlite://" + str(tmp_path / "model.db")
    _seed_db(url, "acme")

    def verdict(hosted: bool, sql: str):
        if hosted:
            monkeypatch.setenv("AGAMI_DB_URL", url)
            monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))  # DB is the only source
        else:
            monkeypatch.delenv("AGAMI_DB_URL", raising=False)
            monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "art"))  # disk is the only source
        _, code = execute_sql._model_safety(sql, "acme", None)
        capsys.readouterr()
        return code

    for sql in ("SELECT id FROM sqlite_master", "SELECT id FROM orders"):
        assert verdict(hosted=True, sql=sql) == verdict(hosted=False, sql=sql)
