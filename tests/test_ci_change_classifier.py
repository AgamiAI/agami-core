"""The `what changed` classifier in ci.yml decides whether a PR runs its tests.

It is shell inside YAML, so nothing else executes it: a quoting or regex slip could quietly mark a code
PR `tests=false`, and the required `lint + test` checks would then pass without running anything. These
run the script itself, the way the runner does, against a throwaway repository.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

VERSIONED = {
    ".claude-plugin/marketplace.json": json.dumps(
        {"metadata": {"version": "1.0.0"}, "plugins": [{"name": "demo", "version": "1.0.0"}]},
        indent=2,
    ),
    "plugins/agami/.claude-plugin/plugin.json": json.dumps(
        {"name": "demo", "version": "1.0.0"}, indent=2
    ),
    "packages/agami-core/pyproject.toml": '[project]\nname = "demo"\nversion = "1.0.0"\n',
}


def _classifier() -> str:
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["changes"]["steps"]
    return next(step for step in steps if step.get("id") == "classify")["run"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=ci", "-c", "user.email=ci@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    base = _commit(
        root,
        {
            **VERSIONED,
            "CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n",
            "packages/agami-core/src/tools.py": "x = 1\n",
        },
    )
    return root, base


def _classify(
    repo: Path, base: str, head: str, *, event: str = "pull_request", draft: str = "false"
) -> str:
    output = repo.parent / "github_output"
    output.write_text("")
    env = {
        **os.environ,
        "EVENT": event,
        "DRAFT": draft,
        "BASE": base,
        "HEAD": head,
        "GITHUB_OUTPUT": str(output),
    }
    # The runner's own shell flags for `run:` steps, so a pipeline that fails there fails here too.
    subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _classifier()],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return output.read_text().strip()


def _bumped() -> dict[str, str]:
    return {path: text.replace("1.0.0", "1.0.1") for path, text in VERSIONED.items()}


def test_a_push_to_main_always_runs_the_tests(repo):
    root, base = repo
    assert _classify(root, base, base, event="push") == "tests=true"


def test_a_draft_runs_no_tests(repo):
    root, base = repo
    head = _commit(root, {"packages/agami-core/src/tools.py": "x = 2\n"})
    assert _classify(root, base, head, draft="true") == "tests=false"


def test_a_code_change_runs_the_tests(repo):
    root, base = repo
    head = _commit(root, {"packages/agami-core/src/tools.py": "x = 2\n"})
    assert _classify(root, base, head) == "tests=true"


def test_a_release_pr_runs_no_tests(repo):
    root, base = repo
    head = _commit(root, {**_bumped(), "CHANGELOG.md": "# Changelog\n\n## [1.0.1]\n"})
    assert _classify(root, base, head) == "tests=false"


@pytest.mark.parametrize(
    "files",
    [
        pytest.param(
            {"CHANGELOG.md": "# Changelog\n\n## [1.0.1]\n"}, id="changelog_without_a_bump"
        ),
        pytest.param(
            {**_bumped(), "packages/agami-core/src/tools.py": "x = 2\n"},
            id="release_files_beside_code",
        ),
        pytest.param(
            {
                **_bumped(),
                "packages/agami-core/pyproject.toml": (
                    '[project]\nname = "demo"\nversion = "1.0.1"\ndependencies = ["sqlglot"]\n'
                ),
            },
            id="a_non_version_line_in_a_versioned_file",
        ),
    ],
)
def test_anything_short_of_a_pure_release_runs_the_tests(repo, files):
    root, base = repo
    head = _commit(root, files)
    assert _classify(root, base, head) == "tests=true"
