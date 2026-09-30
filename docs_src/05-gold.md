# 5 · Gold: from silver to a business star schema

[← 4 · Silver](04-silver.md) · Next: [6 · Semantic layer →](06-semantic-layer.md)

Gold is what the business (and Genie) sees. It answers the question from the article:
**how are the fact and dimension tables built from silver?** This page goes table by table.

## How every gold load works

Each gold load does the same three kinds of work:

1. **Select and conform**: pick the business columns, drop technical ones, take the current version of each record.
2. **Enrich and derive**: look up values from other tables, calculate amounts, precompute flags.
3. **Protect integrity**: one row per key, and every fact row points to a real dimension row or an explicit Unknown member.

And three mechanics hold for every table:

- **Tables come from contracts.** Each gold table is created from a YAML contract in
  [`contracts/gold/`](../contracts/gold/) that lists every column's type, nullability and comment, the keys and tags.
  The same contract generates the Databricks DDL, so local and Databricks can't drift.
- **Loads are upserts.** `INSERT ... ON CONFLICT DO UPDATE` locally, `MERGE` on Databricks. Re-running never duplicates.
- **Order matters.** Dimensions load first, facts last, because facts look up dimensions. Locally, SQLite enforces
  every foreign key, so a fact row pointing at a missing dimension row stops the pipeline.

| Order | Gold table | Built from (silver) | Grain | Load |
|---|---|---|---|---|
| 1 | `dim_date` | `fiscal_calendar` | one row per day | upsert every run (flags move daily) |
| 2 | `dim_store` | `store_history` (current) + `fiscal_calendar` | one row per store + UNKNOWN | upsert every run |
| 3 | `dim_product` | `product` | one row per SKU + UNKNOWN | upsert every run |
| 4 | `dim_promotion` | `promotion` | one row per promotion + NO_PROMO | upsert every run |
| 5 | `dim_customer` | `loyalty_member` + customer ids in sales | one row per customer + ANONYMOUS | merge (keeps placeholders) |
| 6 | `fct_sales_line` | `pos_sales_line` ∪ `online_sales_line` + dims + `product_cost` | one row per receipt line | **incremental** by watermark |
| 7 | `fct_inventory_daily` | `inventory_daily` + dims + `product_cost` | one row per store, product, day | upsert by key |

---

## Gold 01 · `dim_date`

**Steps**: copy the fiscal attributes; find "yesterday" and the last fully completed fiscal week relative to the as-of date
(pinned to {{v: SELECT '${AS_OF_DATE}'}} for reproducibility, `current_date()` in production); precompute five rolling flags.

The flags turn time questions into simple filters. "Last week" is exactly these 7 days:

<!--table
SELECT calendar_date, day_of_week_name, fiscal_year, fiscal_period_name, fiscal_week, fiscal_day_of_year,
       is_last_completed_fiscal_week, is_last_4_completed_fiscal_weeks, is_fiscal_ytd
FROM gold.dim_date WHERE is_last_completed_fiscal_week ORDER BY calendar_date
-->

And "year to date versus last year" compares the same **fiscal days**, which line up weekdays:

<!--table
SELECT fiscal_year, MIN(calendar_date) AS first_day, MAX(calendar_date) AS last_day, COUNT(*) AS days,
       MIN(day_of_week_name) FILTER (WHERE fiscal_day_of_year = 1) AS starts_on
FROM gold.dim_date WHERE is_fiscal_ytd OR is_prior_fiscal_ytd GROUP BY fiscal_year
-->

Without these flags, Genie would have to work out a 4-5-4 calendar from raw dates on every question, and get it wrong
whenever it assumed calendar months or ISO weeks.

<!--file pipeline/gold/01_load_dim_date.sql-->

---

## Gold 02 · `dim_store`

**Steps**: take the **current** version from `silver.store_history`; look up the fiscal year and day the store opened;
apply the **comparable (like-for-like) rule once, here**; derive status; add `UNKNOWN`.

The comparable rule: a store is comparable in fiscal year *N* if it traded **all** of year *N-1*.
Opened on day 1 of a fiscal year → comparable from the next year; opened any other day → from the year after next;
Dark Stores → never.

Before (silver, current versions):

