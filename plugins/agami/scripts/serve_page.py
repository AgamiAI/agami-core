#!/usr/bin/env python3
"""Serve one HTML page, and the files beside it, from localhost for a short while.

    serve_page.py <file.html> [--minutes 30]
    serve_page.py <file.html> --decisions <out.json> [--minutes 30]

Without `--decisions` it prints one JSON line, {"url": "http://localhost:<port>/<name>"}, then answers
requests until the time is up and exits 0. This is the view-only "full view" behind a question card:
a page is never opened as file://, because the Code tab's built-in browser shows a local file outside
the project folder as a frozen snapshot. Every POST is refused.

With `--decisions` it serves a page that saves its own decisions (rendered `--save-to-helper`), the
way the password page saves a password. It prints
    {"url": "http://127.0.0.1:<port>/<token>/", "expires_in": <seconds>}     once listening
and then exactly one of
    {"status": "saved", "decisions": "<out.json>"}     the page posted its decisions; they are in the file
    {"status": "expired"}                              no post before the time was up; nothing written
and exits 0. It accepts one POST to `/<token>/save`: a JSON object of at most 1 MiB, sent by the page
itself (its Origin is this server). Anything else is refused: a wrong token or path, a request
addressed to another host name, a post from another origin, a body that is not JSON, too large or
missing, a second post, and any post after the time is up. It writes the decisions file (0600) and
nothing else: applying them is the skill's job, through the same commands a card uses.

The server binds 127.0.0.1 only, on a random port. It serves the page and, beside it (view-only mode),
regular files with a web-asset extension (html css js svg png jpg json) and nothing else, and it
refuses any request whose Host is not this server's own address (a DNS-rebinding page cannot reach it
under its own name). Stdlib only.
"""

from __future__ import annotations

import argparse
import http.server
import json
import mimetypes
import os
import secrets
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

