#!/usr/bin/env python3
"""
Sample tier-3 (Python driver) introspection for MySQL / MariaDB.

Same shape as sample_introspect_postgres.py — see that file's docstring.

Requires: pip install pymysql pyyaml
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    sys.stderr.write("pymysql is required. Install: pip install pymysql\n")
    sys.exit(1)

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML is required. Install: pip install pyyaml\n")
    sys.exit(1)


SYSTEM_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")


MYSQL_TYPE_MAP = {
    "varchar": "string", "char": "string", "text": "string", "tinytext": "string",
    "mediumtext": "string", "longtext": "string", "enum": "string", "set": "string",
    "json": "string", "binary": "string", "varbinary": "string",
    "tinyint": "integer", "smallint": "integer", "mediumint": "integer",
    "int": "integer", "bigint": "integer",
    "decimal": "decimal", "numeric": "decimal", "float": "decimal", "double": "decimal",
    "datetime": "timestamp", "timestamp": "timestamp",
    "date": "date",
    "bool": "boolean", "boolean": "boolean",
}


def map_type(mysql_type: str, column_type: str = "") -> str:
    base = mysql_type.lower()
    if base == "tinyint" and column_type.lower() == "tinyint(1)":
        return "boolean"
    return MYSQL_TYPE_MAP.get(base, "string")


def introspect(conn, db_name: str) -> dict:
    cur = conn.cursor(pymysql.cursors.DictCursor)

    placeholders = ", ".join(["%s"] * len(SYSTEM_SCHEMAS))

    cur.execute(f"""
        SELECT table_schema, table_name FROM information_schema.tables
        WHERE table_type = 'BASE TABLE'
          AND table_schema = %s
        ORDER BY table_schema, table_name
    """, (db_name,))
    tables_rows = cur.fetchall()

    cur.execute(f"""
        SELECT table_schema, table_name, column_name, data_type, column_type, column_key, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s
        ORDER BY table_schema, table_name, ordinal_position
    """, (db_name,))
    cols_rows = cur.fetchall()

    cur.execute(f"""
        SELECT
          table_schema           AS from_schema,
          table_name             AS from_table,
          column_name            AS from_column,
          referenced_table_schema AS to_schema,
          referenced_table_name   AS to_table,
          referenced_column_name  AS to_column
        FROM information_schema.key_column_usage
        WHERE referenced_table_name IS NOT NULL
          AND table_schema = %s
    """, (db_name,))
    fk_rows = cur.fetchall()

    cur.close()

    pk_set = {(c["table_schema"], c["table_name"], c["column_name"])
              for c in cols_rows if c["column_key"] == "PRI"}

    fk_by_col: dict[tuple, dict] = {}
    for r in fk_rows:
        key = (r["from_schema"], r["from_table"], r["from_column"])
        fk_by_col[key] = {"table": f"{r['to_schema']}.{r['to_table']}", "column": r["to_column"]}

    cols_by_table: dict[tuple, list] = defaultdict(list)
    for r in cols_rows:
        cols_by_table[(r["table_schema"], r["table_name"])].append(r)

    tables_out = []
    for t in tables_rows:
        schema, table = t["table_schema"], t["table_name"]
        columns_out: dict[str, dict] = {}
        for c in cols_by_table[(schema, table)]:
            col = {
                "type": map_type(c["data_type"], c.get("column_type", "")),
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
        "database_type": "MySQL",
        "description": "",
        "tables": tables_out,
        "metrics": {},
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("MYSQL_HOST", "localhost"))
    p.add_argument("--port", default=os.environ.get("MYSQL_PORT", "3306"))
    p.add_argument("--db", required=True)
    p.add_argument("--user", default=os.environ.get("MYSQL_USER"))
    p.add_argument("--password", default=os.environ.get("MYSQL_PWD"))
    p.add_argument("--out", required=True, help="output yaml path")
    args = p.parse_args()

    conn = pymysql.connect(host=args.host, port=int(args.port), database=args.db,
                           user=args.user, password=args.password,
                           charset="utf8mb4", autocommit=True)
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
