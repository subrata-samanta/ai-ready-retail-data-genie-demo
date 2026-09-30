-- =====================================================================================
-- SILVER 02 · fx_rate_daily
-- Source : bronze.fx_rate_raw   (CAD->USD, published on business days only)
-- Grain  : one row per calendar day per currency
-- Steps  : 1. type the published rates
--          2. build a row for EVERY calendar day, carrying the last published rate forward
--             over weekends and holidays (a Saturday sale still needs a rate)
--          3. add USD->USD = 1.0 so every sale can use the same join
-- =====================================================================================
DROP TABLE IF EXISTS silver.fx_rate_daily;
CREATE TABLE silver.fx_rate_daily (
  rate_date          TEXT    NOT NULL,
  from_currency      TEXT    NOT NULL,
  rate_to_usd        REAL    NOT NULL,
  is_carried_forward INTEGER NOT NULL,      -- 1 when no rate was published that day
  PRIMARY KEY (rate_date, from_currency)
);

DROP TABLE IF EXISTS temp._published;
CREATE TEMP TABLE _published AS                                        -- step 1
SELECT try_to_date(RATE_DT, 'yyyy-MM-dd') AS rate_date,
       upper(FROM_CCY)                    AS from_currency,
       CAST(RATE AS REAL)                 AS rate
FROM bronze.fx_rate_raw
WHERE upper(TO_CCY) = 'USD';

INSERT INTO silver.fx_rate_daily
SELECT                                                                 -- step 2
  c.calendar_date,
  'CAD',
  (SELECT p.rate FROM _published p
   WHERE p.from_currency = 'CAD' AND p.rate_date <= c.calendar_date
   ORDER BY p.rate_date DESC LIMIT 1),
  CASE WHEN EXISTS (SELECT 1 FROM _published p
                    WHERE p.from_currency = 'CAD' AND p.rate_date = c.calendar_date) THEN 0 ELSE 1 END
FROM silver.fiscal_calendar c
WHERE c.calendar_date BETWEEN (SELECT MIN(rate_date) FROM _published) AND '${AS_OF_DATE}';

INSERT INTO silver.fx_rate_daily                                       -- step 3
SELECT calendar_date, 'USD', 1.0, 0
FROM silver.fiscal_calendar
WHERE calendar_date BETWEEN (SELECT MIN(rate_date) FROM _published) AND '${AS_OF_DATE}';
