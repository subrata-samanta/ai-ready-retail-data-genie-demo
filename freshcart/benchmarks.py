"""Run the Genie benchmark ground truth and the example queries locally.

Genie itself runs on Databricks, so this does not test Genie. It does three useful things:
  1. proves every expected_sql and example query is valid against the demo data
  2. records the expected answer for each benchmark (shown in docs/07-genie-agent.md)
  3. lets you compare: ask Genie the question on Databricks, check it matches this answer

    python -m freshcart.benchmarks
"""
from __future__ import annotations

import yaml

from . import config as C
from . import metrics
from .db import connect

EXAMPLE_PARAMS = {
    "03_store_week_by_week.sql": {"store_name": "FreshCart Boston Seaport", "fiscal_year": 2026},
    "06_product_across_regions.sql": {"product_search": "chips"},
}


def load_benchmarks():
    return yaml.safe_load((C.GENIE_DIR / "benchmarks.yaml").read_text(encoding="utf-8"))["benchmarks"]


def run_benchmarks(con):
    results = []
    for b in load_benchmarks():
        if not b.get("expected_sql"):
            results.append((b, None, None))
            continue
        cols, rows = metrics.run(con, b["expected_sql"])
        results.append((b, cols, rows))
    return results


def run_examples(con):
    out = []
    for f in sorted((C.GENIE_DIR / "example_queries").glob("*.sql")):
        params = EXAMPLE_PARAMS.get(f.name, {})
        cols, rows = metrics.run(con, f.read_text(encoding="utf-8"), params)
        out.append((f.name, params, cols, rows))
    return out


def main():
    con = connect()
    for b, cols, rows in run_benchmarks(con):
        status = "review" if cols is None else f"{len(rows)} row(s)"
        print(f"#{b['id']:>2} {b['question']:<62} {status}")
        if rows:
            print(f"     {cols} -> {rows[0]}")
    print()
    for name, params, cols, rows in run_examples(con):
        print(f"{name:<52} {len(rows)} row(s) {params or ''}")


if __name__ == "__main__":
    main()
