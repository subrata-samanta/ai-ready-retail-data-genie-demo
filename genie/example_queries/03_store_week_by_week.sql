-- Title: How did a store trade week by week in a fiscal year?   (parameterized -> trusted asset)
-- Parameters:
--   :store_name  (String)  Exact store name from dim_store, for example FreshCart Boston Seaport
--   :fiscal_year (Integer) Fiscal year such as 2026; default to the current fiscal year
SELECT fiscal_week,
       fiscal_week_start_date,
       MEASURE(net_sales)    AS net_sales,
       MEASURE(transactions) AS transactions
FROM   freshcart.semantic.sales_metrics
WHERE  store_name  = :store_name
  AND  fiscal_year = :fiscal_year
GROUP BY ALL
ORDER BY fiscal_week;
