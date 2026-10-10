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
    assert merged.column_groups == {"core": ["a", "b"]}   # an existing group, never an unexposed new one
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
    assert "new tables" not in done.stdout and "changed main.customers: added region" in done.stdout
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


# --- Copilot's review: schema-qualified report and joins, joins and grain on unserved columns -----


def test_the_join_dedup_keeps_schemas_apart_and_matches_a_schemaless_copy():
    from semantic_model import models as m

    a = m.Relationship(from_table="products", from_column="cat_id", to_table="cats", to_column="id",
                       from_schema="billing", to_schema="billing", relationship="many_to_one")
    assert not I._same_join(a, a.model_copy(update={"from_schema": "crm", "to_schema": "crm"}))
    # Stored with no schema, or as schema.table: still the same edge as the catalog's copy.
    assert I._same_join(a, a.model_copy(update={"from_schema": None, "to_schema": None}))
    assert I._same_join(a, a.model_copy(update={"from_table": "billing.products"}))


def test_a_dropped_grain_column_leaves_the_grain():
    from semantic_model import models as m

    old = m.Table(name="t", grain=["k"], columns=[m.Column(name="k", type="integer"), m.Column(name="v", type="string")])
    fresh = m.Table(name="t", grain=["v"], columns=[m.Column(name="v", type="string")])
    merged, _ = I._merge_table(old, fresh)
    assert merged.grain == ["v"]


def test_a_new_column_goes_in_a_group_the_table_already_has():
    from semantic_model import models as m

    old = m.Table(name="t", columns=[m.Column(name="a", type="string")], column_groups={"misc": ["a"]})
    fresh = m.Table(name="t", columns=[m.Column(name="a", type="string"), m.Column(name="b", type="string")],
                    column_groups={"identity": ["a", "b"]})
    merged, _ = I._merge_table(old, fresh)
    assert merged.column_groups == {"misc": ["a", "b"]}


def test_a_join_on_a_column_that_is_not_served_is_not_offered(shop):
    from semantic_model import loader as L

    assert _introspect(shop, "--tables", "customers", "orders").returncode == 0
    org = L.load_datasource(shop["root"])
    assert any(r.from_column == "customer_id" for sa in org.subject_areas for r in sa.relationships)
    path = _table_files(shop["root"])["orders"]
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for c in doc["columns"]:
        if c["name"] == "customer_id":
            c["review_state"] = "stale"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    served = L.load_datasource(shop["root"])
    assert not any(r.from_column == "customer_id" for sa in served.subject_areas for r in sa.relationships)
    kept = L.load_datasource(shop["root"], include_rejected=True)
    assert any(r.from_column == "customer_id" for sa in kept.subject_areas for r in sa.relationships)


def test_the_refresh_report_names_tables_with_their_schema(shop):
    assert _introspect(shop, "--tables", "main.customers").returncode == 0
    _sql(shop, "ALTER TABLE customers ADD COLUMN region TEXT")
    out = _introspect(shop, "--tables", "main.customers", "--append", "--dry-run").stdout
    assert "changed main.customers: added region" in out


def test_joins_on_same_named_tables_in_two_schemas_are_judged_separately(tmp_path):
    from semantic_model import loader as L
    from semantic_model import models as m

    def orders(schema: str, review: str) -> m.Table:
        return m.Table(name="orders", schema=schema, columns=[
            m.Column(name="id", type="integer"), m.Column(name="customer_id", type="integer", review_state=review)])

    def join(schema: str) -> m.Relationship:
        return m.Relationship(from_table="orders", from_column="customer_id", to_table="customers",
                              to_column="id", from_schema=schema, to_schema=schema, relationship="many_to_one")

    sales, archive = orders("sales", "approved"), orders("archive", "approved")
    archive.columns = [c for c in archive.columns if c.name != "customer_id"]   # what the loader leaves when stale
    org = m.Datasource(datasource="d", version=1, subject_areas=[
        m.SubjectArea(name="s", tables_defined=[sales], relationships=[join("sales")]),
        m.SubjectArea(name="a", tables_defined=[archive], relationships=[join("archive")])])
    hidden = L._Hidden(served={("sales", "orders"): {"id", "customer_id"}, ("archive", "orders"): {"id"}},
                       on_disk={("archive", "orders"): {"customer_id"}})
    L._drop_uses_of_unserved(org, hidden)
    assert [r.from_schema for r in org.subject_areas[0].relationships] == ["sales"]   # live: kept
    assert org.subject_areas[1].relationships == []                                  # stale: hidden


