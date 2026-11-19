"""Under `AGAMI_SCRATCH_ONLY=1` the two decision parsers read only agami's scratch files.

The guard already held each parser's output to the scratch folder; `--decisions-file` and
`--items-file` are paths from the call too, so a call steered by text in the data could otherwise make
the parser read, and quote the failure of, any file the person owns."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "agami" / "scripts"
MARK = "zz-private-marker"


@pytest.fixture
def art(tmp_path, monkeypatch) -> Path:
    art = tmp_path / "art"
    (art / "local" / "tmp").mkdir(parents=True)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    return art


@pytest.fixture
def private(tmp_path) -> Path:
    p = tmp_path / "home" / "notes.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"note": MARK}))
    return p


def _scratch(art: Path, name: str, doc: object) -> Path:
    p = art / "local" / "tmp" / name
    p.write_text(json.dumps(doc))
    return p


def _cmd(script: str, art: Path, decisions: Path, items: Path, with_out: bool) -> list[str]:
    scratch = str(art / "local" / "tmp")
    if script == "examples":
        args = ["parse_examples_decisions.py", "--decisions-file", str(decisions),
                "--items-file", str(items), "--expect-profile", "acme"]
        return args + (["--out-dir", scratch] if with_out else [])
    return ["parse_open_questions_decisions.py", "--decisions-file", str(decisions),
            "--items-file", str(items), "--expect-profile", "acme", "--artifacts-dir", str(art),
            "--ops-out", f"{scratch}/agami-ops.json"]


def _run(art: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art), "AGAMI_SCRATCH_ONLY": "1"}
    return subprocess.run([sys.executable, str(SCRIPTS / args[0]), *args[1:]], capture_output=True,
                          text=True, timeout=60, env=env)


def _refused(proc: subprocess.CompletedProcess) -> bool:
    said = proc.stdout + proc.stderr
    return proc.returncode == 2 and "scratch" in said and MARK not in said


CASES = [(s, w) for s in ("examples", "open_questions") for w in (True, False)]


@pytest.mark.parametrize("script,with_out", CASES)
@pytest.mark.parametrize("which", ["decisions", "items"])
def test_an_input_outside_scratch_is_refused_unread(art, private, script, with_out, which):
    ok = _scratch(art, "agami-ok.json", [])
    d, i = (private, ok) if which == "decisions" else (ok, private)
    if script == "open_questions" and not with_out:
        pytest.skip("open_questions always has --ops-out")
    proc = _run(art, *_cmd(script, art, d, i, with_out))
    assert _refused(proc), (proc.stdout, proc.stderr)


@pytest.mark.parametrize("script,with_out", CASES)
@pytest.mark.parametrize("which", ["decisions", "items"])
def test_a_link_in_scratch_to_a_private_file_is_refused_unread(art, private, script, with_out, which):
    if script == "open_questions" and not with_out:
        pytest.skip("open_questions always has --ops-out")
    link = art / "local" / "tmp" / "agami-planted.json"
    link.symlink_to(private)
    ok = _scratch(art, "agami-ok.json", [])
    d, i = (link, ok) if which == "decisions" else (ok, link)
    proc = _run(art, *_cmd(script, art, d, i, with_out))
    assert proc.returncode == 2 and MARK not in proc.stdout + proc.stderr, (proc.stdout, proc.stderr)
    assert "link" in proc.stdout + proc.stderr


def test_scratch_inputs_still_parse_without_an_out_dir(art):
    items = _scratch(art, "agami-items.json", [{"area": "sales", "question": "q", "sql": "select 1"}])
    decisions = _scratch(art, "agami-decisions.json",
                         {"page": "examples", "profile": "acme",
                          "decisions": [{"n": 1, "area": "sales", "question": "q", "action": "keep"}]})
    proc = _run(art, *_cmd("examples", art, decisions, items, with_out=False))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)["ok"] is True
