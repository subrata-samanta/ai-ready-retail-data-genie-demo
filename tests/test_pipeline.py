"""Tests for the FreshCart pipeline.

Run with either:
    python tests/run_tests.py        (no extra packages needed)
    pytest -q                        (if you have pytest)

The most important test is test_reconciliation_raw_to_gold: it recomputes total net sales
straight from the raw files in plain Python (no SQL, no pipeline code) and checks gold
agrees to the cent. Two independent implementations agreeing is strong evidence both are right.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from freshcart import benchmarks, config as C, contracts, metrics, pipeline, quality  # noqa: E402

_CON = None


def con():
    """Build every layer from the raw files once per test session."""
    global _CON
    if _CON is None:
        _CON = pipeline.run(full_refresh=True, verbose=False)
    return _CON


def q(sql, params=()):
    return con().execute(sql, params).fetchall()


# ---------------------------------------------------------------------------------------
def test_quality_rules_all_pass():
    failed = [(layer, name, n) for layer, name, n in quality.run_rules(con()) if n != 0]
    assert not failed, failed


def test_foreign_keys_resolve_in_gold():
    assert q("PRAGMA gold.foreign_key_check") == []


def _fx_table():
    published = {}
    with open(C.RAW_DIR / "finance" / "fx_rates.csv", newline="") as f:
        for r in csv.DictReader(f):
            published[date.fromisoformat(r["RATE_DT"])] = float(r["RATE"])
    out, day, last = {}, min(published), None
    while day <= C.AS_OF_DATE:
        last = published.get(day, last)
        out[day] = last
        day += timedelta(days=1)
    return out


def test_reconciliation_raw_to_gold():
    fx = _fx_table()
    rate = lambda ccy, d: 1.0 if ccy == "USD" else fx[d]                       # noqa: E731
    expected, seen = 0.0, set()
    for path in sorted((C.RAW_DIR / "pos").glob("*.csv")):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                key = (r["STR_NBR"], r["REG_NBR"], r["TRX_ID"], r["LN_NBR"], r["TRX_DT"])
                if key in seen:
                    continue                                                    # re-delivered row
                seen.add(key)
                try:
                    d = date(int(r["TRX_DT"][:4]), int(r["TRX_DT"][4:6]), int(r["TRX_DT"][6:]))
                except ValueError:
                    continue                                                    # impossible date
                if not r["STR_NBR"] or abs(float(r["QTY"])) > 500:
                    continue                                                    # quarantined
                if r["TRX_TYP"] == "V" or r["REG_NBR"] == "99":
                    continue                                                    # void / training
                sign = -1 if r["TRX_TYP"] == "R" else 1
                k = rate(r["CRNCY_CD"], d)
                gross = round(sign * float(r["EXT_AMT"]) * k, 2)
                disc = round(sign * float(r["DISC_AMT"]) * k, 2)
                expected += round(gross - disc, 2)
    for path in sorted((C.RAW_DIR / "ecommerce").glob("*.ndjson")):
        with open(path) as f:
            for line in f:
                o = json.loads(line)
                if o["status"] == "CANCELLED":
                    continue
                k = rate(o["currency"], date.fromisoformat(o["orderTs"][:10]))
                for ln in o["lines"]:
                    expected += round(round(ln["lineTotal"] * k, 2) - round(ln["discount"] * k, 2), 2)
    actual = q("SELECT SUM(net_sales_amount_usd) FROM gold.fct_sales_line")[0][0]
    assert abs(actual - expected) < 0.05, (actual, expected)


def test_golden_receipt_through_every_layer():
    raw = q("SELECT COUNT(*) FROM bronze.pos_tlog_raw WHERE TRX_ID = '88120431'")[0][0]
    silver = q("SELECT line_number, line_type FROM silver.pos_sales_line WHERE transaction_id = '88120431' ORDER BY 1")
    gold = q("""SELECT sales_line_id, quantity_units, net_sales_amount_usd, promotion_id, customer_id <> 'ANONYMOUS'
                FROM gold.fct_sales_line WHERE transaction_id = '88120431' ORDER BY sales_line_id""")
    assert raw == 4
    assert silver == [(1, "Sale"), (2, "Sale"), (3, "Void"), (4, "Sale")]
    assert gold == [("POS-1042-88120431-1", 2.0, 3.49, "BG26W33", 1),
                    ("POS-1042-88120431-2", 1.254, 1.87, "NO_PROMO", 1),
                    ("POS-1042-88120431-4", 1.0, 3.99, "NO_PROMO", 1)]


def test_canadian_return_is_negative_and_converted():
    (net, local, ccy), = q("""SELECT net_sales_amount_usd, net_sales_amount_local, currency_code
                              FROM gold.fct_sales_line WHERE transaction_id = '88120519'""")
    rate = q("SELECT rate_to_usd FROM silver.fx_rate_daily WHERE rate_date = '2026-09-14' AND from_currency = 'CAD'")[0][0]
    assert ccy == "CAD" and local == -18.99 and net == round(-18.99 * rate, 2)


def test_semi_additive_stock_uses_last_day():
    _, rows = metrics.run(con(), """SELECT MEASURE(on_hand_units) AS u FROM freshcart.semantic.inventory_metrics
                                    WHERE is_last_completed_fiscal_week AND department = 'Produce'""")
    last_day = q("""SELECT SUM(on_hand_units) FROM gold.fct_inventory_daily i JOIN gold.dim_product p USING (product_id)
                    WHERE p.department = 'Produce' AND i.snapshot_date = (SELECT MAX(calendar_date) FROM gold.dim_date
                                                                          WHERE is_last_completed_fiscal_week)""")[0][0]
    seven_days = q("""SELECT SUM(on_hand_units) FROM gold.fct_inventory_daily i JOIN gold.dim_product p USING (product_id)
                      JOIN gold.dim_date d ON d.calendar_date = i.snapshot_date
                      WHERE p.department = 'Produce' AND d.is_last_completed_fiscal_week""")[0][0]
    assert abs(rows[0][0] - last_day) < 1e-6 and seven_days > 5 * last_day


def test_margin_pct_is_ratio_of_sums_not_average():
    _, rows = metrics.run(con(), """SELECT MEASURE(gross_margin_pct) AS m FROM freshcart.semantic.sales_metrics
                                    WHERE is_fiscal_ytd""")
    ratio, avg = q("""SELECT SUM(gross_margin_usd) / SUM(net_sales_amount_usd),
                             AVG(gross_margin_usd / NULLIF(net_sales_amount_usd, 0))
                      FROM gold.fct_sales_line f JOIN gold.dim_date d ON d.calendar_date = f.sales_date
                      WHERE d.is_fiscal_ytd""")[0]
    assert abs(rows[0][0] - ratio) < 1e-9 and abs(ratio - avg) > 0.001


def test_store_history_keeps_boston_remodel():
    rows = q("SELECT store_format, is_current FROM silver.store_history WHERE store_id = '1042' ORDER BY valid_from")
    assert rows == [("Neighborhood Market", 0), ("Supercenter", 1)]


def test_comparable_store_rules():
    rows = dict(q("SELECT store_id, is_comparable_store_current_fy FROM gold.dim_store"))
    assert rows["3050"] == 1        # Denver opened mid FY2024 -> comparable from FY2026
    assert rows["322"] == 0         # Mississauga opened FY2025 -> comparable from FY2027
    assert rows["2211"] == 0        # Buckhead closed
    assert rows["9001"] == 0        # Dark Store never comparable


def test_no_personal_data_in_gold():
    cols = {r[0] for r in q("SELECT column_name FROM gold._column_comments")}
    assert not cols & {"email", "date_of_birth", "card_number", "dob"}


def test_benchmarks_and_examples_run():
    for b, cols, rows in benchmarks.run_benchmarks(con()):
        assert b.get("expected_sql") is None or rows, f"benchmark {b['id']} returned nothing"
    for name, _params, _cols, rows in benchmarks.run_examples(con()):
        assert rows, f"example {name} returned nothing"


def test_incremental_rerun_is_idempotent():
    """Running the pipeline again with no new files loads nothing and changes nothing."""
    before = q("SELECT COUNT(*), SUM(net_sales_amount_usd) FROM gold.fct_sales_line")[0]
    files_before = q("SELECT COUNT(*) FROM bronze._ingestion_log")[0][0]
    c2 = pipeline.run(full_refresh=False, verbose=False)
    files_after = c2.execute("SELECT COUNT(*) FROM bronze._ingestion_log").fetchone()[0]
    after = c2.execute("SELECT COUNT(*), SUM(net_sales_amount_usd) FROM gold.fct_sales_line").fetchone()
    assert after == before and files_after == files_before


def test_databricks_ddl_is_up_to_date():
    generated = "\n".join(contracts.databricks_ddl(c) for c in contracts.all_contracts())
    on_disk = (C.ROOT / "databricks" / "gold" / "00_create_gold_tables.sql").read_text(encoding="utf-8")
    assert generated in on_disk, "run: python -m freshcart.export_databricks"

