# 9 · Run it on Databricks

[← 8 · Follow one receipt](08-trace-one-receipt.md) · Next: [10 · Data dictionary →](10-data-dictionary.md)

Everything in [`databricks/`](../databricks/) implements the same design in Unity Catalog. The local SQLite version is the
**tested reference**; the Databricks SQL mirrors it step for step and follows the Databricks documentation as of
September 2026, but it is not executed by this repo's tests. Review it in a development workspace first.

## Files, in the order you run them

| # | File | What it does | How to run |
|---|---|---|---|
| 1 | [`00_setup.sql`](../databricks/00_setup.sql) | Catalog `freshcart`, schemas per layer, landing volume | SQL editor |
| 2 | upload `data/raw/` | Put the raw files in `/Volumes/freshcart/landing/raw/` | CLI (below) or Catalog Explorer |
| 3 | [`pipelines/01_bronze.sql`](../databricks/pipelines/01_bronze.sql) + [`pipelines/02_silver.sql`](../databricks/pipelines/02_silver.sql) | Streaming bronze tables, silver materialized views with expectations | One Lakeflow Declarative Pipeline (serverless), catalog `freshcart` |
| 4 | [`gold/00_create_gold_tables.sql`](../databricks/gold/00_create_gold_tables.sql) | Gold tables with comments, PK/FK (RELY), clustering, tags. **Generated** from `contracts/gold/*.yml` | SQL editor, once |
| 5 | [`gold/01_load_gold.sql`](../databricks/gold/01_load_gold.sql) | MERGE loads: dimensions, then facts (incremental) | SQL task in a Job, after the pipeline |
| 6 | [`governance/01_security.sql`](../databricks/governance/01_security.sql) | Row filters on facts, column masks, grants | SQL editor, once |
| 7 | [`semantic/02_metric_views.sql`](../databricks/semantic/02_metric_views.sql) | The two metric views. **Generated** from `semantic/*.yaml` | SQL editor (DBR 16.4+ warehouse) |
| 8 | [`semantic/03_fn_like_for_like_sales.sql`](../databricks/semantic/03_fn_like_for_like_sales.sql) | The trusted like-for-like function | SQL editor |
| 9 | [`genie_bundle/`](../genie_bundle/) | The Genie Agent and its quality-gate job, for dev, qa and prod | `databricks bundle deploy -t <target>` |

## Step 2: upload the raw files

With the Databricks CLI configured for your workspace:

```bash
databricks fs cp -r data/raw dbfs:/Volumes/freshcart/landing/raw
```

## Step 3: the pipeline

Create a Lakeflow Declarative Pipeline with both SQL files as source code, default catalog `freshcart`, serverless compute.
The bronze tables are **streaming tables** over `read_files`, so a new monthly POS file is picked up on the next update and
never loaded twice. Silver tables are **materialized views** (they use window functions for deduplication and SCD2).
Rows that break a hard rule are dropped by an expectation **and** kept in `silver.quarantine_pos_sales_line` with the reason.

## Step 5: orchestration

A Job with two tasks: (1) the pipeline update, then (2) `gold/01_load_gold.sql` as a SQL task on a SQL warehouse.
Schedule it after the nightly files land. `dim_date` recomputes its rolling flags from `current_date()` on every run.

## Step 6: security design

- **Row filters on the two facts only**, using the `region` column copied onto each fact row.
- **Dimensions stay unfiltered**, so Genie's entity matching works on store names, categories and brands.
- **No personal data in gold**; the restricted silver table has column masks.
- Genie always queries **as the end user**, so the filters apply in every conversation.
- Metric-view **materialisation** is not available on sources with row filters; if you need it, secure a separate aggregate table.

## Step 9: deploy the Genie Agent

The agent is a Declarative Automation Bundle: [`genie_bundle/`](../genie_bundle/) holds the space (data sources, column
settings, SQL expressions, example queries, general instructions and benchmarks) and a quality-gate job, with targets
`sandbox`, `dev`, `qa` and `prod` that differ only in catalog, title and permissions:

```bash
cd genie_bundle
databricks bundle deploy -t dev
databricks bundle run genie_quality_gate -t dev       # runs the benchmarks
```

Compare the benchmark results with the expected answers in [doc 7](07-genie-agent.md#benchmarks-the-expected-answers).
The numbers match when the data and the as-of date are the same; on Databricks "today" is `current_date()`, so relative
periods such as "last week" will differ from the pinned local run. Promotion to qa and prod, version history and rollback
are covered in [`notebooks/Genie_CICD_with_Declarative_Automation_Bundles.ipynb`](../notebooks/Genie_CICD_with_Declarative_Automation_Bundles.ipynb).

## Keeping local and Databricks in step

Two Databricks files are **generated** so they cannot drift:

```bash
python -m freshcart.export_databricks     # contracts -> gold DDL, semantic YAML -> metric views
```

`test_databricks_ddl_is_up_to_date` fails if someone edits a contract without regenerating.

Next: [10 · Data dictionary →](10-data-dictionary.md)
