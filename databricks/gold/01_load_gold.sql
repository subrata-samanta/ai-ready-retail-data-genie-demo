-- =====================================================================================
-- GOLD loads for Databricks (run as a SQL task after the Lakeflow pipeline).
-- Same steps as pipeline/gold/*.sql (the tested local version), in the same order:
-- dimensions first, then facts. Every statement is a MERGE, so re-running is safe.
-- Run 00_create_gold_tables.sql once before the first load.
-- =====================================================================================

-- 01 · dim_date: fiscal attributes + rolling flags relative to today ------------------
MERGE INTO freshcart.gold.dim_date AS t
USING (
  WITH cal AS (SELECT * FROM freshcart.silver.fiscal_calendar),
  y AS (SELECT fiscal_year AS cur_fy, fiscal_quarter AS cur_fq, fiscal_day_of_year AS cur_fdoy
        FROM cal WHERE calendar_date = date_sub(current_date(), 1)),
  lw AS (SELECT MAX(fiscal_week_start_date) AS lw_start FROM cal
         WHERE date_add(fiscal_week_start_date, 6) < current_date())
  SELECT c.calendar_date, c.day_of_week_name, c.fiscal_year, c.fiscal_quarter, c.fiscal_period,
         c.fiscal_period_name, c.fiscal_week, c.fiscal_week_start_date, c.fiscal_day_of_year,
         c.fiscal_week_start_date = lw.lw_start                                     AS is_last_completed_fiscal_week,
         c.fiscal_week_start_date BETWEEN date_sub(lw.lw_start, 21) AND lw.lw_start AS is_last_4_completed_fiscal_weeks,
         c.fiscal_year = y.cur_fy AND c.fiscal_quarter = y.cur_fq                   AS is_current_fiscal_quarter,
         c.fiscal_year = y.cur_fy AND c.fiscal_day_of_year <= y.cur_fdoy            AS is_fiscal_ytd,
         c.fiscal_year = y.cur_fy - 1 AND c.fiscal_day_of_year <= y.cur_fdoy        AS is_prior_fiscal_ytd
  FROM cal c CROSS JOIN y CROSS JOIN lw
) AS s
ON t.calendar_date = s.calendar_date
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;

-- 02 · dim_store: current version, comparable rule, status, UNKNOWN member ------------
MERGE INTO freshcart.gold.dim_store AS t
USING (
  WITH cur AS (SELECT fiscal_year AS cur_fy FROM freshcart.silver.fiscal_calendar
               WHERE calendar_date = date_sub(current_date(), 1)),
  s AS (
    SELECT h.*, cal.fiscal_year AS open_fiscal_year,
           CASE WHEN h.store_format = 'Dark Store' THEN NULL
                WHEN cal.fiscal_day_of_year = 1 THEN cal.fiscal_year + 1
                ELSE cal.fiscal_year + 2 END AS comparable_from_fiscal_year,
           CASE WHEN h.close_date IS NOT NULL AND h.close_date <= current_date() THEN 'Closed' ELSE 'Open' END AS store_status
    FROM freshcart.silver.store_history h
    LEFT JOIN freshcart.silver.fiscal_calendar cal ON cal.calendar_date = h.open_date
    WHERE h.is_current
  )
  SELECT s.store_id, s.store_name, s.store_format, s.city, s.state_province, s.country, s.region,
         s.open_date, s.open_fiscal_year, s.close_date, s.store_status, s.selling_area_sqft,
         s.comparable_from_fiscal_year,
         COALESCE(s.store_status = 'Open' AND s.comparable_from_fiscal_year <= cur.cur_fy, false) AS is_comparable_store_current_fy
  FROM s CROSS JOIN cur
  UNION ALL
  SELECT 'UNKNOWN', 'Unknown store', 'Unknown', NULL, NULL, NULL, 'Unknown', NULL, NULL, NULL, 'Unknown', NULL, NULL, false
) AS s
ON t.store_id = s.store_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;

-- 03 · dim_product --------------------------------------------------------------------
MERGE INTO freshcart.gold.dim_product AS t
USING (
  SELECT product_id, product_name, brand, department, category, subcategory, unit_of_measure,
         brand IN ('FreshCart', 'FreshCart Select') AS is_private_label, product_status
  FROM freshcart.silver.product
  UNION ALL
  SELECT 'UNKNOWN', 'Unknown product', NULL, 'Unknown', 'Unknown', 'Unknown', 'Each', false, 'Unknown'
) AS s
ON t.product_id = s.product_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;

-- 04 · dim_promotion ------------------------------------------------------------------
MERGE INTO freshcart.gold.dim_promotion AS t
USING (
  SELECT promotion_id, promotion_name, promotion_mechanic, start_date, end_date FROM freshcart.silver.promotion
  UNION ALL
  SELECT 'NO_PROMO', 'No promotion', 'No Promotion', NULL, NULL
) AS s
ON t.promotion_id = s.promotion_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;

-- 05 · dim_customer: CRM members, ANONYMOUS, late-arriving placeholders ---------------
MERGE INTO freshcart.gold.dim_customer AS t
USING (
  SELECT customer_id, loyalty_tier, age_band, home_store_id, enrollment_date, false AS is_placeholder
  FROM freshcart.silver.loyalty_member
  UNION ALL
  SELECT 'ANONYMOUS', 'Anonymous', NULL, NULL, NULL, false
) AS s
ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;

MERGE INTO freshcart.gold.dim_customer AS t
USING (
  SELECT DISTINCT customer_id FROM (
    SELECT customer_id FROM freshcart.silver.pos_sales_line
    UNION SELECT customer_id FROM freshcart.silver.online_sales_line)
) AS s
ON t.customer_id = s.customer_id
WHEN NOT MATCHED THEN INSERT (customer_id, loyalty_tier, is_placeholder) VALUES (s.customer_id, 'Unknown', true);

-- 06 · fct_sales_line: incremental by watermark, business rules, money calculated once -
MERGE INTO freshcart.gold.fct_sales_line AS t
USING (
  WITH lines AS (
    SELECT sales_line_id, transaction_id, sales_date, store_id, product_id, customer_id, promotion_code,
           sales_channel, line_type, is_test_transaction, quantity_units, currency_code,
           gross_sales_amount_local, discount_amount_local, gross_sales_amount_usd, discount_amount_usd, _ingested_at
    FROM freshcart.silver.pos_sales_line
    UNION ALL
    SELECT sales_line_id, transaction_id, sales_date, store_id, product_id, customer_id, promotion_code,
           sales_channel, line_type, is_test_transaction, quantity_units, currency_code,
           gross_sales_amount_local, discount_amount_local, gross_sales_amount_usd, discount_amount_usd, _ingested_at
    FROM freshcart.silver.online_sales_line
  ),
  wm AS (SELECT COALESCE(MAX(_source_ingested_at), TIMESTAMP'1900-01-01') AS wm FROM freshcart.gold.fct_sales_line)
  SELECT l.sales_line_id, l.transaction_id, l.sales_date,
         COALESCE(st.store_id, 'UNKNOWN')       AS store_id,
         COALESCE(pr.product_id, 'UNKNOWN')     AS product_id,
         l.customer_id,
         COALESCE(pm.promotion_id, 'NO_PROMO')  AS promotion_id,
         COALESCE(st.region, 'Unknown')         AS region,
         l.sales_channel, l.line_type, l.quantity_units,
         l.gross_sales_amount_usd, l.discount_amount_usd,
         round(l.gross_sales_amount_usd - l.discount_amount_usd, 2)                        AS net_sales_amount_usd,
         round(l.quantity_units * COALESCE(c.unit_cost_usd, 0), 2)                         AS cost_of_goods_usd,
         round(l.gross_sales_amount_usd - l.discount_amount_usd
               - round(l.quantity_units * COALESCE(c.unit_cost_usd, 0), 2), 2)             AS gross_margin_usd,
         round(l.gross_sales_amount_local - l.discount_amount_local, 2)                    AS net_sales_amount_local,
         l.currency_code,
         pm.promotion_id IS NOT NULL                                                       AS is_promo_sale,
         l.customer_id <> 'ANONYMOUS'                                                      AS is_loyalty_sale,
         l._ingested_at                                                                    AS _source_ingested_at
  FROM lines l
  CROSS JOIN wm
  LEFT JOIN freshcart.gold.dim_store     st ON st.store_id     = l.store_id
  LEFT JOIN freshcart.gold.dim_product   pr ON pr.product_id   = l.product_id
  LEFT JOIN freshcart.gold.dim_promotion pm ON pm.promotion_id = l.promotion_code
  LEFT JOIN freshcart.silver.product_cost c ON c.product_id    = l.product_id
                                            AND l.sales_date BETWEEN c.valid_from AND c.valid_to
  WHERE l.line_type IN ('Sale', 'Return')
    AND NOT l.is_test_transaction
    AND l._ingested_at > wm.wm
) AS s
ON t.sales_line_id = s.sales_line_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;

-- 07 · fct_inventory_daily ------------------------------------------------------------
MERGE INTO freshcart.gold.fct_inventory_daily AS t
USING (
  SELECT i.snapshot_date,
         COALESCE(st.store_id, 'UNKNOWN') AS store_id,
         COALESCE(pr.product_id, 'UNKNOWN') AS product_id,
         COALESCE(st.region, 'Unknown') AS region,
         i.on_hand_units,
         round(i.on_hand_units * COALESCE(c.unit_cost_usd, 0), 2) AS on_hand_value_usd,
         i.on_hand_units <= 0 AND COALESCE(pr.product_status = 'Active', false) AS is_out_of_stock
  FROM freshcart.silver.inventory_daily i
  LEFT JOIN freshcart.gold.dim_store   st ON st.store_id   = i.store_id
  LEFT JOIN freshcart.gold.dim_product pr ON pr.product_id = i.product_id
  LEFT JOIN freshcart.silver.product_cost c ON c.product_id = i.product_id
                                            AND i.snapshot_date BETWEEN c.valid_from AND c.valid_to
) AS s
ON t.snapshot_date = s.snapshot_date AND t.store_id = s.store_id AND t.product_id = s.product_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;
