"""Data health monitor: run the checks in monitoring/01_health_checks.sql, keep the history, fail on a breach.

Runs as the last task of the bundle job freshcart_refresh, so every refresh in every environment is checked:

    monitor.py --catalog freshcart_dev --as-of-date 2026-09-27 /Workspace/.../monitoring/01_health_checks.sql

Results are appended to <catalog>.monitoring.health_checks (one row per check and run), so the history can be
queried, charted or alerted on. A failed check raises an error, which fails the task and with it the job: the
job's e-mail notifications (variable alert_emails) tell the owners.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path


def _run_sql_module(checks_file: str):
    """src/run_sql.py. A Databricks Python task may not put this script's folder on sys.path, so fall back to
    the bundle's src/ folder found from the checks file (<bundle files>/monitoring/... -> <bundle files>/src)."""
    try:
        import run_sql
    except ImportError:
        sys.path.insert(0, str(Path(checks_file).resolve().parent.parent / "src"))
        import run_sql
    return run_sql

HISTORY = """CREATE TABLE IF NOT EXISTS {catalog}.monitoring.health_checks (
  run_id     STRING    COMMENT 'One id per monitor run',
  checked_at TIMESTAMP COMMENT 'When the check ran',
  check_name STRING    COMMENT 'Name of the check (see databricks/monitoring/01_health_checks.sql)',
  observed   DOUBLE    COMMENT 'Measured value',
  threshold  DOUBLE    COMMENT 'Limit the value is compared with',
  rule       STRING    COMMENT 'max: observed must be <= threshold; min: observed must be >= threshold',
  passed     BOOLEAN   COMMENT 'TRUE if the check passed',
  detail     STRING    COMMENT 'What the value means'
) COMMENT 'Data health checks after every refresh of the FreshCart data product (task monitor of freshcart_refresh)'"""


class HealthCheckFailed(RuntimeError):
    pass


def run(spark, checks_file: str, catalog: str, as_of_date: str = "today", echo=print) -> list[dict]:
    run_sql = _run_sql_module(checks_file)
    query = run_sql.render(run_sql.split_statements(Path(checks_file).read_text(encoding="utf-8"))[0],
                           {"catalog": catalog, "as_of_date": as_of_date})
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.monitoring COMMENT 'Operational monitoring of the data product'")
    spark.sql(HISTORY.format(catalog=catalog))
    run_id = uuid.uuid4().hex
    spark.sql(f"INSERT INTO {catalog}.monitoring.health_checks "
              f"SELECT '{run_id}', current_timestamp(), * FROM ({query})")
    rows = spark.sql(f"SELECT check_name, observed, threshold, rule, passed, detail "
                     f"FROM {catalog}.monitoring.health_checks WHERE run_id = '{run_id}' ORDER BY check_name").collect()
    results = [r.asDict() if hasattr(r, "asDict") else dict(r) for r in rows]
    for r in results:
        sign = "<=" if r["rule"] == "max" else ">="
        observed = "none" if r["observed"] is None else f"{r['observed']:.4g}"
        echo(f"{'ok  ' if r['passed'] else 'FAIL'}  {r['check_name']:<22} {observed:>8} {sign} {r['threshold']:<5g}"
             f"  {r['detail']}")
    failed = [r["check_name"] for r in results if not r["passed"]]
    if failed:
        raise HealthCheckFailed(f"{len(failed)} health check(s) failed on {catalog}: {', '.join(failed)}")
    echo(f"all {len(results)} health checks passed on {catalog} (run {run_id})")
    return results


def main(argv=None, spark=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--as-of-date", default="today")
    ap.add_argument("checks_file")
    args = ap.parse_args(argv)
    if spark is None:
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
    run(spark, args.checks_file, args.catalog, args.as_of_date)
    return 0


if __name__ == "__main__":
    main()                     # no sys.exit: a Databricks Python task runs inside IPython (see run_sql.py)
