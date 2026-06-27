#!/usr/bin/env python3
"""Throwaway mockup of the ACE-008 admin **Sessions** view, rendered with the real admin-console CSS.
Full-width, browser-local times, and the NL question per query (self-reported). Not shipped."""

import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent
PKG = ROOT / "packages" / "agami-core" / "src"
sys.path.insert(0, str(PKG))

import ui  # noqa: E402

ui._TABS = (("dashboard", "Dashboard"), ("users", "Users"), ("sessions", "Sessions"), ("calls", "Tool calls"))

OUT = ROOT / "previews"
OUT.mkdir(exist_ok=True)
shutil.copytree(PKG / "static", OUT / "static", dirs_exist_ok=True)
ADMIN = {"admin_label": "Alex Kim", "admin_email": "you@example.com"}


def esc(s):
    return ui.esc(s)


def _pill(ok):
    return f'<span class="pill {"active" if ok else "disabled"}">{"ok" if ok else "error"}</span>'


def _t(utc):  # a <time> the inline script localizes to the browser's zone
    return f'<time data-utc="{utc}">{utc}</time>'


# Sessions (best-effort grouping) → each opens to its queries (with the self-reported NL question).
SESSIONS = [
    {"id": "s1", "user": "jordan@example.com", "ds": "SALES_DATA", "started": "2026-06-27T10:39:02Z",
     "last": "2026-06-27T10:42:17Z", "queries": 3, "errors": 0, "avg": "60 ms"},
    {"id": "s2", "user": "sam@example.com", "ds": "SALES_DATA", "started": "2026-06-27T10:41:30Z",
     "last": "2026-06-27T10:41:50Z", "queries": 1, "errors": 1, "avg": "31 ms"},
    {"id": "s3", "user": "morgan@example.com", "ds": "MARKETING", "started": "2026-06-27T09:58:11Z",
     "last": "2026-06-27T10:05:44Z", "queries": 2, "errors": 0, "avg": "120 ms"},
]

QUERIES_S1 = [
    ("2026-06-27T10:42:17Z", "What's our revenue by region this quarter?", "revenue by region",
     "SELECT region, SUM(amount) AS revenue\nFROM orders\nWHERE created_at >= '2026-04-01'\nGROUP BY region\nORDER BY revenue DESC",
     "5", "84 ms", True),
    ("2026-06-27T10:40:55Z", "(Claude inspected the schema)", "",
     "— get_datasource_schema(datasets: orders, customers)", "—", "12 ms", True),
    ("2026-06-27T10:39:02Z", "Show me the 10 most recent orders", "recent orders",
     "SELECT id, customer_id, amount, created_at\nFROM orders\nORDER BY created_at DESC\nLIMIT 10",
     "10", "73 ms", True),
]


def sessions_rows():
    out = ""
    for s in SESSIONS:
        out += (
            f'<tr><td><label for="{s["id"]}" style="cursor:pointer;color:var(--brand)">{_t(s["started"])}</label></td>'
            f'<td><strong>{esc(s["user"])}</strong></td>'
            f'<td class="muted">{esc(s["ds"])}</td>'
            f'<td class="muted">{s["queries"]}</td>'
            f'<td class="muted">{s["errors"] or "—"}</td>'
            f'<td class="muted">{s["avg"]}</td>'
            f'<td class="muted">{_t(s["last"])}</td>'
            f'<td style="text-align:right"><label for="{s["id"]}" class="btn tiny secondary">Open</label></td></tr>'
        )
    return out


def _query_card(ts, question, subq, sql, rows, lat, ok):
    sub = f'<div class="muted" style="margin:2px 0 8px">↳ {esc(subq)} <span class="muted">· self-reported</span></div>' if subq else ""
    return (
        f'<div style="border-top:1px solid var(--line);padding:14px 0">'
        f'<div style="display:flex;justify-content:space-between"><strong>{esc(question)}</strong>'
        f'<span class="muted" style="font-size:13px">{_t(ts)} · {lat} {_pill(ok)}</span></div>{sub}'
        f'<pre class="code" style="white-space:pre-wrap;padding:12px;display:block;margin-top:6px">{esc(sql)}</pre>'
        f'<div class="muted" style="font-size:13px;margin-top:6px">{rows} rows</div></div>'
    )


