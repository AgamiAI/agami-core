"""Turn a rendered agami page into a read-only full view.

A page shown from a question card is for reading: the decisions it used to collect (approve, reject,
exclude, "this looks good") are asked on cards in the chat, so the page's queue, its "Generate
feedback for Claude" button and its copy-and-paste dialog would offer a second, paste-back way to
do the same thing. This hides every control that changes something and says where to decide
instead. Navigation (tabs, sub-tabs, search, the SQL disclosures) keeps working.

Stdlib only; the renderers call `make_read_only` on their finished HTML when given `--read-only`.
"""

from __future__ import annotations

# Every button outside a tab bar changes something on these pages; tab and sub-tab buttons only
# switch what is shown, so they are the ones kept.
_STYLE = """<style id="agami-read-only">
  body.agami-read-only button:not(.tab-bar button):not(.subtab-bar button),
  body.agami-read-only .tab-queued,
  body.agami-read-only .sticky-footer,
  body.agami-read-only .feedback-bar,
  body.agami-read-only .modal,
  body.agami-read-only .modal-backdrop,
  body.agami-read-only .signoff-card,
  body.agami-read-only .reviewer-form,
  body.agami-read-only .global-note-panel,
  body.agami-read-only .intro { display: none !important; }
  body.agami-read-only .inline-editable { pointer-events: none; }
  body.agami-read-only .inline-editable .pencil,
  body.agami-read-only .inline-editable.empty { display: none !important; }
  .agami-read-only-banner { margin: 0 0 16px; padding: 10px 14px; border-radius: 8px;
    border: 1px solid #D2DBF1; background: #F4F6FD; color: #1B2240; font: 14px/1.4 system-ui, sans-serif; }
</style>"""

BANNER_TEXT = "This is a read-only view. Make decisions on the cards in the chat."
_BANNER = f'<div class="agami-read-only-banner" role="note">{BANNER_TEXT}</div>'


def make_read_only(html: str) -> str:
    """Return `html` with its change controls hidden and a banner saying where to decide."""
    if "<body>" not in html:
        raise ValueError("expected a plain <body> tag to mark the page read-only")
    html = html.replace("</head>", _STYLE + "\n</head>", 1)
    return html.replace("<body>", f'<body class="agami-read-only">\n{_BANNER}', 1)
