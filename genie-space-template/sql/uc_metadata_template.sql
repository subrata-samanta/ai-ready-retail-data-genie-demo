-- =====================================================================================================
-- Make your tables AI-ready for Genie: the FreshCart practices, as SQL to copy and adapt.
-- Run in a SQL editor or notebook, once per environment (dev first). Replace the example names
-- (my_catalog, gold.fct_orders, region, net_amount, ...) with yours. Nothing here is run by the bundle.
-- `python scripts/check_space.py --live dev` reports what is still missing.
-- FreshCart's real versions: databricks/gold/00_create_gold_tables.sql, databricks/semantic/,
-- databricks/governance/.
-- =====================================================================================================

-- 1. Put meaning in metadata ---------------------------------------------------------------------------
--    Genie reads table and column comments to pick tables and columns. Say what a row is (the grain),
--    the unit and currency, and which column to use for which question.
COMMENT ON TABLE my_catalog.gold.fct_orders IS
  'One row per order line. Amounts are USD excluding tax. Use for any question about sales, orders or margin.';
ALTER TABLE my_catalog.gold.fct_orders ALTER COLUMN net_amount
  COMMENT 'Line revenue after discounts, USD, excluding tax. Sum it for sales; returns are negative.';
ALTER TABLE my_catalog.gold.fct_orders ALTER COLUMN order_date
  COMMENT 'Business date of the order. Join to dim_date.calendar_date for fiscal periods.';

-- 2. Declare keys, so Genie knows how tables join ---------------------------------------------------------
--    RELY tells the optimizer (and Genie) the constraint holds; Unity Catalog does not enforce it.
ALTER TABLE my_catalog.gold.dim_customer ALTER COLUMN customer_id SET NOT NULL;
ALTER TABLE my_catalog.gold.dim_customer ADD CONSTRAINT pk_dim_customer PRIMARY KEY (customer_id) RELY;
ALTER TABLE my_catalog.gold.fct_orders ADD CONSTRAINT fk_orders_customer
  FOREIGN KEY (customer_id) REFERENCES my_catalog.gold.dim_customer (customer_id);

-- 3. Define every KPI once, in a metric view -----------------------------------------------------------
--    Genie (and dashboards) query MEASURE(net_sales) and can't get a ratio or a semi-additive measure
--    wrong. Synonyms are the words your users type. Databricks Runtime 16.4+ / a current SQL warehouse.
CREATE OR REPLACE VIEW my_catalog.semantic.sales_metrics
WITH METRICS
LANGUAGE YAML
AS $$
version: 1.1
comment: Governed sales KPIs at order-line grain. Amounts are USD excluding tax.
source: my_catalog.gold.fct_orders
joins:
  - name: customer
    source: my_catalog.gold.dim_customer
    on: source.customer_id = customer.customer_id
dimensions:
  - name: order_date
    expr: order_date
    synonyms: [date, day]
  - name: region
    expr: customer.region
    synonyms: [area, territory]
measures:
  - name: net_sales
    expr: SUM(net_amount)
    display_name: Net Sales
    synonyms: [revenue, sales, turnover]
  - name: order_count
    expr: COUNT(DISTINCT order_id)
    synonyms: [orders, transactions]
  - name: average_order_value
    expr: SUM(net_amount) / COUNT(DISTINCT order_id)
    synonyms: [AOV, basket size]
$$;

-- 4. Encode tricky logic as a trusted function ---------------------------------------------------------
--    Add it to the space as a trusted asset; Genie calls it with parameters and can't change the logic.
CREATE OR REPLACE FUNCTION my_catalog.semantic.fn_sales_growth(p_year INT COMMENT 'Year to compare with the year before.')
RETURNS TABLE (region STRING, net_sales_ty DECIMAL(38,2), net_sales_ly DECIMAL(38,2), growth_pct DOUBLE)
COMMENT 'Trusted year-over-year net sales growth by region. Use for any question about growth or YoY.'
RETURN
  WITH s AS (
    SELECT c.region, YEAR(o.order_date) AS yr, o.net_amount
    FROM   my_catalog.gold.fct_orders o
    JOIN   my_catalog.gold.dim_customer c ON o.customer_id = c.customer_id
    WHERE  YEAR(o.order_date) IN (p_year, p_year - 1)           -- parameters only in WHERE, not in aggregates
  )
  SELECT region,
         SUM(CASE WHEN yr = (SELECT MAX(yr) FROM s) THEN net_amount END),
         SUM(CASE WHEN yr = (SELECT MIN(yr) FROM s) THEN net_amount END),
         ROUND(100 * (SUM(CASE WHEN yr = (SELECT MAX(yr) FROM s) THEN net_amount END)
                      / NULLIF(SUM(CASE WHEN yr = (SELECT MIN(yr) FROM s) THEN net_amount END), 0) - 1), 1)
  FROM s GROUP BY region;

-- 5. Govern in the data ---------------------------------------------------------------------------------
--    Genie runs every query as the person asking, so these apply in every conversation.
--    Business users need USE CATALOG, USE SCHEMA and SELECT (account groups only).
GRANT USE CATALOG ON CATALOG my_catalog          TO `my-business-users`;
GRANT USE SCHEMA  ON SCHEMA  my_catalog.gold     TO `my-business-users`;
GRANT USE SCHEMA  ON SCHEMA  my_catalog.semantic TO `my-business-users`;
GRANT SELECT      ON SCHEMA  my_catalog.gold     TO `my-business-users`;
GRANT SELECT      ON SCHEMA  my_catalog.semantic TO `my-business-users`;
GRANT EXECUTE     ON FUNCTION my_catalog.semantic.fn_sales_growth TO `my-business-users`;

--    Row filter on the facts only (dimensions stay unfiltered so entity matching keeps working).
CREATE OR REPLACE FUNCTION my_catalog.gold.rf_region(region STRING)
RETURN is_account_group_member('sales_all_regions') OR is_account_group_member(CONCAT('sales_region_', lower(region)));
ALTER TABLE my_catalog.gold.fct_orders SET ROW FILTER my_catalog.gold.rf_region ON (region);

-- 6. Check what is left --------------------------------------------------------------------------------
--    Columns without a comment, per table, in the schemas the space uses.
SELECT table_schema, table_name, COUNT(*) AS columns_without_comment
FROM   my_catalog.information_schema.columns
WHERE  table_schema IN ('gold', 'semantic') AND (comment IS NULL OR trim(comment) = '')
GROUP  BY ALL
ORDER  BY columns_without_comment DESC;
