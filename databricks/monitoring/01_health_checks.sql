-- =====================================================================================
-- Data health checks, run after every refresh by src/monitor.py (task `monitor` of the job
-- freshcart_refresh). One row per check; a check passes when observed <= threshold (rule 'max')
-- or observed >= threshold (rule 'min'). Results are appended to ${catalog}.monitoring.health_checks,
-- and any failed check fails the task, which sends the job's failure e-mail.
-- ${as_of_date} is the day treated as today, as in the pipeline ('today' = current_date()).
-- =====================================================================================
WITH today AS (SELECT COALESCE(try_to_date('${as_of_date}'), current_date()) AS d),
sales AS (
  SELECT COUNT(*) AS line_count,
         MAX(sales_date) AS last_date,
         COUNT_IF(product_id = 'UNKNOWN') AS unknown_products,
         COUNT_IF(store_id = 'UNKNOWN')   AS unknown_stores
  FROM ${catalog}.gold.fct_sales_line
),
last_week AS (
  SELECT COUNT(*) AS line_count
  FROM ${catalog}.gold.fct_sales_line f
  JOIN ${catalog}.gold.dim_date d ON f.sales_date = d.calendar_date
  WHERE d.is_last_completed_fiscal_week
),
stock AS (SELECT MAX(snapshot_date) AS last_date FROM ${catalog}.gold.fct_inventory_daily),
quarantine AS (
  SELECT (SELECT COUNT(*) FROM ${catalog}.silver.quarantine_pos_sales_line) AS rejected,
         (SELECT COUNT(*) FROM ${catalog}.silver.pos_sales_line)            AS accepted
),
checks AS (
  SELECT 'sales_are_fresh' AS check_name, CAST(datediff(t.d, s.last_date) AS DOUBLE) AS observed,
         1.0 AS threshold, 'max' AS rule, 'days since the last sales date (1 = yesterday)' AS detail
  FROM sales s CROSS JOIN today t
  UNION ALL
  SELECT 'stock_is_fresh', CAST(datediff(t.d, k.last_date) AS DOUBLE), 1.0, 'max',
         'days since the last stock snapshot (1 = yesterday)'
  FROM stock k CROSS JOIN today t
  UNION ALL
  SELECT 'sales_last_week', CAST(line_count AS DOUBLE), 1.0, 'min', 'sales lines in the last completed fiscal week'
  FROM last_week
  UNION ALL
  SELECT 'pos_quarantine_pct', 100.0 * rejected / NULLIF(rejected + accepted, 0), 1.0, 'max',
         'percent of POS lines rejected by a hard rule (silver.quarantine_pos_sales_line)'
  FROM quarantine
  UNION ALL
  SELECT 'unknown_product_pct', 100.0 * unknown_products / NULLIF(line_count, 0), 0.5, 'max',
         'percent of sales lines whose product is not in dim_product'
  FROM sales
  UNION ALL
  SELECT 'unknown_store_pct', 100.0 * unknown_stores / NULLIF(line_count, 0), 0.5, 'max',
         'percent of sales lines whose store is not in dim_store'
  FROM sales
)
SELECT check_name, observed, threshold, rule,
       COALESCE(CASE rule WHEN 'max' THEN observed <= threshold ELSE observed >= threshold END, false) AS passed,
       detail
FROM checks
