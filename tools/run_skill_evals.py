#!/usr/bin/env python3
"""Run per-skill evals against the Anthropic API (manual, opt-in).

Reads ``plugins/agami/skills/<name>/evals.json`` files (Anthropic
skill-creator format), sends each prompt to Claude with the SKILL.md
loaded as a system block, and grades the output via a second
``messages.create`` call. With ``--baseline`` it also runs each prompt
WITHOUT the skill loaded, surfacing the with-vs-without delta described
in Anthropic's skill-creator workflow.

Not run in CI: model calls cost tokens and require ``ANTHROPIC_API_KEY``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "plugins" / "agami" / "skills"
MODEL = os.environ.get("AGAMI_EVAL_MODEL", "claude-opus-4-7")
GRADER_MODEL = os.environ.get("AGAMI_EVAL_GRADER", "claude-opus-4-7")


def _import_anthropic():
    try:
        import anthropic
    except ImportError:
        sys.exit(
            "Missing dependency. Install with:\n"
            "    pip install anthropic"
        )
    return anthropic


def _load_evals(skill_name: str) -> dict:
    p = SKILLS_DIR / skill_name / "evals.json"
    if not p.is_file():
        sys.exit(f"No evals.json for skill {skill_name!r} at {p}")
    return json.loads(p.read_text())


def _all_skill_names() -> list[str]:
    return sorted(
        p.name for p in SKILLS_DIR.iterdir() if (p / "evals.json").is_file()
    )


def _run_one(client, skill_name: str, prompt: str, with_skill: bool) -> str:
    system_blocks = None
    if with_skill:
        skill_md = (SKILLS_DIR / skill_name / "SKILL.md").read_text()
        system_blocks = [
            {"type": "text", "text": f"<skill>\n{skill_md}\n</skill>"}
        ]
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system_blocks,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def _grade(client, expected: str, actual: str) -> tuple[bool, str]:
    grader_prompt = (
        "You grade outputs against an expected-behaviour description.\n\n"
        f"<expected_behaviour>\n{expected}\n</expected_behaviour>\n\n"
        f"<actual_output>\n{actual}\n</actual_output>\n\n"
        "Reply on a single line starting with PASS or FAIL, "
        "followed by a one-sentence reason."
    )
    resp = client.messages.create(
        model=GRADER_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": grader_prompt}],
    )
    line = "".join(b.text for b in resp.content if b.type == "text").strip()
    return line.upper().startswith("PASS"), line


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skill", help="run a single skill (default: all skills)")
    ap.add_argument(
        "--baseline",
        action="store_true",
        help="also run each eval WITHOUT the skill loaded, for the with-vs-without diff",
    )
    args = ap.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        sys.exit("ANTHROPIC_API_KEY not set")

    anthropic = _import_anthropic()
    client = anthropic.Anthropic()
    skills = [args.skill] if args.skill else _all_skill_names()

    total = passed = 0
    for skill in skills:
        spec = _load_evals(skill)
        for ev in spec["evals"]:
            total += 1
            actual = _run_one(client, skill, ev["prompt"], with_skill=True)
            ok, reason = _grade(client, ev["expected_output"], actual)
            mark = "PASS" if ok else "FAIL"
            print(f"[{mark}] {skill}::{ev['id']} — {reason}")
            if ok:
                passed += 1

            if args.baseline:
                base_actual = _run_one(client, skill, ev["prompt"], with_skill=False)
                base_ok, base_reason = _grade(client, ev["expected_output"], base_actual)
                base_mark = "PASS" if base_ok else "FAIL"
                print(f"    baseline (no skill): {base_mark} — {base_reason}")

    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
