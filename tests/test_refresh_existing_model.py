"""Refreshing a model that already exists: #428, #437 and #436.

Run end to end where the defect lived: the real `sm introspect` CLI, the real `execute_sql.py` child,
and a real SQLite database, because #428 was invisible to every canned-runner test (they never pass
through the semantic-model pass that refused the reads) and #437 only showed on a model a person had
already curated. #436 is checked against real git repositories, the profile's own and an enclosing one.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
from semantic_model import curate  # noqa: E402
from semantic_model import introspect as I  # noqa: E402


@pytest.fixture
def shop(tmp_path, monkeypatch):
    """A SQLite shop with credentials in an artifacts folder, as agami-connect leaves it."""
    db = tmp_path / "shop.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, legacy_code TEXT);"
        "CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), total REAL);"
        "INSERT INTO customers VALUES (1, 'a', 'x'), (2, 'b', 'y');"
        "INSERT INTO orders VALUES (1, 1, 9.5), (2, 2, 3.0);"
    )
    con.commit()
    con.close()
    art = tmp_path / "art"
    (art / "local").mkdir(parents=True)
    creds = art / "local" / "credentials"
    creds.write_text(f"[shop]\ntype = sqlite\npath = {db}\n", encoding="utf-8")
    creds.chmod(0o600)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    return {"db": db, "art": art, "root": art / "shop"}


def _introspect(shop, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "semantic_model.cli", "introspect", "--profile", "shop",
         "--db-type", "sqlite", "--artifacts", str(shop["art"]), *args],
        capture_output=True, text=True, encoding="utf-8", cwd=str(PKG_SRC),
        env={**os.environ, "PYTHONPATH": str(PKG_SRC)},
    )


def _table_files(root: Path) -> dict[str, Path]:
    return {p.stem: p for p in root.glob("subject_areas/*/tables/*.yaml")}


def _sql(shop, statement: str) -> None:
    con = sqlite3.connect(shop["db"])
    con.execute(statement)
    con.commit()
    con.close()


# --- #428: the reads that build the model are not confined to the model -----------------------


def test_a_second_batch_is_read_once_a_model_exists(shop):
    first = _introspect(shop, "--tables", "customers")
    assert first.returncode == 0, first.stdout + first.stderr
    second = _introspect(shop, "--tables", "orders", "--append")
    # Before #428 every table in this batch was refused by the model pass and dropped:
    # "no allowlisted table could be described".
    assert second.returncode == 0, second.stdout + second.stderr
    assert set(_table_files(shop["root"])) == {"customers", "orders"}


def test_only_the_model_building_reads_skip_the_model_pass(monkeypatch):
    seen: list[list[str]] = []

    class _Done:
        returncode, stdout, stderr = 0, "a\n1\n", ""

    monkeypatch.setattr(I.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or _Done())
    I.make_execute_sql_runner("p", building_the_model=True)("SELECT 1")
    I.make_execute_sql_runner("p")("SELECT 1")
    assert "--no-safety" in seen[0]
    assert "--no-safety" not in seen[1]  # seed examples and enrichment stay confined


def test_the_model_pass_finds_a_folder_chosen_through_the_pointer(tmp_path, monkeypatch):
    import agami_paths

    chosen = tmp_path / "chosen"
    (chosen / "shop").mkdir(parents=True)
    (chosen / "shop" / "datasource.yaml").write_text("datasource: shop\n", encoding="utf-8")
    pointer = tmp_path / "pointer"
    pointer.write_text(str(chosen) + "\n", encoding="utf-8")
    monkeypatch.delenv("AGAMI_ARTIFACTS_DIR", raising=False)
    monkeypatch.setattr(agami_paths, "POINTER_PATH", pointer)
    monkeypatch.setattr(agami_paths, "DEFAULT_ARTIFACTS_DIR", tmp_path / "default")
    # Before #428 this looked only at the env var and ~/agami-artifacts, found no model, and left the
    # table-scope check off while the model in the chosen folder was in use.
    assert execute_sql._disk_model_root("shop") == (chosen / "shop").resolve()


# --- #437: a refresh changes structure, and keeps the curation -----------------------------------


def _curate(shop) -> None:
    """What a person's curation leaves: a human description, a flag, a sign-off and an area description."""
    path = _table_files(shop["root"])["customers"]
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for c in doc["columns"]:
        if c["name"] == "name":
            c.update(description="The customer's trading name.", description_source="human",
                     sensitive=True, signed_off_by="you@example.com", signed_off_at="2026-01-01")
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    area = path.parent.parent / "subject_area.yaml"
    a = yaml.safe_load(area.read_text(encoding="utf-8"))
    a["description"] = "Customers and what they bought."
    area.write_text(yaml.safe_dump(a, sort_keys=False), encoding="utf-8")


