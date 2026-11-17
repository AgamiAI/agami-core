#!/usr/bin/env python3
"""A local page that shows how a model build is getting on, read from its progress log (#677).

    progress_page.py <artifacts_dir>/<profile>/.introspect/progress.log [--minutes 240]

Prints one JSON line, {"url": "http://127.0.0.1:<port>/<token>/", "expires_in": <seconds>}, then serves
until the time is up and exits 0. The page asks `<token>/status` every few seconds, and the server
reads the log afresh each time, so the page is current for as long as the build writes lines: which
batch, which stage (and the stages still to come, by name), the current stage's count ("joins checked
140 of 300"), tables read, the time elapsed, and, when the log has been quiet for a minute or more,
what the build is waiting on and for how long. It never estimates the time left: how long a build
takes depends on the network, the warehouse and the data.

The log holds names and counts only (the engine writes no value read from the data into it), and the
page shows nothing else. The server binds 127.0.0.1 only, on a random port, behind a random token,
answers GET and HEAD for exactly two paths, refuses any request whose Host is not its own address,
and runs only the page's own inline script. Stdlib only.
"""

from __future__ import annotations

import argparse
import http.server
import json
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from serve_page import script_hashes

HOST = "127.0.0.1"
_REQUEST_TIMEOUT = 10
# The log is quiet this long before the page says what the build is waiting on: the engine's own
# heartbeat writes a line after a minute of silence.
QUIET_S = 60

STAGES = (
    "Reading columns and keys",
    "Finding relationships",
    "Checking joins on the data",
    "Profiling columns",
    "Testing join candidates",
)
_STAMP_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (.*)$")
_BEAT_RE = re.compile(r"^waiting on the database for [^:]+: (.*)$")
# message -> (stage index, count label); a count's numbers come from the pattern's groups.
_LINES: tuple[tuple[re.Pattern[str], int, Optional[str]], ...] = (
    (re.compile(r"^discovered (\d+) tables"), 0, "tables read"),
    (re.compile(r"^columns\+grain (\d+)/(\d+)$"), 0, "tables read"),
    (re.compile(r"^building relationships"), 1, None),
    (re.compile(r"^joins: checking (\d+) on the data$"), 2, "joins checked"),
    (re.compile(r"^joins checked (\d+)/(\d+)$"), 2, "joins checked"),
    (re.compile(r"^profiling (\d+) tables"), 3, "tables profiled"),
    (re.compile(r"^profiled (\d+)/(\d+) tables$"), 3, "tables profiled"),
    (re.compile(r"^join candidates:"), 4, None),
)


