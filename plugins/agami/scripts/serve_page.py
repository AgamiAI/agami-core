#!/usr/bin/env python3
"""Serve one HTML page, and the files beside it, from localhost for a short while.

    serve_page.py <file.html> [--minutes 30] [--open]

Prints one JSON line, {"url": "http://localhost:<port>/<name>"}, then answers requests until the
time is up and exits 0. This is the optional "full view" behind a question card: a page is never
opened as file://, because the Code tab's built-in browser shows a local file outside the project
folder as a frozen snapshot.

With --open the page also opens in the system's default browser, and the line carries
"opened": true or false. A link clicked in the Desktop app's chat opens in its narrow built-in
pane; a whole-model page reads better in a full browser window. False (a headless machine, a
remote session) is not an error: the person still has the link.

The server binds 127.0.0.1 only, on a random port. It serves the page and, beside it, regular
files with a web-asset extension (html css js svg png jpg json) and nothing else, and it refuses
any request whose Host is not this server's own address (a DNS-rebinding page cannot reach it
under its own name). Stdlib only.
"""

from __future__ import annotations

import argparse
import http.server
import json
import mimetypes
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

HOST = "127.0.0.1"
# The page itself is always served; beside it, only the kinds of file a report page loads.
_ALLOWED_SUFFIXES = {".html", ".css", ".js", ".svg", ".png", ".jpg", ".json"}
_REQUEST_TIMEOUT = 10  # a client that stalls mid-request is dropped rather than held open


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True  # a stalled client cannot keep the process alive past its deadline
    block_on_close = False

    def __init__(self, page: Path) -> None:
        super().__init__((HOST, 0), _Handler)
        self.root = page.parent
        self.port = self.server_address[1]
        self.page = page
        self.hosts = {f"localhost:{self.port}", f"{HOST}:{self.port}"}

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a dropped or timed-out connection is not news


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _Server
    timeout = _REQUEST_TIMEOUT

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _file(self) -> Path | None:
        if self.headers.get("Host") not in self.server.hosts:
            return None
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
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        try:
            self._send(200, target.read_bytes(), ctype)
        except OSError:
            self._send(404, b"Not found")

    do_GET = do_HEAD = _serve

    def _refuse(self) -> None:
        self._send(405, b"Method not allowed")

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse


def _open_in_browser(url: str) -> bool:
    """Open `url` in the default browser; False when none can be started, never an exception."""
    try:
        return bool(webbrowser.open(url, new=2))
    except webbrowser.Error:
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Serve one page from localhost for a short while.")
    p.add_argument("page", help="The HTML file to serve; files beside it are served too.")
    p.add_argument(
        "--minutes", type=float, default=30.0, help="How long to keep serving (default 30)."
    )
    p.add_argument(
        "--open", action="store_true", help="Also open the page in the default browser."
    )
    args = p.parse_args(argv)
    page = Path(args.page).expanduser().resolve()
    if not page.is_file():
        sys.stderr.write(f"serve_page: no such file: {page}\n")
        return 1
    server = _Server(page)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"http://localhost:{server.port}/{page.name}"
        line: dict[str, Any] = {"url": url}
        if args.open:
            # The socket is already listening, so the browser's first request is answered.
            line["opened"] = _open_in_browser(url)
        print(json.dumps(line), flush=True)
        worker.join(max(args.minutes, 0) * 60)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
