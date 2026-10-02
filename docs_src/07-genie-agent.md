# 7 · The Genie Agent

[← 6 · Semantic layer](06-semantic-layer.md) · Next: [8 · Follow one receipt →](08-trace-one-receipt.md)

With the data in shape, the agent configuration is short. That is the point: most of the knowledge now lives in the data
and the semantic layer, where every tool can use it. The agent has a single definition, in the Declarative Automation
Bundle [`genie_bundle/`](../genie_bundle/) ([`resources/freshcart_assistant.space.yml`](../genie_bundle/resources/freshcart_assistant.space.yml)),
which is reviewed in pull requests and deployed to dev, qa and prod. Every table on this page is read from that file.

## What the agent can see

<!--genie-sources-->

Four data objects and one function. Databricks recommends starting with five or fewer.

**Why the dimensions are attached separately.** On Databricks the facts carry row filters (regional security).
Genie excludes row-filtered tables from prompt matching, and entity matching must be off for views over filtered
tables, which includes the metric views. The dimension tables are **not** filtered, so attaching them keeps
entity matching for the values users actually type ("boston seaprt", "north east", "chips"). Entity matching stores
up to 1,024 distinct values per column, so it is not used on product names; product questions use a parameterized
example query instead. `comparable_from_fiscal_year` is hidden: use the comparable-store flag or the like-for-like function.

**What is deliberately not here**: no measure definitions in the knowledge store (they live in the metric view only,
so nothing can conflict), no join relationships (the metric views carry them), no personal data.

## Knowledge-store SQL expressions

Business terms that are not KPIs and are not worth a column:

<!--genie-expressions-->

## General instructions (the only free text)

<!--genie-instructions-->

About 250 words: a short business narrative plus the two behaviours that cannot be expressed any other way
(when to ask for clarification, how to write summaries), and guidance for the example queries. Everything else Genie
needs lives lower in the stack.

## Example SQL queries, with the answers they return on the demo data

These are titled the way users ask. Genie reuses them directly for matching questions and learns patterns from them
for similar ones. Each was run against the demo data:

<!--examples-->

## Benchmarks: the expected answers

A benchmark is a question plus the SQL that gives the right answer, signed off by the KPI owner. They are part of the
space definition, and the bundle's `genie_quality_gate` job runs them in qa before every release (and nightly in
prod). Here they are with the answers they produce on the demo data, so you can ask Genie the same question and
compare. What each one tests is noted in [`genie_bundle/benchmark_notes.yml`](../genie_bundle/benchmark_notes.yml). Write these **before** tuning the agent; each failure tells you which
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
