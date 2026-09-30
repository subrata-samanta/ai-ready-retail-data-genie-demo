-- =====================================================================================
-- GOLD 07 · fct_inventory_daily
-- Built from : silver.inventory_daily joined to gold.dim_store, gold.dim_product
--              and silver.product_cost
-- Grain      : one row per store, ranged product and day
-- Load       : upsert by (snapshot_date, store_id, product_id); re-sending a day replaces it
-- Steps      : 1. resolve keys to dimension rows (or UNKNOWN)
--              2. copy region for row-level security
--              3. value the stock at the standard cost valid on the snapshot date
--              4. out-of-stock = an ACTIVE product with no sellable stock.
--                 Products a store does not range never appear in the snapshot, so they
--                 can never be counted as out of stock by mistake.
-- =====================================================================================
INSERT INTO gold.fct_inventory_daily (snapshot_date, store_id, product_id, region, on_hand_units,
                                      on_hand_value_usd, is_out_of_stock)
SELECT
  i.snapshot_date,
  COALESCE(st.store_id, 'UNKNOWN'),                                                        -- step 1
  COALESCE(pr.product_id, 'UNKNOWN'),
  COALESCE(st.region, 'Unknown'),                                                          -- step 2
  i.on_hand_units,
  ROUND(i.on_hand_units * COALESCE(c.unit_cost_usd, 0), 2),                                -- step 3
  CASE WHEN i.on_hand_units <= 0 AND pr.product_status = 'Active' THEN 1 ELSE 0 END        -- step 4
FROM silver.inventory_daily i
LEFT JOIN gold.dim_store   st ON st.store_id   = i.store_id
LEFT JOIN gold.dim_product pr ON pr.product_id = i.product_id
LEFT JOIN silver.product_cost c ON c.product_id = i.product_id
                               AND i.snapshot_date BETWEEN c.valid_from AND c.valid_to
WHERE 1
ON CONFLICT (snapshot_date, store_id, product_id) DO UPDATE SET
  region = excluded.region, on_hand_units = excluded.on_hand_units,
  on_hand_value_usd = excluded.on_hand_value_usd, is_out_of_stock = excluded.is_out_of_stock;
