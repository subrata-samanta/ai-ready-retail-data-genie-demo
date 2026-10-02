"""Gold table contracts: one YAML file per table is the single source of truth for
column names, types, nullability, comments, keys, tags and clustering.

From each contract we generate
  * the local SQLite DDL (STRICT tables, enforced primary and foreign keys)
  * the Databricks DDL (COMMENTs, PRIMARY KEY ... RELY, FOREIGN KEYs, CLUSTER BY, TAGS)
  * the data dictionary in the docs

    python -m freshcart.export_databricks   # regenerates databricks/gold/00_create_gold_tables.sql
"""
from __future__ import annotations

import yaml

from . import config as C

ORDER = ["dim_date", "dim_store", "dim_product", "dim_customer", "dim_promotion",
         "fct_sales_line", "fct_inventory_daily"]

_SQLITE_TYPE = {"STRING": "TEXT", "DATE": "TEXT", "TIMESTAMP": "TEXT", "INT": "INTEGER",
                "BIGINT": "INTEGER", "BOOLEAN": "INTEGER", "DOUBLE": "REAL"}


def load(table: str) -> dict:
    return yaml.safe_load((C.CONTRACTS_DIR / "gold" / f"{table}.yml").read_text(encoding="utf-8"))


def all_contracts() -> list[dict]:
    return [load(t) for t in ORDER]


def sqlite_type(t: str) -> str:
    return "REAL" if t.upper().startswith("DECIMAL") else _SQLITE_TYPE[t.upper()]


def sqlite_ddl(c: dict) -> str:
    lines = []
    for col in c["columns"]:
        line = f'  {col["name"]} {sqlite_type(col["type"])}'
        if col.get("nullable") is False:
            line += " NOT NULL"
        if col["type"].upper() == "BOOLEAN":
            line += f' CHECK ({col["name"]} IN (0, 1))'
        lines.append(line)
    lines.append(f'  PRIMARY KEY ({", ".join(c["primary_key"])})')
    for fk in c.get("foreign_keys", []):
        lines.append(f'  FOREIGN KEY ({fk["column"]}) REFERENCES {fk["references"]} ({fk["ref_column"]})')
    return f'CREATE TABLE IF NOT EXISTS gold.{c["table"]} (\n' + ",\n".join(lines) + "\n) STRICT;"


def create_gold_tables(con) -> None:
    for c in all_contracts():
        con.execute(sqlite_ddl(c))
    # SQLite has no COMMENT syntax, so the comments live in a small metadata table
    con.execute("DROP TABLE IF EXISTS gold._column_comments")
    con.execute("CREATE TABLE gold._column_comments (table_name TEXT, column_name TEXT, data_type TEXT, "
                "is_nullable INTEGER, comment TEXT, ordinal INTEGER)")
    con.execute("DROP TABLE IF EXISTS gold._table_comments")
    con.execute("CREATE TABLE gold._table_comments (table_name TEXT PRIMARY KEY, comment TEXT)")
    for c in all_contracts():
        con.execute("INSERT INTO gold._table_comments VALUES (?, ?)", (c["table"], c["comment"]))
        con.executemany("INSERT INTO gold._column_comments VALUES (?, ?, ?, ?, ?, ?)",
                        [(c["table"], col["name"], col["type"], 0 if col.get("nullable") is False else 1,
                          col.get("comment", ""), i) for i, col in enumerate(c["columns"], start=1)])


def _q(text: str) -> str:
    assert "'" not in text, f"avoid apostrophes in comments: {text}"
    return f"'{text}'"


def databricks_ddl(c: dict) -> str:
    t = f'{C.DATABRICKS_CATALOG}.gold.{c["table"]}'
    width = max(len(col["name"]) for col in c["columns"])
    lines = []
    for col in c["columns"]:
        nn = " NOT NULL" if col.get("nullable") is False else ""
        lines.append(f'  {col["name"]:<{width}} {col["type"]:<13}{nn:<9} COMMENT {_q(col.get("comment", ""))}')
    lines.append(f'  CONSTRAINT pk_{c["table"]} PRIMARY KEY ({", ".join(c["primary_key"])}) RELY')
    for fk in c.get("foreign_keys", []):
        lines.append(f'  CONSTRAINT fk_{c["table"]}_{fk["column"]} FOREIGN KEY ({fk["column"]}) '
                     f'REFERENCES {C.DATABRICKS_CATALOG}.gold.{fk["references"]} ({fk["ref_column"]})')
    ddl = f"CREATE TABLE IF NOT EXISTS {t} (\n" + ",\n".join(lines) + f"\n)\nCOMMENT {_q(c['comment'])}"
    if c.get("cluster_by"):
        ddl += f'\nCLUSTER BY ({", ".join(c["cluster_by"])})'
    ddl += ";\n"
    if c.get("tags"):
        tags = ", ".join(f"'{k}' = '{v}'" for k, v in c["tags"].items())
        ddl += f"ALTER TABLE {t} SET TAGS ({tags});\n"
    for col, tags in (c.get("column_tags") or {}).items():
        tag_sql = ", ".join(f"'{k}' = '{v}'" for k, v in tags.items())
        ddl += f"ALTER TABLE {t} ALTER COLUMN {col} SET TAGS ({tag_sql});\n"
    return ddl


def write_databricks_ddl() -> None:
    header = ("-- GENERATED from contracts/gold/*.yml by `python -m freshcart.export_databricks`. Do not edit by hand.\n"
              "-- Creates the gold star schema in Unity Catalog with comments, keys, clustering and tags.\n"
              "-- Dimensions are created before facts so foreign keys can reference them.\n"
              "-- Note: dimension primary-key columns must be NOT NULL; constraints are informational.\n\n")
    out = header + "\n".join(databricks_ddl(c) for c in all_contracts())
    path = C.ROOT / "databricks" / "gold" / "00_create_gold_tables.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out, encoding="utf-8")
    print(f"wrote {path.relative_to(C.ROOT)}")


if __name__ == "__main__":
    write_databricks_ddl()
