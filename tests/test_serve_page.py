"""serve_page.py: a short-lived loopback server for one page and the files beside it."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "plugins" / "agami" / "scripts" / "serve_page.py"


def _start(page: Path, minutes: str = "30") -> tuple[subprocess.Popen, str]:
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), str(page), "--minutes", minutes],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline()
    assert line, proc.stderr.read()
    return proc, json.loads(line)["url"]


@pytest.fixture
def served(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "report.html").write_text("<p>hello</p>", encoding="utf-8")
    (root / "app.js").write_text("var x=1;", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    proc, url = _start(root / "report.html")
    yield proc, url
    proc.kill()
    proc.wait()


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def test_serves_the_file_on_loopback_only(served):
    _, url = served
    assert url.startswith("http://localhost:") and url.endswith("/report.html")
    port = url.split(":")[2].split("/")[0]
    status, body = _get(f"http://127.0.0.1:{port}/report.html")
    assert (status, body) == (200, b"<p>hello</p>")
    assert _get(f"http://127.0.0.1:{port}/app.js")[0] == 200  # a file beside the page


def test_binds_only_the_loopback_interface(served):
    proc, url = served
    port = url.split(":")[2].split("/")[0]
    out = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(proc.pid), "-iTCP", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
    ).stdout
    if not out:
        pytest.skip("lsof unavailable")
    assert f"127.0.0.1:{port}" in out
    assert "*:" not in out


def test_refuses_paths_outside_its_folder(served):
    _, url = served
    port = url.split(":")[2].split("/")[0]
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/missing.html"):
        assert _get(f"http://127.0.0.1:{port}{path}")[0] == 404, path


def test_refuses_a_foreign_host_header(served):
    _, url = served
    port = url.split(":")[2].split("/")[0]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/report.html", headers={"Host": "evil.example"}
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 404


def test_exits_after_timeout(tmp_path):
    page = tmp_path / "p.html"
    page.write_text("x", encoding="utf-8")
    proc, _ = _start(page, minutes="0.01")  # 0.6 seconds
    try:
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()


def test_missing_page_is_an_error(tmp_path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "nope.html")], capture_output=True, text=True
    )
    assert r.returncode != 0
    assert "nope.html" in r.stderr


def test_in_process_serves_then_returns(tmp_path, capsys):
    """The same behaviour with main() called directly, so the coverage gate can see the lines."""
    import threading

    sys.path.insert(0, str(SCRIPT.parent))
    import serve_page

    page = tmp_path / "p.html"
    page.write_text("<b>x</b>", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    rc: list[int] = []
    t = threading.Thread(
        target=lambda: rc.append(serve_page.main([str(page), "--minutes", "0.05"]))
    )
    t.start()
    url = ""
    for _ in range(100):
        out = capsys.readouterr().out
        if out:
            url = json.loads(out.splitlines()[0])["url"]
            break
        threading.Event().wait(0.05)
    port = url.split(":")[2].split("/")[0]
    base = f"http://127.0.0.1:{port}"
    assert _get(url.replace("localhost", "127.0.0.1"))[0] == 200
    assert _get(f"{base}/sub")[0] == 404  # a folder is not a file
    assert _get(f"{base}/")[0] == 404
    req = urllib.request.Request(f"{base}/p.html", data=b"x", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 405
    t.join(timeout=10)
    assert rc == [0]
    assert serve_page.main([str(tmp_path / "gone.html")]) == 1


def test_only_the_page_and_web_assets_are_served(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "report.data").write_text("the page itself, odd suffix", encoding="utf-8")
    for name in ("a.css", "notes.txt", "key.pem", ".hidden.json", "x.db"):
        (root / name).write_text("x", encoding="utf-8")
    proc, url = _start(root / "report.data")
    try:
        port = url.split(":")[2].split("/")[0]
        base = f"http://127.0.0.1:{port}"
        assert _get(f"{base}/report.data")[0] == 200  # the page, whatever its suffix
        assert _get(f"{base}/a.css")[0] == 200
        for refused in ("notes.txt", "key.pem", "x.db"):  # not an allowlisted extension
            assert _get(f"{base}/{refused}")[0] == 404, refused
        assert _get(f"{base}/.hidden.json")[0] == 404  # dotfile
        assert _get(f"{base}/C:a.css")[0] == 404  # drive-relative on Windows
        assert _get(f"{base}/a.css:stream.css")[0] == 404
    finally:
        proc.kill()
        proc.wait()


def test_a_symlink_out_of_the_folder_is_refused(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "p.html").write_text("x", encoding="utf-8")
    (tmp_path / "outside.json").write_text("secret", encoding="utf-8")
    (root / "link.json").symlink_to(tmp_path / "outside.json")
    proc, url = _start(root / "p.html")
    try:
        port = url.split(":")[2].split("/")[0]
        assert _get(f"http://127.0.0.1:{port}/link.json")[0] == 404
    finally:
        proc.kill()
        proc.wait()


def test_every_header_quoted_in_the_conventions_doc_fits_a_card():
    doc = SCRIPT.parent.parent / "shared" / "question-cards.md"
    headers = re.findall(r'"header":\s*"([^"]*)"', doc.read_text(encoding="utf-8"))
    assert headers
    assert all(len(h) <= 12 for h in headers), [h for h in headers if len(h) > 12]


def test_the_printed_link_reaches_a_page_whose_name_needs_escaping(tmp_path):
    # `#` and `?` in a file name would otherwise end the path, and the browser would get a 404.
    page = tmp_path / "sales#1 ?.html"
    page.write_text("<p>ok</p>", encoding="utf-8")
    proc, url = _start(page)
    try:
        status, body = _get(url)
        assert status == 200 and b"ok" in body
    finally:
        proc.kill()
        proc.wait()
