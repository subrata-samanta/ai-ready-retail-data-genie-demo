# 4 · Silver: clean, type, decode, conform

[← 3 · Bronze](03-bronze.md) · Next: [5 · Gold →](05-gold.md)

Silver is where most of the AI-readiness work happens. Each silver table is built by one SQL file in
[`pipeline/silver/`](../pipeline/silver/), and each file lists its steps as numbered comments that
match the sections below. For every table you will see **the real rows before and after**.

The general rules for silver:

- **Type everything.** Dates are dates, numbers are numbers, and impossible values become NULL instead of being silently "fixed".
- **One id per entity.** Store `01042` (POS), `1042` (ERP) and `1042` (web) all become `1042`.
- **Decode codes into the words people use.** `NM` → `Neighborhood Market`, `C0412` → `Chips and Crisps`.
- **Keep every valid source row.** Voids, cancelled orders and training sales stay in silver, **flagged**; gold decides what counts as a sale.
- **Reject broken rows with a reason** into a quarantine table instead of dropping them silently.
- **Keep personal data apart** from everything else.

The steps run in order because later steps use earlier ones (sales need FX rates; FX needs the calendar).

---

## Silver 01 · `fiscal_calendar`

**From** `bronze.fiscal_calendar_raw` · **Grain** one row per day · **Why** every time question ("last week", "P03", "YTD") depends on it.

Before (bronze) — the first days of FY2026:

<!--table
SELECT CAL_DT, FISC_YR, FISC_QTR, FISC_PRD, FISC_PRD_NM, FISC_WK, FISC_WK_START
FROM bronze.fiscal_calendar_raw WHERE CAL_DT IN ('31/01/2026', '01/02/2026', '02/02/2026', '08/02/2026')
-->

After (silver):

<!--table
SELECT * FROM silver.fiscal_calendar WHERE calendar_date IN ('2026-01-31', '2026-02-01', '2026-02-02', '2026-02-08')
-->

What changed: `DD/MM/YYYY` strings became ISO dates; `FY2026` → `2026`, `P01` → `1`, `W02` → `2`;
two columns were **derived**: `fiscal_day_of_year` (1 on the first Sunday of the fiscal year, which later makes
like-for-like comparisons weekday-aligned) and `day_of_week_name`. Note 31 January 2026 is day 364 of FY2025.

<!--file pipeline/silver/01_fiscal_calendar.sql-->

---

## Silver 02 · `fx_rate_daily`

**From** `bronze.fx_rate_raw` + `silver.fiscal_calendar` · **Grain** one row per day per currency.

Before: rates exist only on business days. There is nothing for Saturday 13 or Sunday 14 September 2025:

<!--table
SELECT RATE_DT, FROM_CCY, TO_CCY, RATE FROM bronze.fx_rate_raw WHERE RATE_DT BETWEEN '2025-09-11' AND '2025-09-16'
-->

After: every calendar day has a rate; weekends carry Friday's rate forward and are flagged. A `USD → USD = 1.0`
row is added so every sale, in either currency, can use the same join.

<!--table
SELECT * FROM silver.fx_rate_daily WHERE rate_date BETWEEN '2025-09-11' AND '2025-09-16' ORDER BY from_currency, rate_date
-->

<!--file pipeline/silver/02_fx_rate_daily.sql-->

---

## Silver 03 · `store_history` (slowly changing dimension, type 2)

**From** `bronze.store_master_raw` (4 snapshots) + region, format and state lookups · **Grain** one row per store per **version**.

Before: Boston Seaport appears in all four snapshots, with codes and US-style dates:

<!--table
SELECT substr(_source_file, -12, 8) AS snapshot, STR_NBR, STR_NM, FMT_CD, RGN_CD, ST_CD, OPEN_DT, CLOSE_DT, SQFT
FROM bronze.store_master_raw WHERE STR_NBR IN ('01042', '02211') ORDER BY STR_NBR, snapshot
-->

