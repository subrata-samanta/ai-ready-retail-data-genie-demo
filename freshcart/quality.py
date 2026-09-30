"""Data-quality checks and the "what did the pipeline fix?" report.

Two kinds of check:
  * RULES   must return 0. A non-zero result fails the tests (and would fail a CI run).
  * FINDINGS are counts of problems the pipeline found and handled. They are expected to be
    non-zero on this deliberately messy data; the docs show them so you can see each fix.

    python -m freshcart.quality
"""
from __future__ import annotations

from .db import connect, scalar

RULES = [
    ("gold", "No voided, cancelled or training lines in the sales fact",
     "SELECT COUNT(*) FROM gold.fct_sales_line WHERE line_type NOT IN ('Sale', 'Return')"),
    ("gold", "Returns are negative, sales are positive",
     "SELECT COUNT(*) FROM gold.fct_sales_line WHERE (line_type = 'Return' AND net_sales_amount_usd > 0) "
     "OR (line_type = 'Sale' AND quantity_units < 0)"),
    ("gold", "net_sales = gross_sales - discount on every line",
     "SELECT COUNT(*) FROM gold.fct_sales_line WHERE ABS(net_sales_amount_usd - (gross_sales_amount_usd - discount_amount_usd)) > 0.011"),
    ("gold", "gross_margin = net_sales - cost_of_goods on every line",
     "SELECT COUNT(*) FROM gold.fct_sales_line WHERE ABS(gross_margin_usd - (net_sales_amount_usd - cost_of_goods_usd)) > 0.011"),
    ("gold", "Every sales date is in dim_date",
     "SELECT COUNT(*) FROM gold.fct_sales_line f LEFT JOIN gold.dim_date d ON d.calendar_date = f.sales_date WHERE d.calendar_date IS NULL"),
    ("gold", "Exactly 7 days flagged as last completed fiscal week",
     "SELECT ABS(COUNT(*) - 7) FROM gold.dim_date WHERE is_last_completed_fiscal_week"),
    ("gold", "YTD and prior YTD cover the same number of days",
     "SELECT ABS(SUM(is_fiscal_ytd) - SUM(is_prior_fiscal_ytd)) FROM gold.dim_date"),
    ("gold", "No stock row for an UNKNOWN store or product",
     "SELECT COUNT(*) FROM gold.fct_inventory_daily WHERE store_id = 'UNKNOWN' OR product_id = 'UNKNOWN'"),
    ("silver", "One current version per store in the SCD2 history",
     "SELECT COUNT(*) FROM (SELECT store_id FROM silver.store_history WHERE is_current = 1 GROUP BY store_id HAVING COUNT(*) <> 1)"),
    ("silver", "An FX rate exists for every day up to the as-of date",
     "SELECT COUNT(*) FROM silver.fiscal_calendar c WHERE c.calendar_date BETWEEN '2025-01-02' AND date('${AS_OF_DATE}', '-1 day') "
     "AND NOT EXISTS (SELECT 1 FROM silver.fx_rate_daily f WHERE f.rate_date = c.calendar_date AND f.from_currency = 'CAD')"),
    ("silver", "One product row per SKU after deduplication",
     "SELECT COUNT(*) - COUNT(DISTINCT product_id) FROM silver.product"),
    ("silver", "No personal data outside the restricted table",
     "SELECT COUNT(*) FROM silver.pos_sales_line WHERE customer_id GLOB '60[0-9]*' AND length(customer_id) = 10"),
]

FINDINGS = [
    ("POS rows in bronze", "SELECT COUNT(*) FROM bronze.pos_tlog_raw"),
    ("Duplicate POS rows removed (store re-sent 10 June 2025)",
     "SELECT (SELECT COUNT(*) FROM bronze.pos_tlog_raw) - (SELECT COUNT(*) FROM silver.pos_sales_line) "
     "- (SELECT COUNT(*) FROM silver.quarantine_pos_sales_line)"),
    ("POS rows quarantined (broke a hard rule)", "SELECT COUNT(*) FROM silver.quarantine_pos_sales_line"),
    ("Voided POS lines excluded from gold", "SELECT COUNT(*) FROM silver.pos_sales_line WHERE line_type = 'Void'"),
    ("Training-register lines excluded from gold", "SELECT COUNT(*) FROM silver.pos_sales_line WHERE is_test_transaction = 1"),
    ("Cancelled online lines excluded from gold", "SELECT COUNT(*) FROM silver.online_sales_line WHERE line_type = 'Cancelled'"),
    ("Online orders whose date would shift if converted to UTC",
     "SELECT COUNT(*) FROM bronze.ecom_order_raw WHERE substr(json_extract(value, '$.orderTs'), 1, 10) <> date(json_extract(value, '$.orderTs'))"),
    ("Product master rows (SKU x sales org) in bronze", "SELECT COUNT(*) FROM bronze.product_master_raw"),
    ("Products after deduplication", "SELECT COUNT(*) FROM silver.product"),
    ("Sales lines mapped to the UNKNOWN product", "SELECT COUNT(*) FROM gold.fct_sales_line WHERE product_id = 'UNKNOWN'"),
    ("Late-arriving loyalty members (placeholders)", "SELECT COUNT(*) FROM gold.dim_customer WHERE is_placeholder = 1"),
    ("FX days filled from the previous business day", "SELECT COUNT(*) FROM silver.fx_rate_daily WHERE from_currency = 'CAD' AND is_carried_forward = 1"),
    ("Negative stock readings clamped to zero", "SELECT COUNT(*) FROM silver.inventory_daily WHERE had_negative_stock = 1"),
    ("Store versions kept in history (SCD2)", "SELECT COUNT(*) FROM silver.store_history"),
]


def run_rules(con):
    from .db import render_sql
    return [(layer, name, scalar(con, render_sql(sql))) for layer, name, sql in RULES]


def run_findings(con):
    return [(name, scalar(con, sql)) for name, sql in FINDINGS]


def main():
    con = connect()
    print("RULES (must be 0)")
    for layer, name, n in run_rules(con):
        print(f"  [{'PASS' if n == 0 else 'FAIL'}] {layer:<6} {name}: {n}")
    print("FINDINGS (problems found and handled)")
    for name, n in run_findings(con):
        print(f"  {n:>8,}  {name}")


if __name__ == "__main__":
    main()
