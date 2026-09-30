"""SQLite connection that behaves like a small lakehouse.

Each medallion layer is its own database file, attached under the layer's name, so SQL can
say `bronze.pos_tlog_raw`, `silver.pos_sales_line` or `gold.fct_sales_line`, just as
Databricks SQL says `freshcart.bronze.pos_tlog_raw` (the catalog prefix is dropped locally).

A few Python functions are registered so the local SQL reads like Databricks SQL:
    try_to_date(text, fmt)  -> ISO date or NULL       (Databricks: try_to_date)
    sha2(text, 256)         -> hex SHA-256            (Databricks: sha2)
    initcap(text)           -> Title Case             (Databricks: initcap)
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config as C

MIN_SQLITE = (3, 38, 0)   # JSON functions built in, FILTER clause, window functions, UPSERT

_FORMATS = {
    "yyyyMMdd": "%Y%m%d",
    "yyyy-MM-dd": "%Y-%m-%d",
    "dd/MM/yyyy": "%d/%m/%Y",
    "MM/dd/yyyy": "%m/%d/%Y",
}


def try_to_date(value, fmt):
    """Strict date parser: returns None for impossible dates such as 30 February.

    Note: SQLite's own date() silently rolls 2025-02-30 forward to 2025-03-02,
    which is exactly the kind of silent corruption a pipeline must not allow.
    """
    if value is None or str(value).strip() == "":
        return None
    try:
        return datetime.strptime(str(value).strip(), _FORMATS[fmt]).date().isoformat()
    except (ValueError, KeyError):
        return None


def sha2(value, bits=256):
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def initcap(value):
    if value is None:
        return None
    return " ".join(w[:1].upper() + w[1:].lower() for w in str(value).split(" "))


def connect(fresh_layers: list[str] | None = None) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ required, found {sqlite3.sqlite_version}. "
                           "Upgrade Python (3.11+ ships a recent SQLite).")
    C.WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
    for layer in fresh_layers or []:
        p = C.WAREHOUSE_DIR / f"{layer}.db"
        if p.exists():
            p.unlink()
    con = sqlite3.connect(":memory:", isolation_level=None)
    for layer in C.LAYERS:
        con.execute(f"ATTACH DATABASE '{C.WAREHOUSE_DIR / (layer + '.db')}' AS {layer}")
    con.execute("PRAGMA foreign_keys = ON")
    con.create_function("try_to_date", 2, try_to_date, deterministic=True)
    con.create_function("sha2", 2, sha2, deterministic=True)
    con.create_function("initcap", 1, initcap, deterministic=True)
    return con


def render_sql(sql: str, params: dict | None = None) -> str:
    """Replace ${NAME} placeholders (the same syntax Databricks pipelines use for parameters)."""
    params = {"AS_OF_DATE": C.AS_OF_DATE.isoformat(), **(params or {})}
    return re.sub(r"\$\{(\w+)\}", lambda m: str(params[m.group(1)]), sql)


def run_sql_file(con: sqlite3.Connection, path: Path, params: dict | None = None) -> None:
    con.executescript(render_sql(path.read_text(encoding="utf-8"), params))


def rows(con, sql, params=()):
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return cols, cur.fetchall()


def scalar(con, sql, params=()):
    return con.execute(sql, params).fetchone()[0]