def _columns(shop, table: str) -> dict[str, dict]:
    doc = yaml.safe_load(_table_files(shop["root"])[table].read_text(encoding="utf-8"))
    return {c["name"]: c for c in doc["columns"]}


def test_a_refresh_adds_new_columns_and_keeps_every_curated_field(shop):
    assert _introspect(shop, "--tables", "customers", "orders").returncode == 0
    _curate(shop)
    _sql(shop, "ALTER TABLE customers ADD COLUMN region TEXT")
    _sql(shop, "ALTER TABLE customers DROP COLUMN legacy_code")

    done = _introspect(shop, "--tables", "customers", "--append")
    assert done.returncode == 0, done.stdout + done.stderr
    cols = _columns(shop, "customers")
    assert cols["name"]["description"] == "The customer's trading name."
    assert cols["name"]["description_source"] == "human"
    assert cols["name"]["sensitive"] is True and cols["name"]["signed_off_by"] == "you@example.com"
    assert "region" in cols
    assert cols["legacy_code"]["review_state"] == "stale"  # kept for the person to decide, not deleted
    area = next(shop["root"].glob("subject_areas/*/subject_area.yaml"))
    assert yaml.safe_load(area.read_text(encoding="utf-8"))["description"] == "Customers and what they bought."
    assert "changed customers: added region; dropped legacy_code" in done.stdout


def test_a_refresh_preview_reports_the_changes_and_writes_nothing(shop):
    assert _introspect(shop, "--tables", "customers").returncode == 0
    before = {p: p.read_bytes() for p in shop["root"].rglob("*.yaml")}
    _sql(shop, "ALTER TABLE customers ADD COLUMN region TEXT")

    preview = _introspect(shop, "--tables", "customers", "--append", "--dry-run")
    assert "changed customers: added region" in preview.stdout, preview.stdout + preview.stderr
    assert {p: p.read_bytes() for p in shop["root"].rglob("*.yaml")} == before


def test_a_new_table_joins_the_existing_area_instead_of_re_proposing_them(shop):
    assert _introspect(shop, "--tables", "customers").returncode == 0
    _curate(shop)
    areas_before = sorted(p.parent.name for p in shop["root"].glob("subject_areas/*/subject_area.yaml"))

    assert _introspect(shop, "--tables", "orders", "--append").returncode == 0
    areas_after = sorted(p.parent.name for p in shop["root"].glob("subject_areas/*/subject_area.yaml"))
    assert areas_after == areas_before
    assert set(_table_files(shop["root"])) == {"customers", "orders"}
    assert _columns(shop, "customers")["name"]["description"] == "The customer's trading name."


def test_merge_table_puts_a_new_column_of_a_deep_table_in_a_group():
    from semantic_model import models as m

    old = m.Table(name="t", columns=[m.Column(name="a", type="string")], column_groups={"core": ["a"]})
    fresh = m.Table(name="t", columns=[m.Column(name="a", type="integer"), m.Column(name="b", type="string")],
                    column_groups={"misc": ["a", "b"]})
    merged, change = I._merge_table(old, fresh)
    assert [c.name for c in merged.columns] == ["a", "b"]
    assert merged.columns[0].type == "integer"
    assert merged.column_groups == {"core": ["a"], "misc": ["b"]}
    assert change == {"added": ["b"], "retyped": ["a: string -> integer"]}


