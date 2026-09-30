-- =====================================================================================
-- SILVER 07 · pos_sales_line  (+ quarantine_pos_sales_line)
-- Source : bronze.pos_tlog_raw   (store POS transaction log, one file per month)
-- Grain  : one row per receipt line, INCLUDING voids and training transactions
--          (silver keeps every valid source row; gold decides what counts as a sale)
-- Steps  : 1. TYPE      string dates/times -> DATE/TIMESTAMP, numbers -> REAL
--          2. NORMALISE store and item ids lose their leading zeros ('01042' -> '1042')
--          3. DECODE    TRX_TYP S/R/V -> Sale/Return/Void; REG_NBR 99 -> test transaction
--          4. SIGN      returns arrive with positive values; make them negative
--          5. PSEUDONYMISE loyalty card -> customer_id = sha2(card), or 'ANONYMOUS'
--          6. CONVERT   local currency -> USD with the daily rate from silver.fx_rate_daily
--          7. DEDUPLICATE the day the store system re-sent (same store/register/receipt/line)
--          8. VALIDATE  rows that break a hard rule go to the quarantine table with a reason
--                       (Databricks: CONSTRAINT ... EXPECT ... ON VIOLATION DROP ROW)
-- =====================================================================================
DROP TABLE IF EXISTS temp._pos_typed;
CREATE TEMP TABLE _pos_typed AS
SELECT
  NULLIF(ltrim(STR_NBR, '0'), '')                                  AS store_id,          -- 2
  TRX_ID                                                           AS transaction_id,
  CAST(LN_NBR AS INTEGER)                                          AS line_number,
  REG_NBR                                                          AS register_number,
  try_to_date(TRX_DT, 'yyyyMMdd')                                  AS sales_date,        -- 1
  printf('%06d', CAST(TRX_TM AS INTEGER))                          AS hhmmss,
  NULLIF(ltrim(ITM_ID, '0'), '')                                   AS product_id,        -- 2
  CASE TRX_TYP WHEN 'S' THEN 'Sale' WHEN 'R' THEN 'Return' WHEN 'V' THEN 'Void' END AS line_type, -- 3
  CASE WHEN REG_NBR = '99' THEN 1 ELSE 0 END                       AS is_test_transaction,
  CASE WHEN TRX_TYP = 'R' THEN -1 ELSE 1 END                       AS sign,              -- 4
  CAST(QTY AS REAL)                                                AS qty_abs,
  CAST(UNIT_PRC AS REAL)                                           AS unit_price_local,
  CAST(EXT_AMT AS REAL)                                            AS gross_abs,
  CAST(DISC_AMT AS REAL)                                           AS discount_abs,
  NULLIF(TRIM(LYL_CARD_NBR), '')                                   AS card,
  NULLIF(TRIM(PROMO_CD), '')                                       AS promotion_code,
  upper(CRNCY_CD)                                                  AS currency_code,
  TRX_DT AS raw_trx_dt, STR_NBR AS raw_str_nbr, QTY AS raw_qty,
  _source_file, _row_number, _ingested_at,
  ROW_NUMBER() OVER (PARTITION BY STR_NBR, REG_NBR, TRX_ID, LN_NBR, TRX_DT
                     ORDER BY _source_file, _row_number)          AS copy_no            -- 7
FROM bronze.pos_tlog_raw;

DROP TABLE IF EXISTS temp._pos_checked;
CREATE TEMP TABLE _pos_checked AS
SELECT t.*,
       fx.rate_to_usd AS fx_rate_to_usd,
       CASE                                                                              -- 8
         WHEN t.store_id IS NULL             THEN 'missing store number'
         WHEN t.sales_date IS NULL           THEN 'invalid transaction date: ' || t.raw_trx_dt
         WHEN t.line_type IS NULL            THEN 'unknown transaction type'
         WHEN t.product_id IS NULL           THEN 'missing item id'
         WHEN ABS(t.qty_abs) > 500           THEN 'implausible quantity: ' || t.raw_qty
         WHEN fx.rate_to_usd IS NULL         THEN 'no FX rate for ' || t.currency_code
       END AS failure_reason
FROM _pos_typed t
LEFT JOIN silver.fx_rate_daily fx                                                        -- 6
       ON fx.rate_date = t.sales_date AND fx.from_currency = t.currency_code
WHERE t.copy_no = 1;

-- Rows that fail a rule: kept, with the reason, so someone can fix the source
DROP TABLE IF EXISTS silver.quarantine_pos_sales_line;
CREATE TABLE silver.quarantine_pos_sales_line AS
SELECT failure_reason, raw_str_nbr, transaction_id, line_number, raw_trx_dt, product_id, raw_qty,
       _source_file, _row_number, _ingested_at
FROM _pos_checked
WHERE failure_reason IS NOT NULL;

DROP TABLE IF EXISTS silver.pos_sales_line;
CREATE TABLE silver.pos_sales_line (
  sales_line_id            TEXT PRIMARY KEY,
  transaction_id           TEXT    NOT NULL,
  line_number              INTEGER NOT NULL,
  register_number          TEXT,
  store_id                 TEXT    NOT NULL,
  sales_date               TEXT    NOT NULL,
  sales_ts                 TEXT    NOT NULL,
  product_id               TEXT    NOT NULL,
  customer_id              TEXT    NOT NULL,
  promotion_code           TEXT,
  sales_channel            TEXT    NOT NULL,
  line_type                TEXT    NOT NULL,
  is_test_transaction      INTEGER NOT NULL,
  quantity_units           REAL    NOT NULL,
  currency_code            TEXT    NOT NULL,
  unit_price_local         REAL,
  gross_sales_amount_local REAL    NOT NULL,
  discount_amount_local    REAL    NOT NULL,
  fx_rate_to_usd           REAL    NOT NULL,
  gross_sales_amount_usd   REAL    NOT NULL,
  discount_amount_usd      REAL    NOT NULL,
  _source_file             TEXT,
  _ingested_at             TEXT
);

INSERT INTO silver.pos_sales_line
SELECT
  'POS-' || store_id || '-' || transaction_id || '-' || line_number,
  transaction_id,
  line_number,
  register_number,
  store_id,
  sales_date,
  sales_date || ' ' || substr(hhmmss, 1, 2) || ':' || substr(hhmmss, 3, 2) || ':' || substr(hhmmss, 5, 2),
  product_id,
  CASE WHEN card IS NULL THEN 'ANONYMOUS' ELSE sha2(card, 256) END,                      -- 5
  promotion_code,
  'In-store',
  line_type,
  is_test_transaction,
  sign * qty_abs,                                                                        -- 4
  currency_code,
  unit_price_local,
  ROUND(sign * gross_abs, 2),
  ROUND(sign * discount_abs, 2),
  fx_rate_to_usd,
  ROUND(sign * gross_abs    * fx_rate_to_usd, 2),                                        -- 6
  ROUND(sign * discount_abs * fx_rate_to_usd, 2),
  _source_file,
  _ingested_at
FROM _pos_checked
WHERE failure_reason IS NULL;
