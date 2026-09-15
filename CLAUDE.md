# Contributing conventions for agami-core

This file is the "how to write it" for **agami-core** — read by Claude Code (automatically, every
session) and by human contributors. It's **guidance**; the things that can be machine-checked are
enforced by the gates, not by this doc:

- style / naming → **ruff** (CI + the local hooks)
- secrets / real credentials → **gitleaks** (CI + the local hooks)
- structure / real-data fixtures → **`/code-review`** + maintainer review (CODEOWNERS)

So this doc raises the floor; the gates catch the enforceable parts. See `CONTRIBUTING.md` for the
full local-setup walkthrough.

## Dev workflow

The only thing to install is [`uv`](https://docs.astral.sh/uv/). Everything else is fetched on
demand by `uvx`, the same on macOS / Linux / Windows. From the repo root:

```bash
uv run dev.py setup     # once: wire the local pre-commit hooks
uv run dev.py check     # ruff + tests + gitleaks — the same checks CI gates on
uv run dev.py cover     # did the lines I changed get tested? (patch coverage)
```

Run `uv run dev.py check` before you push. **CI is the real, unbypassable gate** (`.github/
workflows/ci.yml`); the local hooks are convenience.

## Pull requests

These apply to every contributor and every Claude session, because CI time and review rounds are
shared:

- **Open every PR as a draft** (`gh pr create --draft`). CI skips the test work on drafts (the required checks still report). Handle
  Copilot's review on the draft, posted AND suppressed comments, fixing all of them in one push, then
  mark it ready (`gh pr ready`) so every applicable check runs once.
- **Base PRs on `main`.** CI rules come from the PR's base branch, so a PR stacked on an older branch
  runs without them.
- **Stay in the issue's scope.** Findings outside it go into one follow-up issue, not another round on
  this PR. When a finding hits one code path, fix every path of the same shape in the same push.
- **Reply to each review comment** when you push its fix, or say why you are not fixing it.
- **Release PRs** change only the version strings and the changelog heading; CI skips their tests.
  A PR that depends on a new agami-core release (not the release PR itself) waits until PyPI lists
  that version.

## Code conventions

- **Match the surrounding code.** Its naming, comment density, and idioms are the spec.
- **Naming:** `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_CASE` for
  constants (PEP 8). ruff enforces the mechanical parts.
- **Functions do one thing.** Prefer small, single-purpose functions over deep branching.
- **Type-annotate signatures.** Public functions take and return typed values.
- **Comments explain _why_, not _what_.** The code says what; a comment earns its place by saying
  why — the non-obvious constraint, the edge case, the reason it isn't the simpler thing.
- **No network egress in shipped scripts.** `tests/test_privacy_no_network.py` is a contract; new
  egress fails the build. Everything runs locally on the user's machine.

## Customer-safety rules (this is a public repo)

agami-core is public. **No real customer data, names, or credentials — anywhere**: code, comments,
tests, docstrings, fixtures, sample data, or docs.

- **Never branch on a customer:** no `if customer == "...":`. Behavior is configured by the model,
  never hard-coded per customer.
- **Use neutral placeholders:** `acme` / `demo` for orgs, `you@example.com` for emails,
  `your-cluster…` for hosts, `SALES_DATA` for schemas. Never a real name, dataset, or address.
- **Never commit a secret.** No API keys, tokens, passwords, or connection strings with real
  credentials. `gitleaks` scans every commit and PR; treat a hit as a stop-the-line event.
- This is **pattern-based, with no denylist** — we don't keep a list of customer names to scan for
  (that list would itself be the leak). Write clean by default.

## Tests

- **New behavior comes with tests.** Add them under `tests/` as `test_<thing>.py`, following the
  existing self-contained style (`pytest.importorskip(...)`, `sys.path` to the scripts dir).
- **Check your change is covered:** `uv run dev.py cover` reports coverage of just the lines your
  branch touched and fails on untested ones — independent of overall coverage.
- **Coverage is floored in CI.** The gate is `--cov-fail-under` on the py3.12 leg of `lint + test`
  in `.github/workflows/ci.yml` — the one leg that traces coverage — and that flag is the single
  source of the number (don't hard-code a copy here). It is not in `pyproject.toml`, so a bare
  local `pytest` stays un-floored.

## Proposing a substantial change

For anything beyond a small fix, a short written spec keeps the change reviewable. Cover these
seven points (briefly — a paragraph each is plenty):

1. **Goal** — the one thing this change achieves.
2. **Acceptance criteria** — how we know it's done (decidable checks).
3. **Public interface** — the functions / CLI / files it adds or changes.
4. **Test plan** — what tests prove it, including edge cases.
5. **Customer-safety check** — confirm no real names/data/credentials (per the rules above).
6. **Docs impact** — what README / CONTRIBUTING / this file needs updating.
7. **Out of scope** — what this change deliberately does *not* do.