# --- #436: curation commits only in a repository of its own, and says why otherwise ---------------


# A developer's global git config (signing, hooks) must not decide these tests.
_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "you@example.com", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "you@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, **_GIT_ENV}, check=False)


@pytest.fixture
def git_identity(monkeypatch):
    for k, v in _GIT_ENV.items():
        monkeypatch.setenv(k, v)


def test_a_profile_that_is_its_own_repository_is_committed(tmp_path, git_identity):
    root = tmp_path / "shop"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "datasource.yaml").write_text("datasource: shop\n", encoding="utf-8")
    assert curate._git_commit(root, "curation: 1 change(s)") == (True, "")
    assert _git(root, "log", "--oneline").stdout.count("\n") == 1


def test_a_profile_inside_a_larger_repository_is_never_committed_and_says_so(tmp_path, git_identity):
    team = tmp_path / "team-repo"
    root = team / "agami-artifacts" / "shop"
    root.mkdir(parents=True)
    _git(team, "init", "-q")
    (team / "unrelated.txt").write_text("someone else's work\n", encoding="utf-8")
    (root / "datasource.yaml").write_text("datasource: shop\n", encoding="utf-8")

    committed, note = curate._git_commit(root, "curation: 1 change(s)")
    assert committed is False
    assert "inside the git repository" in note and str(root) in note
    # Nothing was staged or committed in the team's repository, the unrelated file included.
    assert _git(team, "rev-parse", "--verify", "HEAD").returncode != 0
    assert _git(team, "diff", "--cached", "--name-only").stdout == ""


def test_a_profile_outside_git_says_it_has_no_history(tmp_path, monkeypatch):
    root = tmp_path / "shop"
    root.mkdir()
    (root / "datasource.yaml").write_text("datasource: shop\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # never find a repo above the tmp dir
    committed, note = curate._git_commit(root, "x")
    assert committed is False and "git init" in note


def test_the_note_reaches_the_cli_output(tmp_path, monkeypatch, capsys):
    from semantic_model import cli

    res = curate.ApplyResult(validated=True, commit_note="this model isn't in git")
    monkeypatch.setattr(curate, "set_datasource_description", lambda root, d: res)
    cli.main(["set-description", str(tmp_path), "--description", "x"])
    out = json.loads(capsys.readouterr().out)
    assert out["committed"] is False and out["commit_note"] == "this model isn't in git"


# --- the review's cases: onboarding batches, same-named tables, bare names, stale columns ---------


def test_a_batched_first_build_proposes_the_same_areas_as_a_one_shot_build(tmp_path):
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "tests"))
    from semantic_model import loader as L

    from catalog_helpers import col, make_catalog_runner

    names = [f"{fam}_{i}" for fam in ("hr", "inv", "sale") for i in range(10)]
    runner = make_catalog_runner(tables=names, columns={n: [col("id", "integer", nullable=False)] for n in names})

    def areas(root: Path) -> dict[str, set[str]]:
        org = L.load_datasource(root)
        return {sa.name: {t.name for t in sa.tables_defined} for sa in org.subject_areas}

    I.introspect("one", "postgres", runner=runner, artifacts_dir=tmp_path, tables=[f"public.{n}" for n in names])
    for start in range(0, 30, 12):   # the batches agami-connect recommends for 30+ tables
        I.introspect("batched", "postgres", runner=runner, artifacts_dir=tmp_path,
                     tables=[f"public.{n}" for n in names[start:start + 12]], append=True)
    assert len(areas(tmp_path / "one")) > 1
    assert areas(tmp_path / "batched") == areas(tmp_path / "one")


