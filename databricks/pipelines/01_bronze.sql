-- =====================================================================================
-- Lakeflow Declarative Pipeline · BRONZE
-- Mirrors freshcart/bronze.py: every feed lands as delivered, all columns as strings,
-- with audit columns. Streaming tables + read_files give incremental, exactly-once file
-- ingestion (a file is never loaded twice), like the local _ingestion_log.
-- ${catalog} is a pipeline parameter (configuration), set per environment by the bundle in
-- databricks/resources/freshcart_pipeline.yml: freshcart_dev, freshcart_qa or freshcart.
-- =====================================================================================

CREATE OR REFRESH STREAMING TABLE ${catalog}.bronze.pos_tlog_raw
COMMENT 'Raw POS transaction log exactly as delivered, one file per month. Not for analytics.'
AS SELECT *,
          _metadata.file_path AS _source_file,
          current_timestamp()  AS _ingested_at
FROM STREAM read_files('/Volumes/${catalog}/landing/raw/pos/', format => 'csv', header => true,
                       inferColumnTypes => false);

CREATE OR REFRESH STREAMING TABLE ${catalog}.bronze.ecom_order_raw
COMMENT 'Raw online orders, one JSON document per row in the value column. Parsed in silver.'
AS SELECT value,
          _metadata.file_path AS _source_file,
          current_timestamp()  AS _ingested_at
FROM STREAM read_files('/Volumes/${catalog}/landing/raw/ecommerce/', format => 'text');

CREATE OR REFRESH STREAMING TABLE ${catalog}.bronze.inventory_raw
COMMENT 'Raw nightly stock snapshots.'
AS SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM STREAM read_files('/Volumes/${catalog}/landing/raw/inventory/', format => 'csv', header => true,
                       inferColumnTypes => false);

CREATE OR REFRESH STREAMING TABLE ${catalog}.bronze.store_master_raw
COMMENT 'Raw monthly store master snapshots. The snapshot date is in the file name.'
AS SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM STREAM read_files('/Volumes/${catalog}/landing/raw/stores/', format => 'csv', header => true,
                       inferColumnTypes => false, pathGlobFilter => 'store_master_*.csv');

CREATE OR REFRESH STREAMING TABLE ${catalog}.bronze.crm_member_raw
COMMENT 'Raw loyalty CRM extracts. Contains personal data: restricted schema permissions.'
AS SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM STREAM read_files('/Volumes/${catalog}/landing/raw/crm/', format => 'csv', header => true,
                       inferColumnTypes => false);

-- Small reference files: re-read in full on every update (materialized views)
CREATE OR REFRESH MATERIALIZED VIEW ${catalog}.bronze.product_master_raw AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/${catalog}/landing/raw/erp/product_master.csv', format => 'csv', header => true, inferColumnTypes => false);

CREATE OR REFRESH MATERIALIZED VIEW ${catalog}.bronze.merch_hierarchy_raw AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/${catalog}/landing/raw/erp/merch_hierarchy.csv', format => 'csv', header => true, inferColumnTypes => false);

CREATE OR REFRESH MATERIALIZED VIEW ${catalog}.bronze.cost_history_raw AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/${catalog}/landing/raw/erp/cost_history.csv', format => 'csv', header => true, inferColumnTypes => false);

CREATE OR REFRESH MATERIALIZED VIEW ${catalog}.bronze.ref_region_raw AS
SELECT * FROM read_files('/Volumes/${catalog}/landing/raw/stores/ref_region.csv', format => 'csv', header => true, inferColumnTypes => false);

CREATE OR REFRESH MATERIALIZED VIEW ${catalog}.bronze.ref_store_format_raw AS
SELECT * FROM read_files('/Volumes/${catalog}/landing/raw/stores/ref_store_format.csv', format => 'csv', header => true, inferColumnTypes => false);

CREATE OR REFRESH MATERIALIZED VIEW ${catalog}.bronze.ref_state_province_raw AS
SELECT * FROM read_files('/Volumes/${catalog}/landing/raw/stores/ref_state_province.csv', format => 'csv', header => true, inferColumnTypes => false);

CREATE OR REFRESH MATERIALIZED VIEW ${catalog}.bronze.promo_calendar_raw AS
SELECT *, _metadata.file_path AS _source_file FROM read_files('/Volumes/${catalog}/landing/raw/promotions/promo_calendar.csv', format => 'csv', header => true, inferColumnTypes => false);

CREATE OR REFRESH MATERIALIZED VIEW ${catalog}.bronze.promo_item_raw AS
SELECT * FROM read_files('/Volumes/${catalog}/landing/raw/promotions/promo_items.csv', format => 'csv', header => true, inferColumnTypes => false);

CREATE OR REFRESH MATERIALIZED VIEW ${catalog}.bronze.fiscal_calendar_raw AS
SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingested_at
FROM read_files('/Volumes/${catalog}/landing/raw/finance/fiscal_calendar.csv', format => 'csv', header => true, inferColumnTypes => false);

CREATE OR REFRESH MATERIALIZED VIEW ${catalog}.bronze.fx_rate_raw AS
SELECT * FROM read_files('/Volumes/${catalog}/landing/raw/finance/fx_rates.csv', format => 'csv', header => true, inferColumnTypes => false);
