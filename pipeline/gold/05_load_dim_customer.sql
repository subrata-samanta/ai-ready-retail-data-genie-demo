-- =====================================================================================
-- GOLD 05 · dim_customer
-- Built from : silver.loyalty_member  +  customer ids seen in silver sales lines
-- Grain      : one row per loyalty customer, plus ANONYMOUS
-- Load       : MERGE / upsert (not a rebuild) because of step 3
-- Steps      : 1. upsert members from the CRM extract (no personal data: pseudonymous id,
--                 tier, age band, home store)
--              2. add ANONYMOUS for baskets without a loyalty card
--              3. LATE-ARRIVING MEMBERS: a card scanned today may not be in the CRM
--                 extract until next week. Insert a placeholder (tier 'Unknown') so the
--                 sales still load; step 1 fills in the details when the CRM row arrives.
-- =====================================================================================
INSERT INTO gold.dim_customer (customer_id, loyalty_tier, age_band, home_store_id, enrollment_date,   -- step 1
                               is_placeholder)
SELECT customer_id, loyalty_tier, age_band, home_store_id, enrollment_date, 0
FROM silver.loyalty_member
WHERE 1
ON CONFLICT (customer_id) DO UPDATE SET
  loyalty_tier = excluded.loyalty_tier, age_band = excluded.age_band,
  home_store_id = excluded.home_store_id, enrollment_date = excluded.enrollment_date,
  is_placeholder = 0;

INSERT INTO gold.dim_customer (customer_id, loyalty_tier, is_placeholder)                          -- step 2
VALUES ('ANONYMOUS', 'Anonymous', 0)
ON CONFLICT (customer_id) DO NOTHING;

INSERT INTO gold.dim_customer (customer_id, loyalty_tier, is_placeholder)                          -- step 3
SELECT DISTINCT customer_id, 'Unknown', 1
FROM (SELECT customer_id FROM silver.pos_sales_line
      UNION
      SELECT customer_id FROM silver.online_sales_line)
WHERE 1
ON CONFLICT (customer_id) DO NOTHING;