def test_a_bare_name_matching_two_stored_tables_is_refused_not_added():
    from semantic_model import models as m

    existing = {("a", "t"): m.Table(name="t", schema="a"), ("b", "t"): m.Table(name="t", schema="b")}
    with pytest.raises(RuntimeError, match=r"matches 2 tables in the model \(a.t, b.t\)"):
        I._refuse_ambiguous(existing, m.Table(name="t"))
    I._refuse_ambiguous(existing, m.Table(name="t", schema="a"))   # qualified: fine
    I._refuse_ambiguous({("a", "t"): m.Table(name="t", schema="a")}, m.Table(name="t"))   # unique: fine


# --- the full merge rules: key and grain, area provenance, the model's connection ------------------


def _cols(*names: str, key: tuple = ()) -> list:
    from semantic_model import models as m
    return [m.Column(name=n, type="integer", primary_key=n in key) for n in names]


def test_a_key_the_database_moved_moves_the_flags_and_an_engine_grain():
    from semantic_model import models as m

    old = m.Table(name="t", grain=["id"], columns=_cols("id", "external_id", key=("id",)))
    fresh = m.Table(name="t", grain=["external_id"], columns=_cols("id", "external_id", key=("external_id",)))
    merged, change = I._merge_table(old, fresh)
    assert {c.name: c.primary_key for c in merged.columns} == {"id": False, "external_id": True}
    assert merged.grain == ["external_id"]
    assert change["key"] == ["id -> external_id"]


def test_a_grain_a_person_or_spec_stated_is_kept_when_the_key_moves():
    from semantic_model import models as m

    old = m.Table(name="t", grain=["order_id", "line_no"],      # stated, not the key
                  columns=_cols("id", "order_id", "line_no", key=("id",)))
    fresh = m.Table(name="t", grain=["uid"], columns=_cols("uid", "order_id", "line_no", key=("uid",)))
    merged, change = I._merge_table(old, fresh)
    assert merged.grain == ["order_id", "line_no"]
    assert [c.name for c in merged.columns if c.primary_key] == ["uid"]


def test_no_key_reported_leaves_the_key_and_grain_alone():
    from semantic_model import models as m

    old = m.Table(name="t", grain=["id"], columns=_cols("id", "v", key=("id",)))
    fresh = m.Table(name="t", grain=[], columns=_cols("id", "v"))      # the probe was skipped
    merged, change = I._merge_table(old, fresh)
    assert merged.grain == ["id"] and [c.name for c in merged.columns if c.primary_key] == ["id"]
    assert "key" not in change


def test_only_an_area_exactly_as_generated_counts_as_untouched(tmp_path):
    from semantic_model import build
    from semantic_model import models as m

    t = m.Table(name="orders", schema="public", columns=_cols("id"), storage_connection="c")
    generated = build.make_area("shop", [t], [], "c")

    def model(sa) -> object:
        return m.Datasource(datasource="shop", version=1, subject_areas=[sa],
                            storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL", storage_config={})])

    assert I._areas_untouched(model(generated), tmp_path)
    for change in ({"description": generated.description + " Curated."},
                   {"description": "Auto-proposed subject area covering: what we sell."},
                   {"default_time_window": "last 90 days"},
                   {"tables": [generated.tables[0].model_copy(update={"expose_column_groups": ["core"]})]}):
        assert not I._areas_untouched(model(generated.model_copy(update=change)), tmp_path), change


def test_a_new_table_takes_the_models_own_connection(tmp_path):
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "tests"))
    from semantic_model import build
    from semantic_model import loader as L
    from semantic_model import models as m
    from semantic_model import validator as V

    from catalog_helpers import col, make_catalog_runner

    customers = m.Table(name="customers", schema="public", storage_connection="c",
                        columns=[m.Column(name="id", type="integer", primary_key=True)], grain=["id"])
    org = m.Datasource(datasource="shop", version=1,
                       storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL", storage_config={})],
                       subject_areas=[m.SubjectArea(name="sales", description="Curated.", tables_defined=[customers],
                                                    tables=[build.make_table_ref("c", customers)])])
    build.write_tree(org, tmp_path / "shop")
    runner = make_catalog_runner(tables=["customers", "orders"], columns={
        "customers": [col("id", "integer", nullable=False)], "orders": [col("id", "integer", nullable=False)]})

    I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path,
                 tables=["public.customers", "public.orders"], append=True)
    out = L.load_datasource(tmp_path / "shop", include_rejected=True)
    orders = next(t for sa in out.subject_areas for t in sa.tables_defined if t.name == "orders")
    assert orders.storage_connection == "c"
    assert {r.storage_connection for sa in out.subject_areas for r in sa.tables} == {"c"}
    assert V.validate(out).ok


