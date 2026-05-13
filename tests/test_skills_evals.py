"""Schema validation for per-skill ``evals.json`` files.

Each ``plugins/agami/skills/<name>/evals.json`` follows Anthropic's
skill-creator format with a pinned ``schema_version`` so we can migrate
when the upstream format versions. The actual model-graded runs live in
``tools/run_skill_evals.py`` (manual, requires API key) — this test only
validates the on-disk JSON.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "plugins" / "agami" / "skills"

REQUIRED_TOP_KEYS = {"schema_version", "skill_name", "evals"}
REQUIRED_EVAL_KEYS = {"id", "prompt", "expected_output", "files"}
SCHEMA_VERSION = "1"
MIN_EVALS_PER_SKILL = 2


def _skills_with_evals() -> list[Path]:
    return sorted(p for p in SKILLS_DIR.iterdir() if (p / "evals.json").is_file())


def _skills_without_evals() -> list[str]:
    return sorted(
        p.name
        for p in SKILLS_DIR.iterdir()
        if (p / "SKILL.md").is_file() and not (p / "evals.json").is_file()
    )


def test_every_skill_has_evals_file() -> None:
    missing = _skills_without_evals()
    assert not missing, (
        f"Skills missing evals.json: {missing}\n"
        "Every skill must ship 2+ evals (Anthropic skill-creator format)."
    )


@pytest.mark.parametrize("skill_dir", _skills_with_evals(), ids=lambda p: p.name)
def test_evals_json_schema(skill_dir: Path) -> None:
    spec = json.loads((skill_dir / "evals.json").read_text())

    missing_top = REQUIRED_TOP_KEYS - set(spec)
    assert not missing_top, f"{skill_dir.name}: evals.json missing keys {missing_top}"

    assert spec["schema_version"] == SCHEMA_VERSION, (
        f"{skill_dir.name}: schema_version is {spec['schema_version']!r}, "
        f"expected {SCHEMA_VERSION!r} — migrate the file if the format changed."
    )
    assert spec["skill_name"] == skill_dir.name, (
        f"{skill_dir.name}: skill_name {spec['skill_name']!r} != dir name"
    )

    evals = spec["evals"]
    assert isinstance(evals, list), f"{skill_dir.name}: evals must be a list"
    assert len(evals) >= MIN_EVALS_PER_SKILL, (
        f"{skill_dir.name}: only {len(evals)} eval(s); need >= {MIN_EVALS_PER_SKILL}"
    )

    seen_ids: set[int] = set()
    for i, ev in enumerate(evals):
        missing_eval_keys = REQUIRED_EVAL_KEYS - set(ev)
        assert not missing_eval_keys, (
            f"{skill_dir.name}: eval[{i}] missing keys {missing_eval_keys}"
        )
        assert isinstance(ev["id"], int) and ev["id"] > 0, (
            f"{skill_dir.name}: eval[{i}].id must be a positive int"
        )
        assert ev["id"] not in seen_ids, (
            f"{skill_dir.name}: duplicate eval id {ev['id']}"
        )
        seen_ids.add(ev["id"])
        for k in ("prompt", "expected_output"):
            assert isinstance(ev[k], str) and ev[k].strip(), (
                f"{skill_dir.name}: eval[{i}].{k} empty or not a string"
            )
        assert isinstance(ev["files"], list), (
            f"{skill_dir.name}: eval[{i}].files must be a list (use [] if none)"
        )
