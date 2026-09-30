"""Bronze: land every source file exactly as delivered.

Rules of the bronze layer
  * every column is stored as text, nothing is cast, renamed or cleaned
  * three audit columns are added: _source_file, _row_number, _ingested_at
  * ingestion is incremental: a file that has been loaded before is skipped
    (this is what Auto Loader / read_files with a checkpoint does on Databricks)

Databricks equivalent: databricks/pipelines/01_bronze.sql
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone

from . import config as C

# table name -> (glob inside data/raw, format, description)
FEEDS = {
    "pos_tlog_raw":           ("pos/pos_tlog_*.csv",                "csv",    "POS transaction log, one file per month"),
    "ecom_order_raw":         ("ecommerce/ecom_orders_*.ndjson",    "ndjson", "Online orders, one JSON document per line"),
    "product_master_raw":     ("erp/product_master.csv",            "csv",    "ERP material master, one row per SKU per sales org"),
    "merch_hierarchy_raw":    ("erp/merch_hierarchy.csv",           "csv",    "Material group code to department/category/subcategory"),
    "cost_history_raw":       ("erp/cost_history.csv",              "csv",    "Standard cost with effective dates"),
    "store_master_raw":       ("stores/store_master_*.csv",         "csv",    "Monthly snapshots of the store master"),
    "ref_region_raw":         ("stores/ref_region.csv",             "csv",    "Region code lookup"),
    "ref_store_format_raw":   ("stores/ref_store_format.csv",       "csv",    "Store format code lookup"),
    "ref_state_province_raw": ("stores/ref_state_province.csv",     "csv",    "State/province code lookup"),
    "crm_member_raw":         ("crm/crm_members_*.csv",             "csv",    "Loyalty CRM member extract (contains personal data)"),
    "promo_calendar_raw":     ("promotions/promo_calendar.csv",     "csv",    "Promotion calendar"),
    "promo_item_raw":         ("promotions/promo_items.csv",        "csv",    "Products in each promotion"),
    "fiscal_calendar_raw":    ("finance/fiscal_calendar.csv",       "csv",    "Finance's 4-5-4 fiscal calendar"),
    "fx_rate_raw":            ("finance/fx_rates.csv",              "csv",    "CAD to USD rates, business days only"),
    "inventory_raw":          ("inventory/inv_snapshot_*.csv",      "csv",    "Nightly stock snapshots"),
}


def _ensure_log(con):
    con.execute("""CREATE TABLE IF NOT EXISTS bronze._ingestion_log (
        source_file TEXT PRIMARY KEY, target_table TEXT, row_count INTEGER, ingested_at TEXT)""")


def _ensure_table(con, table, columns):
    cols = ", ".join(f'"{c}" TEXT' for c in columns)
    con.execute(f'CREATE TABLE IF NOT EXISTS bronze.{table} ({cols}, _source_file TEXT, _row_number INTEGER, _ingested_at TEXT)')


def ingest(con, verbose=True) -> dict:
    _ensure_log(con)
    done = {r[0] for r in con.execute("SELECT source_file FROM bronze._ingestion_log")}
    stats = {}
    for table, (pattern, fmt, _desc) in FEEDS.items():
        files = sorted(C.RAW_DIR.glob(pattern))
        new_files = [f for f in files if f.relative_to(C.RAW_DIR).as_posix() not in done]
        loaded_rows = 0
        for f in new_files:
            rel = f.relative_to(C.RAW_DIR).as_posix()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if fmt == "csv":
                with open(f, newline="", encoding="utf-8") as fh:
                    reader = csv.reader(fh)
                    header = next(reader)
                    data = [row + [rel, i, now] for i, row in enumerate(reader, start=1)]
            else:                                   # ndjson: keep each document whole, parse in silver
                header = ["value"]
                with open(f, encoding="utf-8") as fh:
                    data = [[line.rstrip("\n"), rel, i, now] for i, line in enumerate(fh, start=1) if line.strip()]
            _ensure_table(con, table, header)
            placeholders = ", ".join("?" * (len(header) + 3))
            con.execute("BEGIN")
            con.executemany(f"INSERT INTO bronze.{table} VALUES ({placeholders})", data)
            con.execute("INSERT INTO bronze._ingestion_log VALUES (?, ?, ?, ?)", (rel, table, len(data), now))
            con.execute("COMMIT")
            loaded_rows += len(data)
        stats[table] = dict(files_total=len(files), files_new=len(new_files), rows_loaded=loaded_rows)
        if verbose:
            print(f"  bronze.{table:<24} {len(new_files):>3} new of {len(files):>3} files  {loaded_rows:>8,} rows")
    return stats


def feed_catalog():
    """Used by the docs: one row per feed."""
    return [(t, p, f, desc) for t, (p, f, desc) in FEEDS.items()]


if __name__ == "__main__":
    from .db import connect
    ingest(connect())
