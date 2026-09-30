-- =====================================================================================
-- GOLD 04 · dim_promotion
-- Built from : silver.promotion
-- Grain      : one row per promotion, plus NO_PROMO
-- Load       : upsert every run
-- Steps      : 1. copy the decoded promotion attributes
--              2. add NO_PROMO so full-price lines still have a promotion to join to
-- =====================================================================================
INSERT INTO gold.dim_promotion (promotion_id, promotion_name, promotion_mechanic, start_date, end_date)
SELECT promotion_id, promotion_name, promotion_mechanic, start_date, end_date
FROM silver.promotion
WHERE 1
ON CONFLICT (promotion_id) DO UPDATE SET
  promotion_name = excluded.promotion_name, promotion_mechanic = excluded.promotion_mechanic,
  start_date = excluded.start_date, end_date = excluded.end_date;

INSERT INTO gold.dim_promotion (promotion_id, promotion_name, promotion_mechanic)
VALUES ('NO_PROMO', 'No promotion', 'No Promotion')
ON CONFLICT (promotion_id) DO NOTHING;