# --- field-ownership review: every database change a refresh must survive ------------------------


def test_a_table_that_becomes_deep_gets_column_groups_and_is_written(shop):
    from semantic_model import loader as L

    cols = ", ".join(f"c{i} TEXT" for i in range(27))
    _sql(shop, f"CREATE TABLE wide(id INTEGER PRIMARY KEY, {cols})")          # 28 columns: not deep
    assert _introspect(shop, "--tables", "wide").returncode == 0
    for i in range(27, 30):
        _sql(shop, f"ALTER TABLE wide ADD COLUMN c{i} TEXT")                  # now 31: deep
    done = _introspect(shop, "--tables", "wide", "--append")
    assert done.returncode == 0, done.stdout + done.stderr
    wide = next(t for sa in L.load_datasource(shop["root"], include_rejected=True).subject_areas
                for t in sa.tables_defined if t.name == "wide")
    assert wide.column_groups and {c for g in wide.column_groups.values() for c in g} == {c.name for c in wide.columns}


def test_a_join_column_that_changes_type_sends_the_join_back_to_review(shop):
    from semantic_model import loader as L

    assert _introspect(shop, "--tables", "customers", "orders").returncode == 0
    _sql(shop, "ALTER TABLE orders RENAME TO o_old")
    _sql(shop, "CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id TEXT REFERENCES customers(id), total REAL)")
    _sql(shop, "DROP TABLE o_old")
    done = _introspect(shop, "--tables", "orders", "--append")
    assert done.returncode == 0, done.stdout + done.stderr           # not blocked by a type mismatch
    joins = [r for sa in L.load_datasource(shop["root"], include_rejected=True).subject_areas
             for r in sa.relationships if r.from_column == "customer_id"]
    assert joins and all(r.review_state == "unreviewed" for r in joins)
    assert "joins to review" in done.stdout


def test_a_retype_re_derives_what_the_old_type_implied():
    from semantic_model import models as m

    old = m.Column(name="created_at", type="integer", date_format="epoch_s", timezone="UTC",
                   aggregation="additive", choice_field={"1": "one"})
    fresh = m.Column(name="created_at", type="timestamp", aggregation="dimension")
    change: dict = {}
    c = I._retyped(old, fresh, change)
    assert (c.type, c.date_format, c.timezone, c.choice_field) == ("timestamp", None, None, None)
    assert change == {"values reset": ["created_at"]}
    curated = I._retyped(old.model_copy(update={"aggregation": "averageable"}), fresh, {})
    assert curated.aggregation == "averageable"        # a person's class is kept


def test_a_sql_defined_table_is_never_merged_with_a_database_table():
    from semantic_model import models as m

    existing = {(None, "big_orders"): m.Table(name="big_orders", source_type="sql", sql="SELECT 1")}
    with pytest.raises(RuntimeError, match="defined by SQL"):
        I._refuse_sql_table(existing, m.Table(name="big_orders", schema="main"))


def test_a_table_the_database_dropped_is_kept_stale_and_not_served(shop):
    from semantic_model import loader as L

    assert _introspect(shop, "--tables", "customers", "orders").returncode == 0
    _sql(shop, "DROP TABLE orders")
    for args in (["--tables", "customers", "orders"], ["--tables", "orders"], []):
        done = _introspect(shop, *args, "--append")
        assert done.returncode == 0, (args, done.stdout + done.stderr)   # never "bad allowlist"
    served = {t.name for sa in L.load_datasource(shop["root"]).subject_areas for t in sa.tables_defined}
    kept = {t.name: t.review_state for sa in L.load_datasource(shop["root"], include_rejected=True).subject_areas
            for t in sa.tables_defined}
    assert "orders" not in served and kept["orders"] == "stale"


