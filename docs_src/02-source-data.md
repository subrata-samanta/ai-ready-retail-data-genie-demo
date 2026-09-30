# 2 · Source data: what arrives, and what is wrong with it

[← 1 · The business](01-business-and-questions.md) · Next: [3 · Bronze →](03-bronze.md)

All source files are in [`data/raw/`](../data/raw/). They were produced by
[`freshcart/generate.py`](../freshcart/generate.py) with a fixed random seed, so anyone who
regenerates them gets identical files. Each problem below was **planted on purpose** because
real retail feeds have it, and each one is fixed somewhere in the pipeline.

## The feeds

<!--feeds-->

## POS transaction log

One CSV per month from the store systems. A receipt is several rows (one per line).

<!--raw pos/pos_tlog_202609.csv lines=6-->

| Column | Meaning | Problem |
|---|---|---|
| `TRX_ID`, `LN_NBR` | Receipt id and line number | Only unique together with store, register and date |
| `STR_NBR` | Store number | Leading zeros (`01042`); ERP and e-commerce use `1042` |
| `REG_NBR` | Register (till) | Register `99` is the **training** till: test sales, not real |
| `TRX_DT`, `TRX_TM` | Date and time | Strings: `20260914`, and `93015` meaning 09:30:15 (no leading zero) |
| `ITM_ID` | Item | Zero-padded to 12 digits (`000004471023`); ERP says `4471023` |
| `QTY` | Quantity | Decimal kilograms for weighed produce (`1.254`) |
| `EXT_AMT`, `DISC_AMT`, `TAX_AMT` | Extended price, discount, tax | Three amounts; "sales" could mean any of them |
| `TRX_TYP` | S / R / V | Sale, Return, **Void**. Returns arrive with **positive** values |
| `LYL_CARD_NBR` | Loyalty card | Personal data |
| `CRNCY_CD` | Currency | `CAD` for the Canadian stores |

Planted problems you can count yourself:

<!--table
SELECT 'Voided lines (TRX_TYP = V)' AS problem, COUNT(*) AS rows FROM bronze.pos_tlog_raw WHERE TRX_TYP = 'V'
UNION ALL SELECT 'Return lines with positive amounts', COUNT(*) FROM bronze.pos_tlog_raw WHERE TRX_TYP = 'R'
UNION ALL SELECT 'Training register 99', COUNT(*) FROM bronze.pos_tlog_raw WHERE REG_NBR = '99'
UNION ALL SELECT 'Rows re-sent twice (10 June 2025 delivered again)', COUNT(*) - COUNT(DISTINCT STR_NBR || REG_NBR || TRX_ID || LN_NBR || TRX_DT) FROM bronze.pos_tlog_raw
UNION ALL SELECT 'Missing store number', COUNT(*) FROM bronze.pos_tlog_raw WHERE STR_NBR = ''
UNION ALL SELECT 'Impossible date (30 February)', COUNT(*) FROM bronze.pos_tlog_raw WHERE substr(TRX_DT, 5, 4) = '0230'
UNION ALL SELECT 'Quantity 9999 (scanner error)', COUNT(*) FROM bronze.pos_tlog_raw WHERE QTY = '9999'
UNION ALL SELECT 'Item not in the product master', COUNT(*) FROM bronze.pos_tlog_raw WHERE ITM_ID = '000009999999'
UNION ALL SELECT 'Lines in Canadian dollars', COUNT(*) FROM bronze.pos_tlog_raw WHERE CRNCY_CD = 'CAD'
-->

> **A trap worth knowing:** SQLite's own `date('2025-02-30')` silently returns `2025-03-02`.
> Many engines and libraries "helpfully" roll invalid dates forward. The pipeline uses a strict
> parser (`try_to_date`, the Databricks function of the same name) so an impossible date becomes
> NULL and is quarantined instead of quietly moving a sale into March.

## E-commerce orders

One JSON document per line (NDJSON), with the order lines **nested** inside the order:

<!--raw ecommerce/ecom_orders_202609.ndjson lines=1-->

