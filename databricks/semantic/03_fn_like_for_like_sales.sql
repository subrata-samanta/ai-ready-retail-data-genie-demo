-- =====================================================================================
-- Trusted asset: like-for-like (comparable store) growth.
-- Genie calls it with a parameter and shows a verified answer; it cannot change the logic.
-- Local, tested equivalent: semantic/fn_like_for_like_sales.local.sql
-- =====================================================================================
CREATE OR REPLACE FUNCTION freshcart.semantic.fn_like_for_like_sales(
  p_fiscal_year INT COMMENT 'Fiscal year to evaluate, for example 2026. Use the current fiscal year when the user does not specify one.'
)
RETURNS TABLE (
  region            STRING,
  comparable_stores BIGINT,
  net_sales_ty      DECIMAL(38,2),
  net_sales_ly      DECIMAL(38,2),
  lfl_growth_pct    DOUBLE
)
COMMENT 'Trusted like-for-like (comparable store) net sales growth by region: fiscal year to date versus the same fiscal days last year, open comparable stores only. Use for any question about LFL, like-for-like, comp sales or same-store growth.'
RETURN
  WITH cutoff AS (
    SELECT MAX(d.fiscal_day_of_year) AS last_day
    FROM   freshcart.gold.fct_sales_line f
    JOIN   freshcart.gold.dim_date d ON f.sales_date = d.calendar_date
    WHERE  d.fiscal_year = p_fiscal_year
  ),
  comp AS (
    SELECT store_id, region FROM freshcart.gold.dim_store
    WHERE  comparable_from_fiscal_year <= p_fiscal_year AND store_status = 'Open'
  ),
  agg AS (
    SELECT c.region,
           COUNT(DISTINCT c.store_id) AS comparable_stores,
           SUM(CASE WHEN d.fiscal_year = p_fiscal_year     THEN f.net_sales_amount_usd END) AS ty,
           SUM(CASE WHEN d.fiscal_year = p_fiscal_year - 1 THEN f.net_sales_amount_usd END) AS ly
    FROM   freshcart.gold.fct_sales_line f
    JOIN   freshcart.gold.dim_date d ON f.sales_date = d.calendar_date
    JOIN   comp c                    ON f.store_id   = c.store_id
    CROSS JOIN cutoff
    WHERE  d.fiscal_year IN (p_fiscal_year, p_fiscal_year - 1)
      AND  d.fiscal_day_of_year <= cutoff.last_day
    GROUP BY c.region
  )
  SELECT region, comparable_stores,
         CAST(ty AS DECIMAL(38,2)), CAST(ly AS DECIMAL(38,2)),
         CAST(try_divide(ty, ly) - 1 AS DOUBLE)
  FROM agg;

GRANT EXECUTE ON FUNCTION freshcart.semantic.fn_like_for_like_sales TO `fc_genie_users`;
