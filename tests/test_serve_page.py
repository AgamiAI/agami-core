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


# --- Saving mode (#651): one post of decisions, the way the password page takes one password -------


def _start_saving(page: Path, out: Path, minutes: str = "30") -> tuple[subprocess.Popen, str]:
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), str(page), "--decisions", str(out), "--minutes", minutes],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline()
    assert line, proc.stderr.read()
    first = json.loads(line)
    assert first["expires_in"] == int(float(minutes) * 60)
    return proc, first["url"]


def _post(
    url: str,
    body: bytes | None = b'{"block": "x"}',
    *,
    origin: str | None = "same",
    ctype: str = "application/json",
    host: str | None = None,
    length: str | None = None,
) -> tuple[int, dict]:
    headers = {"Content-Type": ctype}
    if origin == "same":
        origin = url.split("/", 3)[0] + "//" + url.split("/", 3)[2]
    if origin is not None:
        headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    if length is not None:
        headers["Content-Length"] = length
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {}


@pytest.fixture
def saving(tmp_path):
    page = tmp_path / "review.html"
    page.write_text("<p>review</p>", encoding="utf-8")
    (tmp_path / "beside.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "out" / "decisions.json"
    proc, url = _start_saving(page, out)
    yield proc, url, out
    proc.kill()
    proc.wait()


@pytest.fixture
def saving_here(tmp_path):
    """The same server in this process, so the coverage gate sees each refusal."""
    import threading

    sys.path.insert(0, str(SCRIPT.parent))
    import serve_page

    page = tmp_path / "review.html"
    page.write_text("<p>review</p>", encoding="utf-8")
    (tmp_path / "beside.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "out" / "decisions.json"
    server = serve_page._Server(page.resolve(), out)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class _Running:  # answers like Popen.poll(): None while it still waits for the post
        @staticmethod
        def poll():
            return 0 if server.taken else None

    yield _Running, f"http://127.0.0.1:{server.port}{server.base}", out
    server.shutdown()
    server.server_close()


def test_saving_serves_the_page_under_a_token_on_loopback(saving_here):
    _, url, _ = saving_here
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/[A-Za-z0-9_-]{43}/", url)
    status, body = _get(url)
    assert (status, body) == (200, b"<p>review</p>")
    with urllib.request.urlopen(url, timeout=5) as r:
        assert "connect-src 'self'" in r.headers["Content-Security-Policy"]
        assert r.headers["Referrer-Policy"] == "same-origin"
    base = url.rsplit("/", 2)[0]
    # Nothing else is served: not the page by its name, not a file beside it, not a wrong token.
    for path in ("/review.html", "/beside.json", "/", "/" + "A" * 43 + "/", url.rsplit("/", 2)[1]):
        assert _get(base + ("" if path.startswith("/") else "/") + path)[0] == 404, path


def test_saving_takes_one_post_writes_it_and_exits(saving):
    proc, url, out = saving
    status, body = _post(
        url + "save", json.dumps({"page": "x", "block": "profile: demo\ndone\n"}).encode()
    )
    assert (status, body["ok"]) == (200, True)
    assert proc.wait(timeout=10) == 0
    lines = [json.loads(x) for x in proc.stdout.read().splitlines()]
    assert lines == [{"status": "saved", "decisions": str(out)}]
    assert json.loads(out.read_text()) == {"page": "x", "block": "profile: demo\ndone\n"}
    assert out.stat().st_mode & 0o777 == 0o600


def test_saving_refuses_a_second_post(tmp_path):
    """In-process, so the server is still up when the second post arrives."""
    sys.path.insert(0, str(SCRIPT.parent))
    import serve_page

    page = tmp_path / "p.html"
    page.write_text("x", encoding="utf-8")
    out = tmp_path / "d.json"
    server = serve_page._Server(page.resolve(), out)
    threading_ = __import__("threading")
    threading_.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.port}{server.base}save"
        assert _post(url, b'{"a": 1}')[0] == 200
        status, body = _post(url, b'{"a": 2}')
        assert status == 409 and "already" in body["message"]
        assert (
            _get(f"http://127.0.0.1:{server.port}{server.base}")[0] == 404
        )  # the page is gone too
        assert json.loads(out.read_text()) == {"a": 1}
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"origin": "http://evil.example"}, 403),
        ({"origin": "null"}, 403),
        ({"origin": None}, 403),  # a post with no Origin did not come from the page
        ({"ctype": "text/plain"}, 415),  # a cross-site form can only send these
        ({"ctype": "application/x-www-form-urlencoded"}, 415),
        ({"body": b"not json"}, 400),
        ({"body": b"[1, 2]"}, 400),  # JSON, but not an object
        ({"body": b"\xff\xfe"}, 400),
        ({"body": b"", "length": "0"}, 400),
        ({"body": b"{}", "length": str(2 * 1024 * 1024)}, 413),
    ],
)
def test_saving_refuses_a_bad_post_and_writes_nothing(saving_here, kwargs, code):
    proc, url, out = saving_here
    body = kwargs.pop("body", b'{"block": "x"}')
    assert _post(url + "save", body, **kwargs)[0] == code
    assert not out.exists()
    assert proc.poll() is None  # still waiting for the real one
    assert _post(url + "save", b'{"ok": 1}')[0] == 200  # and takes it


