# 8 · Follow one receipt from raw file to Genie answer

[← 7 · Genie Agent](07-genie-agent.md) · Next: [9 · Run it on Databricks →](09-run-on-databricks.md)

The best way to understand a pipeline is to follow one record through it. On Monday 14 September 2026 at 09:30,
a Gold loyalty member shopped at FreshCart Boston Seaport. The cashier scanned two bags of chips on a
buy-one-get-one offer, weighed some bananas, scanned an extra bag of chips by mistake and voided it, and added milk.

## 1 · Raw file (`data/raw/pos/pos_tlog_202609.csv`)

<!--table
SELECT TRX_ID, STR_NBR, REG_NBR, LN_NBR, TRX_DT, TRX_TM, ITM_ID, QTY, UNIT_PRC, EXT_AMT, DISC_AMT, TAX_AMT, TRX_TYP, LYL_CARD_NBR, PROMO_CD, CRNCY_CD
FROM bronze.pos_tlog_raw WHERE TRX_ID = '88120431' ORDER BY LN_NBR
-->

Questions a newcomer would have: what is `01042`? What is `000004471023`? Is `6.98` what the customer paid?
What is `V`? Is `93015` a time? Genie would have the same questions and no one to ask.

## 2 · Bronze (`bronze.pos_tlog_raw`)

Identical text, plus where it came from:

<!--table
SELECT TRX_ID, LN_NBR, TRX_TYP, _source_file, _row_number FROM bronze.pos_tlog_raw WHERE TRX_ID = '88120431' ORDER BY LN_NBR
-->

## 3 · Silver (`silver.pos_sales_line`)

Typed, normalised, decoded, pseudonymised. The void is still here, flagged:

<!--table
SELECT sales_line_id, store_id, sales_ts, product_id, substr(customer_id, 1, 12) || '...' AS customer_id, promotion_code,
       line_type, quantity_units, gross_sales_amount_usd, discount_amount_usd
FROM silver.pos_sales_line WHERE transaction_id = '88120431' ORDER BY sales_line_id
-->

## 4 · Gold (`gold.fct_sales_line`)

The void is gone. Net sales, cost and margin are calculated. Every key points to a dimension row:

<!--table
SELECT sales_line_id, sales_date, store_id, product_id, promotion_id, line_type, quantity_units,
       net_sales_amount_usd, cost_of_goods_usd, gross_margin_usd, is_promo_sale, is_loyalty_sale
FROM gold.fct_sales_line WHERE transaction_id = '88120431' ORDER BY sales_line_id
-->

…and those keys resolve to words anyone understands:

<!--table
SELECT f.sales_line_id, d.fiscal_year, d.fiscal_week, d.day_of_week_name, s.store_name, s.store_format, s.region,
       p.product_name, p.category, p.unit_of_measure, c.loyalty_tier, pr.promotion_mechanic
FROM gold.fct_sales_line f
JOIN gold.dim_date d ON d.calendar_date = f.sales_date
JOIN gold.dim_store s USING (store_id)
JOIN gold.dim_product p USING (product_id)
JOIN gold.dim_customer c USING (customer_id)
JOIN gold.dim_promotion pr USING (promotion_id)
WHERE f.transaction_id = '88120431' ORDER BY f.sales_line_id
-->

## 5 · Semantic layer

Its contribution to the governed measures:

<!--table
SELECT COUNT(DISTINCT transaction_id) AS transactions, SUM(quantity_units) AS units_sold,
       SUM(net_sales_amount_usd) AS net_sales_usd, SUM(gross_margin_usd) / SUM(net_sales_amount_usd) AS gross_margin_pct
FROM gold.fct_sales_line WHERE transaction_id = '88120431'
-->

One transaction and {{usd: SELECT SUM(net_sales_amount_usd) FROM gold.fct_sales_line WHERE transaction_id = '88120431'}} of net sales.
Adding up the raw extended amounts would give {{usd: SELECT SUM(CAST(EXT_AMT AS REAL)) FROM bronze.pos_tlog_raw WHERE TRX_ID = '88120431'}},
which counts the voided bag and ignores the free one.

## 6 · Genie

A category manager asks: *"How did Snacks do in the Northeast in week 33, and how does last week compare?"*
Everything needed is now a field, a measure or a decoded value, so the SQL is trivial. Week 33 includes this receipt's chips:

<!--table metric
SELECT fiscal_week, is_last_completed_fiscal_week, MEASURE(net_sales) AS net_sales, MEASURE(units_sold) AS units_sold,
       MEASURE(gross_margin_pct) AS gross_margin_pct, MEASURE(promo_sales_share) AS promo_sales_share
FROM freshcart.semantic.sales_metrics
WHERE category = 'Snacks' AND region = 'Northeast' AND fiscal_year = 2026 AND fiscal_week IN (33, 34)
GROUP BY ALL ORDER BY fiscal_week
-->

The chips were on buy-one-get-one in week 33 (promo share almost half) and not in week 34. "Last week" is week 34
(20 to 26 September) because the flag in `dim_date` says so; Genie does not have to work it out, and a benchmark
catches it if it ever does so wrongly.

## The Canadian return, in one line per layer

<!--table
SELECT 'raw / bronze' AS layer, TRX_TYP AS type, QTY AS quantity, EXT_AMT AS amount, CRNCY_CD AS currency, NULL AS usd
FROM bronze.pos_tlog_raw WHERE TRX_ID = '88120519'
UNION ALL
SELECT 'silver', line_type, quantity_units, gross_sales_amount_local, currency_code, gross_sales_amount_usd
FROM silver.pos_sales_line WHERE transaction_id = '88120519'
UNION ALL
SELECT 'gold', line_type, quantity_units, net_sales_amount_local, currency_code, net_sales_amount_usd
FROM gold.fct_sales_line WHERE transaction_id = '88120519'
-->

A positive CAD amount with a type code became a negative USD amount at that day's rate, so `SUM()` just works.

Next: [9 · Run it on Databricks →](09-run-on-databricks.md)