def test_a_filter_on_a_dropped_column_is_not_served(shop):
    from semantic_model import loader as L
    from semantic_model import validator as V

    assert _introspect(shop, "--tables", "customers").returncode == 0
    path = _table_files(shop["root"])["customers"]
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["default_filters"] = ["{alias}.legacy_code <> 'x'"]
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    _sql(shop, "ALTER TABLE customers DROP COLUMN legacy_code")
    assert _introspect(shop, "--tables", "customers", "--append").returncode == 0
    served = L.load_datasource(shop["root"])
    assert served.subject_areas[0].tables_defined[0].default_filters == []
    assert V.validate(served).ok                                            # the runtime view is sound
    on_disk = L.load_datasource(shop["root"], include_rejected=True)
    assert on_disk.subject_areas[0].tables_defined[0].default_filters == ["{alias}.legacy_code <> 'x'"]


def test_entities_and_metrics_on_a_hidden_column_are_not_served(tmp_path):
    from semantic_model import build
    from semantic_model import loader as L
    from semantic_model import models as m

    t = m.Table(name="customers", schema="main", storage_connection="c", columns=[
        m.Column(name="id", type="integer", primary_key=True),
        m.Column(name="region", type="string", review_state="stale"), m.Column(name="spend", type="decimal")])
    sa = m.SubjectArea(
        name="shop", description="Curated.", tables_defined=[t], tables=[build.make_table_ref("c", t)],
        entities=[m.Entity(name="Region", maps_to=[m.EntityMapping(table="customers", column="region")]),
                  m.Entity(name="Customer", maps_to=[m.EntityMapping(table="customers", column="id")])],
        metrics=[m.Metric(name="Regions", calculation="COUNT(DISTINCT region)", source_tables=["customers"]),
                 m.Metric(name="Spend", calculation="SUM(spend)", source_tables=["customers"])])
    build.write_tree(m.Datasource(datasource="shop", version=1, subject_areas=[sa], storage_connections=[
        m.StorageConnection(name="c", storage_type="SQLite", storage_config={})]), tmp_path / "shop")
    served = L.load_datasource(tmp_path / "shop").subject_areas[0]
    assert [e.name for e in served.entities] == ["Customer"]
    assert [mm.name for mm in served.metrics] == ["Spend"]
    on_disk = L.load_datasource(tmp_path / "shop", include_rejected=True).subject_areas[0]
    assert len(on_disk.entities) == 2 and len(on_disk.metrics) == 2


def test_row_count_hints_follow_the_table_and_curated_filters_stay():
    from semantic_model import models as m

    old = m.PerformanceHints(estimated_row_count=1, recommended_filters=["created_at"])
    fresh = m.PerformanceHints(estimated_row_count=5000, estimated_row_count_at="now")
    merged = I._merged_hints(old, fresh)
    assert (merged.estimated_row_count, merged.estimated_row_count_at, merged.recommended_filters) == (5000, "now", ["created_at"])


# --- areas, write path and loader review --------------------------------------------------------


def test_a_new_table_never_pushes_an_area_past_the_size_limit():
    from semantic_model import models as m
    from semantic_model.validator import SIZING_ERROR

    tables = [m.Table(name=f"t{i}", schema="main", columns=_cols("id")) for i in range(SIZING_ERROR)]
    new = m.Table(name="extra", schema="main", columns=_cols("id"))
    report = I.IntrospectReport(profile="shop", db_type="sqlite", out_dir="", dry_run=True)
    org = I._refresh_existing(_model(_area("main", tables)), [*tables, new], [], {("main", "extra")}, "shop", report)
    assert all(len(sa.tables) <= SIZING_ERROR for sa in org.subject_areas)
    assert any("extra" in {t.name for t in sa.tables_defined} for sa in org.subject_areas[1:])


