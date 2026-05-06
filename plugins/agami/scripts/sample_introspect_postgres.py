#!/usr/bin/env python3
"""
Sample tier-3 (Python driver) introspection for Postgres.

This is a reference implementation, not a runtime dependency. The agami skill
itself drives introspection via SQL against information_schema executed
through whichever tier the user has (CLI / DuckDB / Python). Copy this file
into your own project if you want a programmatic Postgres introspection.

Usage:
    python sample_introspect_postgres.py \\
        --host localhost --port 5432 --db shop \\
        --user agami_test --password agami_test_pw \\
        --out ~/.agami/shop.yaml

Requires: pip install psycopg2-binary pyyaml
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.stderr.write("psycopg2 is required. Install: pip install psycopg2-binary\n")
    sys.exit(1)

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML is required. Install: pip install pyyaml\n")
    sys.exit(1)


SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")


PG_TYPE_MAP = {
    "character varying": "string", "varchar": "string", "text": "string",
    "character": "string", "char": "string", "uuid": "string",
    "name": "string", "json": "string", "jsonb": "string",
    "smallint": "integer", "integer": "integer", "bigint": "integer", "int4": "integer",
    "int8": "integer", "int2": "integer", "serial": "integer", "bigserial": "integer",
    "numeric": "decimal", "decimal": "decimal", "real": "decimal", "double precision": "decimal",
    "money": "decimal",
    "timestamp without time zone": "timestamp", "timestamp with time zone": "timestamp",
    "timestamp": "timestamp", "timestamptz": "timestamp",
    "date": "date",
    "boolean": "boolean", "bool": "boolean",
}


def map_type(pg_type: str) -> str:
    return PG_TYPE_MAP.get(pg_type.lower(), "string")


def fetch(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchall()


def introspect(conn, db_name: str) -> dict:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 1. tables
    tables_rows = fetch(cur, """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_type = 'BASE TABLE'
          AND table_schema NOT IN %s
          AND table_schema NOT LIKE 'pg_toast%%'
          AND table_schema NOT LIKE 'pg_temp_%%'
        ORDER BY table_schema, table_name
    """, (SYSTEM_SCHEMAS,))

    # 2. columns
    cols_rows = fetch(cur, """
        SELECT table_schema, table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema NOT IN %s
        ORDER BY table_schema, table_name, ordinal_position
    """, (SYSTEM_SCHEMAS,))

    # 3. primary keys
    pk_rows = fetch(cur, """
        SELECT kcu.table_schema, kcu.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema    = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema NOT IN %s
    """, (SYSTEM_SCHEMAS,))

    # 4. foreign keys
    fk_rows = fetch(cur, """
        SELECT
          tc.table_schema  AS from_schema,
          tc.table_name    AS from_table,
          kcu.column_name  AS from_column,
          ccu.table_schema AS to_schema,
          ccu.table_name   AS to_table,
          ccu.column_name  AS to_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema    = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema    = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema NOT IN %s
    """, (SYSTEM_SCHEMAS,))

    cur.close()

    # Build column → primary-key set
    pk_set = {(r["table_schema"], r["table_name"], r["column_name"]) for r in pk_rows}

    # Index FKs by source column
    fk_by_col: dict[tuple, dict] = {}
    for r in fk_rows:
        key = (r["from_schema"], r["from_table"], r["from_column"])
        fk_by_col[key] = {"table": f"{r['to_schema']}.{r['to_table']}", "column": r["to_column"]}

    # Group columns by table
    cols_by_table: dict[tuple, list] = defaultdict(list)
    for r in cols_rows:
        cols_by_table[(r["table_schema"], r["table_name"])].append(r)

    # Build the model
    tables_out = []
    for t in tables_rows:
        schema, table = t["table_schema"], t["table_name"]
        columns_out: dict[str, dict] = {}
        for c in cols_by_table[(schema, table)]:
            col = {
                "type": map_type(c["data_type"]),
                "description": "",
            }
            if (schema, table, c["column_name"]) in pk_set:
                col["primary_key"] = True
            fk = fk_by_col.get((schema, table, c["column_name"]))
            if fk:
                col["foreign_key"] = fk
            columns_out[c["column_name"]] = col

        relationships = []
        for c in cols_by_table[(schema, table)]:
            fk = fk_by_col.get((schema, table, c["column_name"]))
            if fk:
                relationships.append({
                    "from_column": c["column_name"],
                    "to_table": fk["table"],
                    "to_column": fk["column"],
                    "join_type": "LEFT JOIN",
                    "description": "",
                })

        tables_out.append({
            "table_name": table,
            "schema_name": schema,
            "label": table,
            "display_name": table.replace("_", " ").title(),
            "description": "",
            "columns": columns_out,
            "entities": [],
            "measures": {},
            "relationships": relationships,
        })

    return {
        "database_name": db_name,
        "database_type": "PostgreSQL",
        "description": "",
        "tables": tables_out,
        "metrics": {},
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("PGHOST", "localhost"))
    p.add_argument("--port", default=os.environ.get("PGPORT", "5432"))
    p.add_argument("--db", required=True)
    p.add_argument("--user", default=os.environ.get("PGUSER"))
    p.add_argument("--password", default=os.environ.get("PGPASSWORD"))
    p.add_argument("--out", required=True, help="output yaml path")
    args = p.parse_args()

    conn = psycopg2.connect(host=args.host, port=args.port, dbname=args.db,
                            user=args.user, password=args.password)
    try:
        model = introspect(conn, args.db)
    finally:
        conn.close()

    out = Path(os.path.expanduser(args.out))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        yaml.safe_dump(model, f, sort_keys=False, width=120)
    print(f"Wrote {out} ({len(model['tables'])} tables)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