HOST = "127.0.0.1"
# The page itself is always served; beside it, only the kinds of file a report page loads.
_ALLOWED_SUFFIXES = {".html", ".css", ".js", ".svg", ".png", ".jpg", ".json"}
_REQUEST_TIMEOUT = 10  # a client that stalls mid-request is dropped rather than held open
MAX_DECISIONS = (
    1024 * 1024
)  # a whole review page's queue, edited descriptions included, is far smaller
# A saving page runs its own inline script and posts back to this server, and loads nothing else.
_SAVING_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
    "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True  # a stalled client cannot keep the process alive past its deadline
    block_on_close = False

    def __init__(self, page: Path, decisions: Path | None = None) -> None:
        super().__init__((HOST, 0), _Handler)
        self.root = page.parent
        self.port = self.server_address[1]
        self.page = page
        self.hosts = {f"localhost:{self.port}", f"{HOST}:{self.port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        # Saving mode: the page lives under a random token, and one post of decisions is taken.
        self.decisions = decisions
        self.base = f"/{secrets.token_urlsafe(32)}/"
        self.taken = False  # set under `lock` by the one post that is accepted
        self.closed = False  # set under `lock` when the time is up; no post is taken after it
        self.lock = threading.Lock()
        self.done = threading.Event()  # set once the accepted post has had its answer

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a dropped or timed-out connection is not news


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _Server
    timeout = _REQUEST_TIMEOUT

    def log_message(self, *args: Any) -> None:
        pass  # in saving mode the request line carries the token; nothing about a request is logged

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.server.decisions is not None:
            self.send_header("Content-Security-Policy", _SAVING_CSP)
            # Not no-referrer: under that policy a browser sends `Origin: null` with the page's post,
            # which the Origin check refuses. Same-origin sends the token to no other site.
            self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _host_ok(self) -> bool:
        return self.headers.get("Host") in self.server.hosts

    def _at(self, path: str) -> bool:
        """Whether the request is for exactly `path`, compared in constant time (it carries the token)."""
        return secrets.compare_digest(self.path.encode("utf-8", "surrogateescape"), path.encode())

    def _file(self) -> Path | None:
        if not self._host_ok():
            return None
        if self.server.decisions is not None:
            live = not (self.server.taken or self.server.closed)
            return self.server.page if live and self._at(self.server.base) else None
        name = unquote(urlsplit(self.path).path.lstrip("/"))
        # One path segment only: no separators, no "..", so nothing outside the folder is nameable.
        # ":" would be a drive-relative name on Windows; a leading "." is a dotfile or "..".
        if not name or name.startswith(".") or any(c in name for c in "/\\:\0"):
            return None
        target = self.server.root / name
        if target.name != self.server.page.name and target.suffix.lower() not in _ALLOWED_SUFFIXES:
            return None
        if (
            not target.is_file()
            or target.is_symlink()
            or target.resolve().parent != self.server.root
        ):
            return None
        return target

    def _serve(self) -> None:
        target = self._file()
        if target is None:
            return self._send(404, b"Not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if self.server.decisions is not None:
            ctype = "text/html"  # the one page, whatever its name
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        try:
            self._send(200, target.read_bytes(), ctype)
        except OSError:
            self._send(404, b"Not found")

    do_GET = do_HEAD = _serve

    def _refuse(self) -> None:
        self._send(405, b"Method not allowed")

    do_PUT = do_DELETE = do_PATCH = _refuse

    def _answer(self, code: int, message: str) -> None:
        self._send(
            code, json.dumps({"ok": code == 200, "message": message}).encode(), "application/json"
        )

    def do_POST(self) -> None:  # noqa: N802
        srv = self.server
        if srv.decisions is None:
            return self._refuse()
        # Before the token is checked, every refusal is the same 404: nothing is said to a caller
        # that does not hold the link.
        if not self._host_ok() or not self._at(srv.base + "save"):
            return self._send(404, b"Not found")
        if self.headers.get("Origin") not in srv.origins:
            return self._answer(403, "This page can only be saved from itself.")
        if (self.headers.get("Content-Type") or "").split(";")[
            0
        ].strip().lower() != "application/json":
            return self._answer(415, "The decisions must be sent as JSON.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_DECISIONS:
            return self._answer(413, "Too many decisions to save at once.")
        if length <= 0:
            return self._answer(400, "Nothing was sent.")
        if srv.taken or srv.closed:
            return self._answer(409 if srv.taken else 410, _GONE[srv.taken])
        try:
            body = self.rfile.read(length)
        except OSError:  # the client stopped sending before Content-Length bytes arrived
            self.close_connection = True
            return
        try:
            decisions = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            decisions = None
        if not isinstance(decisions, dict):
            return self._answer(400, "The decisions are not a JSON object.")
        with srv.lock:
            # A second post that raced the first, or one that raced the expiry.
            if srv.taken or srv.closed:
                return self._answer(409 if srv.taken else 410, _GONE[srv.taken])
            try:
                _write_private(srv.decisions, json.dumps(decisions, indent=2))
            except OSError:
                return self._answer(500, "The decisions could not be written. Nothing was saved.")
            srv.taken = True
        try:
            self._answer(200, "Saved.")
        finally:
            srv.done.set()


_GONE = {
    True: "These decisions were already saved; this page saves once.",
    False: "This page has expired; nothing was saved.",
}


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` at 0600 in one step (a temp file, then a rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj), flush=True)


def _wait_for_decisions(server: _Server, seconds: float) -> None:
    _emit({"url": f"http://{HOST}:{server.port}{server.base}", "expires_in": int(seconds)})
    if not server.done.wait(seconds):
        # Closed under the lock, so a post either landed first (and is reported) or is refused.
        with server.lock:
            server.closed = not server.taken
        if server.closed:
            _emit({"status": "expired"})
            return
        server.done.wait(_REQUEST_TIMEOUT)  # the accepted post is still sending its answer
    _emit({"status": "saved", "decisions": str(server.decisions)})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Serve one page from localhost for a short while.")
    p.add_argument(
        "page", help="The HTML file to serve; files beside it are served too (view-only)."
    )
    p.add_argument(
        "--minutes", type=float, default=30.0, help="How long to keep serving (default 30)."
    )
    p.add_argument(
        "--decisions",
        help="Take one post of decisions from the page and write it to this file, then exit.",
    )
    args = p.parse_args(argv)
    page = Path(args.page).expanduser().resolve()
    if not page.is_file():
        sys.stderr.write(f"serve_page: no such file: {page}\n")
        return 1
    decisions = Path(args.decisions).expanduser().resolve() if args.decisions else None
    server = _Server(page, decisions)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    seconds = max(args.minutes, 0) * 60
    try:
        if decisions is not None:
            _wait_for_decisions(server, seconds)
        else:
            _emit({"url": f"http://localhost:{server.port}/{quote(page.name)}"})
            worker.join(seconds)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
