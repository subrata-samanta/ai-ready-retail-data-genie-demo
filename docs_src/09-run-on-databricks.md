# 9 · Run it on Databricks

[← 8 · Follow one receipt](08-trace-one-receipt.md) · Next: [10 · Data dictionary →](10-data-dictionary.md)

[`databricks/`](../databricks/) implements the same design in Unity Catalog, as a Declarative Automation Bundle
(`freshcart-data`): a Lakeflow pipeline for bronze and silver, and a refresh job that builds gold, security and the
semantic layer. The Genie Agent is a second bundle, [`genie_bundle/`](../genie_bundle/), deployed after it.

The local SQLite version is the **tested reference**. The Databricks SQL mirrors it step for step. The repository's
tests check it offline: every file splits into statements, every placeholder has a value, every job statement
parses as Databricks SQL, every MERGE selects exactly the target table's columns, and the end-to-end notebook runs
from top to bottom against a stand-in workspace with the real Databricks CLI. They do not execute it on Databricks;
step 10 of the notebook does that comparison in your workspace.

## The fastest way: one notebook

[`notebooks/FreshCart_End_to_End_on_Databricks.ipynb`](../notebooks/FreshCart_End_to_End_on_Databricks.ipynb) takes an
empty workspace to a governed Genie space in dev, then qa and prod. Add this repository to the workspace as a
**Git folder**, open the notebook from there on **serverless** compute and run it. The same cells are in
`FreshCart_End_to_End_on_Databricks_source.py` (Databricks source format, for clean diffs in reviews); the `.ipynb` is
generated from it. The notebook:

1. installs the Databricks CLI and creates the groups used by row filters, masks and permissions;
2. creates the catalog, schemas and landing volume, and uploads `data/raw/`;
3. deploys the data bundle and runs the refresh job;
4. shows every layer, then compares the answer to every Genie benchmark with the tested local reference;
5. deploys the Genie bundle, asks Genie questions and runs the benchmark gate;
6. walks through version history: an edit in the Genie UI is detected (drift), captured into git, or rolled back;
7. promotes the same bundles to qa and prod, creates the CI/CD service principals and cleans up.

## The data bundle

| Job task | Runs | Builds |
|---|---|---|
| `setup` | [`00_setup.sql`](../databricks/00_setup.sql), [`governance/00_functions.sql`](../databricks/governance/00_functions.sql) | Schemas per layer, the landing volume, the row-filter and mask functions |
| `pipeline` | [`pipelines/01_bronze.sql`](../databricks/pipelines/01_bronze.sql), [`pipelines/02_silver.sql`](../databricks/pipelines/02_silver.sql) | Bronze streaming tables, silver materialized views with expectations and quarantine |
| `gold` | [`gold/00_create_gold_tables.sql`](../databricks/gold/00_create_gold_tables.sql) (**generated** from `contracts/gold/*.yml`), [`gold/01_load_gold.sql`](../databricks/gold/01_load_gold.sql) | Gold tables with comments, PK/FK (RELY), clustering and tags; MERGE loads, dimensions first |
| `security` | [`governance/01_security.sql`](../databricks/governance/01_security.sql) | Row filters on the facts, grants for Genie users |
| `semantic` | [`semantic/02_metric_views.sql`](../databricks/semantic/02_metric_views.sql) (**generated** from `semantic/*.yaml`), [`semantic/03_fn_like_for_like_sales.sql`](../databricks/semantic/03_fn_like_for_like_sales.sql) | The two metric views and the trusted like-for-like function |

The bundle's resources are in [`databricks/resources/`](../databricks/resources/), its environments in
[`databricks/databricks.yml`](../databricks/databricks.yml): `dev` (`freshcart_dev`), `qa` (`freshcart_qa`) and
`prod` (`freshcart`, refreshed nightly at 05:00 UTC), the same catalogs as the Genie bundle.

## One set of SQL for every environment

The SQL never names a catalog. It writes `${catalog}`, and `${as_of_date}` for "today":

