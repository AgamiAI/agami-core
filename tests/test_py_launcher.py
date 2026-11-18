"""`scripts/py`: only its first argument selects what runs.

agami-connect pre-approves `py <script>.py *` for the scripts it runs. That is safe only if nothing
after the script name can reach the interpreter: `py connect_resolve.py -c '<code>'` or
`… -m pip install x` must arrive as the script's own argv. And a bare `<name>.py` the plugin doesn't
have must be refused, not looked up in the current folder, where any file of that name would run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "agami" / "scripts"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="bash launcher")


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    """A copy of the launcher with one script that prints its argv."""
    scripts = tmp_path / "plugin" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("py", "_python.sh", "_plugin_version.py"):
        shutil.copy2(SCRIPTS / name, scripts / name)
    (scripts / "echo_argv.py").write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    return scripts


def _py(plugin: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "AGAMI_PYTHON": sys.executable,
        "AGAMI_ARTIFACTS_DIR": str(cwd / "no-artifacts"),
    }
    return subprocess.run(
        ["bash", str(plugin / "py"), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    "tail",
    [
        ["-c", "print('ran code')"],
        ["-m", "pip", "install", "x"],
        ["-m", "execute_sql", "--no-safety"],
        ["/tmp/other.py"],
        ["-X", "importtime", "-c", "print('ran code')"],
    ],
)
def test_arguments_after_the_script_are_the_scripts(plugin, tmp_path, tail):
    out = _py(plugin, tmp_path, "echo_argv.py", *tail)
    assert out.returncode == 0, out.stderr
    # the script's one line, and nothing the interpreter ran from the tail
    assert out.stdout.splitlines() == [json.dumps(tail)]


def test_a_script_the_plugin_lacks_is_refused_not_run_from_the_current_folder(plugin, tmp_path):
    (tmp_path / "planted.py").write_text("print('planted ran')\n")
    out = _py(plugin, tmp_path, "planted.py")
    assert out.returncode == 2
    assert "planted ran" not in out.stdout
    assert "not one of the plugin's scripts" in out.stderr
