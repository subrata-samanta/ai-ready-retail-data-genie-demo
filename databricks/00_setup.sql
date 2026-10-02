-- =====================================================================================
-- 00 · Schemas and the landing volume (first task of the bundle job freshcart_refresh)
-- ${catalog} is the environment's catalog (freshcart_dev, freshcart_qa or freshcart). The catalog itself
-- is created once by an admin (CREATE CATALOG needs a metastore privilege that deployers should not
-- have): see notebooks/FreshCart_End_to_End_on_Databricks, step 5.
-- Then the raw files go to /Volumes/${catalog}/landing/raw/ (same notebook, step 6).
-- =====================================================================================
CREATE SCHEMA IF NOT EXISTS ${catalog}.landing    COMMENT 'Raw files exactly as delivered by source systems';
CREATE SCHEMA IF NOT EXISTS ${catalog}.bronze     COMMENT 'Raw tables: one per feed, all columns as strings, plus audit columns';
CREATE SCHEMA IF NOT EXISTS ${catalog}.silver     COMMENT 'Cleaned, typed, decoded and conformed tables';
CREATE SCHEMA IF NOT EXISTS ${catalog}.gold       COMMENT 'Business star schema: facts and conformed dimensions';
CREATE SCHEMA IF NOT EXISTS ${catalog}.semantic   COMMENT 'Metric views and trusted functions used by Genie';
CREATE SCHEMA IF NOT EXISTS ${catalog}.governance COMMENT 'Row filter and column mask functions';

CREATE VOLUME IF NOT EXISTS ${catalog}.landing.raw COMMENT 'Landing zone for source files';