After: codes decoded, ids normalised, and only the snapshots where **something changed** kept as versions,
each with a validity range. Boston has two versions (the remodel); Buckhead has two (the closure).

<!--table
SELECT store_id, store_name, store_format, state_province, region, open_date, close_date, selling_area_sqft,
       valid_from, valid_to, is_current
FROM silver.store_history WHERE store_id IN ('1042', '2211') ORDER BY store_id, valid_from
-->

How change detection works: all attributes are concatenated into one string (`attr_hash`); `LAG()` compares each
snapshot with the previous one for the same store; unchanged snapshots are dropped; `LEAD()` then closes each
version the day before the next one starts. Gold uses the `is_current` version; silver keeps history for questions like
"what format was Boston when it made this sale?".

<!--file pipeline/silver/03_store_history.sql-->

---

## Silver 04 · `product` and `product_cost`

**From** `bronze.product_master_raw`, `merch_hierarchy_raw`, `cost_history_raw` · **Grain** one row per SKU; one row per SKU per cost version.

Before: the chips SKU appears **twice** (once per sales organisation), and a Canada-only item exists only as an upper-case `CA01` row:

<!--table
SELECT MATNR, VKORG, MAKTX, MATKL, BRAND_NM, MEINS, STATUS FROM bronze.product_master_raw
WHERE MATNR IN ('4471023', '4471024', '4492050') ORDER BY MATNR, VKORG
-->

After: one row per product. The US01 record wins (so `4471024` is correctly **Discontinued**, which the Canadian
copy never records); the Canada-only item keeps its CA01 record with a readable name; the category code is decoded into three levels.

<!--table
SELECT * FROM silver.product WHERE product_id IN ('4471023', '4471024', '4492050')
-->

Why this matters: had gold joined to the raw master, every sale of a SKU with two rows would be counted twice.
{{n: SELECT COUNT(*) FROM bronze.product_master_raw}} raw rows became {{n: SELECT COUNT(*) FROM silver.product}} products.

Costs become validity ranges, so a sale picks up the cost that was valid **on the day it was sold**:

<!--table
SELECT * FROM silver.product_cost WHERE product_id IN (SELECT product_id FROM silver.product_cost GROUP BY product_id HAVING COUNT(*) > 1 LIMIT 2)
-->

<!--file pipeline/silver/04_product.sql-->

---

## Silver 05 · `promotion` and `promotion_item`

Before → after: mechanic codes decoded, dates typed.

<!--table
SELECT PROMO_CD, PROMO_DESC, MECH_CD, START_DT, END_DT, DISC_PCT FROM bronze.promo_calendar_raw WHERE PROMO_CD IN ('BG26W33', (SELECT MIN(PROMO_CD) FROM bronze.promo_calendar_raw WHERE MECH_CD = 'PCT'), (SELECT MIN(PROMO_CD) FROM bronze.promo_calendar_raw WHERE MECH_CD = 'MULTI'), (SELECT MIN(PROMO_CD) FROM bronze.promo_calendar_raw WHERE MECH_CD = 'LOY'))
-->

<!--table
SELECT * FROM silver.promotion WHERE promotion_id IN ('BG26W33', (SELECT MIN(PROMO_CD) FROM bronze.promo_calendar_raw WHERE MECH_CD = 'PCT'), (SELECT MIN(PROMO_CD) FROM bronze.promo_calendar_raw WHERE MECH_CD = 'MULTI'), (SELECT MIN(PROMO_CD) FROM bronze.promo_calendar_raw WHERE MECH_CD = 'LOY'))
-->

<!--file pipeline/silver/05_promotion.sql-->

---

## Silver 06 · `loyalty_member` and `loyalty_member_restricted`

**From** `bronze.crm_member_raw` · **Grain** one row per member.

Before: card number, email and date of birth, all personal data:

<!--table
SELECT CARD_NBR, EMAIL, DOB, TIER, ENROLL_DT, HOME_STR FROM bronze.crm_member_raw WHERE CARD_NBR = '6034118822'
-->

