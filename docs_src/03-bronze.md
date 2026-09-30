# 3 · Bronze: land it exactly as delivered

[← 2 · Source data](02-source-data.md) · Next: [4 · Silver →](04-silver.md)

Bronze is deliberately boring. Its only job is to make the raw files queryable **without changing them**,
so that any number in gold can be traced back to the exact file and row it came from.

## The rules

1. **One table per feed.** `pos_tlog_*.csv` → `bronze.pos_tlog_raw`, and so on.
2. **Every column is text.** Nothing is cast, renamed, trimmed or cleaned. `01042` stays `01042`,
   `20250230` stays `20250230`. Type inference would quietly "fix" things (drop leading zeros,
   guess dates); fixing is silver's job, done deliberately.
3. **Three audit columns** are added to every row:
   `_source_file` (which file), `_row_number` (which row in it), `_ingested_at` (when it landed).
4. **Each file is loaded once.** The loader records every file in `bronze._ingestion_log`
   and skips files it has already seen. Run the pipeline twice and nothing is duplicated.
   On Databricks, streaming tables with `read_files` (Auto Loader) give the same guarantee.
5. **JSON is kept whole.** Each online order is stored as one JSON text value; silver parses it.

Code: [`freshcart/bronze.py`](../freshcart/bronze.py) (local) and
[`databricks/pipelines/01_bronze.sql`](../databricks/pipelines/01_bronze.sql) (Databricks).

## What landed

<!--feeds-->

## What a bronze row looks like

The golden receipt used throughout these docs (store `01042`, receipt `88120431`) exactly as it sits in bronze:

<!--table
SELECT TRX_ID, STR_NBR, REG_NBR, LN_NBR, TRX_DT, TRX_TM, ITM_ID, QTY, EXT_AMT, DISC_AMT, TRX_TYP, PROMO_CD,
       _source_file, _row_number
FROM bronze.pos_tlog_raw WHERE TRX_ID = '88120431' ORDER BY LN_NBR
-->

Everything is still text, the store has its leading zero, line 3 is a void, and the returns in this file still have
positive amounts. That is correct for bronze.

An online order in bronze is one row holding the whole document:

<!--table
SELECT substr(value, 1, 150) || '...' AS value, _source_file, _row_number
FROM bronze.ecom_order_raw WHERE _source_file LIKE '%202609%' LIMIT 2
-->

## Incremental ingestion

The ingestion log after the first run:

<!--table limit=6
SELECT source_file, target_table, row_count FROM bronze._ingestion_log ORDER BY source_file DESC
-->

When a new month of POS data lands in `data/raw/pos/`, the next run loads only that file.
You can see this in the tests: `test_incremental_rerun_is_idempotent` runs the pipeline a second
time and checks that no file is loaded again and gold does not change.

## Why not clean data on the way in?

Because you will be wrong about something, and you will want to reprocess. If bronze holds the
original bytes, you can fix a silver rule and rebuild everything downstream. If bronze was already
"cleaned", the original is gone. Bronze is cheap insurance.

Next: [4 · Silver →](04-silver.md)
