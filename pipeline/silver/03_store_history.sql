-- =====================================================================================
-- SILVER 03 · store_history   (slowly changing dimension, type 2)
-- Source : bronze.store_master_raw (4 monthly snapshots) + three code lookups
-- Grain  : one row per store per VERSION of its attributes
-- Steps  : 1. decode codes: FMT_CD 'NM' -> 'Neighborhood Market', RGN_CD '02' -> 'Northeast',
--             ST_CD 'MA' -> 'Massachusetts', CNTRY 'US' -> 'United States'
--          2. normalise ids ('01042' -> '1042') and parse US-style MM/DD/YYYY dates
--          3. read the snapshot date from the file name (store_master_20260201.csv)
--          4. keep a snapshot only when something changed since the previous snapshot
--          5. give each version a valid_from / valid_to range and flag the current one
-- Why    : history answers "what format was Boston when it sold this?"; gold uses
--          the current version, silver keeps them all.
-- =====================================================================================
DROP TABLE IF EXISTS silver.store_history;
CREATE TABLE silver.store_history (
  store_id          TEXT    NOT NULL,
  store_name        TEXT    NOT NULL,
  store_format      TEXT    NOT NULL,
  city              TEXT,
  state_province    TEXT,
  country           TEXT,
  region            TEXT    NOT NULL,
  open_date         TEXT,
  close_date        TEXT,
  selling_area_sqft INTEGER,
  valid_from        TEXT    NOT NULL,
  valid_to          TEXT    NOT NULL,
  is_current        INTEGER NOT NULL,
  PRIMARY KEY (store_id, valid_from)
);

INSERT INTO silver.store_history
WITH snap AS (
  SELECT
    ltrim(s.STR_NBR, '0')                                    AS store_id,          -- step 2
    s.STR_NM                                                 AS store_name,
    f.FMT_NM                                                 AS store_format,      -- step 1
    s.CITY                                                   AS city,
    st.ST_NM                                                 AS state_province,
    CASE s.CNTRY WHEN 'US' THEN 'United States' WHEN 'CA' THEN 'Canada' END AS country,
    r.RGN_NM                                                 AS region,
    try_to_date(s.OPEN_DT, 'MM/dd/yyyy')                     AS open_date,         -- step 2
    try_to_date(NULLIF(s.CLOSE_DT, ''), 'MM/dd/yyyy')        AS close_date,
    CAST(s.SQFT AS INTEGER)                                  AS selling_area_sqft,
    try_to_date(substr(s._source_file, -12, 8), 'yyyyMMdd')  AS snapshot_date      -- step 3
  FROM bronze.store_master_raw s
  LEFT JOIN bronze.ref_store_format_raw   f  ON f.FMT_CD  = s.FMT_CD
  LEFT JOIN bronze.ref_region_raw         r  ON r.RGN_CD  = s.RGN_CD
  LEFT JOIN bronze.ref_state_province_raw st ON st.ST_CD  = s.ST_CD
),
hashed AS (
  SELECT *,
         store_name || '|' || store_format || '|' || COALESCE(city, '') || '|' || COALESCE(state_province, '') || '|' ||
         region || '|' || COALESCE(open_date, '') || '|' || COALESCE(close_date, '') || '|' ||
         COALESCE(selling_area_sqft, '') AS attr_hash
  FROM snap
),
changes AS (                                                                         -- step 4
  SELECT *, LAG(attr_hash) OVER (PARTITION BY store_id ORDER BY snapshot_date) AS prev_hash
  FROM hashed
),
versions AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY store_id ORDER BY snapshot_date) AS version_no
  FROM changes
  WHERE prev_hash IS NULL OR prev_hash <> attr_hash
),
ranged AS (                                                                          -- step 5
  SELECT *,
         CASE WHEN version_no = 1 THEN MIN(open_date, snapshot_date) ELSE snapshot_date END AS valid_from
  FROM versions
)
SELECT
  store_id, store_name, store_format, city, state_province, country, region,
  open_date, close_date, selling_area_sqft,
  valid_from,
  COALESCE(date(LEAD(valid_from) OVER (PARTITION BY store_id ORDER BY valid_from), '-1 day'), '9999-12-31') AS valid_to,
  CASE WHEN LEAD(valid_from) OVER (PARTITION BY store_id ORDER BY valid_from) IS NULL THEN 1 ELSE 0 END  AS is_current
FROM ranged;
