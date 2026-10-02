"""Run the whole FreshCart demo end to end.

    python run_demo.py                 # pipeline + checks + benchmarks + docs (uses committed raw data)
    python run_demo.py --regenerate    # also regenerate the synthetic raw data first (same seed, same data)

Steps
  1. (optional) generate messy source files into data/raw/
  2. bronze -> silver -> gold, rebuilt from scratch into warehouse/*.db
  3. data-quality rules and findings
  4. Genie benchmark ground truth and example queries, run locally
  5. export silver/gold snapshots to data/exports/, generate the Databricks SQL and notebook, render docs/
"""
import argparse
import csv

from freshcart import benchmarks, config as C, databricks_notebook, docs, export_databricks, generate, pipeline, quality

EXPORT_TABLES = {
    "gold": ["dim_date", "dim_store", "dim_product", "dim_customer", "dim_promotion",
             "fct_sales_line", "fct_inventory_daily"],
    "silver": ["store_history", "product", "product_cost", "promotion", "loyalty_member", "fx_rate_daily",
               "quarantine_pos_sales_line", "pos_sales_line", "online_sales_line", "inventory_daily"],
}
SAMPLE_ROWS = 500          # large silver tables are exported as a sample


def export(con):
    for layer, tables in EXPORT_TABLES.items():
        out_dir = C.EXPORT_DIR / layer
        out_dir.mkdir(parents=True, exist_ok=True)
        for t in tables:
            big = layer == "silver" and t in ("pos_sales_line", "online_sales_line", "inventory_daily")
            cols = [r[1] for r in con.execute(f"PRAGMA {layer}.table_info({t})")
                    if not r[1].endswith("ingested_at")]            # run-specific timestamps stay out of git
            sql = f"SELECT {', '.join(cols)} FROM {layer}.{t}"
            name = f"{t}.csv"
            if big:
                sql += f" ORDER BY 1 LIMIT {SAMPLE_ROWS}"
                name = f"{t}_sample.csv"
            elif layer == "gold" and t == "dim_date":
                sql += " WHERE fiscal_year BETWEEN 2024 AND 2027"
            elif layer == "gold" and t == "fct_sales_line":           # full table is ~25 MB: export 4 weeks
                sql += (" WHERE sales_date IN (SELECT calendar_date FROM gold.dim_date"
                        " WHERE is_last_4_completed_fiscal_weeks) ORDER BY sales_date, sales_line_id")
                name = "fct_sales_line_last_4_weeks.csv"
            cur = con.execute(sql)
            with open(out_dir / name, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([c[0] for c in cur.description])
                w.writerows(cur.fetchall())
    print(f"Exported silver and gold snapshots to {C.EXPORT_DIR.relative_to(C.ROOT)}/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regenerate", action="store_true", help="regenerate the synthetic raw data first")
    args = ap.parse_args()
    if args.regenerate or not C.RAW_DIR.exists():
        print("STEP 1  generate synthetic source data")
        generate.main()
    print("STEP 2  run the pipeline")
    con = pipeline.run(full_refresh=True)
    print("STEP 3  data quality")
    failed = [r for r in quality.run_rules(con) if r[2] != 0]
    print(f"  {len(quality.RULES) - len(failed)} of {len(quality.RULES)} rules pass")
    if failed:
        raise SystemExit(f"Data-quality rules failed: {failed}")
    print("STEP 4  Genie benchmarks and example queries (ground truth)")
    b = benchmarks.run_benchmarks(con)
    e = benchmarks.run_examples(con)
    print(f"  {sum(1 for x in b if x[1] is not None)} benchmark answers and {len(e)} example queries computed")
    print("STEP 5  exports, Databricks DDL and docs")
    export(con)
    export_databricks.main()
    databricks_notebook.main()
    docs.main()
    print("\nDone. Start reading at README.md, then docs/01-business-and-questions.md")


if __name__ == "__main__":
    main()
