-- =====================================================================================
-- GOLD 01 · dim_date
-- Built from : silver.fiscal_calendar
-- Grain      : one row per calendar day
-- Load       : upsert every run (the rolling flags move every day)
-- Steps      : 1. find "yesterday" and the last fully completed fiscal week relative to
--                 the as-of date (${AS_OF_DATE}; current_date() in production)
--              2. copy the fiscal attributes
--              3. precompute the rolling flags so "last week" or "YTD vs last year"
--                 becomes a simple filter instead of date arithmetic Genie must guess
-- =====================================================================================
WITH cal AS (
  SELECT * FROM silver.fiscal_calendar
),
yesterday AS (                                                                   -- step 1
  SELECT fiscal_year AS cur_fy, fiscal_quarter AS cur_fq, fiscal_day_of_year AS cur_fdoy
  FROM cal
  WHERE calendar_date = date('${AS_OF_DATE}', '-1 day')
),
last_week AS (
  SELECT MAX(fiscal_week_start_date) AS lw_start
  FROM cal
  WHERE date(fiscal_week_start_date, '+6 days') < '${AS_OF_DATE}'
)
INSERT INTO gold.dim_date (
  calendar_date, day_of_week_name, fiscal_year, fiscal_quarter, fiscal_period, fiscal_period_name,
  fiscal_week, fiscal_week_start_date, fiscal_day_of_year,
  is_last_completed_fiscal_week, is_last_4_completed_fiscal_weeks, is_current_fiscal_quarter,
  is_fiscal_ytd, is_prior_fiscal_ytd)
SELECT
  c.calendar_date, c.day_of_week_name, c.fiscal_year, c.fiscal_quarter, c.fiscal_period,  -- step 2
  c.fiscal_period_name, c.fiscal_week, c.fiscal_week_start_date, c.fiscal_day_of_year,
  c.fiscal_week_start_date = lw.lw_start,                                                  -- step 3
  c.fiscal_week_start_date BETWEEN date(lw.lw_start, '-21 days') AND lw.lw_start,
  c.fiscal_year = y.cur_fy AND c.fiscal_quarter = y.cur_fq,
  c.fiscal_year = y.cur_fy     AND c.fiscal_day_of_year <= y.cur_fdoy,
  c.fiscal_year = y.cur_fy - 1 AND c.fiscal_day_of_year <= y.cur_fdoy
FROM cal c
CROSS JOIN yesterday y
CROSS JOIN last_week lw
WHERE 1
ON CONFLICT (calendar_date) DO UPDATE SET
  day_of_week_name                 = excluded.day_of_week_name,
  fiscal_year                      = excluded.fiscal_year,
  fiscal_quarter                   = excluded.fiscal_quarter,
  fiscal_period                    = excluded.fiscal_period,
  fiscal_period_name               = excluded.fiscal_period_name,
  fiscal_week                      = excluded.fiscal_week,
  fiscal_week_start_date           = excluded.fiscal_week_start_date,
  fiscal_day_of_year               = excluded.fiscal_day_of_year,
  is_last_completed_fiscal_week    = excluded.is_last_completed_fiscal_week,
  is_last_4_completed_fiscal_weeks = excluded.is_last_4_completed_fiscal_weeks,
  is_current_fiscal_quarter        = excluded.is_current_fiscal_quarter,
  is_fiscal_ytd                    = excluded.is_fiscal_ytd,
  is_prior_fiscal_ytd              = excluded.is_prior_fiscal_ytd;
