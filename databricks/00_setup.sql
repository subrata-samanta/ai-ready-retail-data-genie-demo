-- =====================================================================================
-- 00 · One-time setup in Unity Catalog
-- Creates the catalog, one schema per layer, and a volume that acts as the landing zone.
-- Then upload data/raw/* to /Volumes/freshcart/landing/raw/ (see databricks/README.md).
-- =====================================================================================
CREATE CATALOG IF NOT EXISTS freshcart COMMENT 'FreshCart demo: AI-ready retail data for Genie';

CREATE SCHEMA IF NOT EXISTS freshcart.landing   COMMENT 'Raw files exactly as delivered by source systems';
CREATE SCHEMA IF NOT EXISTS freshcart.bronze    COMMENT 'Raw tables: one per feed, all columns as strings, plus audit columns';
CREATE SCHEMA IF NOT EXISTS freshcart.silver    COMMENT 'Cleaned, typed, decoded and conformed tables';
CREATE SCHEMA IF NOT EXISTS freshcart.gold      COMMENT 'Business star schema: facts and conformed dimensions';
CREATE SCHEMA IF NOT EXISTS freshcart.semantic  COMMENT 'Metric views and trusted functions used by Genie';
CREATE SCHEMA IF NOT EXISTS freshcart.governance COMMENT 'Row filter and column mask functions';

CREATE VOLUME IF NOT EXISTS freshcart.landing.raw COMMENT 'Landing zone for source files';
