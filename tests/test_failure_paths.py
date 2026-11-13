"""A build's failure reason is posted whole, so the paths in it (the home folder, a temp folder) are
cut to their last component before it reaches the chat. And an OSError whose file name is bytes is
named by its decoded name, with no stray quote.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli, steps  # noqa: E402


@pytest.mark.parametrize("reason, scrubbed", [
    ("/Users/you/agami-artifacts/local/creds.yaml not found", "creds.yaml not found"),
    ("cannot open '/home/you/.pgpass': denied", "cannot open '.pgpass': denied"),
    ("~/agami-artifacts/local/credentials missing", "credentials missing"),
    (r"C:\Users\you\agami\creds.yaml not found", "creds.yaml not found"),
    (r"open C:\creds.yaml failed", "open creds.yaml failed"),
    ("read local/tmp/agami-keep.txt (from the keep list)", "read agami-keep.txt (from the keep list)"),
    (r"in folder\sub\file.txt, stopped.", "in file.txt, stopped."),
    ("/var/folders/x/T/tmp1/ is gone", "tmp1 is gone"),
    ("no tables discovered. If the catalog is locked down, try 1/2 of them",
     "no tables discovered. If the catalog is locked down, try 1/2 of them"),
    ("connection refused", "connection refused"),
    ("/", "/"),
])
def test_paths_in_a_failure_reason_keep_only_their_last_part(reason, scrubbed):
    assert steps.strip_paths(reason) == scrubbed


def _log(root: Path, line: str) -> None:
    log = root / ".introspect" / "progress.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"discovered 3 tables\n{line}\n", encoding="utf-8")


def test_steps_and_progress_post_the_reason_without_the_path(tmp_path, capsys):
    root = tmp_path / "demo"
    _log(root, f"failed: {tmp_path}/local/creds.yaml not found")
    steps.mark(root, "build", "in_progress")
    assert cli.main(["progress", str(root)]) == 0
    line = json.loads(capsys.readouterr().out)["line"]
    assert line == 'The build didn\'t finish: "creds.yaml not found"'
    assert steps.onboarding_steps(root)["problem"] == line
    assert str(tmp_path) not in line


def test_a_bytes_file_name_is_decoded():
    exc = FileNotFoundError(2, "No such file or directory", b"/tmp/x/bytes.yaml")
    assert steps._load_error(exc) == "No such file or directory: 'bytes.yaml'"
