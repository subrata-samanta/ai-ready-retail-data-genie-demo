-- =====================================================================================
-- Lakeflow Declarative Pipeline · SILVER
-- Same logic, step for step, as pipeline/silver/*.sql (the tested local version).
-- Differences are only dialect: TRIM(LEADING '0' FROM x) instead of ltrim(x, '0'),
-- greatest()/least() instead of scalar MAX()/MIN(), JSON path syntax, window last_value.
-- Hard rules use expectations; the quarantine table keeps the rejected rows with a reason.
-- Materialized views are used so window-based deduplication is allowed; serverless
-- pipelines refresh them incrementally where possible.
-- =====================================================================================

-- 01 · fiscal_calendar ------------------------------------------------------------------
CREATE OR REFRESH MATERIALIZED VIEW freshcart.silver.fiscal_calendar (
  CONSTRAINT valid_date EXPECT (calendar_date IS NOT NULL) ON VIOLATION FAIL UPDATE
)
COMMENT 'Finance 4-5-4 fiscal calendar, typed. One row per calendar day.'
AS
WITH latest AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY CAL_DT ORDER BY _ingested_at DESC) AS rn
  FROM freshcart.bronze.fiscal_calendar_raw
),
typed AS (
  SELECT try_to_date(CAL_DT, 'dd/MM/yyyy')        AS calendar_date,
         CAST(substr(FISC_YR, 3) AS INT)          AS fiscal_year,
         CAST(substr(FISC_QTR, 2) AS INT)         AS fiscal_quarter,
         CAST(substr(FISC_PRD, 2) AS INT)         AS fiscal_period,
         concat(FISC_PRD, ' ', FISC_PRD_NM)       AS fiscal_period_name,
         CAST(substr(FISC_WK, 2) AS INT)          AS fiscal_week,
         try_to_date(FISC_WK_START, 'dd/MM/yyyy') AS fiscal_week_start_date
  FROM latest WHERE rn = 1
)
SELECT *,
       datediff(calendar_date, MIN(calendar_date) OVER (PARTITION BY fiscal_year)) + 1 AS fiscal_day_of_year,
       date_format(calendar_date, 'EEEE') AS day_of_week_name
FROM typed;

-- 02 · fx_rate_daily --------------------------------------------------------------------
CREATE OR REFRESH MATERIALIZED VIEW freshcart.silver.fx_rate_daily
COMMENT 'Daily CAD and USD to USD rates. Weekends and holidays carry the last published rate.'
AS
WITH published AS (
  SELECT try_to_date(RATE_DT, 'yyyy-MM-dd') AS rate_date, upper(FROM_CCY) AS from_currency,
         CAST(RATE AS DOUBLE) AS rate
  FROM freshcart.bronze.fx_rate_raw WHERE upper(TO_CCY) = 'USD'
),
days AS (
  SELECT calendar_date FROM freshcart.silver.fiscal_calendar
  WHERE calendar_date BETWEEN (SELECT MIN(rate_date) FROM published) AND current_date()
)
SELECT d.calendar_date AS rate_date,
       'CAD' AS from_currency,
       last_value(p.rate, true) OVER (ORDER BY d.calendar_date
                                      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS rate_to_usd,
       CASE WHEN p.rate IS NULL THEN 1 ELSE 0 END AS is_carried_forward
FROM days d LEFT JOIN published p ON p.rate_date = d.calendar_date AND p.from_currency = 'CAD'
UNION ALL
SELECT calendar_date, 'USD', 1.0, 0 FROM days;

-- 03 · store_history (SCD2 from snapshots) ----------------------------------------------
CREATE OR REFRESH MATERIALIZED VIEW freshcart.silver.store_history
COMMENT 'Store master history, one row per store per version of its attributes (SCD type 2).'
AS
WITH snap AS (
  SELECT TRIM(LEADING '0' FROM s.STR_NBR)                         AS store_id,
         s.STR_NM AS store_name, f.FMT_NM AS store_format, s.CITY AS city, st.ST_NM AS state_province,
         CASE s.CNTRY WHEN 'US' THEN 'United States' WHEN 'CA' THEN 'Canada' END AS country,
         r.RGN_NM                                                 AS region,
         try_to_date(s.OPEN_DT, 'MM/dd/yyyy')                     AS open_date,
         try_to_date(NULLIF(s.CLOSE_DT, ''), 'MM/dd/yyyy')        AS close_date,
         CAST(s.SQFT AS INT)                                      AS selling_area_sqft,
         try_to_date(substr(s._source_file, -12, 8), 'yyyyMMdd')  AS snapshot_date
  FROM freshcart.bronze.store_master_raw s
  LEFT JOIN freshcart.bronze.ref_store_format_raw   f  ON f.FMT_CD = s.FMT_CD
  LEFT JOIN freshcart.bronze.ref_region_raw         r  ON r.RGN_CD = s.RGN_CD
  LEFT JOIN freshcart.bronze.ref_state_province_raw st ON st.ST_CD = s.ST_CD
),
changes AS (
  SELECT *, sha2(concat_ws('|', store_name, store_format, city, state_province, region,
                           open_date, close_date, selling_area_sqft), 256) AS attr_hash
  FROM snap
),
versions AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY store_id ORDER BY snapshot_date) AS version_no
  FROM (SELECT *, LAG(attr_hash) OVER (PARTITION BY store_id ORDER BY snapshot_date) AS prev_hash FROM changes)
  WHERE prev_hash IS NULL OR prev_hash <> attr_hash
),
ranged AS (
  SELECT *, CASE WHEN version_no = 1 THEN least(open_date, snapshot_date) ELSE snapshot_date END AS valid_from
  FROM versions
)
SELECT store_id, store_name, store_format, city, state_province, country, region, open_date, close_date,
       selling_area_sqft, valid_from,
       COALESCE(date_sub(LEAD(valid_from) OVER (PARTITION BY store_id ORDER BY valid_from), 1),
                DATE'9999-12-31') AS valid_to,
       LEAD(valid_from) OVER (PARTITION BY store_id ORDER BY valid_from) IS NULL AS is_current
FROM ranged;

-- 04 · product and product_cost ---------------------------------------------------------
CREATE OR REFRESH MATERIALIZED VIEW freshcart.silver.product (
  CONSTRAINT has_hierarchy EXPECT (department IS NOT NULL)
)
COMMENT 'One row per SKU: US01 record preferred, CA01 for Canada-only items. Hierarchy decoded.'
AS
WITH ranked AS (
  SELECT p.*, TRIM(LEADING '0' FROM p.MATNR) AS product_id,
         ROW_NUMBER() OVER (PARTITION BY TRIM(LEADING '0' FROM p.MATNR)
                            ORDER BY CASE p.VKORG WHEN 'US01' THEN 1 ELSE 2 END, p._ingested_at DESC) AS rn
  FROM freshcart.bronze.product_master_raw p
)
SELECT r.product_id,
       CASE WHEN r.VKORG = 'US01' THEN r.MAKTX ELSE initcap(r.MAKTX) END AS product_name,
       r.BRAND_NM AS brand, h.DEPT_NM AS department, h.CAT_NM AS category, h.SUBCAT_NM AS subcategory,
       CASE r.MEINS WHEN 'EA' THEN 'Each' WHEN 'KG' THEN 'Kilogram' ELSE r.MEINS END AS unit_of_measure,
       CASE r.STATUS WHEN 'A' THEN 'Active' WHEN 'D' THEN 'Discontinued' ELSE 'Unknown' END AS product_status,
       r.MATKL AS material_group_code, r.VKORG AS source_sales_org
FROM ranked r LEFT JOIN freshcart.bronze.merch_hierarchy_raw h ON h.MATKL = r.MATKL
WHERE r.rn = 1;

CREATE OR REFRESH MATERIALIZED VIEW freshcart.silver.product_cost
COMMENT 'Standard cost per product with validity ranges.'
AS
SELECT TRIM(LEADING '0' FROM MATNR) AS product_id,
       try_to_date(COST_EFF_DT, 'yyyyMMdd') AS valid_from,
       COALESCE(date_sub(LEAD(try_to_date(COST_EFF_DT, 'yyyyMMdd'))
                  OVER (PARTITION BY TRIM(LEADING '0' FROM MATNR) ORDER BY COST_EFF_DT), 1), DATE'9999-12-31') AS valid_to,
       CAST(STD_COST AS DECIMAL(18,4)) AS unit_cost_usd
FROM freshcart.bronze.cost_history_raw;

-- 05 · promotion ------------------------------------------------------------------------
CREATE OR REFRESH MATERIALIZED VIEW freshcart.silver.promotion
COMMENT 'Promotions with decoded mechanics.'
AS
SELECT PROMO_CD AS promotion_id, PROMO_DESC AS promotion_name,
       CASE MECH_CD WHEN 'BOGO' THEN 'Buy One Get One' WHEN 'PCT' THEN 'Percent Off'
                    WHEN 'MULTI' THEN 'Multi-buy' WHEN 'LOY' THEN 'Loyalty Price' ELSE 'Other' END AS promotion_mechanic,
       try_to_date(START_DT, 'yyyy-MM-dd') AS start_date,
       try_to_date(END_DT, 'yyyy-MM-dd')   AS end_date,
       CAST(NULLIF(DISC_PCT, '') AS DOUBLE) AS discount_pct
FROM freshcart.bronze.promo_calendar_raw;

-- 06 · loyalty_member (pseudonymous) and loyalty_member_restricted (personal data) -------
CREATE OR REFRESH MATERIALIZED VIEW freshcart.silver.loyalty_member
COMMENT 'Loyalty members without personal data: pseudonymous id, tier, age band, home store.'
AS
WITH latest AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY CARD_NBR ORDER BY _source_file DESC) AS rn
  FROM freshcart.bronze.crm_member_raw
)
SELECT sha2(CARD_NBR, 256) AS customer_id,
       CASE TIER WHEN 'G' THEN 'Gold' WHEN 'S' THEN 'Silver' WHEN 'B' THEN 'Bronze' ELSE 'Unknown' END AS loyalty_tier,
       try_to_date(ENROLL_DT, 'yyyy-MM-dd') AS enrollment_date,
       CASE WHEN age < 18 THEN 'Under 18' WHEN age < 25 THEN '18-24' WHEN age < 35 THEN '25-34'
            WHEN age < 45 THEN '35-44' WHEN age < 55 THEN '45-54' WHEN age < 65 THEN '55-64' ELSE '65+' END AS age_band,
       TRIM(LEADING '0' FROM HOME_STR) AS home_store_id
FROM (SELECT *, floor(months_between(current_date(), try_to_date(DOB, 'yyyy-MM-dd')) / 12) AS age
      FROM latest WHERE rn = 1);

CREATE OR REFRESH MATERIALIZED VIEW freshcart.silver.loyalty_member_restricted
COMMENT 'RESTRICTED: loyalty personal data. Column masks applied in governance; never used in gold.'
AS
SELECT sha2(CARD_NBR, 256) AS customer_id, CARD_NBR AS card_number, EMAIL AS email,
       try_to_date(DOB, 'yyyy-MM-dd') AS date_of_birth
FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY CARD_NBR ORDER BY _source_file DESC) AS rn
      FROM freshcart.bronze.crm_member_raw)
WHERE rn = 1;

-- 07 · pos_sales_line (+ quarantine) ----------------------------------------------------
CREATE OR REFRESH MATERIALIZED VIEW freshcart.silver._pos_checked
COMMENT 'Internal: typed, deduplicated POS lines with a failure_reason for rows that break a rule.'
AS
WITH typed AS (
  SELECT NULLIF(TRIM(LEADING '0' FROM STR_NBR), '')                 AS store_id,
         TRX_ID AS transaction_id, CAST(LN_NBR AS INT) AS line_number, REG_NBR AS register_number,
         try_to_date(TRX_DT, 'yyyyMMdd')                            AS sales_date,
         lpad(TRX_TM, 6, '0')                                       AS hhmmss,
         NULLIF(TRIM(LEADING '0' FROM ITM_ID), '')                  AS product_id,
         CASE TRX_TYP WHEN 'S' THEN 'Sale' WHEN 'R' THEN 'Return' WHEN 'V' THEN 'Void' END AS line_type,
         REG_NBR = '99'                                             AS is_test_transaction,
         CASE WHEN TRX_TYP = 'R' THEN -1 ELSE 1 END                 AS sign,
         CAST(QTY AS DECIMAL(12,3))      AS qty_abs,
         CAST(UNIT_PRC AS DECIMAL(18,2)) AS unit_price_local,
         CAST(EXT_AMT AS DECIMAL(18,2))  AS gross_abs,
         CAST(DISC_AMT AS DECIMAL(18,2)) AS discount_abs,
         NULLIF(TRIM(LYL_CARD_NBR), '')  AS card,
         NULLIF(TRIM(PROMO_CD), '')      AS promotion_code,
         upper(CRNCY_CD)                 AS currency_code,
         TRX_DT AS raw_trx_dt, STR_NBR AS raw_str_nbr, QTY AS raw_qty, _source_file, _ingested_at,
         ROW_NUMBER() OVER (PARTITION BY STR_NBR, REG_NBR, TRX_ID, LN_NBR, TRX_DT
                            ORDER BY _source_file, _ingested_at) AS copy_no
  FROM freshcart.bronze.pos_tlog_raw
)
SELECT t.*, fx.rate_to_usd AS fx_rate_to_usd,
       CASE WHEN t.store_id IS NULL     THEN 'missing store number'
            WHEN t.sales_date IS NULL   THEN concat('invalid transaction date: ', t.raw_trx_dt)
            WHEN t.line_type IS NULL    THEN 'unknown transaction type'
            WHEN t.product_id IS NULL   THEN 'missing item id'
            WHEN abs(t.qty_abs) > 500   THEN concat('implausible quantity: ', t.raw_qty)
            WHEN fx.rate_to_usd IS NULL THEN concat('no FX rate for ', t.currency_code) END AS failure_reason
FROM typed t
LEFT JOIN freshcart.silver.fx_rate_daily fx ON fx.rate_date = t.sales_date AND fx.from_currency = t.currency_code
WHERE t.copy_no = 1;

CREATE OR REFRESH MATERIALIZED VIEW freshcart.silver.quarantine_pos_sales_line
COMMENT 'POS rows rejected by a hard rule, with the reason. Review and fix at source.'
AS SELECT failure_reason, raw_str_nbr, transaction_id, line_number, raw_trx_dt, product_id, raw_qty,
          _source_file, _ingested_at
FROM freshcart.silver._pos_checked WHERE failure_reason IS NOT NULL;

CREATE OR REFRESH MATERIALIZED VIEW freshcart.silver.pos_sales_line (
  CONSTRAINT passes_rules EXPECT (failure_reason IS NULL) ON VIOLATION DROP ROW
)
COMMENT 'Cleansed POS lines incl. voids and training lines (flagged). Amounts local and USD, returns negative.'
AS
SELECT concat('POS-', store_id, '-', transaction_id, '-', line_number) AS sales_line_id,
       transaction_id, line_number, register_number, store_id, sales_date,
       to_timestamp(concat(sales_date, ' ', hhmmss), 'yyyy-MM-dd HHmmss') AS sales_ts,
       product_id,
       CASE WHEN card IS NULL THEN 'ANONYMOUS' ELSE sha2(card, 256) END AS customer_id,
       promotion_code, 'In-store' AS sales_channel, line_type, is_test_transaction,
       sign * qty_abs AS quantity_units, currency_code, unit_price_local,
       sign * gross_abs AS gross_sales_amount_local, sign * discount_abs AS discount_amount_local,
       fx_rate_to_usd,
       round(sign * gross_abs * fx_rate_to_usd, 2)    AS gross_sales_amount_usd,
       round(sign * discount_abs * fx_rate_to_usd, 2) AS discount_amount_usd,
       failure_reason, _source_file, _ingested_at
FROM freshcart.silver._pos_checked;

-- 08 · online_sales_line ----------------------------------------------------------------
CREATE OR REFRESH MATERIALIZED VIEW freshcart.silver.online_sales_line
COMMENT 'Online order lines flattened from JSON; business date from the local timestamp.'
AS
WITH orders AS (
  SELECT value:orderId::string            AS order_id,
         value:orderTs::string            AS order_ts,
         value:fulfilmentStoreId::string  AS store_nbr,
         value:loyaltyId::string          AS card,
         upper(value:currency::string)    AS currency_code,
         value:status::string             AS status,
         explode(from_json(value:lines,
           'ARRAY<STRUCT<lineNo: INT, sku: STRING, qty: DOUBLE, unitPrice: DOUBLE, lineTotal: DOUBLE, discount: DOUBLE, promoCode: STRING>>')) AS l,
         _source_file, _ingested_at
  FROM freshcart.bronze.ecom_order_raw
)
SELECT concat('WEB-', o.order_id, '-', o.l.lineNo) AS sales_line_id,
       o.order_id AS transaction_id, o.l.lineNo AS line_number, CAST(NULL AS STRING) AS register_number,
       TRIM(LEADING '0' FROM o.store_nbr) AS store_id,
       CAST(substr(o.order_ts, 1, 10) AS DATE)          AS sales_date,   -- local business date, not UTC
       to_timestamp(substr(o.order_ts, 1, 19))           AS sales_ts,
       TRIM(LEADING '0' FROM o.l.sku) AS product_id,
       CASE WHEN o.card IS NULL THEN 'ANONYMOUS' ELSE sha2(o.card, 256) END AS customer_id,
       o.l.promoCode AS promotion_code, 'Online' AS sales_channel,
       CASE o.status WHEN 'CANCELLED' THEN 'Cancelled' ELSE 'Sale' END AS line_type,
       false AS is_test_transaction,
       CAST(o.l.qty AS DECIMAL(12,3)) AS quantity_units, o.currency_code,
       CAST(o.l.unitPrice AS DECIMAL(18,2)) AS unit_price_local,
       CAST(o.l.lineTotal AS DECIMAL(18,2)) AS gross_sales_amount_local,
       CAST(o.l.discount  AS DECIMAL(18,2)) AS discount_amount_local,
       fx.rate_to_usd AS fx_rate_to_usd,
       round(o.l.lineTotal * fx.rate_to_usd, 2) AS gross_sales_amount_usd,
       round(o.l.discount  * fx.rate_to_usd, 2) AS discount_amount_usd,
       CAST(NULL AS STRING) AS failure_reason, o._source_file, o._ingested_at
FROM orders o
JOIN freshcart.silver.fx_rate_daily fx
  ON fx.rate_date = CAST(substr(o.order_ts, 1, 10) AS DATE) AND fx.from_currency = o.currency_code;

-- 09 · inventory_daily ------------------------------------------------------------------
CREATE OR REFRESH MATERIALIZED VIEW freshcart.silver.inventory_daily
COMMENT 'Daily stock per store and product. Negative readings clamped to 0 and flagged.'
AS
SELECT snapshot_date, store_id, product_id,
       greatest(0, raw_on_hand) AS on_hand_units, on_order_units,
       raw_on_hand < 0 AS had_negative_stock, _ingested_at
FROM (
  SELECT try_to_date(SNAP_DT, 'yyyyMMdd') AS snapshot_date,
         TRIM(LEADING '0' FROM STR_NBR) AS store_id, TRIM(LEADING '0' FROM ITM_ID) AS product_id,
         CAST(OH_QTY AS DECIMAL(12,3)) AS raw_on_hand, CAST(ON_ORD_QTY AS DECIMAL(12,3)) AS on_order_units,
         _ingested_at,
         ROW_NUMBER() OVER (PARTITION BY SNAP_DT, STR_NBR, ITM_ID ORDER BY _ingested_at DESC) AS rn
  FROM freshcart.bronze.inventory_raw
)
WHERE rn = 1 AND snapshot_date IS NOT NULL;
