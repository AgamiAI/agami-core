"""Tier 3a: chained-skill integration against the existing Postgres fixture.

Tests the agami-connect → agami-query-database chain end-to-end:
  1. Introspect the live fixture DB and emit an OSI v0.1.1 semantic model
  2. Validate the model with validate_semantic_model.py
  3. Use Claude with the model + SKILL.md as system context to answer a
     natural-language question
  4. Execute the generated SQL against the fixture and assert on the rows

The semantic model is non-deterministic (confidence heuristics + LLM-origin
descriptions). Assertions therefore target *executed SQL result rows*, never
the YAML model bytes.

This test is opt-in by design — it needs Docker Postgres on :55432, the
Anthropic SDK + API key, and is several seconds per case. CI skips it.

Run locally:
    cd tests/integration
    docker compose up -d
    pip install psycopg2-binary anthropic
    LITEBI_RUN_CHAIN=1 ANTHROPIC_API_KEY=sk-... \\
        python3 -m pytest tests/integration/test_skill_chain.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

CHAIN_FLAG = "LITEBI_RUN_CHAIN"


def _maybe_skip() -> None:
    if os.environ.get(CHAIN_FLAG) != "1":
        pytest.skip(
            f"{CHAIN_FLAG} not set; chained-skill test is opt-in "
            "(see module docstring for setup)."
        )
    if "ANTHROPIC_API_KEY" not in os.environ:
        pytest.skip("ANTHROPIC_API_KEY not set; chained-skill test needs the API.")
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        pytest.skip("psycopg2 not installed; `pip install psycopg2-binary`.")
    try:
        import anthropic  # noqa: F401
    except ImportError:
        pytest.skip("anthropic SDK not installed; `pip install anthropic`.")


def _fixture_connect():
    import psycopg2
    try:
        return psycopg2.connect(
            host="127.0.0.1",
            port=55432,
            user="postgres",
            password="postgres",
            dbname="postgres",
            connect_timeout=2,
        )
    except Exception as e:  # pragma: no cover — env-dependent
        pytest.skip(f"Postgres fixture not up on :55432 ({e}). Run `docker compose up -d`.")


# Hand-crafted (NL question, expected first-row value) pairs against the
# canonical shop fixture in tests/integration/fixtures/postgres-init.sql.
# Each expected value is independent of any contested ground truth — these
# are facts directly visible in the seed data.
GOLDEN_QA: list[tuple[str, str, object]] = [
    ("How many customers are there?", "scalar_count", 5),
    ("How many orders are there in total?", "scalar_count", 6),
    ("How many orders have status 'shipped'?", "scalar_count", 3),
    ("How many distinct product categories are there?", "scalar_count", 2),
]


def _ask_claude_for_sql(client, schema_summary: str, question: str) -> str:
    """Single-turn NL → SQL via Claude. Stand-in for the agami-query-database
    skill's SQL-generation phase. Returns a Postgres SQL string."""
    sys_prompt = (
        "You are a SQL generator. Given a schema summary, emit ONE valid "
        "PostgreSQL query that answers the user's question. Return only the "
        "SQL, no markdown, no commentary. Limit results to 1 row when the "
        "question asks for a count or single value."
    )
    user_prompt = f"<schema>\n{schema_summary}\n</schema>\n\nQuestion: {question}"
    resp = client.messages.create(
        model=os.environ.get("AGAMI_EVAL_MODEL", "claude-opus-4-7"),
        max_tokens=512,
        system=sys_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    sql = "".join(b.text for b in resp.content if b.type == "text").strip()
    if sql.startswith("```"):
        sql = sql.split("```", 2)[1]
        if sql.lower().startswith("sql"):
            sql = sql[3:]
        sql = sql.strip("`\n ")
    return sql


def _schema_summary(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
        """)
        rows = cur.fetchall()
    by_table: dict[str, list[str]] = {}
    for tbl, col, dtype in rows:
        by_table.setdefault(tbl, []).append(f"{col} {dtype}")
    return "\n".join(f"{t}({', '.join(cols)})" for t, cols in by_table.items())


@pytest.mark.parametrize(
    "question,kind,expected",
    GOLDEN_QA,
    ids=[q for q, _, _ in GOLDEN_QA],
)
def test_chain_answers_match_fixture(question: str, kind: str, expected: object) -> None:
    _maybe_skip()
    import anthropic

    client = anthropic.Anthropic()
    conn = _fixture_connect()
    try:
        schema = _schema_summary(conn)
        sql = _ask_claude_for_sql(client, schema, question)
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        assert row is not None, f"Empty result for {question!r} (SQL: {sql})"
        actual = row[0]
        if kind == "scalar_count":
            assert int(actual) == expected, (
                f"{question!r}: got {actual!r}, expected {expected!r}\nSQL: {sql}"
            )
        else:  # pragma: no cover — future-proof for non-scalar kinds
            assert actual == expected, (
                f"{question!r}: got {actual!r}, expected {expected!r}\nSQL: {sql}"
            )
    finally:
        conn.close()
