-- Title: Which categories rely most on promotions this quarter?
SELECT department,
       category,
       MEASURE(net_sales)         AS net_sales,
       MEASURE(promo_sales_share) AS promo_sales_share
FROM   freshcart.semantic.sales_metrics
WHERE  is_current_fiscal_quarter
GROUP BY ALL
ORDER BY promo_sales_share DESC;