<!--table
SELECT store_id, store_name, store_format, region, open_date, close_date, is_current
FROM silver.store_history WHERE is_current = 1 ORDER BY store_id
-->

After (gold):

<!--table
SELECT store_id, store_name, store_format, region, open_date, open_fiscal_year, store_status,
       comparable_from_fiscal_year, is_comparable_store_current_fy
FROM gold.dim_store ORDER BY store_id
-->

Read the last three columns against the story from [doc 1](01-business-and-questions.md): Denver opened mid-FY2024 so it is
comparable from FY2026; Mississauga opened in FY2025 so not until FY2027; Buckhead is closed; the Newark Dark Store never is.
Genie never has to know this rule. It only filters on `is_comparable_store` or calls the like-for-like function.

<!--file pipeline/gold/02_load_dim_store.sql-->

---

## Gold 03 · `dim_product`

**Steps**: keep business columns only (drop `material_group_code` and `source_sales_org`); derive `is_private_label`
from the brand; add `UNKNOWN`.

<!--table
SELECT * FROM gold.dim_product WHERE product_id IN ('4471023', '4471040', '93321', '4492050', 'UNKNOWN')
-->

The `UNKNOWN` row exists because {{n: SELECT COUNT(*) FROM gold.fct_sales_line WHERE product_id = 'UNKNOWN'}} sales lines
scanned an item (`9999999`) that is not in the product master. Those sales are real money, so they are kept and
counted, pointing at `UNKNOWN` instead of being dropped by an inner join.

<!--file pipeline/gold/03_load_dim_product.sql-->

---

## Gold 04 · `dim_promotion`

**Steps**: copy the decoded promotion; add `NO_PROMO` so full-price lines still join.

<!--table
SELECT * FROM gold.dim_promotion WHERE promotion_id IN ('BG26W33', 'NO_PROMO')
-->

<!--file pipeline/gold/04_load_dim_promotion.sql-->

---

## Gold 05 · `dim_customer`

**Steps**: upsert members from the CRM (no personal data); add `ANONYMOUS`; add **placeholders for late-arriving members**.

A loyalty card scanned today may not appear in the CRM extract until next week. If the fact load required the member
to exist, those sales would fail or be dropped. Instead a placeholder row with tier `Unknown` is inserted, and the next
CRM extract fills in the details (the merge sets `is_placeholder` back to false):

<!--table
SELECT loyalty_tier, is_placeholder, COUNT(*) AS customers FROM gold.dim_customer GROUP BY 1, 2 ORDER BY 2, 3 DESC
-->

This is why `dim_customer` is merged, not rebuilt: a rebuild would delete the placeholders the facts point to.

<!--file pipeline/gold/05_load_dim_customer.sql-->

---

## Gold 06 · `fct_sales_line`

This is the answer to "how are silver tables combined into a fact?". Six steps, in the order the SQL runs them:

**Step 1 · Stack the two channels.** `silver.pos_sales_line UNION ALL silver.online_sales_line`. Both silver tables were built
with identical columns for exactly this reason.

**Step 2 · Apply the business inclusion rules.** Keep `Sale` and `Return`; drop `Void`, `Cancelled` and training transactions:

<!--table
SELECT sales_channel, line_type, is_test_transaction, COUNT(*) AS silver_rows,
       CASE WHEN line_type IN ('Sale', 'Return') AND is_test_transaction = 0 THEN 'kept' ELSE 'excluded' END AS in_gold
FROM (SELECT * FROM silver.pos_sales_line UNION ALL SELECT * FROM silver.online_sales_line)
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
-->

**Step 3 · Resolve every key.** `LEFT JOIN` each dimension; anything without a match gets its Unknown member
(`UNKNOWN` store/product, `NO_PROMO`). Customer ids always match because of step 3 in `dim_customer`.

**Step 4 · Copy `region` from `dim_store`.** This column exists only for row-level security on Databricks
(the row filter needs it on the fact). It is tagged `purpose = security_only` and hidden from analysis.

**Step 5 · Calculate the money once.**

