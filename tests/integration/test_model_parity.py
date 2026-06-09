"""Integration parity — the new model must not lose information vs the legacy OSI.

**Model coverage (no live DB):** every table + column the legacy per-table OSI
model exposes is present in the converted semantic model — no information is lost
in the rearchitecture. Covers FinBud (Snowflake) and main/Turning Pages (Postgres).

(Live execution-parity through execute_sql lands with the query-path port — PR3.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from semantic_model import loader as L  # noqa: E402
from semantic_model import migrate as M  # noqa: E402

ARTIFACTS = Path(os.environ.get("AGAMI_ARTIFACTS_DIR", Path.home() / "agami-artifacts"))


def _has(profile: str) -> bool:
    return (ARTIFACTS / profile / "index.yaml").exists()


def _legacy_tables_and_columns(profile: str) -> dict[str, set[str]]:
    """Read the legacy per-table OSI model -> {table: {columns}}."""
    profile_dir = ARTIFACTS / profile
    index = yaml.safe_load((profile_dir / "index.yaml").read_text())
    out: dict[str, set[str]] = {}
    for sch in index.get("schemas", []):
        schema_doc = yaml.safe_load((profile_dir / sch["file"]).read_text())
        sdir = (profile_dir / sch["file"]).parent
        for tinfo in schema_doc.get("tables", []):
            tdoc = yaml.safe_load((sdir / tinfo["file"]).read_text())
            cols: set[str] = set()
            for entry in tdoc.get("semantic_model", []):
                for ds in entry.get("datasets", []):
                    for f in ds.get("fields", []):
                        cols.add(f["name"])
            out[tinfo["name"]] = cols
    return out


def _v2_tables_and_columns(out_dir: Path) -> dict[str, set[str]]:
    org = L.load_organization(out_dir)
    res: dict[str, set[str]] = {}
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            res[t.name] = {c.name for c in t.columns}
    return res


@pytest.mark.parametrize("profile", ["finbud", "main"])
def test_v2_model_covers_legacy_columns(profile, tmp_path):
    if not _has(profile):
        pytest.skip(f"{profile} profile not installed locally")
    out = tmp_path / f"{profile}_v2"
    M.migrate_profile(profile, ARTIFACTS, out_dir=out, dry_run=False)
    legacy = _legacy_tables_and_columns(profile)
    v2 = _v2_tables_and_columns(out)
    # same table set
    assert set(legacy) == set(v2), f"table set differs: {set(legacy) ^ set(v2)}"
    # every legacy column present in v2 (no column dropped in migration)
    for table, cols in legacy.items():
        missing = cols - v2[table]
        assert not missing, f"{table}: v2 dropped columns {missing}"

# Live execution-parity (execute_sql --v2-area is a no-op for trap-free,
# filter-free queries) lands with the query-path port in PR3, once execute_sql
# speaks the semantic model.
