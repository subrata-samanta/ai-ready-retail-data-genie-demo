-- Title: Where are we out of stock most often in fresh?
SELECT region,
       store_name,
       category,
       MEASURE(out_of_stock_rate) AS out_of_stock_rate,
       MEASURE(out_of_stock_days) AS out_of_stock_days
FROM   freshcart.semantic.inventory_metrics
WHERE  is_last_4_completed_fiscal_weeks
  AND  department IN ('Produce', 'Bakery', 'Meat and Seafood', 'Deli', 'Dairy and Eggs')
GROUP BY ALL
ORDER BY out_of_stock_rate DESC
LIMIT 10;
