-- =====================================================================================
-- GOLD 06 · fct_sales_line
-- Built from : silver.pos_sales_line UNION ALL silver.online_sales_line
--              joined to gold.dim_store, gold.dim_product, gold.dim_promotion
--              and silver.product_cost
-- Grain      : one row per receipt / order line  (same grain as silver: nothing is summed)
-- Load       : INCREMENTAL. Only lines whose source file landed after the newest line
--              already in gold are processed (a "watermark"), then upserted by key,
--              so re-running never duplicates.
-- Steps      : 1. stack the two channels (identical silver schemas)
--              2. apply the business inclusion rules: keep Sale and Return,
--                 drop Void, Cancelled and training (test) transactions
--              3. resolve every key to a real dimension row or its Unknown member
--              4. copy region from dim_store (used only by row-level security)
--              5. calculate the money once:
--                   net_sales     = gross_sales - discount
--                   cost_of_goods = quantity x standard cost valid on the sale date
--                   gross_margin  = net_sales - cost_of_goods
--              6. set the is_promo_sale and is_loyalty_sale flags
-- =====================================================================================
WITH lines AS (                                                                            -- step 1
  SELECT * FROM silver.pos_sales_line
  UNION ALL
  SELECT * FROM silver.online_sales_line
),
watermark AS (
  SELECT COALESCE(MAX(_source_ingested_at), '') AS wm FROM gold.fct_sales_line
)
INSERT INTO gold.fct_sales_line (
  sales_line_id, transaction_id, sales_date, store_id, product_id, customer_id, promotion_id,
  region, sales_channel, line_type, quantity_units, gross_sales_amount_usd, discount_amount_usd,
  net_sales_amount_usd, cost_of_goods_usd, gross_margin_usd, net_sales_amount_local,
  currency_code, is_promo_sale, is_loyalty_sale, _source_ingested_at)
SELECT
  l.sales_line_id,
  l.transaction_id,
  l.sales_date,
  COALESCE(st.store_id, 'UNKNOWN'),                                                        -- step 3
  COALESCE(pr.product_id, 'UNKNOWN'),
  l.customer_id,
  COALESCE(pm.promotion_id, 'NO_PROMO'),
  COALESCE(st.region, 'Unknown'),                                                          -- step 4
  l.sales_channel,
  l.line_type,
  l.quantity_units,
  l.gross_sales_amount_usd,
  l.discount_amount_usd,
  ROUND(l.gross_sales_amount_usd - l.discount_amount_usd, 2),                              -- step 5
  ROUND(l.quantity_units * COALESCE(c.unit_cost_usd, 0), 2),
  ROUND(l.gross_sales_amount_usd - l.discount_amount_usd
        - ROUND(l.quantity_units * COALESCE(c.unit_cost_usd, 0), 2), 2),
  ROUND(l.gross_sales_amount_local - l.discount_amount_local, 2),
  l.currency_code,
  CASE WHEN pm.promotion_id IS NOT NULL THEN 1 ELSE 0 END,                                 -- step 6
  CASE WHEN l.customer_id <> 'ANONYMOUS' THEN 1 ELSE 0 END,
  l._ingested_at
FROM lines l
CROSS JOIN watermark w
LEFT JOIN gold.dim_store     st ON st.store_id     = l.store_id
LEFT JOIN gold.dim_product   pr ON pr.product_id   = l.product_id
LEFT JOIN gold.dim_promotion pm ON pm.promotion_id = l.promotion_code
LEFT JOIN silver.product_cost c ON c.product_id    = l.product_id
                               AND l.sales_date BETWEEN c.valid_from AND c.valid_to
WHERE l.line_type IN ('Sale', 'Return')                                                    -- step 2
  AND l.is_test_transaction = 0
  AND l._ingested_at > w.wm
ON CONFLICT (sales_line_id) DO UPDATE SET
  transaction_id = excluded.transaction_id, sales_date = excluded.sales_date,
  store_id = excluded.store_id, product_id = excluded.product_id,
  customer_id = excluded.customer_id, promotion_id = excluded.promotion_id,
  region = excluded.region, sales_channel = excluded.sales_channel, line_type = excluded.line_type,
  quantity_units = excluded.quantity_units, gross_sales_amount_usd = excluded.gross_sales_amount_usd,
  discount_amount_usd = excluded.discount_amount_usd, net_sales_amount_usd = excluded.net_sales_amount_usd,
  cost_of_goods_usd = excluded.cost_of_goods_usd, gross_margin_usd = excluded.gross_margin_usd,
  net_sales_amount_local = excluded.net_sales_amount_local, currency_code = excluded.currency_code,
  is_promo_sale = excluded.is_promo_sale, is_loyalty_sale = excluded.is_loyalty_sale,
  _source_ingested_at = excluded._source_ingested_at;
