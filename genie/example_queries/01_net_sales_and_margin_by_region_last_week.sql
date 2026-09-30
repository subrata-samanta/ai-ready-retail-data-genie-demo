-- Title (the way users ask): What were net sales and margin by region last week?
-- Usage guidance: "Last week" always means the last completed fiscal week, Sunday to Saturday.
SELECT region,
       MEASURE(net_sales)        AS net_sales,
       MEASURE(gross_margin)     AS gross_margin,
       MEASURE(gross_margin_pct) AS gross_margin_pct
FROM   freshcart.semantic.sales_metrics
WHERE  is_last_completed_fiscal_week
GROUP BY ALL
ORDER BY net_sales DESC;
