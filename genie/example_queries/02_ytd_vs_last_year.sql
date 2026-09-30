-- Title: How are we trading year to date versus last year?
-- Usage guidance: Total business comparison. For comparable-store growth use fn_like_for_like_sales.
SELECT fiscal_year,
       MEASURE(net_sales)            AS net_sales,
       MEASURE(transactions)         AS transactions,
       MEASURE(average_basket_value) AS average_basket_value
FROM   freshcart.semantic.sales_metrics
WHERE  is_fiscal_ytd OR is_prior_fiscal_ytd
GROUP BY ALL
ORDER BY fiscal_year;
