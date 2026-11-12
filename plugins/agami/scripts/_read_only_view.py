"""The two ways a rendered agami page is shown: a read-only view, or a page that saves itself.

Read-only (`make_read_only`, a renderer's `--read-only`): the page is for reading, and its decisions
(approve, reject, exclude, "this looks good") are asked on cards in the chat, so the page's queue, its
"Generate feedback for Claude" button and its copy-and-paste dialog would offer a second, paste-back
way to do the same thing. This hides every control that changes something and says where to decide
instead. Navigation and filtering (tabs, sub-tabs, filter chips, search, the SQL disclosures) keep
working.

Saving (`make_saving`, a renderer's `--save-to-helper`): the page is served by `serve_page.py
--decisions`, and its own button sends the queued decisions to that helper, the way the password page
sends a password (#651). The copy-and-paste dialog is hidden and never opens; the banner says who
approvals are signed as, from the saved settings, and the page cannot change it.

Stdlib only.
"""

from __future__ import annotations

import html as _html
import json
from pathlib import Path

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

BANNER_TEXT = "This page is a view: nothing on it is saved. Make decisions on the cards in the chat."
_BANNER = f'<div class="agami-read-only-banner" role="note">{BANNER_TEXT}</div>'


def make_read_only(html: str) -> str:
    """Return `html` with its change controls hidden and a banner saying where to decide."""
    if "<body>" not in html:
        raise ValueError("expected a plain <body> tag to mark the page read-only")
    html = html.replace("</head>", _STYLE + "\n</head>", 1)
    return html.replace("<body>", f'<body class="agami-read-only">\n{_BANNER}', 1)


# ---- A page that saves its own decisions (#651) ----------------------------------------------------

# Hidden on a saving page: the paste-back dialog and its instructions, the typed identity (the
# saved one is shown in the banner instead), and the examples page's own "add" form, which belongs to
# the add-your-own-examples flow rather than to deciding the seeds.
SAVING_HIDDEN = ("#modal", ".intro", ".reviewer-form", "#btn-add-example")
SAVE_LABEL = "Save decisions"

_SAVING_STYLE = (
    '<style id="agami-saves">\n'
    + ",\n".join(f"  body.agami-saves {s}" for s in SAVING_HIDDEN)
    + " { display: none !important; }\n"
    + """  body.agami-saved .sticky-footer { display: none !important; }
  body.agami-save-over #btn-generate { pointer-events: none; opacity: 0.5; }
  .agami-save-banner { margin: 0 0 16px; padding: 10px 14px; border-radius: 8px;
    border: 1px solid #D2DBF1; background: #F4F6FD; color: #1B2240; font: 14px/1.4 system-ui, sans-serif; }
  .agami-save-status:empty { display: none; }
  .agami-save-status { display: block; margin-top: 6px; font-weight: 600; }
  .agami-save-status.ok { color: #1E6B3A; }
  .agami-save-status.bad { color: #A12B2B; }
</style>"""
)

# Posts once to `save` beside the page (the helper's token path). The helper answers each refusal
# with a sentence, which is shown as it is; a refusal that cannot change (already saved, expired)
# leaves the button off.
_SAVING_SCRIPT = """<script id="agami-saves-script">
window.AGAMI_SAVE = %(config)s;
window.agamiSave = (function () {
  var state = 'idle';
  function say(text, kind) {
    var n = document.getElementById('agami-save-status');
    if (n) { n.textContent = text; n.className = 'agami-save-status ' + (kind || ''); }
  }
  function buttons(off) { document.querySelectorAll('#btn-generate').forEach(function (b) { b.disabled = off; }); }
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('#btn-generate').forEach(function (b) { b.textContent = %(label)s; });
  });
  function save(payload) {
    if (state !== 'idle') return false;
    state = 'saving'; buttons(true); say('Saving…');
    fetch('save', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload), cache: 'no-store', credentials: 'omit' })
      .then(function (r) { return r.json().catch(function () { return {}; })
        .then(function (b) { return { ok: r.ok, status: r.status, b: b }; }); })
      .then(function (res) {
        if (res.ok) {
          state = 'saved'; document.body.classList.add('agami-saved');
          say(%(saved)s, 'ok');
          return;
        }
        state = (res.status === 409 || res.status === 410) ? 'over' : 'idle';
        // A refusal that cannot change keeps the button off, even when the page redraws its footer.
        if (state === 'over') document.body.classList.add('agami-save-over');
        buttons(state === 'over');
        say((res.b && res.b.message) || ('Not saved (' + res.status + ').'), 'bad');
      })
      .catch(function () {
        state = 'idle'; buttons(false);
        say('Not saved: this page could not reach agami. If the chat has moved on, ask there for a new link.', 'bad');
      });
    return true;
  }
  save.say = say;
  return save;
})();
</script>"""


def saved_signoff(artifacts_dir: Path) -> dict:
    """The sign-off email and role saved in `local/.config` by the cards or an earlier page."""
    try:
        config = json.loads((artifacts_dir / "local" / ".config").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(config, dict):
        return {}
    return {k: v for k in ("email", "role") if isinstance(v := config.get(f"reviewer_{k}"), str) and v}


def saving_banner(signoff: dict) -> str:
    """What the page says at the top: how saving works, and who approvals are signed as."""
    head = (
        f"Decisions you make on this page are saved when you press <strong>{SAVE_LABEL}</strong>: "
        "they go straight to the chat, which applies them and says what changed. The page saves once."
    )
    if signoff.get("email") and signoff.get("role"):
        who = (
            f" Approvals are signed as <strong>{_html.escape(signoff['email'])}</strong> "
            f"({_html.escape(signoff['role'])}), from your agami settings."
        )
    else:
        who = " No sign-off identity is saved yet, so approvals can't be saved from this page; set it in the chat."
    return (
        '<div class="agami-save-banner" role="note">' + head + who
        + '<span class="agami-save-status" id="agami-save-status" role="status"></span></div>'
    )


def make_saving(html: str, signoff: dict, back_label: str = "I've saved it") -> str:
    """Return `html` as a page that posts its decisions to the helper serving it (`serve_page.py --decisions`).

    `back_label` is the option on the chat's `Page` card that the person presses once the page is
    saved: an open card is not interrupted when the helper exits, so the page says to press it."""
    if "<body>" not in html:
        raise ValueError("expected a plain <body> tag to mark the page as saving")
    signer = signoff if signoff.get("email") and signoff.get("role") else None
    config = json.dumps({"signer": signer}).replace("<", "\\u003c")  # nothing in it can close the script
    saved = f"Saved. Go back to the chat and press {back_label}: agami applies your decisions there and says what changed."
    script = _SAVING_SCRIPT % {
        "config": config,
        "label": json.dumps(SAVE_LABEL),
        "saved": json.dumps(saved).replace("<", "\\u003c"),
    }
    html = html.replace("</head>", _SAVING_STYLE + "\n" + script + "\n</head>", 1)
    return html.replace("<body>", f'<body class="agami-saves">\n{saving_banner(signoff)}', 1)