@pytest.mark.parametrize("evidence", ["examples", "log", "table description", "column description"])
def test_curation_outside_the_areas_also_keeps_them(tmp_path, evidence):
    from semantic_model import build
    from semantic_model import models as m

    t = m.Table(name="orders", schema="public", columns=_cols("id"), storage_connection="c")
    if evidence == "table description":
        t = t.model_copy(update={"description": "Every order placed."})
    if evidence == "column description":
        t = t.model_copy(update={"columns": [m.Column(name="id", type="integer", description="The order's key.")]})
    root = tmp_path / "shop"
    (root / "prompt_examples" / "shop").mkdir(parents=True)
    if evidence == "examples":
        (root / "prompt_examples" / "shop" / "examples.yaml").write_text("examples: []\n", encoding="utf-8")
    if evidence == "log":
        (root / "curation_log.jsonl").write_text("{}\n", encoding="utf-8")
    org = m.Datasource(datasource="shop", version=1, subject_areas=[build.make_area("shop", [t], [], "c")],
                       storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL", storage_config={})])
    assert not I._areas_untouched(org, root)


def test_a_valid_metric_on_one_schema_survives_a_stale_column_in_another(tmp_path):
    from semantic_model import build
    from semantic_model import loader as L
    from semantic_model import models as m

    def orders(schema: str, review: str) -> m.Table:
        return m.Table(name="orders", schema=schema, storage_connection="c", columns=[
            m.Column(name="id", type="integer", primary_key=True),
            m.Column(name="amount", type="decimal", review_state=review)])

    sales, archive = orders("sales", "approved"), orders("archive", "stale")
    areas = [m.SubjectArea(name=n, description="Curated.", tables_defined=[t], tables=[build.make_table_ref("c", t)],
                           metrics=[m.Metric(name=f"{n} revenue", calculation="SUM(amount)", source_tables=[f"{n}.orders"])])
             for n, t in (("sales", sales), ("archive", archive))]
    build.write_tree(m.Datasource(datasource="shop", version=1, subject_areas=areas, storage_connections=[
        m.StorageConnection(name="c", storage_type="PostgreSQL", storage_config={})]), tmp_path / "shop")
    served = {sa.name: [mm.name for mm in sa.metrics] for sa in L.load_datasource(tmp_path / "shop").subject_areas}
    assert served == {"sales": ["sales revenue"], "archive": []}


def test_re_proposed_areas_leave_no_table_defined_twice(tmp_path):
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "tests"))
    from semantic_model import loader as L

    from catalog_helpers import col, make_catalog_runner

    def runner_for(schema: str, tables: list[str]):
        return make_catalog_runner(tables=tables, schema=schema, columns={t: [col("id", "integer", nullable=False)] for t in tables})

    pub, ana = runner_for("public", ["customers", "orders"]), runner_for("analytics", ["daily", "weekly"])

    def runner(sql: str) -> list[dict]:   # two schemas: answer from whichever the statement names
        return ana(sql) if "analytics" in sql else pub(sql)

    I.introspect("analytics", "postgres", runner=runner, artifacts_dir=tmp_path,
                 tables=["public.customers", "public.orders"], append=True)
    I.introspect("analytics", "postgres", runner=runner, artifacts_dir=tmp_path,
                 tables=["analytics.daily", "analytics.weekly"], append=True)
    org = L.load_datasource(tmp_path / "analytics", include_rejected=True)
    defined = [(t.schema_name, t.name) for sa in org.subject_areas for t in sa.tables_defined]
    assert len(defined) == len(set(defined)) == 4


def test_a_dropped_tables_joins_and_references_are_not_served(shop):
    from semantic_model import loader as L
    from semantic_model import validator as V

    assert _introspect(shop, "--tables", "customers", "orders").returncode == 0
    _sql(shop, "DROP TABLE orders")
    assert _introspect(shop, "--tables", "customers", "orders", "--append").returncode == 0
    served = L.load_datasource(shop["root"])
    assert not any("orders" in (r.from_table, r.to_table) for sa in served.subject_areas for r in sa.relationships)
    assert not any(r.table == "orders" for sa in served.subject_areas for r in sa.tables)
    assert V.validate(served).ok


def test_an_exported_git_dir_cannot_redirect_the_commit(tmp_path, git_identity, monkeypatch):
    outer, root = tmp_path / "outer", tmp_path / "shop"
    outer.mkdir()
    root.mkdir()
    _git(outer, "init", "-q")
    (outer / "secret.txt").write_text("not the model's\n", encoding="utf-8")
    _git(root, "init", "-q")
    (root / "datasource.yaml").write_text("datasource: shop\n", encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(outer / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outer))
    assert curate._git_commit(root, "x") == (True, "")
    assert _git(outer, "rev-parse", "--verify", "HEAD").returncode != 0      # nothing landed outside
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    assert "datasource.yaml" in _git(root, "show", "--name-only", "HEAD").stdout


