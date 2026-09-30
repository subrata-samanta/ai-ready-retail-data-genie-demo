-- =====================================================================================
-- Governance: enforce security in the data, never in a Genie instruction.
-- * Row filters on the two FACT tables: regional teams see only their region.
-- * Dimensions stay unfiltered, so Genie can still use entity matching on store names,
--   categories and brands (entity matching is not allowed on row-filtered tables).
-- * Column mask on the restricted loyalty table.
-- Groups used: fc_all_regions, fc_region_<region>, fc_crm_admins, fc_genie_users
-- =====================================================================================
CREATE OR REPLACE FUNCTION freshcart.governance.rls_region(region STRING)
RETURNS BOOLEAN
COMMENT 'Row filter: head office sees all regions, regional groups see their own region.'
RETURN is_account_group_member('fc_all_regions')
    OR is_account_group_member(concat('fc_region_', lower(replace(region, ' ', '_'))));

ALTER TABLE freshcart.gold.fct_sales_line      SET ROW FILTER freshcart.governance.rls_region ON (region);
ALTER TABLE freshcart.gold.fct_inventory_daily SET ROW FILTER freshcart.governance.rls_region ON (region);

CREATE OR REPLACE FUNCTION freshcart.governance.mask_pii(value STRING)
RETURNS STRING
COMMENT 'Column mask: only CRM administrators see personal data.'
RETURN CASE WHEN is_account_group_member('fc_crm_admins') THEN value ELSE '***' END;

-- Note: column masks apply to tables; if the silver loyalty tables are pipeline-managed
-- materialized views, apply the masks in the pipeline definition or copy to a managed table.
ALTER TABLE freshcart.silver.loyalty_member_restricted ALTER COLUMN email       SET MASK freshcart.governance.mask_pii;
ALTER TABLE freshcart.silver.loyalty_member_restricted ALTER COLUMN card_number SET MASK freshcart.governance.mask_pii;

-- Genie users read gold and semantic only
GRANT USE CATALOG ON CATALOG freshcart          TO `fc_genie_users`;
GRANT USE SCHEMA  ON SCHEMA  freshcart.gold     TO `fc_genie_users`;
GRANT USE SCHEMA  ON SCHEMA  freshcart.semantic TO `fc_genie_users`;
GRANT SELECT      ON SCHEMA  freshcart.gold     TO `fc_genie_users`;
GRANT SELECT      ON SCHEMA  freshcart.semantic TO `fc_genie_users`;