def _area(name: str, tables: list, description: str = "Curated.") -> object:
    from semantic_model import build
    from semantic_model import models as m
    return m.SubjectArea(name=name, description=description, tables_defined=tables,
                         tables=[build.make_table_ref("c", t) for t in tables])


def _model(*areas) -> object:
    from semantic_model import models as m
    return m.Datasource(datasource="shop", version=1, subject_areas=list(areas),
                        storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL", storage_config={})])


def test_a_new_table_never_shares_an_area_with_a_table_of_the_same_name():
    from semantic_model import models as m

    billing = m.Table(name="products", schema="billing", columns=[m.Column(name="id", type="integer")])
    crm = m.Table(name="products", schema="crm", columns=[m.Column(name="id", type="integer")])
    report = I.IntrospectReport(profile="shop", db_type="postgres", out_dir="", dry_run=True)
    org = I._refresh_existing(_model(_area("shop", [billing])), [billing, crm], [], {("crm", "products")}, "shop", report)
    placed = {sa.name: [(t.schema_name, t.name) for t in sa.tables_defined] for sa in org.subject_areas}
    assert placed["shop"] == [("billing", "products")]           # once, not twice
    assert placed["crm"] == [("crm", "products")]                # its own area, not overwriting the other
    assert org.storage_connections[0].name == "c" and org.subject_areas[1].tables[0].storage_connection == "c"


def test_a_bare_name_refreshes_the_stored_schema_qualified_table():
    from semantic_model import models as m

    stored = m.Table(name="customers", schema="main", columns=[])
    existing = {("main", "customers"): stored}
    assert I._existing_table(existing, m.Table(name="customers", columns=[])) is stored
    assert I._existing_table(existing, m.Table(name="Customers", columns=[])) is stored
    two = {("a", "t"): m.Table(name="t", schema="a"), ("b", "t"): m.Table(name="t", schema="b")}
    assert I._existing_table(two, m.Table(name="t", columns=[])) is None    # ambiguous: never guess


def test_a_bare_name_refresh_updates_the_table_instead_of_adding_it_again(shop):
    assert _introspect(shop, "--tables", "main.customers").returncode == 0
    _sql(shop, "ALTER TABLE customers ADD COLUMN region TEXT")
    done = _introspect(shop, "--tables", "customers", "--append")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "new tables" not in done.stdout and "changed customers: added region" in done.stdout
    assert "region" in _columns(shop, "customers")


def test_a_dropped_column_is_kept_but_never_served(shop):
    from semantic_model import loader as L

    assert _introspect(shop, "--tables", "customers").returncode == 0
    _sql(shop, "ALTER TABLE customers DROP COLUMN legacy_code")
    assert _introspect(shop, "--tables", "customers", "--append").returncode == 0
    served = {c.name for t in L.load_datasource(shop["root"]).subject_areas[0].tables_defined for c in t.columns}
    kept = {c.name for t in L.load_datasource(shop["root"], include_rejected=True).subject_areas[0].tables_defined
            for c in t.columns}
    assert "legacy_code" not in served and "legacy_code" in kept


def test_a_stale_column_the_database_has_again_is_restored():
    from semantic_model import models as m

    old = m.Table(name="t", columns=[m.Column(name="a", type="string", review_state="stale")])
    merged, change = I._merge_table(old, m.Table(name="t", columns=[m.Column(name="a", type="string")]))
    assert merged.columns[0].review_state == "approved" and change == {"restored": ["a"]}


def test_a_profile_git_ignores_is_not_treated_as_inside_that_repository(tmp_path, git_identity):
    home = tmp_path / "home"
    root = home / "agami-artifacts" / "shop"
    root.mkdir(parents=True)
    _git(home, "init", "-q")
    (home / ".gitignore").write_text("agami-artifacts/\n", encoding="utf-8")
    committed, note = curate._git_commit(root, "x")
    assert committed is False and "git init" in note