def session_drawer():
    cards = "".join(_query_card(*q) for q in QUERIES_S1)
    return f"""<input type="checkbox" id="s1" class="drawer-toggle">
<div class="drawer-wrap"><label for="s1" class="drawer-backdrop"></label>
<aside class="drawer" style="width:560px">
<div class="drawer-head"><h1 style="font-size:17px">Session</h1>
<label for="s1" class="drawer-x" aria-label="Close">&times;</label></div>
<p class="sub" style="margin-bottom:8px">jordan@example.com · SALES_DATA · 3 queries · started {_t("2026-06-27T10:39:02Z")}</p>
{cards}
</aside></div>"""


def sessions_panel():
    table = f"""<div class="table-wrap"><table>
<thead><tr><th>Started</th><th>User</th><th>Datasource</th><th>Queries</th><th>Errors</th>
<th>Avg time</th><th>Last activity</th><th></th></tr></thead>
<tbody>{sessions_rows()}</tbody></table></div>"""
    return table


# --- localize times to the browser + make the console full-width ---------------
_SCRIPT = """<script>
for (const t of document.querySelectorAll('time[data-utc]')) {
  const d = new Date(t.dataset.utc);
  if (!isNaN(d)) t.textContent = d.toLocaleString(undefined, {dateStyle:'medium', timeStyle:'short'});
}
</script>"""

