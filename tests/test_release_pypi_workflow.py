"""OCR-032 guard: the PyPI release workflow publishes via TRUSTED PUBLISHING and carries NO API token.

The workflow itself (`.github/workflows/release-pypi.yml`) isn't ruff/pytest-covered — it only runs on a
GitHub release — so this test locks the invariants OCR-032 promises: OIDC `id-token: write` (not a stored
PyPI token), the `pypa/gh-action-pypi-publish` action, a build from `packages/agami-core`, and the
release-vs-dispatch (PyPI vs TestPyPI) split. A change that reintroduces an API-token publish path or
points the build elsewhere fails here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release-pypi.yml"


def _load():
    wf = yaml.safe_load(WORKFLOW.read_text())
    # YAML 1.1 parses the bare key `on:` as the boolean True (the "Norway problem"), so the trigger block
    # lives under wf[True], not wf["on"].
    triggers = wf.get("on", wf.get(True))
    return wf, triggers


def test_triggers_are_release_and_dispatch():
    _, triggers = _load()
    assert "release" in triggers, triggers
    assert "workflow_dispatch" in triggers, triggers
    assert triggers["release"]["types"] == ["published"], triggers


def test_trusted_publishing_no_api_token():
    wf, _ = _load()
    job = wf["jobs"]["publish"]
    # OIDC token minted per-run is what replaces a stored PyPI API token.
    assert job["permissions"].get("id-token") == "write", job["permissions"]
    assert job["permissions"].get("contents") == "read", job["permissions"]

    raw = WORKFLOW.read_text()
    # No API-token publish path: the pypa action must never be handed a password, and no secret is
    # referenced anywhere in the workflow (trusted publishing needs none).
    assert "password:" not in raw, "publish must not use a password/API token"
    assert "secrets." not in raw, "no repo secret should be referenced (trusted publishing is tokenless)"

    publish_steps = [s for s in job["steps"] if "gh-action-pypi-publish" in str(s.get("uses", ""))]
    assert publish_steps, "no pypa/gh-action-pypi-publish step found"


def test_builds_from_agami_core_package():
    wf, _ = _load()
    steps = wf["jobs"]["publish"]["steps"]
    build = next((s for s in steps if "python -m build" in str(s.get("run", ""))), None)
    assert build is not None, "no `python -m build` step"
    assert "packages/agami-core" in build["run"], build["run"]


def test_release_goes_to_pypi_dispatch_to_testpypi():
    wf, _ = _load()
    steps = wf["jobs"]["publish"]["steps"]
    publish = [s for s in steps if "gh-action-pypi-publish" in str(s.get("uses", ""))]

    dispatch = [s for s in publish if s.get("if") == "github.event_name == 'workflow_dispatch'"]
    release = [s for s in publish if s.get("if") == "github.event_name == 'release'"]
    assert len(dispatch) == 1, "expected one workflow_dispatch (TestPyPI) publish step"
    assert len(release) == 1, "expected one release (PyPI) publish step"

    # Dispatch → TestPyPI; release → default index (no repository-url override = real PyPI).
    assert "test.pypi.org" in str(dispatch[0].get("with", {}).get("repository-url", "")), dispatch[0]
    assert "repository-url" not in (release[0].get("with") or {}), release[0]
