-- =====================================================================================
-- GOLD 02 · dim_store
-- Built from : silver.store_history (current version only) + silver.fiscal_calendar
-- Grain      : one row per store, plus UNKNOWN
-- Load       : upsert every run (status and comparable flag depend on the as-of date)
-- Steps      : 1. take the CURRENT version of each store from the SCD2 history
--              2. look up the fiscal year and fiscal day on which the store opened
--              3. comparable (like-for-like) rule, written once, here:
--                   a store is comparable in fiscal year N if it traded ALL of year N-1
--                   -> opened on day 1 of a fiscal year : comparable from that year + 1
--                   -> opened on any other day          : comparable from that year + 2
--                   -> Dark Stores are never comparable
--              4. status (Open/Closed) and the current-year comparable flag
--              5. add the UNKNOWN member so no fact row is ever orphaned
-- =====================================================================================
WITH cur AS (
  SELECT fiscal_year AS cur_fy
  FROM silver.fiscal_calendar
  WHERE calendar_date = date('${AS_OF_DATE}', '-1 day')
),
s AS (
  SELECT
    h.*,
    cal.fiscal_year                                                     AS open_fiscal_year,      -- step 2
    CASE WHEN h.store_format = 'Dark Store'  THEN NULL                                            -- step 3
         WHEN cal.fiscal_day_of_year = 1     THEN cal.fiscal_year + 1
         ELSE cal.fiscal_year + 2 END                                   AS comparable_from_fiscal_year,
    CASE WHEN h.close_date IS NOT NULL AND h.close_date <= '${AS_OF_DATE}'
         THEN 'Closed' ELSE 'Open' END                                  AS store_status             -- step 4
  FROM silver.store_history h
  LEFT JOIN silver.fiscal_calendar cal ON cal.calendar_date = h.open_date
  WHERE h.is_current = 1                                                                          -- step 1
)
INSERT INTO gold.dim_store (
  store_id, store_name, store_format, city, state_province, country, region, open_date,
  open_fiscal_year, close_date, store_status, selling_area_sqft, comparable_from_fiscal_year,
  is_comparable_store_current_fy)
SELECT
  s.store_id, s.store_name, s.store_format, s.city, s.state_province, s.country, s.region,
  s.open_date, s.open_fiscal_year, s.close_date, s.store_status, s.selling_area_sqft,
  s.comparable_from_fiscal_year,
  CASE WHEN s.store_status = 'Open' AND s.comparable_from_fiscal_year <= cur.cur_fy THEN 1 ELSE 0 END
FROM s CROSS JOIN cur
WHERE 1
ON CONFLICT (store_id) DO UPDATE SET
  store_name = excluded.store_name, store_format = excluded.store_format, city = excluded.city,
  state_province = excluded.state_province, country = excluded.country, region = excluded.region,
  open_date = excluded.open_date, open_fiscal_year = excluded.open_fiscal_year,
  close_date = excluded.close_date, store_status = excluded.store_status,
  selling_area_sqft = excluded.selling_area_sqft,
  comparable_from_fiscal_year = excluded.comparable_from_fiscal_year,
  is_comparable_store_current_fy = excluded.is_comparable_store_current_fy;

INSERT INTO gold.dim_store (store_id, store_name, store_format, region, store_status,              -- step 5
                            is_comparable_store_current_fy)
VALUES ('UNKNOWN', 'Unknown store', 'Unknown', 'Unknown', 'Unknown', 0)
ON CONFLICT (store_id) DO NOTHING;
