"""Stop hook: keep the turn going, once, when say_gate.py refused a card and the assistant is stopping
without having asked it again.

say_gate.py refuses an AskUserQuestion card while text the engine handed over for the person has not
been shown, and its refusal says to post the text and ask the same card again. In live runs the
assistant posted the text and then ended its turn with no card, so the person had nothing to answer.
When the assistant stops, this hook looks at the same pending file (`say-pending.json`, format owned
by `semantic_model/say_pending.py`) and the session's transcript, and blocks the stop only when all
of these hold:

  1. The event is Stop and `stop_hook_active` is exactly false (never when Claude Code is already
     continuing because of a stop hook, nor when the field is missing).
  2. An entry is fresh and shaped as the engine writes it (the gate's own checks), the gate has
     refused a card for it (`refused` >= 1), and this hook has not blocked for it before
     (`resumed` not true). With none, it returns before reading the transcript.
  3. The entry belongs to this session: its id is in a tool result of the transcript (the gate's test).
  4. After that, an AskUserQuestion of the main thread got the gate's refusal for this text (its
     tool result holds the refusal's first sentence and the text's first line).
  5. Since the last such refusal there is no AskUserQuestion of the main thread and no message from
     the person.

Then it sets `resumed` on the entry, saves the file (a block it can't record is not made), and
blocks with REASON, one fixed sentence: nothing from the pending file or the transcript is echoed.
So a stop is blocked at most once per text, and the pending file stays the one source of truth (the
gate drops the entry once the text is shown or the card let through). It never prints anything else:
no permission decision, no context. Every doubt prints nothing, so the turn ends as it would have.

It shares the gate's code (the artifacts folder, the safe reads, the entry checks, the transcript
reader, the atomic save) by loading `say_gate.py` from its own folder by path: hooks.json runs both
with `python3 -I`, which puts no script folder on the import path, and the plugin's scripts folder is
the only place anything is loaded from.

Contract (https://code.claude.com/docs/en/hooks, "Stop input" and "Stop decision control"): stdin
has the common fields (`transcript_path`, `hook_event_name`) and `stop_hook_active`, "true when
Claude Code is already continuing as a result of a stop hook"; `{"decision": "block", "reason"}`
"prevents Claude from stopping" and the reason "tells Claude why it should continue"; exit 0 with no
output lets it stop. The transcript "may lag": the refusal and any card asked after it come well
before the stop, so this reads the tail once and does not wait.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

GATE_FILE = "say_gate.py"
REASON = "agami was about to ask the person a question card, and it has not been asked. Ask that card now."
MAX_INPUT_BYTES = 1024 * 1024

_GATE: Any = None


def gate() -> Any:
    """say_gate.py from this script's own folder, loaded once."""
    global _GATE
    if _GATE is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), GATE_FILE)
        spec = importlib.util.spec_from_file_location("agami_say_gate", path)
        if spec is None or spec.loader is None:
            raise ImportError(path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _GATE = module
    return _GATE


def _resumable(g: Any, entries: list, now: datetime) -> list:
    """Entries the gate has refused a card for and this hook has not resumed, fresh and well formed."""
    return [e for e in entries
            if now - g._created(e) <= g.MAX_AGE and g.well_formed(e)
            and e["refused"] >= 1 and e.get("resumed", False) is False]


def _turn_events(g: Any, rows: list) -> tuple:
    """(refusals as (position, text), positions of cards asked, positions where the person spoke), main
    thread only. A refusal is a tool result, holding the gate's first sentence, of an AskUserQuestion."""
    cards: dict = {}
    refusals, spoke = [], []
    for n, row in enumerate(rows):
        for block in g._blocks(row, "assistant", "assistant"):
            if block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
                cards[block.get("id")] = n
        for block in g._blocks(row, "user", "user"):
            if block.get("type") == "tool_result" and block.get("tool_use_id") in cards:
                body = g._result_text(block)
                if g.REFUSAL_MARK in body:
                    refusals.append((n, g._squash(body)))
            elif block.get("type") == "text" and row.get("isMeta") is not True:
                spoke.append(n)
        message = row.get("message")
        if (row.get("type") == "user" and row.get("isSidechain") is not True and row.get("isMeta") is not True
                and isinstance(message, dict) and isinstance(message.get("content"), str)):
            spoke.append(n)
    return refusals, sorted(cards.values()), spoke


def _left_unasked(g: Any, entry: dict, rows: list) -> bool:
    """This session printed `entry`, a card was refused for it after that, and since the last such
    refusal no card was asked and the person said nothing."""
    results, _texts = g.read_events(rows)
    printed = g.printed_at(entry["id"], results)
    if printed is None:
        return False
    refusals, cards, spoke = _turn_events(g, rows)
    needle = g._squash(entry["needle"])
    last = max((n for n, body in refusals if n > printed and needle in body), default=None)
    if last is None:
        return False
    return not any(n > last for n in cards) and not any(n > last for n in spoke)


def decide(event: Any, now: Optional[datetime] = None) -> Optional[str]:
    """REASON when this stop should be blocked (and recorded as such), else None."""
    if not isinstance(event, dict) or event.get("hook_event_name") != "Stop" \
            or event.get("stop_hook_active") is not False:
        return None
    g = gate()
    path = g.pending_file()
    if path is None:
        return None
    entries = g.load(path)
    now = now or datetime.now(timezone.utc)
    if any(g._created(e) - now > g.MAX_SKEW for e in entries):
        raise g.Doubt("an entry from the future")
    candidates = _resumable(g, entries, now)
    if not candidates:
        return None
    rows = g.transcript_tail(event.get("transcript_path"))
    entry = next((e for e in candidates if _left_unasked(g, e, rows)), None)
    if entry is None:
        return None
    entry["resumed"] = True
    g.save(path, entries)   # before the block: one that can't be recorded could repeat
    return REASON


def main() -> int:
    try:
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            return 0
        reason = decide(json.loads(data.decode("utf-8")))
    except Exception:  # noqa: BLE001 — any doubt is silence: the turn ends as it would have
        return 0
    if reason:
        sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
