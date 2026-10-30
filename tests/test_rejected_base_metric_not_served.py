"""A metric built on a rejected metric is not served (#571).

The loader dropped a derived metric whose base was left out only when a table or column was also
hidden: it returned early otherwise, so with just a metric rejected, a metric built on it stayed in
the served model and failed to expand ("references unknown base metric"). A base the model never
had is different: that is a mistake in the model, kept for the validator to report.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import yaml  # noqa: E402
from semantic_model import build, loader  # noqa: E402
from semantic_model import models as m  # noqa: E402


def _table() -> m.Table:
    return m.Table(
        name="orders",
        schema="public",
        storage_connection="c",
        grain=["id"],
        description="orders",
        columns=[
            m.Column(name="id", type="integer", primary_key=True),
            m.Column(name="amount", type="decimal"),
        ],
    )


def _metric(
    name: str, sql: str, *, base: tuple[str, ...] = (), state: str = "approved"
) -> m.Metric:
    return m.Metric(
        name=name,
        calculation=name,
        bindings={"PostgreSQL": sql},
        base_metrics=list(base),
        source_tables=["orders"],
        review_state=state,
    )


def _write(tmp_path, monkeypatch, areas: list[m.SubjectArea], cross: list[dict] | None = None):
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    out = tmp_path / "shop"
    build.write_tree(
        m.Datasource(
            datasource="shop",
            storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
            subject_areas=areas,
        ),
        out,
    )
    if cross:
        ds = yaml.safe_load((out / "datasource.yaml").read_text())
        ds["cross_subject_area_metrics"] = cross
        (out / "datasource.yaml").write_text(yaml.safe_dump(ds, sort_keys=False))
    return out


def _served(path) -> set[str]:
    org = loader.load_datasource(path)
    return {x.name for sa in org.subject_areas for x in sa.metrics} | {
        x.name for x in org.cross_subject_area_metrics
    }


def _sales(*metrics: m.Metric, name: str = "sales") -> m.SubjectArea:
    return m.SubjectArea(name=name, tables_defined=[_table()], metrics=list(metrics))


def test_a_metric_on_a_rejected_base_in_its_area_is_not_served(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        monkeypatch,
        [
            _sales(
                _metric("revenue", "SUM(amount)", state="rejected"),
                _metric("orders_n", "COUNT(*)"),
                _metric("avg_order", "{revenue} / {orders_n}", base=("revenue", "orders_n")),
            )
        ],
    )
    assert _served(path) == {"orders_n"}
    # The curation view still has all three, for a person to decide.
    full = loader.load_datasource(path, include_rejected=True)
    assert {x.name for x in full.subject_areas[0].metrics} == {"revenue", "orders_n", "avg_order"}


def test_a_placeholder_alone_names_the_base(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        monkeypatch,
        [
            _sales(
                _metric("revenue", "SUM(amount)", state="rejected"),
                _metric("half", "{revenue} / 2"),
            )
        ],
    )
    assert _served(path) == set()


def test_a_metric_on_a_rejected_cross_area_base_is_not_served(tmp_path, monkeypatch):
    cross = [
        {
            "name": "revenue",
            "calculation": "revenue",
            "bindings": {"PostgreSQL": "SUM(amount)"},
            "source_tables": ["orders"],
            "review_state": "rejected",
        },
        {
            "name": "revenue_x2",
            "calculation": "revenue_x2",
            "bindings": {"PostgreSQL": "{revenue} * 2"},
            "base_metrics": ["revenue"],
            "source_tables": ["orders"],
        },
    ]
    path = _write(
        tmp_path,
        monkeypatch,
        [_sales(_metric("per_order", "{revenue} / COUNT(*)", base=("revenue",)))],
        cross,
    )
    assert _served(path) == set()


def test_a_chain_of_metrics_goes_with_its_rejected_root(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        monkeypatch,
        [
            _sales(
                _metric("revenue", "SUM(amount)", state="rejected"),
                _metric("net", "{revenue} * 0.9", base=("revenue",)),
                _metric("net_x2", "{net} * 2", base=("net",)),
                _metric("orders_n", "COUNT(*)"),
            )
        ],
    )
    assert _served(path) == {"orders_n"}


def test_a_base_the_model_never_had_is_left_for_the_validator(tmp_path, monkeypatch):
    # A typo is a mistake in the model, not something the runtime chose to leave out: dropping it
    # silently would hide the error the validator reports.
    path = _write(
        tmp_path,
        monkeypatch,
        [_sales(_metric("avg_order", "{revnue} / 2", base=("revnue",)))],
    )
    assert _served(path) == {"avg_order"}


def test_a_base_still_served_from_another_area_keeps_its_metrics(tmp_path, monkeypatch):
    # Rejecting one area's copy of `revenue` does not remove the name while another area serves it.
    path = _write(
        tmp_path,
        monkeypatch,
        [
            _sales(
                _metric("revenue", "SUM(amount)", state="rejected"),
                _metric("half", "{revenue} / 2", base=("revenue",)),
            ),
            _sales(_metric("revenue", "SUM(amount)"), name="finance"),
        ],
    )
    assert _served(path) == {"half", "revenue"}


def test_an_ambiguity_naming_a_dropped_metric_loses_it(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        monkeypatch,
        [
            _sales(
                _metric("revenue", "SUM(amount)", state="rejected"),
                _metric("half", "{revenue} / 2", base=("revenue",)),
                _metric("orders_n", "COUNT(*)"),
            )
        ],
    )
    ds = yaml.safe_load((path / "datasource.yaml").read_text())
    ds["ambiguities"] = [
        {
            "name": "which_count",
            "when": "a question about a count without saying which one",
            "options": [
                {"label": "Half", "how": "use half"},
                {"label": "Orders", "how": "use orders_n"},
            ],
            "metrics": ["half", "orders_n"],
        }
    ]
    (path / "datasource.yaml").write_text(yaml.safe_dump(ds, sort_keys=False))
    [amb] = loader.load_datasource(path).ambiguities
    assert amb.metrics == ["orders_n"]


def _with_hidden_column(*metrics: m.Metric) -> m.SubjectArea:
    table = _table()
    table.columns.append(m.Column(name="secret", type="decimal", review_state="rejected"))
    return m.SubjectArea(name="sales", tables_defined=[table], metrics=list(metrics))


def test_a_chain_on_a_metric_whose_column_is_hidden_goes_too(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        monkeypatch,
        [
            _with_hidden_column(
                _metric("s", "SUM(secret)"),
                _metric("s2", "{s} * 2", base=("s",)),
                _metric("s4", "{s2} * 2", base=("s2",)),
                _metric("orders_n", "COUNT(*)"),
            )
        ],
    )
    assert _served(path) == {"orders_n"}


def test_a_typo_base_is_kept_even_when_a_column_is_hidden(tmp_path, monkeypatch):
    # The path that used to drop it: something hidden made the cleanup run, and it dropped every
    # metric whose base was not served, typos included.
    path = _write(
        tmp_path,
        monkeypatch,
        [_with_hidden_column(_metric("avg_order", "{revnue} / 2", base=("revnue",)))],
    )
    assert _served(path) == {"avg_order"}