After, the general table: a **pseudonymous id** (`sha2(card, 256)`), the tier in words and an **age band** instead of a birth date.
POS and online sales apply the same function to the card number, so the ids still join, but nobody can read a card number from it.

<!--table
SELECT * FROM silver.loyalty_member WHERE customer_id = sha2('6034118822', 256)
-->

The personal data goes to a separate restricted table, which gets a column mask on Databricks and is never used by gold:

<!--table
SELECT substr(customer_id, 1, 16) || '...' AS customer_id, card_number, email, date_of_birth
FROM silver.loyalty_member_restricted WHERE customer_id = sha2('6034118822', 256)
-->

<!--file pipeline/silver/06_loyalty_member.sql-->

---

## Silver 07 · `pos_sales_line` and `quarantine_pos_sales_line`

**From** `bronze.pos_tlog_raw` + `silver.fx_rate_daily` · **Grain** one row per receipt line (voids and training included, flagged).
This is the most important silver table. It applies eight steps.

**Before** — the golden receipt plus a Canadian return, as delivered:

<!--table
SELECT TRX_ID, STR_NBR, REG_NBR, LN_NBR, TRX_DT, TRX_TM, ITM_ID, QTY, EXT_AMT, DISC_AMT, TRX_TYP, LYL_CARD_NBR, PROMO_CD, CRNCY_CD
FROM bronze.pos_tlog_raw WHERE TRX_ID IN ('88120431', '88120519') ORDER BY TRX_ID, LN_NBR
-->

**After**:

<!--table
SELECT sales_line_id, store_id, sales_date, sales_ts, product_id, substr(customer_id, 1, 10) || '...' AS customer_id,
       line_type, is_test_transaction, quantity_units, currency_code, gross_sales_amount_local, discount_amount_local,
       fx_rate_to_usd, gross_sales_amount_usd, discount_amount_usd
FROM silver.pos_sales_line WHERE transaction_id IN ('88120431', '88120519') ORDER BY sales_line_id
-->

Step by step, what happened to these rows:

1. **Type**: `20260914` + `93015` → `2026-09-14 09:30:15`; text amounts → numbers.
2. **Normalise**: store `01042` → `1042`; item `000004471023` → `4471023`.
3. **Decode**: `S/R/V` → `Sale/Return/Void`; register `99` → `is_test_transaction = 1`.
4. **Sign**: the Toronto return arrived as `+18.99`; it is now `-18.99` so sums net it off.
5. **Pseudonymise**: the card number became a SHA-256 customer id; no card → `ANONYMOUS`.
6. **Convert**: CAD × that day's rate = USD. The Toronto return is `-18.99 CAD × rate = USD`.
7. **Deduplicate**: see below.
8. **Validate**: see below.

The void (line 3) is **still here**, flagged `Void`. Silver keeps it because it is a real source event (useful for
shrink and fraud analysis). Gold removes it because it is not a sale.

**Step 7: the re-delivered day.** On 10 June 2025 the store system sent the same rows twice:

<!--table
SELECT TRX_DT, COUNT(*) AS rows_in_bronze, COUNT(DISTINCT STR_NBR || REG_NBR || TRX_ID || LN_NBR) AS distinct_lines
FROM bronze.pos_tlog_raw WHERE TRX_DT IN ('20250609', '20250610', '20250611') GROUP BY TRX_DT
-->

`ROW_NUMBER() OVER (PARTITION BY store, register, receipt, line, date)` keeps the first copy only.

**Step 8: quarantine.** Rows that break a hard rule are not silently dropped. They are written, with the reason,
to `silver.quarantine_pos_sales_line` so someone can fix the source:

<!--table
SELECT failure_reason, COUNT(*) AS rows FROM silver.quarantine_pos_sales_line GROUP BY failure_reason ORDER BY rows DESC
-->

