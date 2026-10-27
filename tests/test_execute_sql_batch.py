"""`python -m execute_sql --batch`: a plan of statements in one process, the model resolved once,
the connection kept open, every item still through the guard on its own (ACE-137)."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402

PROFILE = "acme"


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    db = tmp_path / "warehouse.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE orders (id INTEGER, region TEXT)")
    conn.executemany("INSERT INTO orders VALUES (?, ?)", [(1, "EU"), (2, "US"), (3, "EU")])
    conn.commit()
    conn.close()
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", f"sqlite:///{db}")
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    return tmp_path


class _Connects:
    """Counts the connections sqlite opens, whatever thread opens them."""

    def __init__(self, monkeypatch):
        self.count = 0
        real = sqlite3.connect

        def counted(*args, **kwargs):
            self.count += 1
            return real(*args, **kwargs)
        monkeypatch.setattr(sqlite3, "connect", counted)


def _plan(tmp_path: Path, items: list[dict]) -> Path:
    plan = tmp_path / "probes.plan.json"
    plan.write_text(json.dumps(items))
    return plan


def _main(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["execute_sql", "--profile", PROFILE, *argv])
    return execute_sql.main()


def test_a_plan_runs_every_item_on_one_connection_and_writes_csvs_run_files_and_a_manifest(warehouse, monkeypatch, capsys):
    connects = _Connects(monkeypatch)
    out = warehouse / "rows" / "1"
    plan = _plan(warehouse, [
        {"id": "count", "sql": "SELECT COUNT(*) AS n FROM orders", "out": str(out / "lit-1.exists.csv")},
        {"id": "distinct", "sql": "SELECT DISTINCT region AS v FROM orders ORDER BY v", "out": str(out / "orders.region.distinct.csv")},
        {"id": "from-file", "sql_file": str(warehouse / "q.sql"), "out": str(out / "join-1.overlap.0.csv")},
    ])
    (warehouse / "q.sql").write_text("SELECT COUNT(*) AS matched FROM orders WHERE region = 'EU'")

    rc = _main(monkeypatch, "--batch", str(plan))

    assert rc == 0 and connects.count == 1, connects.count
    assert (out / "lit-1.exists.csv").read_bytes() == b"n\r\n3\r\n"
    assert (out / "orders.region.distinct.csv").read_bytes() == b"v\r\nEU\r\nUS\r\n"
    assert (out / "join-1.overlap.0.csv").read_bytes() == b"matched\r\n2\r\n"
    run = json.loads((out / "lit-1.exists.run.json").read_text())
    assert run == {"status": "ok", "exit": 0, "kind": None, "rule": None, "detail": None, "rows": 1}
    manifest = json.loads((warehouse / "probes.plan.json.manifest.json").read_text())
    assert manifest["total"] == 3 and manifest["ok"] == 3 and [i["id"] for i in manifest["items"]] == ["count", "distinct", "from-file"]
    assert json.loads(capsys.readouterr().out) == manifest
    # The single-statement door still connects per call.
    connects.count = 0
    for sql in ("SELECT 1", "SELECT 2"):
        assert execute_sql.execute_guarded(sql, PROFILE, None, executor=execute_sql.BUILTIN_EXECUTOR).status == "ok"
    assert connects.count == 2 and execute_sql._BATCH_CONNECTIONS is None


def test_a_refused_and_a_failed_item_leave_an_empty_csv_and_stop_nothing_else(warehouse, monkeypatch, capsys):
    out = warehouse / "rows" / "2"
    plan = _plan(warehouse, [
        {"id": "write", "sql": "DELETE FROM orders", "out": str(out / "a.csv")},
        {"id": "missing", "sql": "SELECT COUNT(*) FROM no_such_table", "out": str(out / "b.csv")},
        {"id": "fine", "sql": "SELECT COUNT(*) AS n FROM orders", "out": str(out / "c.csv")},
    ])
    rc = _main(monkeypatch, "--batch", str(plan), "--manifest", str(warehouse / "m.json"))
    assert rc == 1  # the first item that did not run was refused, and a refusal exits 1 at the single door
    assert (out / "a.csv").read_text() == "" and (out / "b.csv").read_text() == "" and (out / "c.csv").read_bytes() == b"n\r\n3\r\n"
    refused = json.loads((out / "a.run.json").read_text())
    assert refused["status"] == "refused" and refused["exit"] == 1 and refused["rule"] and "DELETE" not in json.dumps(refused)
    failed = json.loads((out / "b.run.json").read_text())
    assert failed["status"] == "failed" and failed["kind"] == "table_not_found" and failed["exit"] == 8
    assert "no_such_table" not in failed["detail"]  # the classifier's message, never the driver's text
    manifest = json.loads((warehouse / "m.json").read_text())
    assert manifest["ok"] == 1 and [i["status"] for i in manifest["items"]] == ["refused", "failed", "ok"]
    assert execute_sql._BATCH_CONNECTIONS is None


def test_a_plan_that_is_not_a_list_of_runnable_items_runs_nothing(warehouse, monkeypatch, capsys):
    bad = warehouse / "bad.json"
    bad.write_text(json.dumps({"sql": "SELECT 1"}))
    assert _main(monkeypatch, "--batch", str(bad)) == 2 and "must be a JSON list" in capsys.readouterr().err
    bad.write_text(json.dumps([{"sql": "SELECT 1"}]))
    assert _main(monkeypatch, "--batch", str(bad)) == 2 and "has no `out`" in capsys.readouterr().err
    bad.write_text(json.dumps([{"out": str(warehouse / "x.csv")}]))
    assert _main(monkeypatch, "--batch", str(bad)) == 2 and "neither `sql` nor `sql_file`" in capsys.readouterr().err
    # A malformed item is recorded like any item that did not run: an empty CSV and a run file (#523).
    assert (warehouse / "x.csv").read_text() == ""
    assert json.loads((warehouse / "x.run.json").read_text())["status"] == "failed"
    bad.write_text("not json")
    assert _main(monkeypatch, "--batch", str(bad)) == 2 and "cannot read the plan" in capsys.readouterr().err


def test_a_connection_a_statement_broke_is_dropped_and_the_next_item_reconnects(warehouse, monkeypatch):
    connects = _Connects(monkeypatch)
    out = warehouse / "rows" / "3"
    plan = _plan(warehouse, [
        {"id": "a", "sql": "SELECT COUNT(*) AS n FROM orders", "out": str(out / "a.csv")},
        {"id": "b", "sql": "SELECT COUNT(*) FROM no_such_table", "out": str(out / "b.csv")},
        {"id": "c", "sql": "SELECT COUNT(*) AS n FROM orders", "out": str(out / "c.csv")},
    ])
    assert _main(monkeypatch, "--batch", str(plan)) == 8
    assert connects.count == 2  # one for a and b; b broke it; one more for c
    assert (out / "c.csv").read_bytes() == b"n\r\n3\r\n"


# --- the model memo -------------------------------------------------------------------------------


@pytest.fixture
def local_model(tmp_path, monkeypatch):
    for key in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    root = tmp_path / PROFILE
    root.mkdir()
    (root / "datasource.yaml").write_text("datasource: Shop\n")
    execute_sql._MODEL_MEMO.clear()
    return root


def test_the_model_is_resolved_once_per_process_until_a_file_under_it_changes(local_model, monkeypatch):
    from semantic_model import loader as L

    loads: list[Path] = []

    def load(root):
        loads.append(root)
        return object()
    monkeypatch.setattr(L, "load_datasource", load)

    first = execute_sql._resolve_guard_model(PROFILE)
    second = execute_sql._resolve_guard_model(PROFILE)
    assert first is second and len(loads) == 1
    # A changed model is read again.
    yaml = local_model / "datasource.yaml"
    yaml.write_text("datasource: Shop\nversion: 2\n")
    os.utime(yaml, ns=(yaml.stat().st_atime_ns, yaml.stat().st_mtime_ns + 1_000_000))
    third = execute_sql._resolve_guard_model(PROFILE)
    assert third is not first and len(loads) == 2
    # A model that cannot be read is never cached: it is looked for again next time.
    monkeypatch.setattr(L, "load_datasource", lambda root: (_ for _ in ()).throw(ValueError("broken")))
    execute_sql._MODEL_MEMO.clear()
    assert execute_sql._resolve_guard_model(PROFILE) is None and execute_sql._resolve_guard_model(PROFILE) is None
    assert execute_sql._MODEL_MEMO == {}


def test_a_relative_out_resolves_from_the_current_directory_and_the_manifest_says_where(warehouse, monkeypatch, capsys):
    # relative to where the command runs, not the plan's folder — and the manifest shows the real path
    _Connects(monkeypatch)
    (warehouse / "plans").mkdir()
    plan = _plan(warehouse / "plans", [{"id": "count", "sql": "SELECT COUNT(*) AS n FROM orders",
                                        "out": "results/count.csv"}])
    monkeypatch.chdir(warehouse)
    assert _main(monkeypatch, "--batch", str(plan)) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["items"][0]["out"] == str((warehouse / "results" / "count.csv").resolve())
    assert (warehouse / "results" / "count.csv").exists()


def test_a_malformed_item_is_recorded_and_the_rest_still_run(warehouse, monkeypatch, capsys):
    out = warehouse / "rows" / "3"
    plan = _plan(warehouse, [
        {"id": "first", "sql": "SELECT COUNT(*) AS n FROM orders", "out": str(out / "a.csv")},
        {"id": "no-sql", "out": str(out / "b.csv")},
        "not an object",
        {"id": "no-out", "sql": "SELECT 1"},
        {"id": "last", "sql": "SELECT COUNT(*) AS n FROM orders", "out": str(out / "e.csv")},
    ])
    rc = _main(monkeypatch, "--batch", str(plan))
    assert rc == 2  # the first item that did not run was malformed, a usage error at the single door
    assert (out / "a.csv").read_bytes() == (out / "e.csv").read_bytes() == b"n\r\n3\r\n"
    assert (out / "b.csv").read_text() == ""
    manifest = json.loads((warehouse / "probes.plan.json.manifest.json").read_text())
    assert [(i["id"], i["status"], i["exit"]) for i in manifest["items"]] == [
        ("first", "ok", 0), ("no-sql", "failed", 2), ("3", "failed", 2), ("no-out", "failed", 2), ("last", "ok", 0)]
    assert manifest["total"] == 5 and manifest["ok"] == 2 and manifest["items"][3]["out"] is None
    assert "item 2 has neither `sql` nor `sql_file`" in capsys.readouterr().err


def test_an_exception_mid_plan_still_writes_the_manifest(warehouse, monkeypatch, capsys):
    out = warehouse / "rows" / "4"
    plan = _plan(warehouse, [
        {"id": "a", "sql": "SELECT COUNT(*) AS n FROM orders", "out": str(out / "a.csv")},
        {"id": "b", "sql": "SELECT 2", "out": str(out / "b.csv")},
        {"id": "c", "sql": "SELECT COUNT(*) AS n FROM orders", "out": str(out / "c.csv")},
    ])
    real = execute_sql.execute_guarded

    def breaks_on_b(sql, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if sql == "SELECT 2":
            raise RuntimeError("an adapter bug at /secret/path")
        return real(sql, *args, **kwargs)

    monkeypatch.setattr(execute_sql, "execute_guarded", breaks_on_b)
    assert _main(monkeypatch, "--batch", str(plan)) == 6
    manifest = json.loads((warehouse / "probes.plan.json.manifest.json").read_text())
    assert [i["status"] for i in manifest["items"]] == ["ok", "failed", "ok"]
    assert manifest["items"][1]["kind"] == "other" and "/secret/path" not in json.dumps(manifest)

    def interrupted(sql, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if sql == "SELECT 2":
            raise KeyboardInterrupt
        return real(sql, *args, **kwargs)

    monkeypatch.setattr(execute_sql, "execute_guarded", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _main(monkeypatch, "--batch", str(plan), "--manifest", str(warehouse / "partial.json"))
    partial = json.loads((warehouse / "partial.json").read_text())
    assert [i["id"] for i in partial["items"]] == ["a"] and execute_sql._BATCH_CONNECTIONS is None


def test_an_unreadable_plan_still_writes_a_manifest_saying_why(warehouse, monkeypatch, capsys):
    plan = warehouse / "broken.json"
    plan.write_text("not json")
    assert _main(monkeypatch, "--batch", str(plan)) == 2
    manifest = json.loads((warehouse / "broken.json.manifest.json").read_text())
    assert manifest["total"] == 0 and manifest["items"] == [] and "cannot read the plan" in manifest["error"]


def test_a_failed_item_records_the_database_s_first_line_locally(warehouse, monkeypatch, capsys):
    out = warehouse / "rows" / "5"
    plan = _plan(warehouse, [{"id": "nested", "sql": "SELECT SUM(SUM(id)) FROM orders", "out": str(out / "a.csv")}])
    assert _main(monkeypatch, "--batch", str(plan)) == 6
    run = json.loads((out / "a.run.json").read_text())
    assert run["detail"] == execute_sql.UNEXPECTED_FAILURE_MESSAGE
    assert run["database_error"] == "SQLite execution error: misuse of aggregate function SUM()"
    monkeypatch.setenv(execute_sql.SERVED_CHILD_ENV, "1")
    assert _main(monkeypatch, "--batch", str(plan)) == 6
    assert "database_error" not in json.loads((out / "a.run.json").read_text())


def test_a_plan_that_is_json_null_is_refused(warehouse, monkeypatch, capsys):
    plan = warehouse / "null.json"
    plan.write_text("null")
    assert _main(monkeypatch, "--batch", str(plan)) == 2 and "must be a JSON list" in capsys.readouterr().err
    assert "must be a JSON list" in json.loads((warehouse / "null.json.manifest.json").read_text())["error"]


def test_a_failed_item_empties_its_csv_and_the_next_item_is_clean(warehouse, monkeypatch, capsys):
    out = warehouse / "rows" / "6"
    out.mkdir(parents=True)
    (out / "b.csv").write_text("n\nrows from an earlier run\n")
    (warehouse / "bad.sql").write_bytes(b"SELECT \xff")
    plan = _plan(warehouse, [
        {"id": "a", "sql": "SELECT COUNT(*) AS n FROM orders", "out": str(out / "a.csv")},
        {"id": "b", "sql_file": str(warehouse / "bad.sql"), "out": str(out / "b.csv")},
        {"id": "c", "sql": "SELECT COUNT(*) AS n FROM orders", "out": str(out / "c.csv")},
    ])
    assert _main(monkeypatch, "--batch", str(plan)) == 6
    assert (out / "b.csv").read_text() == "" and (out / "c.csv").read_bytes() == b"n\r\n3\r\n"
    assert json.loads((out / "b.run.json").read_text())["status"] == "failed"
    manifest = json.loads((warehouse / "probes.plan.json.manifest.json").read_text())
    assert [i["status"] for i in manifest["items"]] == ["ok", "failed", "ok"]


def test_an_item_whose_output_cannot_be_written_stays_in_the_manifest_and_the_rest_run(warehouse, monkeypatch, capsys):
    blocker = warehouse / "not-a-dir"
    blocker.write_text("")
    out = warehouse / "rows" / "7"
    plan = _plan(warehouse, [
        {"id": "a", "sql": "SELECT COUNT(*) AS n FROM orders", "out": str(blocker / "a.csv")},
        {"id": "no-sql", "out": str(blocker / "b.csv")},
        {"id": "c", "sql": "SELECT COUNT(*) AS n FROM orders", "out": str(out / "c.csv")},
    ])
    assert _main(monkeypatch, "--batch", str(plan)) == 6
    manifest = json.loads((warehouse / "probes.plan.json.manifest.json").read_text())
    assert [(i["id"], i["status"]) for i in manifest["items"]] == [("a", "failed"), ("no-sql", "failed"), ("c", "ok")]
    first = manifest["items"][0]
    assert first["detail"].startswith("the output could not be written") and "artifact_error" in first
    assert str(warehouse) not in first["detail"] + first["artifact_error"]
    assert (out / "c.csv").read_bytes() == b"n\r\n3\r\n"


def test_a_run_file_that_cannot_be_replaced_keeps_its_last_whole_copy(warehouse, monkeypatch, capsys):
    out = warehouse / "rows" / "8"
    plan = _plan(warehouse, [{"id": "a", "sql": "SELECT COUNT(*) AS n FROM orders", "out": str(out / "a.csv")}])
    assert _main(monkeypatch, "--batch", str(plan)) == 0
    before = (out / "a.run.json").read_text()
    real_replace = os.replace

    def replace(src, dst):  # noqa: ANN001, ANN202
        if str(dst).endswith(".run.json"):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(execute_sql.os, "replace", replace)
    plan = _plan(warehouse, [{"id": "a", "sql": "DELETE FROM orders", "out": str(out / "a.csv")}])
    assert _main(monkeypatch, "--batch", str(plan)) == 1
    assert (out / "a.run.json").read_text() == before
    assert not [p for p in out.iterdir() if p.name.endswith(".tmp")]
    item = json.loads((warehouse / "probes.plan.json.manifest.json").read_text())["items"][0]
    assert item["status"] == "refused" and item["artifact_error"] == "the output could not be written: No space left on device"
