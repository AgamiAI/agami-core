"""Shared canned `information_schema` runner for introspect-style tests.

`semantic_model.introspect` drives a DB through a `runner(sql) -> list[dict]`
callback. Several test modules each hand-rolled the same dispatch (schemata →
tables → columns → PRIMARY KEY → FOREIGN KEY → reltuples, plus an optional
sample-rows probe). `make_catalog_runner` builds that dispatch from a compact
table+column spec, so a test declares only the catalog shape it cares about.
"""

from __future__ import annotations

from typing import Callable, Optional


def col(name: str, data_type: str, *, nullable: bool = True, scale: str | int = "") -> dict:
    """One `information_schema.columns` row, minus `ordinal_position` (the builder
    assigns that from list order). `nullable=False` marks a key column."""
    return {"column_name": name, "data_type": data_type,
            "is_nullable": "YES" if nullable else "NO", "numeric_scale": str(scale)}


def make_catalog_runner(
    *, tables: list[str], columns: dict[str, list[dict]], schema: str = "public",
    fks: list[dict] = (), pk: Optional[str] = "id", estimate: Optional[str] = "1000",
    sample: Optional[Callable[[str], list[dict]]] = None,
) -> Callable[[str], list[dict]]:
    """Build a `runner(sql)` answering the catalog queries introspect issues.

    tables   — bare table names, all in `schema`.
    columns  — {table: [col(...), ...]}; the builder stamps `ordinal_position`.
    fks      — FK rows ({from_table, from_column, to_table, to_column[, *_schema]}).
    pk       — PK column name shared by every table (None → no catalog PK).
    estimate — reltuples row estimate (None → none reported).
    sample   — optional callable for the `SELECT … LIMIT` sample probe.
    """
    table_rows = [{"schema_name": schema, "table_name": t, "table_type": "BASE TABLE"} for t in tables]
    cols_by_table = {
        t: [{**c, "ordinal_position": str(i + 1)} for i, c in enumerate(cs)]
        for t, cs in columns.items()
    }

    def run(sql: str) -> list[dict]:
        s = " ".join(sql.split())
        if "information_schema.schemata" in s:
            return [{"schema_name": schema}]
        if "information_schema.tables" in s and "table_type" in s:
            return list(table_rows)
        if "information_schema.columns" in s:
            for t in tables:
                if f"'{t}'" in s:
                    return list(cols_by_table.get(t, []))
            return list(cols_by_table.get(tables[0], [])) if len(tables) == 1 else []
        if "PRIMARY KEY" in s:
            return [{"column_name": pk}] if pk else []
        if "FOREIGN KEY" in s:
            return list(fks)
        if "reltuples" in s:
            return [{"estimated_rows": estimate}] if estimate is not None else []
        if sample is not None and s.upper().startswith("SELECT") and "LIMIT" in s.upper():
            return sample(sql)
        return []

    return run