| Column | Formula | Why here |
|---|---|---|
| `net_sales_amount_usd` | `gross_sales_amount_usd - discount_amount_usd` | the company definition of sales; nobody re-derives it |
| `cost_of_goods_usd` | `quantity_units × unit_cost_usd` valid on the sale date | needs a range join to `silver.product_cost` |
| `gross_margin_usd` | `net_sales - cost_of_goods` | additive, so any grouping sums correctly |

**Step 6 · Flags.** `is_promo_sale` (a promotion matched) and `is_loyalty_sale` (not anonymous).

**Incremental load.** Only lines whose source file landed after the newest line already in gold are processed
(the **watermark** is `MAX(_source_ingested_at)`), then upserted by `sales_line_id`. A late correction to a line
updates it; nothing is ever duplicated.

The golden receipt, before (silver) and after (gold). The void is gone; margin is now on every line:

<!--table
SELECT sales_line_id, line_type, quantity_units, gross_sales_amount_usd, discount_amount_usd, promotion_code
FROM silver.pos_sales_line WHERE transaction_id = '88120431' ORDER BY sales_line_id
-->

<!--table
SELECT sales_line_id, line_type, quantity_units, gross_sales_amount_usd, discount_amount_usd, net_sales_amount_usd,
       cost_of_goods_usd, gross_margin_usd, promotion_id, is_promo_sale, is_loyalty_sale, region
FROM gold.fct_sales_line WHERE transaction_id = '88120431' ORDER BY sales_line_id
-->

Look at line 1: two bags of chips on buy-one-get-one. Net sales $3.49, cost $4.46, **margin negative**. That is real
retail economics, and exactly the kind of insight a governed margin measure lets Genie report correctly.

Nothing is aggregated: gold keeps the receipt-line grain, so every question, at any level, can be answered from it.

<!--file pipeline/gold/06_load_fct_sales_line.sql-->

---

## Gold 07 · `fct_inventory_daily`

**Steps**: resolve keys; copy region for security; value stock at the standard cost valid on the snapshot date;
define out-of-stock as an **active** product with no sellable stock. Products a store does not range never appear in
its snapshot, so they can never be counted as out of stock by mistake.

<!--table limit=6
SELECT i.snapshot_date, s.store_name, p.product_name, i.on_hand_units, i.on_hand_value_usd, i.is_out_of_stock
FROM gold.fct_inventory_daily i
JOIN gold.dim_store s USING (store_id) JOIN gold.dim_product p USING (product_id)
WHERE i.snapshot_date = '2026-09-26' AND s.store_id = '1042' ORDER BY i.is_out_of_stock DESC, p.product_name
-->

Stock is **semi-additive**: you can add it across stores and products on one day, but never across days.
The comment on `on_hand_units` says so, and the metric view enforces it (next doc).

<!--file pipeline/gold/07_load_fct_inventory_daily.sql-->

---

## Gold in numbers, and integrity

<!--table
SELECT 'dim_date' AS gold_table, COUNT(*) AS rows FROM gold.dim_date
UNION ALL SELECT 'dim_store', COUNT(*) FROM gold.dim_store
UNION ALL SELECT 'dim_product', COUNT(*) FROM gold.dim_product
UNION ALL SELECT 'dim_promotion', COUNT(*) FROM gold.dim_promotion
UNION ALL SELECT 'dim_customer', COUNT(*) FROM gold.dim_customer
UNION ALL SELECT 'fct_sales_line', COUNT(*) FROM gold.fct_sales_line
UNION ALL SELECT 'fct_inventory_daily', COUNT(*) FROM gold.fct_inventory_daily
-->

Foreign-key violations in gold: {{n: SELECT COUNT(*) FROM pragma_foreign_key_check('fct_sales_line', 'gold')}}.
Total net sales in gold: {{usd: SELECT SUM(net_sales_amount_usd) FROM gold.fct_sales_line}}, which the test suite
recomputes independently from the raw files (see [11 · Data quality](11-data-quality.md)).

The CREATE TABLE statements generated from the contracts, for Databricks:
[`databricks/gold/00_create_gold_tables.sql`](../databricks/gold/00_create_gold_tables.sql).
The full column list with comments: [10 · Data dictionary](10-data-dictionary.md).

Next: [6 · Semantic layer →](06-semantic-layer.md)
