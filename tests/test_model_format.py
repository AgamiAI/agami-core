"""An older agami-core refuses to rewrite a model a newer one wrote in a breaking format (#633).

Each write stamps datasource.yaml with `written_by` and `rewrite_needs`, the oldest core that can
rewrite the model safely. A rewrite by a core below `rewrite_needs` is refused and writes nothing; a
missing or unreadable stamp never refuses, and loading or validating is never refused.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import build, curate, loader, model_format, model_spec, validator  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import models as m  # noqa: E402

from catalog_helpers import col as _col  # noqa: E402
from catalog_helpers import make_catalog_runner  # noqa: E402
from test_model_spec import _introspected, _spec  # noqa: E402


@pytest.fixture(autouse=True)
def _no_db_env(monkeypatch):
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)


def _on(monkeypatch, version):
    monkeypatch.setattr(model_format, "core_version", lambda: version)


def _org(*areas: str) -> m.Datasource:
    return m.Datasource(
        datasource="shop",
        storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
        subject_areas=[m.SubjectArea(name=a) for a in areas],
    )


def _files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def _doc(root: Path) -> dict:
    return yaml.safe_load((root / "datasource.yaml").read_text())


def _set(root: Path, **keys) -> None:
    doc = _doc(root)
    doc.update(keys)
    (root / "datasource.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))


@pytest.fixture
def newer(tmp_path, monkeypatch):
    """A model written by 0.9.22, now opened by 0.9.10."""
    root = tmp_path / "shop"
    _on(monkeypatch, "0.9.22")
    build.write_tree(_org("sales"), root)
    _on(monkeypatch, "0.9.10")
    return root


@pytest.mark.parametrize("version,expected", [
    ("0.9.22", {"written_by": "agami-core 0.9.22", "rewrite_needs": "agami-core 0.9.21"}),
    ("0.9.21", {"written_by": "agami-core 0.9.21", "rewrite_needs": "agami-core 0.9.21"}),
    ("0.9.20", {"written_by": "agami-core 0.9.20", "rewrite_needs": "agami-core 0.9.16"}),
    ("0.9.10", {"written_by": "agami-core 0.9.10"}),
    ("0.9.23.dev1", {"written_by": "agami-core 0.9.23.dev1", "rewrite_needs": "agami-core 0.9.21"}),
    (None, {}),
])
def test_the_stamp_names_the_writer_and_the_newest_breaking_format(monkeypatch, version, expected):
    _on(monkeypatch, version)
    assert model_format.stamp() == expected


def test_formats_are_listed_oldest_first():
    versions = [model_format._parse(v) for v, _, _ in model_format.FORMATS]
    assert None not in versions and versions == sorted(versions)


def test_a_write_stamps_datasource_yaml_and_the_keys_are_known(newer):
    assert _doc(newer)["written_by"] == "agami-core 0.9.22"
    assert _doc(newer)["rewrite_needs"] == "agami-core 0.9.21"
    assert loader.unknown_datasource_keys(newer) == []


def test_write_tree_refuses_and_writes_nothing(newer):
    before = _files(newer)
    with pytest.raises(model_format.ModelTooNew) as exc:
        build.write_tree(_org("sales", "returns"), newer)
    msg = str(exc.value)
    assert "agami-core 0.9.22" in msg and "(0.9.10)" in msg and "0.9.21 or later" in msg
    assert _files(newer) == before


def test_a_dry_run_is_not_refused(newer):
    before = _files(newer)
    build.write_tree(_org("sales", "returns"), newer, dry_run=True)
    assert _files(newer) == before


_runner = make_catalog_runner(
    tables=["orders"],
    columns={"orders": [_col("id", "integer", nullable=False), _col("amount", "numeric", scale=2)]},
    estimate=None,
)


def test_re_introspection_is_refused_and_writes_nothing(tmp_path, monkeypatch):
    _on(monkeypatch, "0.9.22")
    I.introspect("shop", "postgres", runner=_runner, artifacts_dir=tmp_path)
    root = tmp_path / "shop"
    before = _files(root)
    _on(monkeypatch, "0.9.10")
    progress = root / ".introspect" / "progress.log"
    _, report = I.introspect("shop", "postgres", runner=_runner, artifacts_dir=tmp_path,
                             progress_path=progress)
    assert report.dry_run and report.notes[0].startswith("NOT WRITTEN: ")
    assert "Upgrade agami-core to 0.9.21" in report.notes[0]
    assert _files(root) == before and not progress.exists()


def test_applying_a_spec_is_refused_and_writes_nothing(tmp_path, monkeypatch):
    _introspected(tmp_path)
    _set(tmp_path, written_by="agami-core 0.9.22", rewrite_needs="agami-core 0.9.16")
    before = _files(tmp_path)
    _on(monkeypatch, "0.9.10")
    res = model_spec.apply_spec(tmp_path, _spec(), signer="you@example.com")
    assert not res.applied and "0.9.16 or later" in res.errors[0]
    assert _files(tmp_path) == before


@pytest.mark.parametrize("write", [
    lambda root: curate.apply(root, [{"op": "approve", "kind": "metric", "area": "sales", "name": "x"}]),
    lambda root: curate.set_key_terminology(root, {"order": "a purchase"}),
    lambda root: curate.set_datasource_description(root, "a shop"),
    lambda root: curate.write_items(root, "sales", "metric", [{"name": "x"}]),
    lambda root: curate.write_items(root, curate.CROSS_AREA, "metric", [{"name": "x"}]),
    lambda root: curate.add_relationships(root, intra={"sales": [{"from_table": "a"}]}),
])
def test_curation_is_refused_and_writes_nothing(newer, write):
    before = _files(newer)
    res = write(newer)
    assert not res.applied and "0.9.21 or later" in res.errors[0]
    assert _files(newer) == before


def test_a_core_older_than_the_choice_field_change_is_refused(newer, monkeypatch):
    # 0.9.20 trims choice_field keys on load, merging 'Open' and 'Open ' (0.9.21).
    _on(monkeypatch, "0.9.20")
    assert "0.9.21 or later" in model_format.rewrite_refusal(newer)


def test_a_core_at_or_above_rewrite_needs_rebuilds_and_restamps(newer, monkeypatch):
    _on(monkeypatch, "0.9.21")
    build.write_tree(_org("sales", "returns"), newer)
    assert (newer / "subject_areas" / "returns" / "subject_area.yaml").is_file()
    assert _doc(newer)["written_by"] == "agami-core 0.9.21"


@pytest.mark.parametrize("change", [
    {"rewrite_needs": None},            # a model from before the stamp
    {"rewrite_needs": "soon"},          # unreadable
    {"rewrite_needs": ["0.9.16"]},      # not a string
])
def test_a_missing_or_unreadable_stamp_never_refuses(newer, change):
    doc = _doc(newer)
    doc.pop("written_by")
    doc.update({k: v for k, v in change.items() if v is not None})
    if change["rewrite_needs"] is None:
        doc.pop("rewrite_needs")
    (newer / "datasource.yaml").write_text(yaml.safe_dump(doc))
    assert model_format.rewrite_refusal(newer) is None
    build.write_tree(_org("sales", "returns"), newer)


def test_a_datasource_yaml_that_is_not_utf8_never_refuses(newer):
    (newer / "datasource.yaml").write_bytes(b"rewrite_needs: agami-core 9.9.9\n\xff\xfe\n")
    assert model_format.rewrite_refusal(newer) is None


def test_an_unknown_running_version_never_refuses(newer, monkeypatch):
    _on(monkeypatch, None)
    assert model_format.rewrite_refusal(newer) is None


def test_a_refusal_without_written_by_still_reads(newer):
    doc = _doc(newer)
    doc.pop("written_by")
    (newer / "datasource.yaml").write_text(yaml.safe_dump(doc))
    assert "written by a newer agami-core" in model_format.rewrite_refusal(newer)


def test_reading_and_validating_are_never_refused(newer):
    org = loader.load_datasource(newer)
    assert org.datasource == "shop"
    assert validator.validate(org).ok
