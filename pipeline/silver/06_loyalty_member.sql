-- =====================================================================================
-- SILVER 06 · loyalty_member  and  loyalty_member_restricted
-- Source : bronze.crm_member_raw  (card number, email and date of birth = personal data)
-- Grain  : one row per loyalty member (latest extract wins)
-- Steps  : 1. replace the card number with a pseudonymous customer_id = sha2(card, 256).
--             POS and online sales use the same function, so the ids still join.
--          2. derive an age band instead of keeping the date of birth
--          3. decode tier 'G' -> 'Gold'; normalise the home store id
--          4. put the personal data in a separate RESTRICTED table.
--             On Databricks that table gets a column mask; it never flows to gold.
-- =====================================================================================
DROP TABLE IF EXISTS silver.loyalty_member;
CREATE TABLE silver.loyalty_member (
  customer_id     TEXT PRIMARY KEY,
  loyalty_tier    TEXT NOT NULL,
  enrollment_date TEXT,
  age_band        TEXT,
  home_store_id   TEXT,
  extract_date    TEXT
);

DROP TABLE IF EXISTS temp._members;
CREATE TEMP TABLE _members AS
SELECT *
FROM (
  SELECT m.*,
         try_to_date(substr(m._source_file, -12, 8), 'yyyyMMdd') AS extract_date,
         ROW_NUMBER() OVER (PARTITION BY CARD_NBR ORDER BY m._source_file DESC) AS rn
  FROM bronze.crm_member_raw m
)
WHERE rn = 1;

INSERT INTO silver.loyalty_member
SELECT
  sha2(CARD_NBR, 256),                                                              -- step 1
  CASE TIER WHEN 'G' THEN 'Gold' WHEN 'S' THEN 'Silver' WHEN 'B' THEN 'Bronze' ELSE 'Unknown' END,
  try_to_date(ENROLL_DT, 'yyyy-MM-dd'),
  CASE                                                                              -- step 2
    WHEN age < 18 THEN 'Under 18'
    WHEN age < 25 THEN '18-24'
    WHEN age < 35 THEN '25-34'
    WHEN age < 45 THEN '35-44'
    WHEN age < 55 THEN '45-54'
    WHEN age < 65 THEN '55-64'
    ELSE '65+' END,
  ltrim(HOME_STR, '0'),
  extract_date
FROM (
  SELECT *, CAST((julianday('${AS_OF_DATE}') - julianday(try_to_date(DOB, 'yyyy-MM-dd'))) / 365.25 AS INTEGER) AS age
  FROM _members
);

DROP TABLE IF EXISTS silver.loyalty_member_restricted;                              -- step 4
CREATE TABLE silver.loyalty_member_restricted (
  customer_id   TEXT PRIMARY KEY,
  card_number   TEXT NOT NULL,
  email         TEXT,
  date_of_birth TEXT
);

INSERT INTO silver.loyalty_member_restricted
SELECT sha2(CARD_NBR, 256), CARD_NBR, EMAIL, try_to_date(DOB, 'yyyy-MM-dd')
FROM _members;
