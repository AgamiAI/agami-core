"""Guards for two card rules a live run in the Desktop Code tab showed people tripping over.

- Rule 5: a value to type is asked for in the question text, never offered as an option. An option
  such as "Type it in Other" is clicked with nothing typed.
- Rule 5a: a question typed in "Other" ("show me the SQL", "why?") is answered and the same card
  asked again. Every skill's "re-asked, never guessed" fallback must point at that rule, or the
  question is said back as something that didn't match.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "agami"
SKILLS_DIR = PLUGIN / "skills"

_TYPE_IN_OTHER_OPTION = re.compile(r"\*\*Type [^*\n]{0,40}? in Other\*\*")
_FALLBACK = re.compile(r"re-asked, never guessed|can't place")
_RULE_5A = "question-cards.md#5a-a-question-typed-in-other-is-answered-then-the-same-card-is-asked-again"


def _skills() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def test_rule_5a_is_in_the_shared_doc() -> None:
    text = (PLUGIN / "shared" / "question-cards.md").read_text()
    assert "### 5a. A question typed in \"Other\" is answered, then the same card is asked again" in text


def test_no_option_asks_the_person_to_type_in_other() -> None:
    offenders = [
        f"{p.parent.name}: {m.group(0)}" for p in _skills() for m in _TYPE_IN_OTHER_OPTION.finditer(p.read_text())
    ]
    assert not offenders, offenders


def test_every_other_fallback_answers_questions_first() -> None:
    offenders = []
    for p in _skills():
        for paragraph in re.split(r"\n\s*\n", p.read_text()):
            if _FALLBACK.search(paragraph) and _RULE_5A not in paragraph:
                offenders.append(f"{p.parent.name}: {paragraph[:80]!r}")
    assert not offenders, offenders
