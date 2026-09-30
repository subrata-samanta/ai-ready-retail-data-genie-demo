-- =====================================================================================
-- SILVER 09 · inventory_daily
-- Source : bronze.inventory_raw   (nightly stock snapshot per store and item)
-- Grain  : one row per store, product and day
-- Steps  : 1. type and normalise ids and dates
--          2. negative stock is physically impossible ("phantom inventory" from
--             unrecorded shrink); clamp it to zero and FLAG it so it can be investigated
--          3. keep the latest delivery if a snapshot is re-sent
-- =====================================================================================
DROP TABLE IF EXISTS silver.inventory_daily;
CREATE TABLE silver.inventory_daily (
  snapshot_date      TEXT    NOT NULL,
  store_id           TEXT    NOT NULL,
  product_id         TEXT    NOT NULL,
  on_hand_units      REAL    NOT NULL,
  on_order_units     REAL    NOT NULL,
  had_negative_stock INTEGER NOT NULL,
  _ingested_at       TEXT,
  PRIMARY KEY (snapshot_date, store_id, product_id)
);

INSERT INTO silver.inventory_daily
SELECT snapshot_date, store_id, product_id,
       MAX(0, raw_on_hand),                                                     -- step 2
       on_order_units,
       CASE WHEN raw_on_hand < 0 THEN 1 ELSE 0 END,
       _ingested_at
FROM (
  SELECT try_to_date(SNAP_DT, 'yyyyMMdd')  AS snapshot_date,                    -- step 1
         ltrim(STR_NBR, '0')              AS store_id,
         ltrim(ITM_ID, '0')               AS product_id,
         CAST(OH_QTY AS REAL)             AS raw_on_hand,
         CAST(ON_ORD_QTY AS REAL)         AS on_order_units,
         _ingested_at,
         ROW_NUMBER() OVER (PARTITION BY SNAP_DT, STR_NBR, ITM_ID
                            ORDER BY _ingested_at DESC, _row_number DESC) AS rn  -- step 3
  FROM bronze.inventory_raw
)
WHERE rn = 1 AND snapshot_date IS NOT NULL;
