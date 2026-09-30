-- Title: How does a product sell across regions?   (parameterized for product names)
-- Parameters:
--   :product_search (String) Part of a product name, for example chips
SELECT region,
       product_name,
       MEASURE(units_sold) AS units_sold,
       MEASURE(net_sales)  AS net_sales
FROM   freshcart.semantic.sales_metrics
WHERE  product_name ILIKE '%' || :product_search || '%'
  AND  is_last_4_completed_fiscal_weeks
GROUP BY ALL
ORDER BY net_sales DESC;
