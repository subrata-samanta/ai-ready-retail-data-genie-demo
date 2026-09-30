# 6 · Semantic layer: define every metric once

[← 5 · Gold](05-gold.md) · Next: [7 · Genie Agent →](07-genie-agent.md)

Gold holds clean facts. The semantic layer holds **definitions**: what "net sales", "basket size" or "margin %" mean,
written once and used by Genie, dashboards and notebooks alike. On Databricks this is a **Unity Catalog metric view**.
A metric view separates **fields** (things you group and filter by) from **measures** (aggregations), so a measure can be
queried at any grain:

```sql
SELECT region, MEASURE(net_sales) AS net_sales
FROM   freshcart.semantic.sales_metrics
WHERE  is_last_completed_fiscal_week
GROUP BY ALL;
```

SQLite has no metric views, so [`freshcart/metrics.py`](../freshcart/metrics.py) reads **the same YAML** and turns
queries like the one above into plain SQL. That is how this repo runs Genie's example queries and benchmarks locally.

## The sales metric view

<!--file semantic/sales_metrics.yaml-->

Things to notice:

- **Joins are declared once** (fact to five dimensions). Nobody writes them again.
- **Every field and measure has synonyms.** Genie imports them, so "turnover", "takings" and "revenue" all land on `net_sales`.
- **Ratios are composed from measures**: `average_basket_value = MEASURE(net_sales) / NULLIF(MEASURE(transactions), 0)`.
- **Formats travel with the metric** (currency, percentage), so every tool displays them the same way.

## Why "ratio of sums" matters

A common text-to-SQL mistake is to average line-level percentages. Here is margin % year to date computed both ways:

<!--table
SELECT 'Metric view: SUM(margin) / SUM(net sales)' AS method,
       SUM(gross_margin_usd) / SUM(net_sales_amount_usd) AS gross_margin_pct
FROM gold.fct_sales_line f JOIN gold.dim_date d ON d.calendar_date = f.sales_date WHERE d.is_fiscal_ytd
UNION ALL
SELECT 'Wrong: AVG(margin / net sales) per line',
       AVG(gross_margin_usd / NULLIF(net_sales_amount_usd, 0))
FROM gold.fct_sales_line f JOIN gold.dim_date d ON d.calendar_date = f.sales_date WHERE d.is_fiscal_ytd
-->

The average gives every line equal weight, so a $2.79 chocolate bar counts as much as a $40 salmon fillet. The metric view
makes the right answer the only answer.

## The inventory metric view: semi-additive stock

<!--file semantic/inventory_metrics.yaml-->

The `window` block says: for any period, `on_hand_units` is the value on the **last** `snapshot_date` of that period.
Compare "Produce stock at the end of last week" computed both ways:

<!--table
SELECT 'Metric view (semi-additive, last day)' AS method,
       (SELECT SUM(on_hand_units) FROM gold.fct_inventory_daily i JOIN gold.dim_product p USING (product_id)
        WHERE p.department = 'Produce' AND i.snapshot_date = (SELECT MAX(calendar_date) FROM gold.dim_date WHERE is_last_completed_fiscal_week)) AS produce_units
UNION ALL
SELECT 'Wrong: SUM over the 7 daily snapshots',
       (SELECT SUM(on_hand_units) FROM gold.fct_inventory_daily i JOIN gold.dim_product p USING (product_id)
        JOIN gold.dim_date d ON d.calendar_date = i.snapshot_date
        WHERE p.department = 'Produce' AND d.is_last_completed_fiscal_week)
-->

The same question through the metric view returns the correct value:

<!--table metric
SELECT MEASURE(on_hand_units) AS on_hand_units, MEASURE(on_hand_value) AS on_hand_value
FROM freshcart.semantic.inventory_metrics
WHERE is_last_completed_fiscal_week AND department = 'Produce'
-->

And week by week, each week shows its own closing stock, not a weekly total:

<!--table metric
SELECT fiscal_week, MEASURE(on_hand_units) AS on_hand_units, MEASURE(out_of_stock_rate) AS out_of_stock_rate
FROM freshcart.semantic.inventory_metrics
WHERE is_last_4_completed_fiscal_weeks
GROUP BY ALL ORDER BY fiscal_week
-->

## Querying the metric views: examples with real results

Net sales, transactions and basket by region, last week:

<!--table metric
SELECT region, MEASURE(net_sales) AS net_sales, MEASURE(transactions) AS transactions,
       MEASURE(average_basket_value) AS average_basket_value, MEASURE(gross_margin_pct) AS gross_margin_pct
FROM freshcart.semantic.sales_metrics
WHERE is_last_completed_fiscal_week
GROUP BY ALL ORDER BY net_sales DESC
-->

Year to date versus the same fiscal days last year:

<!--table metric
SELECT fiscal_year, MEASURE(net_sales) AS net_sales, MEASURE(transactions) AS transactions,
       MEASURE(average_basket_value) AS average_basket_value, MEASURE(promo_sales_share) AS promo_sales_share
FROM freshcart.semantic.sales_metrics
WHERE is_fiscal_ytd OR is_prior_fiscal_ytd
GROUP BY ALL ORDER BY fiscal_year
-->

Traffic (transactions) is up, the average basket is down and the promotional share is up: FY2026 has deeper promotions.
That is the kind of story a business user can now get from one question.

## What the local engine generates

For the curious, this is the exact SQLite SQL that `metrics.py` generates for
`SELECT region, MEASURE(net_sales) AS net_sales FROM freshcart.semantic.sales_metrics WHERE is_last_completed_fiscal_week GROUP BY ALL`.
On Databricks the metric-view engine does the equivalent for you.

<!--translate
SELECT region, MEASURE(net_sales) AS net_sales FROM freshcart.semantic.sales_metrics WHERE is_last_completed_fiscal_week GROUP BY ALL
-->

`mv_rows` is "what the metric view looks like": one row per fact row with every field computed through the declared
joins. The measure is then a plain aggregate over it, grouped by whatever fields you asked for.

## The trusted function: like-for-like growth

Like-for-like growth has rules no model should improvise: comparable stores only, the same fiscal days in both years,
closed stores excluded. It is written once as a function; Genie calls it with a parameter and shows a **verified answer**.

<!--table metric
SELECT * FROM freshcart.semantic.fn_like_for_like_sales(2026)
-->

The Northeast is up strongly because Boston Seaport was remodelled into a Supercenter at the start of FY2026.
The Denver store counts (comparable from FY2026); Mississauga and the Newark Dark Store do not; Buckhead is closed.

- Local body: [`semantic/fn_like_for_like_sales.local.sql`](../semantic/fn_like_for_like_sales.local.sql)
- Databricks: [`databricks/semantic/03_fn_like_for_like_sales.sql`](../databricks/semantic/03_fn_like_for_like_sales.sql)
- Generated metric-view DDL: [`databricks/semantic/02_metric_views.sql`](../databricks/semantic/02_metric_views.sql)

Next: [7 · Genie Agent →](07-genie-agent.md)
