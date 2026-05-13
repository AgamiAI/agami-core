# Contributor tools

Manual, opt-in scripts. Not run in CI.

## `run_skill_evals.py`

Runs per-skill `evals.json` against the Anthropic API and grades each
response. Format follows Anthropic's skill-creator
([anthropics/skills](https://github.com/anthropics/skills)).

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# All skills
python3 tools/run_skill_evals.py

# A single skill
python3 tools/run_skill_evals.py --skill agami-save-correction

# With/without-skill baseline (Anthropic skill-creator parallel run)
python3 tools/run_skill_evals.py --skill agami-connect --baseline
```

Overrides via env: `AGAMI_EVAL_MODEL` (default `claude-opus-4-7`) and
`AGAMI_EVAL_GRADER` (default same). Exit code is 0 if every eval passes,
1 otherwise.

**Cost note:** each eval is 2 API calls (one runner + one grader),
4 with `--baseline`. Run on a single skill while iterating.