* the **pipeline** receives both as parameters from the bundle (`configuration` in `resources/freshcart_pipeline.yml`),
  and Lakeflow substitutes them in its SQL;
* every other task runs [`src/run_sql.py`](../databricks/src/run_sql.py), which splits a file into statements,
  substitutes both values, refuses any placeholder left over, and runs each statement with `spark.sql`.

`as_of_date` is `2026-09-27` by default, because the demo files end the day before. Relative periods such as "last
week" (the flags in `gold.dim_date`) are then the same as in the local run, so the answers match
[doc 7](07-genie-agent.md#benchmarks-the-expected-answers). For live daily files set it to `today`: the SQL then uses
`current_date()`.

## From a laptop or CI instead of the notebook

Once per environment, an admin creates the catalog and uploads the raw files (the catalog needs a metastore privilege
that deployers should not have):

```bash
databricks catalogs create freshcart_dev
databricks schemas create landing freshcart_dev
databricks volumes create freshcart_dev landing raw MANAGED
databricks fs cp -r data/raw dbfs:/Volumes/freshcart_dev/landing/raw
```

Then, from the repository root:

```bash
(cd databricks   && databricks bundle deploy -t dev && databricks bundle run freshcart_refresh -t dev)
(cd genie_bundle && databricks bundle deploy -t dev && databricks bundle run genie_quality_gate -t dev)
```

The groups used below (`fc_all_regions`, `fc_region_<region>`, `fc_crm_admins`, `freshcart-business-users`,
`freshcart-genie-developers`, `freshcart-genie-deployers`) must exist before the first deploy, as **account
groups**: Unity Catalog grants data access only to account groups, not to workspace-local groups. In an
identity-federated workspace, an admin creates them in *Settings* → *Identity and access* → *Groups*; the notebook
creates them through the workspace's identity API. A grant to a group that is not an account group is reported by the
refresh job and skipped, so it never blocks the rest of the build.

## The pipeline

One Lakeflow Declarative Pipeline on serverless compute, with both SQL files as source code.
The bronze tables are **streaming tables** over `read_files`, so a new monthly POS file is picked up on the next update and
never loaded twice. Silver tables are **materialized views** (they use window functions for deduplication and SCD2).
Rows that break a hard rule are dropped by an expectation **and** kept in `silver.quarantine_pos_sales_line` with the reason.

## Security design

- **Row filters on the two facts only**, using the `region` column copied onto each fact row.
- **Dimensions stay unfiltered**, so Genie's entity matching works on store names, categories and brands.
- **No personal data in gold**; the restricted silver table masks card numbers and e-mail addresses. The mask is declared
  in the pipeline (`MASK` in the column list), because a pipeline's tables cannot be altered from outside.
- The filter and mask functions accept account groups and workspace groups (`is_account_group_member` or `is_member`).
- Genie always queries **as the end user**, so the filters apply in every conversation.
- Metric-view **materialisation** is not available on sources with row filters; if you need it, secure a separate aggregate table.

## Deploy the Genie Agent

The agent is the second bundle: [`genie_bundle/`](../genie_bundle/) holds the space (data sources, column
settings, SQL expressions, example queries, general instructions and benchmarks) and a quality-gate job, with targets
`sandbox`, `dev`, `qa` and `prod` that differ only in catalog, title and permissions. Promotion to qa and prod,
version history and rollback are explained in
[`notebooks/Genie_CICD_with_Declarative_Automation_Bundles.ipynb`](../notebooks/Genie_CICD_with_Declarative_Automation_Bundles.ipynb)
and automated by the `genie-*` workflows in `.github/workflows/`.

## Keeping local and Databricks in step

Two Databricks files are **generated** so they cannot drift:

```bash
python -m freshcart.export_databricks     # contracts -> gold DDL, semantic YAML -> metric views
```

`test_databricks_ddl_is_up_to_date` fails if someone edits a contract without regenerating.

Next: [10 · Data dictionary →](10-data-dictionary.md)