<!--table limit=5
SELECT failure_reason, raw_str_nbr, transaction_id, line_number, raw_trx_dt, product_id, raw_qty, _source_file, _row_number
FROM silver.quarantine_pos_sales_line ORDER BY failure_reason, _source_file
-->

On Databricks the same rules are declared as expectations
(`CONSTRAINT passes_rules EXPECT (failure_reason IS NULL) ON VIOLATION DROP ROW`), which also records the drop counts in the pipeline event log.

<!--file pipeline/silver/07_pos_sales_line.sql-->

---

## Silver 08 · `online_sales_line`

**From** `bronze.ecom_order_raw` + `silver.fx_rate_daily` · **Grain** one row per order line, with **exactly the same columns as the POS table** so gold can stack the two channels.

Before: one JSON document per order, lines nested inside (see [2 · Source data](02-source-data.md#e-commerce-orders)).

After: the order's lines flattened into rows with `json_each` (Databricks: `explode(from_json(...))`):

<!--table
SELECT sales_line_id, transaction_id, line_number, store_id, sales_date, sales_ts, product_id, line_type,
       quantity_units, currency_code, gross_sales_amount_local, discount_amount_local, gross_sales_amount_usd
FROM silver.online_sales_line
WHERE transaction_id = (SELECT json_extract(value, '$.orderId') FROM bronze.ecom_order_raw WHERE _source_file LIKE '%202609%' ORDER BY _row_number LIMIT 1)
-->

The business date comes from the **local** timestamp: this order was placed at 23:25 in New Jersey on 1 September,
so it is a 1 September sale, even though it was already 2 September in UTC. Cancelled orders stay in silver as
`line_type = 'Cancelled'` ({{n: SELECT COUNT(*) FROM silver.online_sales_line WHERE line_type = 'Cancelled'}} lines) and are excluded in gold.

<!--file pipeline/silver/08_online_sales_line.sql-->

---

## Silver 09 · `inventory_daily`

**From** `bronze.inventory_raw` · **Grain** one row per store, product and day.

Negative stock is physically impossible; it comes from unrecorded shrink. It is clamped to zero **and flagged**
so the stock team can investigate, rather than hidden:

<!--table limit=4
SELECT i.STR_NBR, i.ITM_ID, i.SNAP_DT, i.OH_QTY AS raw_on_hand, s.on_hand_units, s.had_negative_stock
FROM bronze.inventory_raw i
JOIN silver.inventory_daily s ON s.store_id = ltrim(i.STR_NBR, '0') AND s.product_id = ltrim(i.ITM_ID, '0')
                             AND s.snapshot_date = try_to_date(i.SNAP_DT, 'yyyyMMdd')
WHERE CAST(i.OH_QTY AS REAL) < 0
-->

<!--file pipeline/silver/09_inventory_daily.sql-->

---

## Silver in numbers

<!--table
SELECT 'fiscal_calendar' AS silver_table, COUNT(*) AS rows FROM silver.fiscal_calendar
UNION ALL SELECT 'fx_rate_daily', COUNT(*) FROM silver.fx_rate_daily
UNION ALL SELECT 'store_history', COUNT(*) FROM silver.store_history
UNION ALL SELECT 'product', COUNT(*) FROM silver.product
UNION ALL SELECT 'product_cost', COUNT(*) FROM silver.product_cost
UNION ALL SELECT 'promotion', COUNT(*) FROM silver.promotion
UNION ALL SELECT 'loyalty_member', COUNT(*) FROM silver.loyalty_member
UNION ALL SELECT 'pos_sales_line', COUNT(*) FROM silver.pos_sales_line
UNION ALL SELECT 'quarantine_pos_sales_line', COUNT(*) FROM silver.quarantine_pos_sales_line
UNION ALL SELECT 'online_sales_line', COUNT(*) FROM silver.online_sales_line
UNION ALL SELECT 'inventory_daily', COUNT(*) FROM silver.inventory_daily
-->

Next: [5 · Gold →](05-gold.md)
