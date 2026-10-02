-- =====================================================================================
-- Trusted asset: like-for-like (comparable store) growth. ${catalog}: see 00_setup.sql.
-- Genie calls it with a parameter and shows a verified answer; it cannot change the logic.
-- Local, tested equivalent: semantic/fn_like_for_like_sales.local.sql
-- =====================================================================================
CREATE OR REPLACE FUNCTION ${catalog}.semantic.fn_like_for_like_sales(
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
  -- The parameter p_fiscal_year is used only in WHERE filters: inside a SQL function it is an outer reference,
  -- and an aggregate that mixes it with table columns (SUM(CASE WHEN fiscal_year = p_fiscal_year ...)) is rejected
  -- (UNSUPPORTED_SUBQUERY_EXPRESSION_CATEGORY.AGGREGATE_FUNCTION_MIXED_OUTER_LOCAL_REFERENCES).
  WITH cutoff AS (                     -- last fiscal day with sales in the year: compare the same days last year
    SELECT MAX(d.fiscal_day_of_year) AS last_day
    FROM   ${catalog}.gold.fct_sales_line f
    JOIN   ${catalog}.gold.dim_date d ON f.sales_date = d.calendar_date
    WHERE  d.fiscal_year = p_fiscal_year
  ),
  comp AS (                            -- open stores that are comparable in that year
    SELECT store_id, region FROM ${catalog}.gold.dim_store
    WHERE  comparable_from_fiscal_year <= p_fiscal_year AND store_status = 'Open'
  ),
  sales AS (                           -- their sales in both years, up to the cutoff day
    SELECT c.region, c.store_id, d.fiscal_year, f.net_sales_amount_usd
    FROM   ${catalog}.gold.fct_sales_line f
    JOIN   ${catalog}.gold.dim_date d ON f.sales_date = d.calendar_date
    JOIN   comp c                     ON f.store_id   = c.store_id
    CROSS JOIN cutoff
    WHERE  d.fiscal_year IN (p_fiscal_year, p_fiscal_year - 1)
      AND  d.fiscal_day_of_year <= cutoff.last_day
  ),
  stores AS (SELECT region, COUNT(DISTINCT store_id) AS comparable_stores FROM sales GROUP BY region),
  ty     AS (SELECT region, SUM(net_sales_amount_usd) AS ty FROM sales WHERE fiscal_year = p_fiscal_year     GROUP BY region),
  ly     AS (SELECT region, SUM(net_sales_amount_usd) AS ly FROM sales WHERE fiscal_year = p_fiscal_year - 1 GROUP BY region)
  SELECT s.region, s.comparable_stores,
         CAST(ty.ty AS DECIMAL(38,2)), CAST(ly.ly AS DECIMAL(38,2)),
         CAST(try_divide(ty.ty, ly.ly) - 1 AS DOUBLE)
  FROM stores s
  LEFT JOIN ty ON ty.region = s.region
  LEFT JOIN ly ON ly.region = s.region;

GRANT EXECUTE ON FUNCTION ${catalog}.semantic.fn_like_for_like_sales TO `freshcart-business-users`;
