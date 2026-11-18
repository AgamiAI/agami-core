"""`sm steps` / `sm progress` read and write files under `<root>/.introspect/`. A link there to a UNC or
device path must be refused from its text, before anything stats or resolves it: on Windows that alone
can open an SMB session and send the person's credentials to the host."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
from semantic_model import steps  # noqa: E402

SYMLINKS = pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="needs POSIX links")
PROBES = ["//uncprobe/share/own.log", "\\\\uncprobe\\share\\own.log"]


def _spy(monkeypatch) -> list:
    touched: list = []
    for mod, names in ((os.path, ("realpath", "exists", "isfile", "isdir", "islink", "lexists")),
                       (os, ("stat", "lstat", "open", "mkdir"))):
        for name in names:
            real = getattr(mod, name)

            def spy(p, *a, _real=real, **k):
                if "uncprobe" in str(p):
                    touched.append(p)
                return _real(p, *a, **k)

            monkeypatch.setattr(mod, name, spy)
    return touched


def _model(tmp_path: Path, target: str, member: str | None) -> Path:
    root = tmp_path / "acme"
    root.mkdir()
    (root / "datasource.yaml").write_text("datasource: acme\n")
    if member is None:
        (root / ".introspect").symlink_to(target)
    else:
        (root / ".introspect").mkdir()
        (root / ".introspect" / member).symlink_to(target)
    return root


@SYMLINKS
@pytest.mark.parametrize("target", PROBES)
@pytest.mark.parametrize("member", [None, "progress.log", "timeline.jsonl", "steps.last"])
def test_a_link_to_a_network_path_under_introspect_is_never_touched(tmp_path, monkeypatch, target, member):
    root = _model(tmp_path, target, member)
    touched = _spy(monkeypatch)
    steps.onboarding_steps(root)
    steps.attempt_progress_line(root, 0.0)
    assert steps.last_progress_line(root / ".introspect" / "progress.log") == ""
    assert steps.read_printed(root) == {}
    steps.remember_printed(root, {}, {"line": "x"})
    try:
        steps.mark(root, "connect", "done")
    except ValueError:
        pass  # refused: the outcome that matters is below
    assert touched == []
