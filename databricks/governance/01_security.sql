-- =====================================================================================
-- Governance: enforce security in the data, never in a Genie instruction.
-- * Row filters on the two FACT tables: regional teams see only their region.
-- * Dimensions stay unfiltered, so Genie can still use entity matching on store names,
--   categories and brands (entity matching is not allowed on row-filtered tables).
-- * The column mask on the restricted loyalty table is declared in the pipeline (02_silver.sql).
-- * Genie users (the group that can run the Genie space) read gold and semantic only.
-- The functions are in 00_functions.sql. Every statement can be re-run.
-- =====================================================================================
ALTER TABLE ${catalog}.gold.fct_sales_line      SET ROW FILTER ${catalog}.governance.rls_region ON (region);
ALTER TABLE ${catalog}.gold.fct_inventory_daily SET ROW FILTER ${catalog}.governance.rls_region ON (region);

GRANT USE CATALOG ON CATALOG ${catalog}          TO `freshcart-business-users`;
GRANT USE SCHEMA  ON SCHEMA  ${catalog}.gold     TO `freshcart-business-users`;
GRANT USE SCHEMA  ON SCHEMA  ${catalog}.semantic TO `freshcart-business-users`;
GRANT SELECT      ON SCHEMA  ${catalog}.gold     TO `freshcart-business-users`;
GRANT SELECT      ON SCHEMA  ${catalog}.semantic TO `freshcart-business-users`;
