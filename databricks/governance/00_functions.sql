-- =====================================================================================
-- Governance functions. Created before the pipeline runs, because the pipeline declares the
-- column mask on silver.loyalty_member_restricted (pipelines/02_silver.sql).
-- Groups: fc_all_regions (sees every region), fc_region_<region> (one region), fc_crm_admins
-- (sees personal data). Account groups are the recommended kind; is_member() also accepts
-- workspace-local groups, which is what a new workspace without account-admin access can create.
-- =====================================================================================
CREATE OR REPLACE FUNCTION ${catalog}.governance.rls_region(region STRING)
RETURNS BOOLEAN
COMMENT 'Row filter: head office sees all regions, regional groups see their own region.'
RETURN is_account_group_member('fc_all_regions') OR is_member('fc_all_regions')
    OR is_account_group_member(concat('fc_region_', lower(replace(region, ' ', '_'))))
    OR is_member(concat('fc_region_', lower(replace(region, ' ', '_'))));

CREATE OR REPLACE FUNCTION ${catalog}.governance.mask_pii(value STRING)
RETURNS STRING
COMMENT 'Column mask: only CRM administrators see personal data.'
RETURN CASE WHEN is_account_group_member('fc_crm_admins') OR is_member('fc_crm_admins') THEN value ELSE '***' END;
