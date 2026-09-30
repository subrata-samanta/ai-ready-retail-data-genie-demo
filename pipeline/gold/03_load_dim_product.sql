-- =====================================================================================
-- GOLD 03 · dim_product
-- Built from : silver.product (already one row per SKU with decoded hierarchy)
-- Grain      : one row per product, plus UNKNOWN
-- Load       : upsert every run
-- Steps      : 1. keep only business columns (drop material_group_code, source_sales_org)
--              2. derive is_private_label from the brand
--              3. add the UNKNOWN member for items sold but missing from the master
-- =====================================================================================
INSERT INTO gold.dim_product (
  product_id, product_name, brand, department, category, subcategory, unit_of_measure,
  is_private_label, product_status)
SELECT
  product_id, product_name, brand, department, category, subcategory, unit_of_measure,   -- step 1
  CASE WHEN brand IN ('FreshCart', 'FreshCart Select') THEN 1 ELSE 0 END,              -- step 2
  product_status
FROM silver.product
WHERE 1
ON CONFLICT (product_id) DO UPDATE SET
  product_name = excluded.product_name, brand = excluded.brand, department = excluded.department,
  category = excluded.category, subcategory = excluded.subcategory,
  unit_of_measure = excluded.unit_of_measure, is_private_label = excluded.is_private_label,
  product_status = excluded.product_status;

INSERT INTO gold.dim_product (product_id, product_name, brand, department, category, subcategory,  -- step 3
                              unit_of_measure, is_private_label, product_status)
VALUES ('UNKNOWN', 'Unknown product', NULL, 'Unknown', 'Unknown', 'Unknown', 'Each', 0, 'Unknown')
ON CONFLICT (product_id) DO NOTHING;
