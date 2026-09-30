-- =====================================================================================
-- SILVER 08 · online_sales_line
-- Source : bronze.ecom_order_raw   (one JSON document per order, lines nested inside)
-- Grain  : one row per order line - the SAME columns as silver.pos_sales_line,
--          so gold can simply stack the two channels
-- Steps  : 1. FLATTEN   explode the nested "lines" array: one order -> many rows
--          2. BUSINESS DATE from the LOCAL timestamp. '2026-09-01T23:25:38-04:00' is a
--             1 September sale; converting to UTC first would move it to 2 September.
--          3. DECODE    status CANCELLED -> line_type 'Cancelled' (excluded in gold)
--          4. PSEUDONYMISE and CONVERT exactly like POS
-- =====================================================================================
DROP TABLE IF EXISTS silver.online_sales_line;
CREATE TABLE silver.online_sales_line AS
SELECT * FROM silver.pos_sales_line WHERE 0;          -- identical schema to the POS table

INSERT INTO silver.online_sales_line
SELECT
  'WEB-' || json_extract(o.value, '$.orderId') || '-' || json_extract(l.value, '$.lineNo'),
  json_extract(o.value, '$.orderId'),
  json_extract(l.value, '$.lineNo'),
  NULL,
  ltrim(json_extract(o.value, '$.fulfilmentStoreId'), '0'),
  substr(json_extract(o.value, '$.orderTs'), 1, 10),                                           -- 2
  replace(substr(json_extract(o.value, '$.orderTs'), 1, 19), 'T', ' '),
  ltrim(json_extract(l.value, '$.sku'), '0'),
  CASE WHEN json_extract(o.value, '$.loyaltyId') IS NULL THEN 'ANONYMOUS'
       ELSE sha2(CAST(json_extract(o.value, '$.loyaltyId') AS TEXT), 256) END,                 -- 4
  json_extract(l.value, '$.promoCode'),
  'Online',
  CASE json_extract(o.value, '$.status') WHEN 'CANCELLED' THEN 'Cancelled' ELSE 'Sale' END,  -- 3
  0,
  json_extract(l.value, '$.qty'),
  upper(json_extract(o.value, '$.currency')),
  json_extract(l.value, '$.unitPrice'),
  ROUND(json_extract(l.value, '$.lineTotal'), 2),
  ROUND(json_extract(l.value, '$.discount'), 2),
  fx.rate_to_usd,
  ROUND(json_extract(l.value, '$.lineTotal') * fx.rate_to_usd, 2),
  ROUND(json_extract(l.value, '$.discount')  * fx.rate_to_usd, 2),
  o._source_file,
  o._ingested_at
FROM bronze.ecom_order_raw o,
     json_each(o.value, '$.lines') l                                                           -- 1
JOIN silver.fx_rate_daily fx
  ON fx.rate_date = substr(json_extract(o.value, '$.orderTs'), 1, 10)
 AND fx.from_currency = upper(json_extract(o.value, '$.currency'));
