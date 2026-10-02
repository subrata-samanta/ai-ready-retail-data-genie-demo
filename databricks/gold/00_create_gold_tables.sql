-- GENERATED from contracts/gold/*.yml by `python -m freshcart.export_databricks`. Do not edit by hand.
-- Creates the gold star schema in Unity Catalog with comments, keys, clustering and tags.
-- Dimensions are created before facts so foreign keys can reference them.
-- Note: dimension primary-key columns must be NOT NULL; constraints are informational.

CREATE TABLE IF NOT EXISTS ${catalog}.gold.dim_date (
  calendar_date                    DATE          NOT NULL COMMENT 'Calendar day. Join key for sales_date and snapshot_date on the facts.',
  day_of_week_name                 STRING                 COMMENT 'Monday to Sunday. FreshCart weeks run Sunday to Saturday.',
  fiscal_year                      INT                    COMMENT 'FreshCart fiscal year, named for the calendar year it starts in. FY2026 runs from Sunday 1 Feb 2026 to Saturday 30 Jan 2027.',
  fiscal_quarter                   INT                    COMMENT 'Fiscal quarter 1 to 4. Q1 starts in February.',
  fiscal_period                    INT                    COMMENT 'Fiscal month 1 to 12 on a 4-5-4 pattern. Period 1 starts in February. Use for any question about months.',
  fiscal_period_name               STRING                 COMMENT 'Label such as P01 Feb. Use for display.',
  fiscal_week                      INT                    COMMENT 'Fiscal week of year, 1 to 52, or 53 in 53-week years such as FY2023.',
  fiscal_week_start_date           DATE                   COMMENT 'Sunday that starts the fiscal week.',
  fiscal_day_of_year               INT                    COMMENT 'Day number within the fiscal year (1 to 364 or 371). The same number means the same weekday last year; use for like-for-like comparisons.',
  is_last_completed_fiscal_week    BOOLEAN                COMMENT 'TRUE for the 7 days of the most recent fully completed fiscal week. Use for last week.',
  is_last_4_completed_fiscal_weeks BOOLEAN                COMMENT 'TRUE for the 28 days of the four most recent completed fiscal weeks. Use for last 4 weeks.',
  is_current_fiscal_quarter        BOOLEAN                COMMENT 'TRUE for all days of the fiscal quarter that contains yesterday.',
  is_fiscal_ytd                    BOOLEAN                COMMENT 'TRUE from the first day of the current fiscal year up to and including yesterday.',
  is_prior_fiscal_ytd              BOOLEAN                COMMENT 'TRUE for the same fiscal days in the previous fiscal year. Compare with is_fiscal_ytd for year to date versus last year.',
  CONSTRAINT pk_dim_date PRIMARY KEY (calendar_date) RELY
)
COMMENT 'FreshCart fiscal calendar, one row per calendar day from FY2010 to FY2027. Use to convert dates to fiscal weeks, periods, quarters and years and to filter relative periods such as last week or year to date. Rolling flags are recomputed on every run relative to the as-of date of the pipeline.';
ALTER TABLE ${catalog}.gold.dim_date SET TAGS ('domain' = 'retail_performance', 'grain' = 'day');

CREATE TABLE IF NOT EXISTS ${catalog}.gold.dim_store (
  store_id                       STRING        NOT NULL COMMENT 'FreshCart store number without leading zeros, for example 1042. Join key to the facts.',
  store_name                     STRING        NOT NULL COMMENT 'Trading name, for example FreshCart Boston Seaport. Use when a question names a store.',
  store_format                   STRING        NOT NULL COMMENT 'One of: Supercenter, Neighborhood Market, Express, Dark Store. Dark Stores fulfil online orders only.',
  city                           STRING                 COMMENT 'City where the store is located.',
  state_province                 STRING                 COMMENT 'US state or Canadian province, full name, for example Massachusetts.',
  country                        STRING                 COMMENT 'United States or Canada.',
  region                         STRING        NOT NULL COMMENT 'FreshCart operating region (management hierarchy, not census region). One of: Northeast, Southeast, Midwest, West, Canada.',
  open_date                      DATE                   COMMENT 'Date the store first traded.',
  open_fiscal_year               INT                    COMMENT 'Fiscal year in which the store first traded. Use for questions about store openings by year.',
  close_date                     DATE                   COMMENT 'Date the store stopped trading, if closed.',
  store_status                   STRING        NOT NULL COMMENT 'Open or Closed on the as-of date of the pipeline run.',
  selling_area_sqft              INT                    COMMENT 'Selling floor area in square feet, excluding back room. Zero for Dark Stores. Use for sales per square foot.',
  comparable_from_fiscal_year    INT                    COMMENT 'First fiscal year in which the store counts as comparable (like-for-like): the first year after one full fiscal year of trading. NULL for Dark Stores, which are never comparable.',
  is_comparable_store_current_fy BOOLEAN       NOT NULL COMMENT 'TRUE if the store is open and comparable in the current fiscal year.',
  CONSTRAINT pk_dim_store PRIMARY KEY (store_id) RELY
)
COMMENT 'One row per FreshCart store or Dark Store with current attributes, plus an UNKNOWN member. Use to filter or group sales and stock by store, format, city, state, region or country, and for store lists such as openings. Attribute history is in silver.store_history.'
CLUSTER BY (region);
ALTER TABLE ${catalog}.gold.dim_store SET TAGS ('domain' = 'retail_performance', 'grain' = 'store');

CREATE TABLE IF NOT EXISTS ${catalog}.gold.dim_product (
  product_id       STRING        NOT NULL COMMENT 'FreshCart item number without leading zeros. Join key to the facts.',
  product_name     STRING        NOT NULL COMMENT 'Shelf description, for example Crunchy Sea Salt Chips 200g.',
  brand            STRING                 COMMENT 'Brand name. FreshCart own-brand products use FreshCart or FreshCart Select.',
  department       STRING        NOT NULL COMMENT 'Top level of the merchandise hierarchy: Produce, Bakery, Meat and Seafood, Deli, Dairy and Eggs, Grocery, Household.',
  category         STRING        NOT NULL COMMENT 'Second level, for example Snacks, Beverages, Cleaning.',
  subcategory      STRING        NOT NULL COMMENT 'Third level, for example Chips and Crisps, Cookies and Biscuits.',
  unit_of_measure  STRING        NOT NULL COMMENT 'Each or Kilogram. Kilogram items are weighed at the till; their quantities are weights.',
  is_private_label BOOLEAN       NOT NULL COMMENT 'TRUE for FreshCart own-brand products (own brand, store brand).',
  product_status   STRING        NOT NULL COMMENT 'Active or Discontinued.',
  CONSTRAINT pk_dim_product PRIMARY KEY (product_id) RELY
)
COMMENT 'One row per product (SKU) with the current merchandise hierarchy, plus an UNKNOWN member. Use to filter or group by department, category, subcategory, brand or private label.'
CLUSTER BY (department, category);
ALTER TABLE ${catalog}.gold.dim_product SET TAGS ('domain' = 'retail_performance', 'grain' = 'product');

CREATE TABLE IF NOT EXISTS ${catalog}.gold.dim_customer (
  customer_id     STRING        NOT NULL COMMENT 'Pseudonymous loyalty customer id (SHA-256 of the card number), or ANONYMOUS.',
  loyalty_tier    STRING        NOT NULL COMMENT 'Gold, Silver or Bronze; Anonymous for non-members; Unknown for new members not yet in the CRM extract.',
  age_band        STRING                 COMMENT 'Age band derived from date of birth, for example 35-44. The date of birth itself is never stored here.',
  home_store_id   STRING                 COMMENT 'Store the member chose as home store.',
  enrollment_date DATE                   COMMENT 'Date the member joined the loyalty programme.',
  is_placeholder  BOOLEAN       NOT NULL COMMENT 'TRUE for a late-arriving member seen in sales before the CRM extract caught up. Details are filled in when the CRM row arrives.',
  CONSTRAINT pk_dim_customer PRIMARY KEY (customer_id) RELY
)
COMMENT 'One row per loyalty customer, identified only by a pseudonymous id, plus the ANONYMOUS member for baskets without a loyalty card. Contains no names, emails or dates of birth. Use to group sales by loyalty tier, age band or home store.';
ALTER TABLE ${catalog}.gold.dim_customer SET TAGS ('domain' = 'retail_performance', 'grain' = 'customer', 'contains_pii' = 'false');

CREATE TABLE IF NOT EXISTS ${catalog}.gold.dim_promotion (
  promotion_id       STRING        NOT NULL COMMENT 'Promotion code, for example BG26W33, or NO_PROMO.',
  promotion_name     STRING        NOT NULL COMMENT 'Promotion description shown in marketing, for example Snack Attack - Buy one get one free.',
  promotion_mechanic STRING        NOT NULL COMMENT 'Buy One Get One, Percent Off, Multi-buy, Loyalty Price, or No Promotion.',
  start_date         DATE                   COMMENT 'First day the promotion runs.',
  end_date           DATE                   COMMENT 'Last day the promotion runs.',
  CONSTRAINT pk_dim_promotion PRIMARY KEY (promotion_id) RELY
)
COMMENT 'One row per promotion, plus NO_PROMO for lines sold at full price. Use to group sales by promotion or promotion mechanic.';
ALTER TABLE ${catalog}.gold.dim_promotion SET TAGS ('domain' = 'retail_performance', 'grain' = 'promotion');

CREATE TABLE IF NOT EXISTS ${catalog}.gold.fct_sales_line (
  sales_line_id          STRING        NOT NULL COMMENT 'Unique id of the line: channel, store or order, receipt and line number.',
  transaction_id         STRING        NOT NULL COMMENT 'Receipt or online order id. COUNT(DISTINCT transaction_id) gives transactions (baskets).',
  sales_date             DATE          NOT NULL COMMENT 'Business date of the sale in store local time. Join to dim_date.calendar_date for fiscal periods.',
  store_id               STRING        NOT NULL COMMENT 'Selling store; for online orders, the fulfilling store or Dark Store.',
  product_id             STRING        NOT NULL COMMENT 'Product sold or returned. UNKNOWN if the item was not in the product master.',
  customer_id            STRING        NOT NULL COMMENT 'Pseudonymous loyalty customer id, or ANONYMOUS when no card was scanned.',
  promotion_id           STRING        NOT NULL COMMENT 'Promotion applied to the line, or NO_PROMO.',
  region                 STRING        NOT NULL COMMENT 'Copy of dim_store.region used only for row-level security. Use dim_store.region for analysis.',
  sales_channel          STRING        NOT NULL COMMENT 'In-store or Online.',
  line_type              STRING        NOT NULL COMMENT 'Sale or Return.',
  quantity_units         DECIMAL(12,3) NOT NULL COMMENT 'Units sold in selling units, not cases. Negative for returns so SUM gives net units. Kilograms for weighed items.',
  gross_sales_amount_usd DECIMAL(18,2) NOT NULL COMMENT 'Value at shelf price before discounts, excluding tax, USD. Negative for returns.',
  discount_amount_usd    DECIMAL(18,2) NOT NULL COMMENT 'Promotional and loyalty discounts, USD, as a positive number that reduces sales.',
  net_sales_amount_usd   DECIMAL(18,2) NOT NULL COMMENT 'Gross sales minus discounts, excluding tax, USD. Negative for returns. The company definition of sales and revenue.',
  cost_of_goods_usd      DECIMAL(18,2) NOT NULL COMMENT 'Standard cost of the units on the day of sale, USD. Negative for returns. Zero when the product is UNKNOWN.',
  gross_margin_usd       DECIMAL(18,2) NOT NULL COMMENT 'Net sales minus cost of goods, USD.',
  net_sales_amount_local DECIMAL(18,2) NOT NULL COMMENT 'Net sales in the store local currency. Only for statutory local reporting.',
  currency_code          STRING        NOT NULL COMMENT 'ISO code of the local currency: USD or CAD.',
  is_promo_sale          BOOLEAN       NOT NULL COMMENT 'TRUE when a promotion was applied to the line.',
  is_loyalty_sale        BOOLEAN       NOT NULL COMMENT 'TRUE when a loyalty card was scanned on the transaction.',
  _source_ingested_at    TIMESTAMP              COMMENT 'Pipeline audit column: when the source file landed in bronze. Not for analysis.',
  CONSTRAINT pk_fct_sales_line PRIMARY KEY (sales_line_id) RELY,
  CONSTRAINT fk_fct_sales_line_sales_date FOREIGN KEY (sales_date) REFERENCES ${catalog}.gold.dim_date (calendar_date),
  CONSTRAINT fk_fct_sales_line_store_id FOREIGN KEY (store_id) REFERENCES ${catalog}.gold.dim_store (store_id),
  CONSTRAINT fk_fct_sales_line_product_id FOREIGN KEY (product_id) REFERENCES ${catalog}.gold.dim_product (product_id),
  CONSTRAINT fk_fct_sales_line_customer_id FOREIGN KEY (customer_id) REFERENCES ${catalog}.gold.dim_customer (customer_id),
  CONSTRAINT fk_fct_sales_line_promotion_id FOREIGN KEY (promotion_id) REFERENCES ${catalog}.gold.dim_promotion (promotion_id)
)
COMMENT 'Sales fact. One row per sold or returned item line on a FreshCart receipt or online order (voids, cancelled orders and training transactions excluded), since FY2025. Answers questions about sales, revenue, units, baskets, margin, returns, promotions and loyalty by day, store and product. All amounts are USD and exclude sales tax unless the column name says local.'
CLUSTER BY (sales_date, store_id);
ALTER TABLE ${catalog}.gold.fct_sales_line SET TAGS ('domain' = 'retail_performance', 'grain' = 'sales_line', 'genie_ready' = 'true');
ALTER TABLE ${catalog}.gold.fct_sales_line ALTER COLUMN region SET TAGS ('purpose' = 'security_only');

CREATE TABLE IF NOT EXISTS ${catalog}.gold.fct_inventory_daily (
  snapshot_date     DATE          NOT NULL COMMENT 'Day of the close-of-business stock count. Join to dim_date.calendar_date.',
  store_id          STRING        NOT NULL COMMENT 'Store holding the stock.',
  product_id        STRING        NOT NULL COMMENT 'Product in stock.',
  region            STRING        NOT NULL COMMENT 'Copy of dim_store.region used only for row-level security.',
  on_hand_units     DECIMAL(12,3) NOT NULL COMMENT 'Sellable units in stock at close of business. Semi-additive: can be summed across stores and products but never across days.',
  on_hand_value_usd DECIMAL(18,2) NOT NULL COMMENT 'Value of on-hand stock at standard cost, USD. Semi-additive like on_hand_units.',
  is_out_of_stock   BOOLEAN       NOT NULL COMMENT 'TRUE when a ranged, active product had zero sellable stock at close of business.',
  CONSTRAINT pk_fct_inventory_daily PRIMARY KEY (snapshot_date, store_id, product_id) RELY,
  CONSTRAINT fk_fct_inventory_daily_snapshot_date FOREIGN KEY (snapshot_date) REFERENCES ${catalog}.gold.dim_date (calendar_date),
  CONSTRAINT fk_fct_inventory_daily_store_id FOREIGN KEY (store_id) REFERENCES ${catalog}.gold.dim_store (store_id),
  CONSTRAINT fk_fct_inventory_daily_product_id FOREIGN KEY (product_id) REFERENCES ${catalog}.gold.dim_product (product_id)
)
COMMENT 'Daily stock position. One row per store, ranged product and day, from nightly snapshots covering the last 13 fiscal weeks. Use for stock on hand, stock value and out-of-stock questions. Stock levels must not be summed across days.'
CLUSTER BY (snapshot_date, store_id);
ALTER TABLE ${catalog}.gold.fct_inventory_daily SET TAGS ('domain' = 'retail_performance', 'grain' = 'store_product_day', 'genie_ready' = 'true');
ALTER TABLE ${catalog}.gold.fct_inventory_daily ALTER COLUMN region SET TAGS ('purpose' = 'security_only');