# Polish pass — a real product font (Inter) + refined buttons/tables/typography + full-width.
# (In the build, Inter is self-hosted under /static so the admin's browser makes no call to Google.)
_POLISH = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;450;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--ink:#0e1525;--muted:#69748b;--line:#e7ebf3;--ring:rgba(11,87,208,.18);--bg:#fbfcfe}
body{font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  letter-spacing:-.006em;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
h1{font-weight:660;letter-spacing:-.024em}
.sub{color:var(--muted)}
.main{max-width:none;padding:30px 40px 64px}
.topbar{height:64px;padding:0 40px;background:#fff;position:sticky;top:0;z-index:30}
.tabs{gap:8px}
.tabs a{font-weight:550;color:var(--muted)}
.tabs a.active{font-weight:650;color:var(--ink)}
.panel{margin-top:22px}
.btn{font-weight:600;border-radius:10px;height:42px;box-shadow:0 1px 2px rgba(13,20,38,.07);
  transition:transform .12s ease,box-shadow .18s ease,background .15s}
.btn:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(11,87,208,.24)}
.btn.tiny{height:30px;border-radius:8px;font-weight:550;box-shadow:none}
.btn.tiny:hover{transform:none;box-shadow:0 1px 2px rgba(13,20,38,.08)}
.btn.secondary{background:#fff;color:var(--ink);border-color:var(--line)}
.btn.secondary:hover{background:#f4f7fe;border-color:#cdd8ee}
table{font-size:13.5px}
thead th{font-size:11px;letter-spacing:.07em;color:#8a94a8;padding-bottom:12px}
td{padding:15px 12px;border-top:1px solid var(--line)}
tbody tr:hover{background:#f6f9ff}
.pill{font-weight:600;letter-spacing:.01em;padding:3px 11px}
.code,pre.code{font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
pre.code{background:#0e1525;color:#e8edf7;border:0;border-radius:12px;line-height:1.6}
input,select{border-radius:10px;border-color:var(--line)}
input:focus,select:focus{border-color:var(--brand);box-shadow:0 0 0 4px var(--ring)}
.drawer{box-shadow:-26px 0 70px rgba(13,20,38,.20);border-left:1px solid var(--line)}
time{font-variant-numeric:tabular-nums}
@media(max-width:680px){.main{padding:18px 16px 48px}.topbar{padding:0 16px}.drawer{width:94vw!important}}
</style>"""


def write(name, html):
    # ui.py now ships the polish + the browser-local time script — render exactly what ships.
    (OUT / name).write_text(html.replace('"/static/', '"static/'))


write("20-sessions.html", ui.admin_shell("Sessions · agami admin", "sessions", sessions_panel(),
                                          extra=session_drawer(), **ADMIN))


# --- Tool calls tab (flat, audit-grade — every call) ---------------------------
CALLS = [
    ("2026-06-27T10:42:17Z", "jordan@example.com", "execute_sql", "SALES_DATA", "5", "84 ms", True, True),
    ("2026-06-27T10:40:55Z", "jordan@example.com", "get_datasource_schema", "SALES_DATA", "—", "12 ms", True, False),
    ("2026-06-27T10:41:50Z", "sam@example.com", "execute_sql", "SALES_DATA", "0", "31 ms", False, False),
    ("2026-06-27T10:40:03Z", "jordan@example.com", "list_datasources", "—", "—", "3 ms", True, False),
    ("2026-06-27T10:39:40Z", "morgan@example.com", "log_feedback", "MARKETING", "—", "2 ms", True, False),
    ("2026-06-27T10:39:02Z", "jordan@example.com", "execute_sql", "SALES_DATA", "10", "73 ms", True, False),
]


def calls_rows():
    out = ""
    for ts, user, tool, ds, rows, lat, ok, drawer in CALLS:
        open_ = f'<label for="call" class="btn tiny secondary">Open</label>' if drawer else ""
        out += (
            f"<tr><td>{_t(ts)}</td><td><strong>{esc(user)}</strong></td>"
            f'<td><span class="pill" style="background:var(--chip);color:var(--ink)">{esc(tool)}</span></td>'
            f'<td class="muted">{esc(ds)}</td><td class="muted">{rows}</td><td class="muted">{lat}</td>'
            f'<td>{_pill(ok)}</td><td style="text-align:right">{open_}</td></tr>'
        )
    return out


CALL_DRAWER = f"""<input type="checkbox" id="call" class="drawer-toggle">
<div class="drawer-wrap"><label for="call" class="drawer-backdrop"></label>
<aside class="drawer" style="width:560px">
<div class="drawer-head"><h1 style="font-size:17px">Tool call</h1>
<label for="call" class="drawer-x" aria-label="Close">&times;</label></div>
<p class="sub" style="margin-bottom:14px">execute_sql · jordan@example.com · {_t("2026-06-27T10:42:17Z")}</p>
<label>User question <span class="muted">— self-reported</span></label>
<div class="muted">What's our revenue by region this quarter?</div>
<label>Agent query <span class="muted">— self-reported</span></label>
<div class="muted">revenue by region</div>
<label>SQL</label>
<pre class="code" style="white-space:pre-wrap;padding:12px;display:block">SELECT region, SUM(amount) AS revenue
FROM orders
WHERE created_at >= '2026-04-01'
GROUP BY region
ORDER BY revenue DESC</pre>
<div class="grid2" style="margin-top:16px">
<div><label>Rows</label><div class="muted">5</div></div>
<div><label>Latency</label><div class="muted">84 ms</div></div>
<div><label>Status</label><div>{_pill(True)}</div></div>
<div><label>Datasource</label><div class="muted">SALES_DATA</div></div>
</div>
<label>Tables used <span class="muted">— from the query receipt</span></label>
<div class="muted">orders</div>
</aside></div>"""


def calls_panel():
    return f"""<div class="row" style="display:flex;gap:10px;justify-content:flex-end;margin-bottom:14px">
<select style="width:auto"><option>All users</option></select>
<select style="width:auto"><option>All tools</option></select></div>
<div class="table-wrap"><table>
<thead><tr><th>Time</th><th>User</th><th>Tool</th><th>Datasource</th><th>Rows</th><th>Latency</th><th>Status</th><th></th></tr></thead>
<tbody>{calls_rows()}</tbody></table></div>"""


write("21-tool-calls.html", ui.admin_shell("Tool calls · agami admin", "calls", calls_panel(),
                                            extra=CALL_DRAWER, **ADMIN))
print("wrote previews/20-sessions.html + 21-tool-calls.html  (click Open to see the drawers)")
