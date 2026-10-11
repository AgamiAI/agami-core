"""The skill instructions run on macOS, Linux and Windows alike (#362, part 2).

Claude follows these docs literally, and on Windows (Git Bash) three habits broke: bare `python3`
is often the Microsoft Store stub, a `/tmp` path means different places to Claude's file tools and to
Git Bash, and the `open || xdg-open || start` chain can never reach Windows' `start`. The fixes are
`scripts/py`, the `<artifacts_dir>/local/tmp/` scratch folder and `scripts/open_in_browser`; this
keeps the old habits out of every skill and shared doc.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "agami"
DOCS = sorted([*PLUGIN.glob("skills/*/SKILL.md"), *PLUGIN.glob("shared/*.md")])

# Telling Claude to RUN python3: a plugin script, `-c`, or `-m`. Prose that forbids `python3 -c`, and
# the `tool_paths.python3` setting name, are not commands.
_RUNS_PYTHON3 = re.compile(r'(?<![\w.])python3 (?:"\$AGAMI_PLUGIN_ROOT|-[cm] )')
_FORBIDS = re.compile(r"never[^.\n]{0,60}python3 -c|python3 -c`\)|`python3 -c` —|no `python3 -c`|forbidden", re.I)


def _offending(pattern: re.Pattern, *, allow: re.Pattern | None = None) -> list[str]:
    out = []
    for doc in DOCS:
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not (allow and allow.search(line)):
                out.append(f"{doc.relative_to(REPO_ROOT)}:{n}: {line.strip()[:120]}")
    return out


def test_no_skill_tells_claude_to_run_bare_python3():
    found = _offending(_RUNS_PYTHON3, allow=_FORBIDS)
    assert not found, "Run Python through scripts/py (see shared/invocation-conventions.md):\n" + "\n".join(found)


def test_no_skill_uses_tmp_for_scratch_files():
    found = _offending(re.compile(r"(?<![\w.<>])/tmp/"))
    assert not found, "Use <artifacts_dir>/local/tmp/ for scratch files:\n" + "\n".join(found)


def test_no_skill_inlines_the_open_chain():
    found = _offending(re.compile(r"xdg-open|cmd /c start|command -v start|(?<![\w/])open \""),
                       allow=re.compile(r"\bnever\b", re.I))
    assert not found, "Use scripts/open_in_browser:\n" + "\n".join(found)


def test_the_scan_catches_what_it_claims_to():
    assert _RUNS_PYTHON3.search('python3 "$AGAMI_PLUGIN_ROOT/scripts/x.py"')
    assert _RUNS_PYTHON3.search("`python3 -c 'import yaml'`")
    assert not _RUNS_PYTHON3.search("tool_paths.python3 = $PY")
    assert _FORBIDS.search("(Write tool — never a heredoc/`python3 -c`)")


# --- the scripts themselves -----------------------------------------------------------------------


def _shim(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\necho "ran $0 $*"\ncase "$2" in *agami-python-ok*) echo agami-python-ok ;; esac\n',
                    encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name == "nt", reason="POSIX shims")
def test_py_resolves_like_sm_names_plugin_scripts_and_makes_the_scratch_folder(tmp_path):
    real = _shim(tmp_path / "bin" / "python3")
    (tmp_path / "art").mkdir()   # an artifacts folder agami-connect already set up
    env = {"HOME": str(tmp_path / "home"), "PATH": f"{real.parent}:/usr/bin:/bin",
           "AGAMI_ARTIFACTS_DIR": str(tmp_path / "art")}
    out = subprocess.run(["bash", str(PLUGIN / "scripts" / "py"), "connect_resolve.py", "--x"],
                         env=env, capture_output=True, text=True, check=False).stdout
    assert out.strip() == f"ran {real} -X utf8 {PLUGIN / 'scripts' / 'connect_resolve.py'} --x"
    assert (tmp_path / "art" / "local" / "tmp").is_dir()


def test_sm_and_py_share_one_interpreter_rule():
    for name in ("sm", "py"):
        text = (PLUGIN / "scripts" / name).read_text(encoding="utf-8")
        assert '. "$SCRIPT_DIR/_python.sh"' in text, name
        assert "resolve_python() {" not in text, f"{name} must not carry its own copy"


@pytest.mark.skipif(os.name == "nt", reason="POSIX uname")
def test_open_in_browser_says_where_the_file_is_when_it_cannot_open_it(tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<p>x</p>", encoding="utf-8")
    env = {"PATH": "/nonexistent", "HOME": str(tmp_path)}   # no open, no xdg-open
    done = subprocess.run(["/bin/bash", str(PLUGIN / "scripts" / "open_in_browser"), str(page)],
                          env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 1 and str(page) in done.stderr



def test_the_shell_scripts_keep_unix_line_endings_on_every_checkout():
    scripts = ["sm", "py", "open_in_browser", "_python.sh"]
    attrs = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    for name in scripts:
        assert b"\r" not in (PLUGIN / "scripts" / name).read_bytes(), name
        assert name in attrs or (name.endswith(".sh") and "*.sh text eol=lf" in attrs), name


def test_windows_opens_without_cmd_re_parsing_the_target():
    # cmd.exe would split a URL at `&`; rundll32's handler takes it whole.
    text = (PLUGIN / "scripts" / "open_in_browser").read_text(encoding="utf-8")
    assert 'rundll32.exe url.dll,FileProtocolHandler "$win"' in text and "cmd //c start" not in text



@pytest.mark.skipif(os.name == "nt", reason="POSIX shims")
def test_no_artifacts_folder_is_created_before_one_was_chosen(tmp_path):
    real = _shim(tmp_path / "bin" / "python3")
    env = {"HOME": str(tmp_path / "home"), "PATH": f"{real.parent}:/usr/bin:/bin"}
    subprocess.run(["bash", str(PLUGIN / "scripts" / "py"), "-c", "pass"], env=env, capture_output=True, check=False)
    assert not (tmp_path / "home" / "agami-artifacts").exists()
