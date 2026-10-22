"""The server's CA bundle and the image's pip are floored past the eMudhra (emSign) distrust.

Those intermediates' private keys were weak enough to forge a certificate for any host, and both
places a fix has to reach are easy to regress silently: a dependency line someone tidies away, and
an upgrade step that a Dockerfile rewrite drops. Neither fails a build when it goes.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "packages" / "agami-core" / "pyproject.toml"
DOCKERFILE = REPO / "deploy" / "Dockerfile"

CERTIFI_FLOOR = (2026, 10, 19)
PIP_FLOOR = (26, 2, 2)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def test_the_server_extra_floors_certifi_at_the_distrusting_release():
    # A text scan rather than tomllib, which 3.10 does not have.
    server = re.search(r"^server = \[(.*?)^\]", PYPROJECT.read_text(), re.M | re.S)
    assert server, "the server extra is gone"
    floor = re.search(r'"certifi>=([\d.]+)"', server.group(1))
    assert floor, "the server extra must declare a certifi floor"
    assert _version(floor.group(1)) >= CERTIFI_FLOOR


def test_the_image_upgrades_pip_from_a_hash_pinned_requirement_before_installing():
    text = DOCKERFILE.read_text()
    pinned = re.search(r"pip==([\d.]+) --hash=sha256:[0-9a-f]{64}", text)
    assert pinned, "pip must be upgraded from a hash-pinned requirement"
    assert _version(pinned.group(1)) >= PIP_FLOOR
    assert "--require-hashes" in text
    # Upgrading after the install would leave every package fetched by the affected pip.
    assert pinned.start() < text.index("/app/packages/agami-core[")
