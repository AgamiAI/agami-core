"""Turn a rendered agami page into a read-only full view.

A page shown from a question card is for reading: the decisions it used to collect (approve, reject,
exclude, "this looks good") are asked on cards in the chat, so the page's queue, its "Generate
feedback for Claude" button and its copy-and-paste dialog would offer a second, paste-back way to
do the same thing. This hides every control that changes something and says where to decide
instead. Navigation and filtering (tabs, sub-tabs, filter chips, search, the SQL disclosures) keep
working.

Stdlib only; the renderers call `make_read_only` on their finished HTML when given `--read-only`.
"""

from __future__ import annotations

# The controls that change something or produce a paste-back block, named one by one. Navigation
# and filtering (tabs, sub-tabs, status chips and pills, "Show all" / "Clear filters", search,
# pagination, copying SQL) are not listed and keep working: a blanket rule on every button hid the
# filters too. A control added to a template that changes something must be listed here.
CHANGE_CONTROLS = (
    # shared: the queue, the generate footer and the copy-and-paste dialog
    ".tab-queued",
    ".sticky-footer",
    ".feedback-bar",
    ".footer",
    ".modal",
    ".modal-backdrop",
    ".signoff-card",
    ".reviewer-form",
    ".global-note-panel",
    ".intro",
    # chart: metric approve/change, the send button, the SQL editor
    ".map-actions",
    ".map-edit",
    ".map-send",
    ".sql-edit-btn",
    ".sql-edit-bar",
    ".sql-edit-area",
    # model explorer and examples validation: approve / reject / exclude / edit / PII / add forms
    ".card-actions",
    ".field-actions",
    ".table-actions",
    ".edit-form",
    ".add-form",
    ".inline-edit-bar",
    ".inline-edit-ta",
    ".btn-pii",
    ".btn-propagate",
    ".btn-edit-prominent",
    ".bulk-approve",
    ".bulk-reject",
    ".add-metric-open",
    ".add-metric-go",
    ".rt-toolbar",
    ".sql-editor",
    # golden datasets: tag, match, question, remove and withdraw controls, and the queue's Undo
    ".actions-row",
    ".queue-row .chip-btn",
    # reconcile: the intake's keep box, the report's decision radios and words
    'input[data-field="keep"]',
    ".grade.opts",
    ".grade.opts + .more",
)
# Fields that show text the page reads but must not take edits.
_FROZEN = (".inline-editable", 'textarea[data-field="question"]', ".code-editable", ".rt-editor")

_STYLE = (
    '<style id="agami-read-only">\n'
    + ",\n".join(f"  body.agami-read-only {s}" for s in CHANGE_CONTROLS)
    + " { display: none !important; }\n"
    + ",\n".join(f"  body.agami-read-only {s}" for s in _FROZEN)
    + " { pointer-events: none; resize: none; }\n"
    + """  body.agami-read-only .inline-editable .pencil,
  body.agami-read-only .inline-editable.empty { display: none !important; }
  .agami-read-only-banner { margin: 0 0 16px; padding: 10px 14px; border-radius: 8px;
    border: 1px solid #D2DBF1; background: #F4F6FD; color: #1B2240; font: 14px/1.4 system-ui, sans-serif; }
</style>"""
)

BANNER_TEXT = "This is a read-only view. Make decisions on the cards in the chat."
_BANNER = f'<div class="agami-read-only-banner" role="note">{BANNER_TEXT}</div>'


def make_read_only(html: str) -> str:
    """Return `html` with its change controls hidden and a banner saying where to decide."""
    if "<body>" not in html:
        raise ValueError("expected a plain <body> tag to mark the page read-only")
    html = html.replace("</head>", _STYLE + "\n</head>", 1)
    return html.replace("<body>", f'<body class="agami-read-only">\n{_BANNER}', 1)