def test_a_merge_that_does_not_validate_says_so_first_and_writes_nothing(shop, monkeypatch):
    from semantic_model import validator as V

    assert _introspect(shop, "--tables", "customers").returncode == 0
    before = {p: p.read_bytes() for p in shop["root"].rglob("*.yaml")}

    class _Bad:
        ok, errors, warnings = False, ["x"], []

    monkeypatch.setattr(V, "validate", lambda *a, **k: _Bad())
    _, report = I.introspect("shop", "sqlite", runner=I.make_execute_sql_runner("shop", building_the_model=True),
                             artifacts_dir=shop["art"], tables=["customers"], append=True)
    assert report.notes[0].startswith("NOT WRITTEN") and report.dry_run is True
    assert {p: p.read_bytes() for p in shop["root"].rglob("*.yaml")} == before



# --- the runtime filter hides only on positive evidence, across every area's copy ---------------


def _sample_copy(tmp_path):
    import shutil
    src = REPO_ROOT / "plugins" / "agami" / "samples" / "store" / "model"
    dst = tmp_path / "model"
    shutil.copytree(src, dst)
    return dst


def _view(root, filtered: bool, monkeypatch):
    from semantic_model import loader as L
    if not filtered:
        monkeypatch.setattr(L, "_drop_uses_of_unserved", lambda org, hidden: None)
    org = L.load_datasource(root)
    monkeypatch.undo()
    return org.model_dump()


def test_the_filter_changes_nothing_on_a_model_with_nothing_hidden(tmp_path, monkeypatch):
    root = _sample_copy(tmp_path)
    assert _view(root, True, monkeypatch) == _view(root, False, monkeypatch)


def test_an_unlisted_join_or_grain_column_is_not_evidence_of_anything(tmp_path, monkeypatch):
    # A join or grain column a table doesn't list is allowed; only an excluded/stale one is hidden.
    root = _sample_copy(tmp_path)
    path = next(root.glob("subject_areas/*/tables/order_items.yaml"))
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    listed = [c["name"] for c in doc["columns"]]
    doc["columns"] = doc["columns"][:1]        # stop listing every other column
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    assert len(listed) > 1
    assert _view(root, True, monkeypatch) == _view(root, False, monkeypatch)


def test_one_areas_excluded_copy_does_not_hide_anothers_served_one(tmp_path):
    from semantic_model import build
    from semantic_model import loader as L
    from semantic_model import models as m

    def invoices(review: str) -> m.Table:
        return m.Table(name="invoices", schema="main", storage_connection="c", review_state=review,
                       columns=[m.Column(name="id", type="integer", primary_key=True),
                                m.Column(name="subscription_id", type="integer")])

    subs = m.Table(name="subscriptions", schema="main", storage_connection="c",
                   columns=[m.Column(name="id", type="integer", primary_key=True)])
    join = m.Relationship(from_table="invoices", from_column="subscription_id", to_table="subscriptions",
                          to_column="id", from_schema="main", to_schema="main", relationship="many_to_one")
    a = m.SubjectArea(name="billing", description="Curated.", tables_defined=[invoices("approved"), subs],
                      tables=[build.make_table_ref("c", invoices("approved")), build.make_table_ref("c", subs)],
                      relationships=[join],
                      metrics=[m.Metric(name="Invoices", calculation="Count of invoices", bindings={"sqlite": "COUNT(subscription_id)"},
                                        source_tables=["invoices"])])
    b = m.SubjectArea(name="other", description="Curated.", tables_defined=[invoices("rejected")],
                      tables=[build.make_table_ref("c", invoices("rejected"))])
    build.write_tree(m.Datasource(datasource="shop", version=1, subject_areas=[a, b], storage_connections=[
        m.StorageConnection(name="c", storage_type="SQLite", storage_config={})]), tmp_path / "shop")
    billing = next(sa for sa in L.load_datasource(tmp_path / "shop").subject_areas if sa.name == "billing")
    assert [r.from_column for r in billing.relationships] == ["subscription_id"]
    assert {r.table for r in billing.tables} == {"invoices", "subscriptions"}
    assert [mm.name for mm in billing.metrics] == ["Invoices"]


