"""A datasource.yaml key this agami-core does not read is reported, not silently dropped (#575).

`load_datasource` reads a fixed list of top-level keys, so a model written for a newer core that
declared a field the running core did not know lost it without a word, and a deploy fed by that load
stored the model without it. The model still loads; `sm validate` and the deploy name the key. The
load itself says nothing: the executor loads the model in a child process whose stderr is its output.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import yaml  # noqa: E402
from semantic_model import build, cli, loader  # noqa: E402
from semantic_model import models as m  # noqa: E402


@pytest.fixture
def root(tmp_path, monkeypatch):
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    out = tmp_path / "shop"
    build.write_tree(
        m.Datasource(
            datasource="shop",
            description="a shop",
            storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
            subject_areas=[m.SubjectArea(name="sales")],
        ),
        out,
    )
    return out


def _add(root, **keys) -> None:
    path = root / "datasource.yaml"
    doc = yaml.safe_load(path.read_text())
    doc.update(keys)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_a_written_model_has_no_unknown_keys(root):
    assert loader.unknown_datasource_keys(root) == []


def _table(name: str) -> m.Table:
    return m.Table(
        name=name,
        schema="public",
        storage_connection="c",
        grain=["id"],
        description=name,
        columns=[m.Column(name="id", type="integer", primary_key=True)],
    )


def test_every_field_the_model_declares_is_read_back(tmp_path, monkeypatch):
    """The known keys ARE the model's fields, which is only right while the loader reads every one.
    Each field gets a non-default value, and each must come back non-default: a field added to the
    model that the loader never reads comes back as its default, and fails here by name."""
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "shop_dir"
    build.write_tree(
        m.Datasource(
            datasource="shop",
            description="a shop",
            storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
            subject_areas=[
                m.SubjectArea(name="sales", tables_defined=[_table("orders")]),
                m.SubjectArea(name="people", tables_defined=[_table("customers")]),
            ],
        ),
        root,
    )
    _add(
        root,
        org_id="00000000-0000-0000-0000-000000000001",
        version=3,
        fiscal_year_start_month=9,
        key_terminology={"order": "a purchase"},
        ambiguities=[
            {
                "name": "which_date",
                "when": "a date without saying which",
                "options": [{"label": "A", "how": "use a"}, {"label": "B", "how": "use b"}],
            }
        ],
        cross_subject_area_relationships=[
            {
                "from_subject_area": "sales",
                "to_subject_area": "people",
                "from_table": "orders",
                "to_table": "customers",
                "from_column": "id",
                "to_column": "id",
                "relationship": "many_to_one",
            }
        ],
        cross_subject_area_entities=[
            {"name": "buyer", "maps_to": [{"table": "orders", "column": "id"}]}
        ],
        cross_subject_area_metrics=[
            {
                "name": "order_count",
                "calculation": "count",
                "bindings": {"PostgreSQL": "COUNT(*)"},
                "source_tables": ["orders"],
            }
        ],
    )
    org = loader.load_datasource(root)
    defaults = {
        name: f.get_default(call_default_factory=True)
        for name, f in m.Datasource.model_fields.items()
    }
    # `datasource` defaults to the folder name, which the file's value differs from here.
    defaults["datasource"] = root.name
    unread = [name for name, default in defaults.items() if getattr(org, name) == default]
    assert unread == []
    assert loader.unknown_datasource_keys(root) == []


def test_an_unknown_key_still_loads_and_is_named(root):
    _add(root, future_rules=["x"], another={"a": 1})
    assert loader.unknown_datasource_keys(root) == ["another", "future_rules"]
    assert loader.load_datasource(root).datasource == "shop"
    message = loader.unknown_keys_message(loader.unknown_datasource_keys(root))
    assert "another, future_rules" in message and "newer agami-core" in message


def test_the_load_itself_writes_nothing(root, capsys, caplog):
    # The forked executor relays a config error's stderr to the caller; the load must not add to it.
    _add(root, future_rules=["x"])
    with caplog.at_level(logging.DEBUG):
        loader.load_datasource(root)
    assert capsys.readouterr().err == ""
    assert not [r for r in caplog.records if "future_rules" in r.getMessage()]


def test_a_malformed_file_reports_no_keys(root):
    (root / "datasource.yaml").write_text("- just\n- a list\n")
    assert loader.unknown_datasource_keys(root) == []


def test_validate_lists_it_as_a_warning_and_still_passes(root, capsys):
    _add(root, future_rules=["x"])

    class Args:
        pass

    args = Args()
    args.root = str(root)
    assert cli.cmd_validate(args) == 0
    out = capsys.readouterr().out
    assert "unknown_datasource_key" in out and "future_rules" in out


def test_deploy_says_the_field_is_not_deployed(root, tmp_path, monkeypatch, capsys):
    import model_deploy
    from store import Store

    _add(root, future_rules=["x"])
    store = Store.connect("sqlite://" + str(tmp_path / "app.db"))
    try:
        store.run_migrations()
        model_deploy.deploy_one(store, "shop", root, org_id="acme")
    finally:
        store.close()
    err = capsys.readouterr().err
    assert "future_rules" in err and "'shop'" in err and "newer agami-core" in err
