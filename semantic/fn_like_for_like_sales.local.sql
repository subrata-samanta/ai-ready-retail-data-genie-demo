-- Local (SQLite) body of the trusted function freshcart.semantic.fn_like_for_like_sales.
-- The Databricks version is databricks/semantic/03_fn_like_for_like_sales.sql.
-- Parameter :p_fiscal_year  - fiscal year to evaluate, for example 2026.
--
-- Like-for-like (comparable store) growth:
--   * only stores that are open and comparable in the evaluated year
--     (comparable_from_fiscal_year is computed once, in gold.dim_store)
--   * this year to date versus the SAME fiscal days last year
--     (fiscal_day_of_year lines up weekdays because fiscal years start on a Sunday)
WITH cutoff AS (
  SELECT MAX(d.fiscal_day_of_year) AS last_day
  FROM   gold.fct_sales_line f
  JOIN   gold.dim_date d ON f.sales_date = d.calendar_date
  WHERE  d.fiscal_year = :p_fiscal_year
),
comp AS (
  SELECT store_id, region
  FROM   gold.dim_store
  WHERE  comparable_from_fiscal_year <= :p_fiscal_year
    AND  store_status = 'Open'
),
agg AS (
  SELECT
    c.region,
    COUNT(DISTINCT c.store_id) AS comparable_stores,
    SUM(CASE WHEN d.fiscal_year = :p_fiscal_year     THEN f.net_sales_amount_usd END) AS ty,
    SUM(CASE WHEN d.fiscal_year = :p_fiscal_year - 1 THEN f.net_sales_amount_usd END) AS ly
  FROM   gold.fct_sales_line f
  JOIN   gold.dim_date d ON f.sales_date = d.calendar_date
  JOIN   comp c          ON f.store_id   = c.store_id
  CROSS JOIN cutoff
  WHERE  d.fiscal_year IN (:p_fiscal_year, :p_fiscal_year - 1)
    AND  d.fiscal_day_of_year <= cutoff.last_day
  GROUP BY c.region
)
SELECT region,
       comparable_stores,
       ROUND(ty, 2)                 AS net_sales_ty,
       ROUND(ly, 2)                 AS net_sales_ly,
       ty / NULLIF(ly, 0) - 1       AS lfl_growth_pct
FROM agg
ORDER BY region
