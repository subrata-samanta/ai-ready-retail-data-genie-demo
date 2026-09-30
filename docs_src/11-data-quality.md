# 11 · Data quality: rules, findings and proof

[← 10 · Data dictionary](10-data-dictionary.md) · [Back to README](../README.md)

AI-ready means you can **prove** the answers are right. This repo does that three ways.

## 1 · Rules and findings

Rules must return zero; a non-zero result fails the tests. Findings count the problems the pipeline found and handled
on this deliberately messy data. Source: [`freshcart/quality.py`](../freshcart/quality.py).

<!--quality-->

## 2 · Independent reconciliation

`test_reconciliation_raw_to_gold` in [`tests/test_pipeline.py`](../tests/test_pipeline.py) recomputes total net sales
**straight from the raw CSV and JSON files in plain Python**, with no SQL and no pipeline code: skip re-delivered rows,
impossible dates, missing stores, quantity 9999, voids, training tills and cancelled orders; flip returns negative;
convert CAD with the last published rate. Then it checks gold agrees to within five cents.

- Gold total net sales: {{usd: SELECT SUM(net_sales_amount_usd) FROM gold.fct_sales_line}}

Two independent implementations agreeing is strong evidence that both are right.

## 3 · Behaviour tests

| Test | What it proves |
|---|---|
| `test_golden_receipt_through_every_layer` | the traced receipt has exactly the expected rows in every layer |
| `test_canadian_return_is_negative_and_converted` | sign convention and FX conversion |
| `test_semi_additive_stock_uses_last_day` | the metric view returns closing stock, not a sum of days |
| `test_margin_pct_is_ratio_of_sums_not_average` | the margin % measure is a ratio of sums |
| `test_store_history_keeps_boston_remodel` | SCD2 history is built from snapshots |
| `test_comparable_store_rules` | Denver comparable, Mississauga not, Buckhead closed, Dark Store never |
| `test_no_personal_data_in_gold` | no email, card or birth date columns in gold |
| `test_benchmarks_and_examples_run` | every Genie benchmark and example query runs and returns rows |
| `test_incremental_rerun_is_idempotent` | a second run loads no file twice and changes nothing |
| `test_foreign_keys_resolve_in_gold` | every fact row points to a dimension row |
| `test_databricks_ddl_is_up_to_date` | generated Databricks DDL matches the contracts |

Run them:

```bash
python tests/run_tests.py     # or: pytest -q
```

[Back to README](../README.md)
