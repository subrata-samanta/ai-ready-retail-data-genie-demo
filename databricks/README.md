# Databricks version

The same design as the local demo, for Unity Catalog. Run order, commands and design notes:
**[docs/09-run-on-databricks.md](../docs/09-run-on-databricks.md)**.

| Folder | Contents |
|---|---|
| `00_setup.sql` | catalog, schemas, landing volume |
| `pipelines/` | Lakeflow Declarative Pipeline: bronze streaming tables, silver materialized views with expectations |
| `gold/` | `00_create_gold_tables.sql` (generated from `contracts/gold/`), `01_load_gold.sql` (MERGE loads) |
| `governance/` | row filters, column masks, grants |
| `semantic/` | `02_metric_views.sql` (generated from `semantic/*.yaml`), like-for-like trusted function |

The local SQLite pipeline in `pipeline/` is the tested reference. This SQL mirrors it step for step and follows the
Databricks documentation as of September 2026, but is not executed by this repo's tests.
