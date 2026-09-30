-- =====================================================================================
-- SILVER 01 · fiscal_calendar
-- Source : bronze.fiscal_calendar_raw   (Finance's file, text labels, DD/MM/YYYY dates)
-- Grain  : one row per calendar day
-- Steps  : 1. keep the latest delivery of each day (a re-sent file must not duplicate days)
--          2. parse DD/MM/YYYY strictly
--          3. turn text labels into numbers: 'FY2026' -> 2026, 'P01' -> 1, 'W34' -> 34
--          4. derive fiscal_day_of_year and the weekday name
-- =====================================================================================
DROP TABLE IF EXISTS silver.fiscal_calendar;
CREATE TABLE silver.fiscal_calendar (
  calendar_date          TEXT    PRIMARY KEY,
  fiscal_year            INTEGER NOT NULL,
  fiscal_quarter         INTEGER NOT NULL,
  fiscal_period          INTEGER NOT NULL,
  fiscal_period_name     TEXT    NOT NULL,
  fiscal_week            INTEGER NOT NULL,
  fiscal_week_start_date TEXT    NOT NULL,
  fiscal_day_of_year     INTEGER NOT NULL,
  day_of_week_name       TEXT    NOT NULL
);

INSERT INTO silver.fiscal_calendar
WITH latest AS (                                   -- step 1
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY CAL_DT ORDER BY _ingested_at DESC, _row_number DESC) AS rn
  FROM bronze.fiscal_calendar_raw
),
typed AS (                                         -- steps 2 and 3
  SELECT
    try_to_date(CAL_DT, 'dd/MM/yyyy')          AS calendar_date,
    CAST(substr(FISC_YR, 3) AS INTEGER)        AS fiscal_year,
    CAST(substr(FISC_QTR, 2) AS INTEGER)       AS fiscal_quarter,
    CAST(substr(FISC_PRD, 2) AS INTEGER)       AS fiscal_period,
    FISC_PRD || ' ' || FISC_PRD_NM             AS fiscal_period_name,
    CAST(substr(FISC_WK, 2) AS INTEGER)        AS fiscal_week,
    try_to_date(FISC_WK_START, 'dd/MM/yyyy')   AS fiscal_week_start_date
  FROM latest
  WHERE rn = 1
)
SELECT                                             -- step 4
  calendar_date,
  fiscal_year,
  fiscal_quarter,
  fiscal_period,
  fiscal_period_name,
  fiscal_week,
  fiscal_week_start_date,
  CAST(julianday(calendar_date)
       - julianday(MIN(calendar_date) OVER (PARTITION BY fiscal_year)) AS INTEGER) + 1 AS fiscal_day_of_year,
  CASE strftime('%w', calendar_date)
    WHEN '0' THEN 'Sunday'   WHEN '1' THEN 'Monday' WHEN '2' THEN 'Tuesday' WHEN '3' THEN 'Wednesday'
    WHEN '4' THEN 'Thursday' WHEN '5' THEN 'Friday' WHEN '6' THEN 'Saturday' END AS day_of_week_name
FROM typed;
