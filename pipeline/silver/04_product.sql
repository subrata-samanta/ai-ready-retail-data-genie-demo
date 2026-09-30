-- =====================================================================================
-- SILVER 04 · product  and  product_cost
-- Source : bronze.product_master_raw  (one row per SKU PER SALES ORG: US01 and CA01)
--          bronze.merch_hierarchy_raw (material group code -> names)
--          bronze.cost_history_raw    (standard cost with effective dates)
-- Grain  : product      -> one row per SKU
--          product_cost -> one row per SKU per cost version
-- Steps  : 1. normalise the SKU (no leading zeros) so POS, online and ERP ids match
--          2. DEDUPLICATE: prefer the US01 record, fall back to CA01 for Canada-only items.
--             Without this, every sale joins to two product rows and sales double.
--          3. decode MATKL 'C0412' -> Grocery / Snacks / Chips and Crisps
--          4. decode unit and status codes; fix the upper-case CA01 descriptions
--          5. turn cost effective dates into valid_from / valid_to ranges
-- =====================================================================================
DROP TABLE IF EXISTS silver.product;
CREATE TABLE silver.product (
  product_id          TEXT PRIMARY KEY,
  product_name        TEXT NOT NULL,
  brand               TEXT,
  department          TEXT NOT NULL,
  category            TEXT NOT NULL,
  subcategory         TEXT NOT NULL,
  unit_of_measure     TEXT NOT NULL,
  product_status      TEXT NOT NULL,
  material_group_code TEXT NOT NULL,
  source_sales_org    TEXT NOT NULL
);

INSERT INTO silver.product
WITH ranked AS (
  SELECT p.*,
         ltrim(p.MATNR, '0') AS product_id,                                             -- step 1
         ROW_NUMBER() OVER (PARTITION BY ltrim(p.MATNR, '0')
                            ORDER BY CASE p.VKORG WHEN 'US01' THEN 1 ELSE 2 END,
                                     p._ingested_at DESC) AS rn                          -- step 2
  FROM bronze.product_master_raw p
)
SELECT
  r.product_id,
  CASE WHEN r.VKORG = 'US01' THEN r.MAKTX ELSE initcap(r.MAKTX) END AS product_name,    -- step 4
  r.BRAND_NM,
  h.DEPT_NM, h.CAT_NM, h.SUBCAT_NM,                                                      -- step 3
  CASE r.MEINS WHEN 'EA' THEN 'Each' WHEN 'KG' THEN 'Kilogram' ELSE r.MEINS END,
  CASE r.STATUS WHEN 'A' THEN 'Active' WHEN 'D' THEN 'Discontinued' ELSE 'Unknown' END,
  r.MATKL,
  r.VKORG
FROM ranked r
LEFT JOIN bronze.merch_hierarchy_raw h ON h.MATKL = r.MATKL
WHERE r.rn = 1;

DROP TABLE IF EXISTS silver.product_cost;
CREATE TABLE silver.product_cost (
  product_id    TEXT NOT NULL,
  valid_from    TEXT NOT NULL,
  valid_to      TEXT NOT NULL,
  unit_cost_usd REAL NOT NULL,
  PRIMARY KEY (product_id, valid_from)
);

INSERT INTO silver.product_cost                                                          -- step 5
SELECT
  ltrim(MATNR, '0'),
  try_to_date(COST_EFF_DT, 'yyyyMMdd'),
  COALESCE(date(LEAD(try_to_date(COST_EFF_DT, 'yyyyMMdd'))
                  OVER (PARTITION BY ltrim(MATNR, '0') ORDER BY COST_EFF_DT), '-1 day'), '9999-12-31'),
  CAST(STD_COST AS REAL)
FROM bronze.cost_history_raw;
