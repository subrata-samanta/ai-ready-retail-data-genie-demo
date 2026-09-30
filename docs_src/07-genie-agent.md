# 7 · The Genie Agent

[← 6 · Semantic layer](06-semantic-layer.md) · Next: [8 · Follow one receipt →](08-trace-one-receipt.md)

With the data in shape, the agent configuration is short. That is the point: most of the knowledge now lives in the data
and the semantic layer, where every tool can use it. The full configuration is in
[`genie/agent_config.yaml`](../genie/agent_config.yaml), written so it can be reviewed in pull requests.

## What the agent can see

| Object | Why it is attached |
|---|---|
| `freshcart.semantic.sales_metrics` | All sales, margin, basket, promotion and loyalty questions |
| `freshcart.semantic.inventory_metrics` | Stock and availability questions |
| `freshcart.gold.dim_store` | Store lookups and **entity matching** on store names, cities, regions |
| `freshcart.gold.dim_product` | Product lookups and **entity matching** on categories, subcategories, brands |
| `freshcart.semantic.fn_like_for_like_sales` | Trusted like-for-like growth |

Four data objects and one function. Databricks recommends starting with five or fewer.

**Why the dimensions are attached separately.** On Databricks the facts carry row filters (regional security).
Genie excludes row-filtered tables from prompt matching, and entity matching must be off for views over filtered
tables, which includes the metric views. The dimension tables are **not** filtered, so attaching them keeps
entity matching for the values users actually type ("boston seaprt", "north east", "chips").

**What is deliberately not here**: no measure definitions in the knowledge store (they live in the metric view only,
so nothing can conflict), no join relationships (the metric views carry them), no personal data.

## Knowledge-store SQL expressions

Business terms that are not KPIs and are not worth a column:

| Type | Name | Code | Synonyms |
|---|---|---|---|
| Filter | Fresh departments | `department IN ('Produce', 'Bakery', 'Meat and Seafood', 'Deli', 'Dairy and Eggs')` | fresh, perishables |
| Filter | Comparable stores | `is_comparable_store = true` | comp stores, LFL stores |
| Field | Day type | `CASE WHEN day_of_week_name IN ('Saturday', 'Sunday') THEN 'Weekend' ELSE 'Weekday' END` | weekend vs weekday |

## General instructions (the only free text)

<!--file genie/general_instructions.md lang=markdown-->

About 250 words: a short business narrative plus the two behaviours that cannot be expressed any other way
(when to ask for clarification, how to write summaries). Everything else Genie needs lives lower in the stack.

## Example SQL queries, with the answers they return on the demo data

These are titled the way users ask. Genie reuses them directly for matching questions and learns patterns from them
for similar ones. Each was run against the demo data:

<!--examples-->

## Benchmarks: the expected answers

A benchmark is a question plus the SQL that gives the right answer, signed off by the KPI owner. On Databricks you add
them to the agent and run them to score Genie. Here they are with the answers they produce on the demo data, so you
can ask Genie the same question and compare. Write these **before** tuning the agent; each failure tells you which
layer lacks context.

<!--benchmarks-->

## When Genie gets it wrong: fix at the lowest layer

| Symptom | Fix it in |
|---|---|
| Wrong table chosen | Table comment (the "mission statement") in the contract |
| Wrong column (gross instead of net) | Column comment, measure synonyms, hide the raw column |
| "north east" returns nothing | Decoded values in silver; entity matching on the dimension |
| "Last week" means the wrong days | Precomputed flag in `dim_date` |
| Margin % looks wrong | Ratio-of-sums measure in the metric view |
| Monthly stock 30× too high | Semi-additive window measure |
| Like-for-like differs from Finance | Trusted function owned by Finance |
| Ignores an instruction in long chats | Move the rule into the data, the metric view or an example query |

Next: [8 · Follow one receipt →](08-trace-one-receipt.md)
