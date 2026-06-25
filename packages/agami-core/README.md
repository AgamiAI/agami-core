# agami-core (library)

The importable core behind agami: the governed **semantic model**, the shared **MCP `TOOLS`
harness** (stdio entrypoint), and the **unified local query executor** (`execute_sql` + the
read-only safety pass + unit formatting).

One package serves every consumer — the local Claude Code skill, the MCP server, and any
downstream that imports the same flat module names.

## Install

```bash
pip install -e packages/agami-core            # executor + stdio harness (pure-stdlib)
pip install -e 'packages/agami-core[model]'   # + the semantic model (pydantic / sqlglot / pyyaml)
```

## Flat module names (an invariant)

`semantic_model`, `mcp_harness`, `execute_sql`, `agami_paths` are top-level importable names —
no `sys.path` manipulation, no parent package — so a consumer's imports resolve unchanged:

```python
from mcp_harness import TOOLS
import semantic_model
import execute_sql
```

## Entry points

```bash
python -m mcp_harness          # the stdio MCP server (Claude Desktop)
python -m execute_sql --sql …  # the local query executor
python -m semantic_model.cli   # the semantic-model CLI (driven by the `sm` launcher)
```
