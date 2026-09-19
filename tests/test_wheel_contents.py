"""The wheel really contains the data the installed server reads (#122).

**Why this cannot be tested any other way.** Every other test in this suite runs against an
*editable* install, where `migrations/` and `static/` resolve into the source tree — which exists
whatever `pyproject.toml` packages. So the packaging configuration is the one part of this repo that
the entire suite is structurally blind to, and it has already shipped broken once:

  * `migrations/` was missing from the wheel, so `MIGRATIONS_DIR` globbed to nothing and **the
    server booted on an empty schema without an error**;
  * `static/` was missing, so building the app raised "Directory does not exist".

Neither reproduces in a checkout, and neither reproduces in the Docker deploy, which installs with
`-e`. They reproduce only in a real wheel install — which is what a user gets.

**So this test builds the wheel and looks inside it.** Not "is the config still spelled the way we
left it" — a config assertion passes whenever the format means something new, which is a class of
drift this repo has no other guard against. The artifact is the thing that broke, so the artifact is
the thing asserted.

The build takes about a second, which is the entire reason this is an ordinary test rather than a
release-time check nobody runs.

**What is NOT asserted here:** that the files are correct, complete or in a working order. That is
every other test's job. This one answers exactly one question — did they get into the wheel — and a
wider claim here would duplicate cover that already exists and make the file harder to trust.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "packages" / "agami-core"

# The two directories the flat modules resolve as `Path(__file__).parent / …`, so they have to land
# NEXT TO those modules in site-packages rather than under a parent package. Each is paired with a
# file that must be inside it: a directory entry alone would satisfy a membership test while the
# contents were empty, which is the exact shape of the migrations failure (a glob over nothing).
REQUIRED = {
    "migrations/core/001_serving.sql": "the core schema — without it the server boots on an empty database",
    "static/logo_h.svg": "the brand assets served at /static — without them building the app raises",
}


# Missing entirely is a skip, because a contributor running a bare `pytest` without the test
# dependencies should not get a red suite. A build that RUNS AND FAILS is not — see the fixture.
build = pytest.importorskip("build", reason="the test dependencies include `build`; see dev.py")


@pytest.fixture(scope="session")
def wheel_names(tmp_path_factory: pytest.TempPathFactory) -> frozenset[str]:
    """Every path inside a freshly built wheel.

    **Built from a COPY of the package, not from the tree.** setuptools writes `build/` and
    `*.egg-info/` beside the sources, so two builds of one tree at the same moment overwrite each
    other's intermediates — and this suite runs under `-n auto`. The first version of this test did
    build in place, and under xdist three of its four cases skipped with a clobbered-metadata error
    while one passed. A guard that can skip quietly is the same as no guard, which is the whole
    complaint in #122, so this one is isolated and cannot skip for that reason.

    **A failed build FAILS.** The only skip is `build` not being importable at all, decided above.
    """
    src = tmp_path_factory.mktemp("pkg") / "agami-core"
    shutil.copytree(
        PACKAGE_DIR,
        src,
        ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__", "*.pyc"),
    )
    out = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out), str(src)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "the wheel did not build, so the packaging this file exists to guard is unverified:\n"
        + result.stderr.strip()[-2000:]
    )
    built = sorted(out.glob("*.whl"))
    assert len(built) == 1, f"expected one wheel, built {[p.name for p in built]}"
    with zipfile.ZipFile(built[0]) as wheel:
        return frozenset(wheel.namelist())


@pytest.mark.parametrize("path,why", sorted(REQUIRED.items()))
def test_the_wheel_ships_the_data_the_server_reads(path: str, why: str, wheel_names) -> None:
    assert path in wheel_names, (
        f"{path} is missing from the built wheel — {why}. "
        "Check `packages` and `package-data` in packages/agami-core/pyproject.toml; "
        "an editable install hides this because the source tree satisfies the path either way."
    )


def test_the_migrations_in_the_wheel_are_the_ones_on_disk(wheel_names) -> None:
    """Every migration, not merely the first one.

    A `package-data` glob that stopped matching — a new subdirectory, a renamed suffix — would leave
    the file above present and later ones absent, and the server would migrate part of the way and
    report success. So the wheel's set is compared against the source tree's.
    """
    on_disk = {
        f"migrations/{p.relative_to(PACKAGE_DIR / 'src' / 'migrations').as_posix()}"
        for p in (PACKAGE_DIR / "src" / "migrations").rglob("*.sql")
    }
    assert on_disk, "no migrations found on disk — this test is looking in the wrong place"
    missing = sorted(on_disk - wheel_names)
    assert not missing, f"{len(missing)} migration(s) never reached the wheel: {missing[:5]}"


def test_the_flat_modules_are_not_nested_under_a_package(wheel_names) -> None:
    """`store.py` and its siblings must sit at the wheel's root.

    The data directories above are resolved RELATIVE to these modules, so nesting the modules one
    level deeper moves both of them out from under their own lookups — the same failure as omitting
    them, arriving by a different route and with the files still visibly present in the wheel.
    """
    assert "store.py" in wheel_names
    assert "mcp_http.py" in wheel_names