def test_saving_refuses_a_wrong_token_path_or_host_with_a_plain_404(saving_here):
    _, url, out = saving_here
    base, token = url.rstrip("/").rsplit("/", 1)
    for target in (
        f"{base}/{'B' * 43}/save",
        f"{base}/{token}/",
        f"{base}/{token}/save?x=1",
        f"{base}/save",
    ):
        assert _post(target)[0] == 404, target
    assert _post(url + "save", host="evil.example")[0] == 404
    assert _get(url.replace("127.0.0.1", "localhost"))[0] == 200  # its other own name is fine
    assert not out.exists()


def test_saving_expires_and_then_refuses(tmp_path):
    page = tmp_path / "p.html"
    page.write_text("x", encoding="utf-8")
    out = tmp_path / "d.json"
    proc, _ = _start_saving(page, out, minutes="0.01")
    try:
        assert proc.wait(timeout=10) == 0
        assert [json.loads(x) for x in proc.stdout.read().splitlines()] == [{"status": "expired"}]
        assert not out.exists()
    finally:
        proc.kill()


def test_saving_refuses_a_post_that_arrives_after_expiry_in_process(tmp_path, capsys):
    sys.path.insert(0, str(SCRIPT.parent))
    import serve_page

    page = tmp_path / "p.html"
    page.write_text("x", encoding="utf-8")
    out = tmp_path / "d.json"
    server = serve_page._Server(page.resolve(), out)
    __import__("threading").Thread(target=server.serve_forever, daemon=True).start()
    try:
        serve_page._wait_for_decisions(server, 0.05)
        assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"status": "expired"}
        status, body = _post(f"http://127.0.0.1:{server.port}{server.base}save", b'{"a": 1}')
        assert status == 410 and "expired" in body["message"]
        assert not out.exists()
    finally:
        server.shutdown()
        server.server_close()


def test_saving_in_process_reports_the_save(tmp_path, capsys):
    """main() end to end in this process, so the coverage gate sees the saving path."""
    import threading

    sys.path.insert(0, str(SCRIPT.parent))
    import serve_page

    page = tmp_path / "p.html"
    page.write_text("<b>x</b>", encoding="utf-8")
    out = tmp_path / "d.json"
    rc: list[int] = []
    t = threading.Thread(
        target=lambda: rc.append(serve_page.main([str(page), "--decisions", str(out)]))
    )
    t.start()
    url = ""
    for _ in range(100):
        got = capsys.readouterr().out
        if got:
            url = json.loads(got.splitlines()[0])["url"]
            break
        threading.Event().wait(0.05)
    assert _post(url + "save", b'{"x": true}')[0] == 200
    t.join(timeout=10)
    assert rc == [0]
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {
        "status": "saved",
        "decisions": str(out),
    }


def test_saving_reports_a_write_failure_and_stays_open(tmp_path):
    sys.path.insert(0, str(SCRIPT.parent))
    import serve_page

    page = tmp_path / "p.html"
    page.write_text("x", encoding="utf-8")
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    server = serve_page._Server(page.resolve(), blocker / "d.json")  # its folder is a file
    __import__("threading").Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.port}{server.base}save"
        assert _post(url, b'{"a": 1}')[0] == 500
        assert not server.taken  # a fixed folder could still take the post
    finally:
        server.shutdown()
        server.server_close()


def test_view_only_mode_still_refuses_every_post(served):
    _, url = served
    port = url.split(":")[2].split("/")[0]
    assert _post(f"http://127.0.0.1:{port}/report.html")[0] == 405