Problems: nested lines must be flattened; the store id has **no** leading zeros (`9001`) while POS has them;
the timestamp carries a UTC offset. If you convert `2026-09-01T23:25:38-04:00` to UTC you get
2 September, the wrong business day. That would move {{n: SELECT COUNT(*) FROM bronze.ecom_order_raw WHERE substr(json_extract(value, '$.orderTs'), 1, 10) <> date(json_extract(value, '$.orderTs'))}}
orders to the next day. Cancelled orders ({{n: SELECT COUNT(*) FROM bronze.ecom_order_raw WHERE json_extract(value, '$.status') = 'CANCELLED'}} of them) must not count as sales.

## ERP product master

<!--raw erp/product_master.csv lines=6-->

The ERP keeps **one row per SKU per sales organisation** (`US01` and `CA01`), so there are
{{n: SELECT COUNT(*) FROM bronze.product_master_raw}} rows for {{n: SELECT COUNT(DISTINCT ltrim(MATNR, '0')) FROM bronze.product_master_raw}} products.
Join sales to this table as-is and most sales **double**. Other problems: `MATKL` is a code
(`C0412`), the Canadian copy is upper-cased and never marks items discontinued, and two items exist only in Canada.

<!--raw erp/merch_hierarchy.csv lines=5-->

## Store master

A snapshot of the store master is delivered each time something changes. There are
{{n: SELECT COUNT(DISTINCT _source_file) FROM bronze.store_master_raw}} snapshots; history has to be **derived** by comparing them.

<!--raw stores/store_master_20260601.csv lines=5-->

Problems: region (`02`), format (`NM`) and state (`MA`) are codes that need the lookup files;
dates are US-style `MM/DD/YYYY`; the store number has leading zeros.

## Loyalty CRM

<!--table
SELECT CARD_NBR, EMAIL, DOB, TIER, ENROLL_DT, HOME_STR FROM bronze.crm_member_raw LIMIT 3
-->

Card number, email and date of birth are **personal data**: they must never reach the gold layer or Genie.
The extract is from 6 September 2026, so members who joined after that date are already shopping but
are not in the file yet (late-arriving members).

## Finance: fiscal calendar and FX rates

<!--raw finance/fiscal_calendar.csv lines=4-->

Dates are `DD/MM/YYYY` (the opposite of the store master!) and the fiscal attributes are text labels (`FY2026`, `P01`, `W01`).

<!--raw finance/fx_rates.csv lines=4-->

Rates are published on business days only. A Saturday sale in Toronto still needs a rate.

## Inventory snapshots and promotions

<!--raw inventory/inv_snapshot_202609.csv lines=4-->

Stock is counted every night. A few readings are negative ({{n: SELECT COUNT(*) FROM bronze.inventory_raw WHERE CAST(OH_QTY AS REAL) < 0}} rows), which is physically impossible.

<!--raw promotions/promo_calendar.csv lines=4-->

## Summary: problem → fix → where

| Problem | Fix | Where |
|---|---|---|
| Strings for dates, times and numbers | Strict typing | Silver 01, 03, 07, 09 |
| Leading zeros differ between systems | One normalised id per entity | Silver 03, 04, 07, 08, 09 |
| Codes for region, format, category, tier, mechanic | Decoded to labels | Silver 03, 04, 05, 06 |
| Re-delivered POS day | Deduplicate on the natural key | Silver 07 |
| Broken rows (no store, impossible date, qty 9999) | Quarantine with a reason | Silver 07 |
| Returns with positive values | Sign carries the meaning | Silver 07 |
| Voids, cancellations, training till | Kept and flagged in silver, excluded in gold | Silver 07/08, Gold 06 |
| Product master duplicated per sales org | Deduplicate, prefer US01 | Silver 04 |
| Item missing from the master | Unknown member | Gold 03, 06 |
| Nested JSON, UTC offsets | Flatten; business date from local time | Silver 08 |
| CAD amounts | Convert at the daily rate | Silver 02, 07, 08 |
| FX on business days only | Carry the last rate forward | Silver 02 |
| Store history as snapshots | SCD type 2 | Silver 03 |
| Personal data | Pseudonymise; separate restricted table | Silver 06 |
| Late-arriving loyalty members | Placeholder customers | Gold 05 |
| Negative stock | Clamp to zero and flag | Silver 09 |
| Fiscal calendar as text labels | Typed calendar plus rolling flags | Silver 01, Gold 01 |

Next: [3 · Bronze →](03-bronze.md)