def parse_stamp(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def summarise(text: str, now: float) -> dict[str, Any]:
    """What a progress log says so far, as the page shows it. `now` is epoch seconds."""
    batch = tables = count = waiting = None
    stage: Optional[int] = None
    finished = False
    first = last_real = last_any = None
    for raw in text.splitlines():
        m = _STAMP_RE.match(raw)
        at, msg = (parse_stamp(m.group(1)), m.group(2)) if m else (None, raw.strip())
        if not msg:
            continue
        if at is not None:
            first = at if first is None else first
            last_any = at
        beat = _BEAT_RE.match(msg)
        if beat:
            waiting = beat.group(1)
            continue
        waiting = None
        if at is not None:
            last_real = at
        if b := re.match(r"^batch (\d+) of (\d+)$", msg):
            batch = {"n": int(b.group(1)), "of": int(b.group(2))}
        elif msg.startswith("done:"):
            finished = True
        for rx, idx, label in _LINES:
            hit = rx.match(msg)
            if not hit:
                continue
            stage = idx
            nums = [int(g) for g in hit.groups()]
            if label is None:
                count = None
            else:
                done, of = (nums[0], nums[1]) if len(nums) == 2 else (0, nums[0])
                count = {"label": label, "done": done, "of": of}
                if idx == 0:
                    tables = {"done": done, "of": of}
            break
    if finished:
        stage, count, waiting = len(STAGES), None, None
    states = [
        "todo" if stage is None or i > stage else ("current" if i == stage else "done")
        for i in range(len(STAGES))
    ]
    end = last_any if finished else now
    return {
        "batch": batch,
        "stage": "Done" if finished else (STAGES[stage] if stage is not None else None),
        "stages": [{"name": n, "state": s} for n, s in zip(STAGES, states)],
        "count": count,
        "tables": tables,
        "first_at": first,
        "elapsed_s": int(max(0, end - first)) if first is not None and end is not None else 0,
        "quiet_s": 0 if finished or last_real is None else int(max(0, now - last_real)),
        "waiting_on": waiting,
        "finished": finished,
    }


def read_status(log: Path, first_seen: Optional[float], now: float) -> dict[str, Any]:
    """The summary of the log as it is now. `first_seen` is the earliest start this page has seen:
    a batched build starts a fresh log per batch, and the whole run's time counts from the first."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    s = summarise(text, now)
    s["started"] = bool(text.strip())
    starts = [t for t in (first_seen, s["first_at"]) if t is not None]
    s["run_elapsed_s"] = int(max(0, now - min(starts))) if starts and not s["finished"] else s["elapsed_s"]
    if starts and s["finished"] and first_seen is not None and s["first_at"] is not None:
        s["run_elapsed_s"] = s["elapsed_s"] + int(max(0, s["first_at"] - first_seen))
    return s


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Model build progress</title>
<style>
:root { --bg:#fbfaf7; --fg:#1d1d1b; --muted:#6b6a65; --line:#e3e1da; --accent:#2f6f4f; --warn:#9a5b00; }
@media (prefers-color-scheme: dark) { :root { --bg:#171716; --fg:#ecebe6; --muted:#a3a29b; --line:#33322f;
  --accent:#7cc39b; --warn:#e6a64a; } }
body { background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:0; }
main { max-width:560px; margin:0 auto; padding:24px 16px; }
h1 { font-size:20px; margin:0 0 4px; } .muted { color:var(--muted); }
ol { list-style:none; padding:0; margin:20px 0; }
li { padding:6px 0; border-bottom:1px solid var(--line); }
li.done::before { content:"\\2713  "; color:var(--accent); }
li.current { font-weight:600; } li.current::before { content:"\\25CF  "; color:var(--accent); }
li.todo { color:var(--muted); } li.todo::before { content:"\\25CB  "; }
.bar { height:8px; background:var(--line); border-radius:4px; overflow:hidden; margin:6px 0 2px; }
.bar > div { height:100%; background:var(--accent); width:0; }
#waiting { color:var(--warn); }
</style></head>
<body><main>
<h1 id="title">Building the model</h1>
<div class="muted" id="sub">Waiting for the build to start.</div>
<ol id="stages"></ol>
<div id="count"></div>
<div class="bar" id="bar" hidden><div id="fill"></div></div>
<p id="tables" class="muted"></p>
<p id="waiting" hidden></p>
<p id="elapsed" class="muted"></p>
</main>
<script>
function dur(s) {
  if (s < 60) return s + " s";
  var m = Math.floor(s / 60), h = Math.floor(m / 60);
  return h ? h + " h " + (m % 60) + " min" : m + " min";
}
function show(s) {
  var el = function (id) { return document.getElementById(id); };
  el("title").textContent = s.finished ? "The model is built" : "Building the model";
  el("sub").textContent = !s.started ? "Waiting for the build to start." :
    (s.batch ? "Batch " + s.batch.n + " of " + s.batch.of + " \\u00b7 " : "") + (s.stage || "Starting");
  var ol = el("stages"); ol.textContent = "";
  s.stages.forEach(function (st) {
    var li = document.createElement("li"); li.className = st.state; li.textContent = st.name; ol.appendChild(li);
  });
  var c = s.count;
  el("count").textContent = c ? c.label.charAt(0).toUpperCase() + c.label.slice(1) + ": " + c.done + " of " + c.of : "";
  el("bar").hidden = !c || !c.of;
  if (c && c.of) el("fill").style.width = Math.min(100, 100 * c.done / c.of) + "%";
  el("tables").textContent = s.tables && (!c || c.label !== "tables read") ?
    "Tables read: " + s.tables.done + " of " + s.tables.of : "";
  var w = el("waiting");
  w.hidden = s.finished || !s.started || s.quiet_s < __QUIET__;
  w.textContent = "Waiting on the database for " + dur(s.quiet_s) + (s.waiting_on ? ": " + s.waiting_on : "") + ".";
  el("elapsed").textContent = !s.started ? "" : "Elapsed: " + dur(s.run_elapsed_s) +
    (s.batch && s.run_elapsed_s !== s.elapsed_s ? " (this batch " + dur(s.elapsed_s) + ")" : "");
}
function poll() {
  fetch("status", {cache: "no-store"}).then(function (r) { return r.json(); }).then(show)
    .catch(function () { document.getElementById("sub").textContent = "This page has stopped updating."; })
    .finally(function () { setTimeout(poll, 3000); });
}
poll();
</script>
</body></html>
""".replace("__QUIET__", str(QUIET_S))


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, log: Path) -> None:
        super().__init__((HOST, 0), _Handler)
        self.log = log
        self.port = self.server_address[1]
        self.hosts = {f"{HOST}:{self.port}", f"localhost:{self.port}"}
        self.base = f"/{secrets.token_urlsafe(32)}/"
        self.first_seen: Optional[float] = None
        self.lock = threading.Lock()
        self.page = PAGE.encode("utf-8")
        hashes = " ".join(script_hashes(PAGE))
        self.csp = (f"default-src 'none'; script-src {hashes}; style-src 'unsafe-inline'; "
                    "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")

    def status(self) -> bytes:
        now = time.time()
        with self.lock:
            s = read_status(self.log, self.first_seen, now)
            if s["first_at"] is not None and (self.first_seen is None or s["first_at"] < self.first_seen):
                self.first_seen = s["first_at"]
        return json.dumps(s).encode("utf-8")

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _Server
    timeout = _REQUEST_TIMEOUT

    def log_message(self, *args: Any) -> None:
        pass  # the request line carries the token

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", self.server.csp)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _at(self, path: str) -> bool:
        return secrets.compare_digest(self.path.encode("utf-8", "surrogateescape"), path.encode())

    def _serve(self) -> None:
        srv = self.server
        if self.headers.get("Host") not in srv.hosts:
            return self._send(404, b"Not found", "text/plain")
        if self._at(srv.base):
            return self._send(200, srv.page, "text/html; charset=utf-8")
        if self._at(srv.base + "status"):
            return self._send(200, srv.status(), "application/json")
        self._send(404, b"Not found", "text/plain")

    do_GET = do_HEAD = _serve

    def _refuse(self) -> None:
        self._send(405, b"Method not allowed", "text/plain")

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Serve a live view of a model build's progress log.")
    p.add_argument("log", help="The build's progress log (<artifacts_dir>/<profile>/.introspect/progress.log).")
    p.add_argument("--minutes", type=float, default=240.0, help="How long to keep serving (default 240).")
    args = p.parse_args(argv)
    server = _Server(Path(args.log).expanduser().resolve())
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    seconds = max(args.minutes, 0) * 60
    print(json.dumps({"url": f"http://{HOST}:{server.port}{server.base}", "expires_in": int(seconds)}),
          flush=True)
    try:
        worker.join(seconds)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