def test_a_metric_is_judged_on_its_sql_not_its_prose(tmp_path):
    from semantic_model import build
    from semantic_model import loader as L
    from semantic_model import models as m

    t = m.Table(name="orders", schema="main", storage_connection="c", columns=[
        m.Column(name="id", type="integer", primary_key=True), m.Column(name="total", type="decimal", review_state="stale"),
        m.Column(name="amount", type="decimal")])
    sa = m.SubjectArea(name="s", description="Curated.", tables_defined=[t], tables=[build.make_table_ref("c", t)],
                       metrics=[m.Metric(name="Revenue", calculation="The total of every paid order",
                                         bindings={"sqlite": "SUM(amount)"}, source_tables=["orders"])])
    build.write_tree(m.Datasource(datasource="shop", version=1, subject_areas=[sa], storage_connections=[
        m.StorageConnection(name="c", storage_type="SQLite", storage_config={})]), tmp_path / "shop")
    assert [mm.name for mm in L.load_datasource(tmp_path / "shop").subject_areas[0].metrics] == ["Revenue"]


def test_each_areas_copy_of_a_table_is_refreshed_on_its_own(tmp_path):
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "tests"))
    from semantic_model import build
    from semantic_model import loader as L
    from semantic_model import models as m

    from catalog_helpers import col, make_catalog_runner

    def orders(desc: str, review: str) -> m.Table:
        return m.Table(name="orders", schema="public", storage_connection="c", review_state=review, grain=["id"],
                       columns=[m.Column(name="id", type="integer", primary_key=True, description=desc)])

    served, excluded = orders("Served copy, curated.", "approved"), orders("Excluded copy.", "rejected")
    areas = [m.SubjectArea(name=n, description="Curated.", tables_defined=[t], tables=[build.make_table_ref("c", t)])
             for n, t in (("sales", served), ("archive", excluded))]
    build.write_tree(m.Datasource(datasource="shop", version=1, subject_areas=areas, storage_connections=[
        m.StorageConnection(name="c", storage_type="PostgreSQL", storage_config={})]), tmp_path / "shop")
    runner = make_catalog_runner(tables=["orders"], columns={"orders": [col("id", "integer", nullable=False), col("region", "varchar")]})

    I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path, tables=["public.orders"], append=True)
    on_disk = {sa.name: sa.tables_defined[0] for sa in L.load_datasource(tmp_path / "shop", include_rejected=True).subject_areas}
    assert on_disk["sales"].columns[0].description == "Served copy, curated."
    assert on_disk["archive"].columns[0].description == "Excluded copy."
    assert on_disk["archive"].review_state == "rejected"                   # never re-exposed
    assert {c.name for c in on_disk["sales"].columns} == {c.name for c in on_disk["archive"].columns} == {"id", "region"}
    served_view = {sa.name: [t.name for t in sa.tables_defined] for sa in L.load_datasource(tmp_path / "shop").subject_areas}
    assert served_view == {"sales": ["orders"], "archive": []}


def test_cross_area_entities_on_a_hidden_column_are_not_served(tmp_path):
    from semantic_model import build
    from semantic_model import loader as L
    from semantic_model import models as m

    t = m.Table(name="customers", schema="main", storage_connection="c", columns=[
        m.Column(name="id", type="integer", primary_key=True), m.Column(name="region", type="string", review_state="stale")])
    sa = m.SubjectArea(name="shop", description="Curated.", tables_defined=[t], tables=[build.make_table_ref("c", t)])
    org = m.Datasource(datasource="shop", version=1, subject_areas=[sa], storage_connections=[
        m.StorageConnection(name="c", storage_type="SQLite", storage_config={})])
    build.write_tree(org, tmp_path / "shop")
    (tmp_path / "shop" / "cross_subject_area_entities.yaml").write_text(yaml.safe_dump({"entities": [
        {"name": "Region", "maps_to": [{"table": "customers", "column": "region"}]},
        {"name": "Customer", "maps_to": [{"table": "customers", "column": "id"}]}]}), encoding="utf-8")
    assert len(L.load_datasource(tmp_path / "shop", include_rejected=True).cross_subject_area_entities) == 2
    assert [e.name for e in L.load_datasource(tmp_path / "shop").cross_subject_area_entities] == ["Customer"]
