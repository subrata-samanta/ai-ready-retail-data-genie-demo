-- =====================================================================================
-- SILVER 05 · promotion  and  promotion_item
-- Source : bronze.promo_calendar_raw, bronze.promo_item_raw
-- Grain  : promotion      -> one row per promotion
--          promotion_item -> one row per promotion per product
-- Steps  : 1. decode the mechanic: BOGO -> 'Buy One Get One', PCT -> 'Percent Off',
--             MULTI -> 'Multi-buy', LOY -> 'Loyalty Price'
--          2. type dates and the discount percentage
--          3. normalise product ids
-- =====================================================================================
DROP TABLE IF EXISTS silver.promotion;
CREATE TABLE silver.promotion (
  promotion_id       TEXT PRIMARY KEY,
  promotion_name     TEXT NOT NULL,
  promotion_mechanic TEXT NOT NULL,
  start_date         TEXT NOT NULL,
  end_date           TEXT NOT NULL,
  discount_pct       REAL
);

INSERT INTO silver.promotion
SELECT
  PROMO_CD,
  PROMO_DESC,
  CASE MECH_CD WHEN 'BOGO'  THEN 'Buy One Get One'
               WHEN 'PCT'   THEN 'Percent Off'
               WHEN 'MULTI' THEN 'Multi-buy'
               WHEN 'LOY'   THEN 'Loyalty Price'
               ELSE 'Other' END,
  try_to_date(START_DT, 'yyyy-MM-dd'),
  try_to_date(END_DT, 'yyyy-MM-dd'),
  CAST(NULLIF(DISC_PCT, '') AS REAL)
FROM bronze.promo_calendar_raw;

DROP TABLE IF EXISTS silver.promotion_item;
CREATE TABLE silver.promotion_item (
  promotion_id TEXT NOT NULL,
  product_id   TEXT NOT NULL,
  PRIMARY KEY (promotion_id, product_id)
);

INSERT OR IGNORE INTO silver.promotion_item
SELECT PROMO_CD, ltrim(MATNR, '0') FROM bronze.promo_item_raw;
