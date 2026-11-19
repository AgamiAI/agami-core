"""`sm progress` speaks for this build attempt only, and `introspect` logs its own early failures.

A rebuild marks `build=in_progress` and starts the engine in the background. Until the engine writes,
`.introspect/progress.log` still holds the last attempt's end, `done: …` or `failed: …`, and
`sm progress --wait 20` read that as this build finished (or failed) at once. A log older than the
standing build mark is now no log, the same rule `sm steps` already applied. And `introspect`
failing before the engine started (a missing `--tables-file`, a bad `--batch`) wrote nothing, so the
skill's progress loop waited out its 30-minute silence rule.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli  # noqa: E402


def _run(argv: list[str]) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert cli.main(argv) == 0
    return json.loads(buf.getvalue())


def _old_log(root: Path, last: str) -> Path:
    log = root / ".introspect" / "progress.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"2026-11-01T10:00:00Z tables 3/3\n2026-11-01T10:00:05Z {last}\n")
    old = time.time() - 600
    os.utime(log, (old, old))
    return log


@pytest.mark.parametrize("last", ["done: 3 tables, 2 joins", "failed: no such table: orders"])
def test_an_earlier_attempts_log_is_not_this_builds(tmp_path, last):
    root = tmp_path / "acme"
    _old_log(root, last)
    _run(["steps", str(root), "--mark", "build=in_progress"])
    started = time.monotonic()
    out = _run(["progress", str(root), "--wait", "1"])
    assert out["done"] is False and out["failed"] is False
    assert out["started"] is False and out["line"] == ""
    assert time.monotonic() - started >= 0.9  # it waited for this build instead of returning


def test_the_log_counts_once_this_build_writes_to_it(tmp_path):
    root = tmp_path / "acme"
    log = _old_log(root, "done: 3 tables, 2 joins")
    _run(["steps", str(root), "--mark", "build=in_progress"])
    with log.open("a") as fh:
        fh.write("2026-11-18T10:00:00Z columns+grain 1/4\n")
    out = _run(["progress", str(root)])
    assert out["started"] is True and out["n"] == 1 and out["total"] == 4


def test_with_no_build_mark_the_log_is_read_as_it_is(tmp_path):
    root = tmp_path / "acme"
    _old_log(root, "done: 3 tables, 2 joins")
    assert _run(["progress", str(root)])["done"] is True


def _introspect(art: Path, *extra: str) -> list[str]:
    return [
        "introspect",
        "--profile",
        "acme",
        "--db-type",
        "sqlite",
        "--artifacts",
        str(art),
        *extra,
    ]


def _last_line(art: Path) -> str:
    text = (art / "acme" / ".introspect" / "progress.log").read_text()
    return text.strip().splitlines()[-1]


def test_a_missing_tables_file_ends_the_log_with_failed(tmp_path):
    art = tmp_path / "art"
    with pytest.raises(FileNotFoundError):
        cli.main(_introspect(art, "--tables-file", str(tmp_path / "agami-keep.txt")))
    assert " failed: " in _last_line(art)


def test_a_bad_batch_ends_the_log_with_failed(tmp_path):
    art = tmp_path / "art"
    with pytest.raises(SystemExit):
        cli.main(_introspect(art, "--batch", "5/3"))
    assert "failed: --batch takes n/total" in _last_line(art)


def test_a_refused_tables_file_ends_the_log_with_failed(tmp_path, monkeypatch):
    art = tmp_path / "art"
    (art / "local" / "tmp").mkdir(parents=True)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    monkeypatch.setenv("AGAMI_SCRATCH_ONLY", "1")
    (art / "local" / "tmp" / "agami-keep.txt").symlink_to(tmp_path / "elsewhere.txt")
    (tmp_path / "elsewhere.txt").write_text("orders\n")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cli.main(
            _introspect(art, "--tables-file", str(art / "local" / "tmp" / "agami-keep.txt"))
        )
    assert rc == 2
    assert " failed: " in _last_line(art)
