# freshcart-data bundle

The FreshCart data product on Databricks: the Lakeflow pipeline (bronze, silver) and the refresh job (setup,
pipeline, gold, security, semantic layer), as one Declarative Automation Bundle with the environments dev, qa and
prod. Design, run order and commands: **[docs/09-run-on-databricks.md](../docs/09-run-on-databricks.md)**.
From an empty workspace, run [`notebooks/FreshCart_End_to_End_on_Databricks.ipynb`](../notebooks/FreshCart_End_to_End_on_Databricks.ipynb).

```bash
databricks bundle deploy -t dev
databricks bundle run freshcart_refresh -t dev
```

| Path | Contents |
|---|---|
| `databricks.yml` | bundle, variables (`catalog`, `as_of_date`), targets dev / qa / prod |
| `resources/` | the pipeline and the refresh job |
| `src/run_sql.py` | runs a SQL file statement by statement, filling in `${catalog}` and `${as_of_date}` |
| `00_setup.sql` | schemas, landing volume |
| `pipelines/` | bronze streaming tables, silver materialized views with expectations |
| `gold/` | `00_create_gold_tables.sql` (generated from `contracts/gold/`), `01_load_gold.sql` (MERGE loads) |
| `governance/` | row-filter and mask functions, row filters, grants |
| `semantic/` | `02_metric_views.sql` (generated from `semantic/*.yaml`), like-for-like trusted function |
| `monitoring/` | data health checks run after every refresh by `src/monitor.py`; history in `<catalog>.monitoring.health_checks` |

The local SQLite pipeline in `pipeline/` is the tested reference; `tests/test_data_bundle.py` and
`tests/test_databricks_notebook.py` check this bundle offline.
