"""Run the medallion pipeline: bronze -> silver -> gold.

    python -m freshcart.pipeline                  # incremental run (only new raw files)
    python -m freshcart.pipeline --full-refresh   # rebuild every layer from the raw files
"""
from __future__ import annotations

import argparse
import time

from . import bronze, config as C, contracts
from .db import connect, run_sql_file, scalar

SILVER_STEPS = sorted((C.PIPELINE_DIR / "silver").glob("*.sql"))
GOLD_STEPS = sorted((C.PIPELINE_DIR / "gold").glob("*.sql"))


def run(full_refresh: bool = False, verbose: bool = True):
    t0 = time.time()
    con = connect(fresh_layers=C.LAYERS if full_refresh else None)

    if verbose:
        print("BRONZE  land raw files as delivered")
    bronze.ingest(con, verbose=verbose)

    if verbose:
        print("SILVER  clean, type, decode, conform")
    for step in SILVER_STEPS:
        run_sql_file(con, step)
        if verbose:
            print(f"  {step.name:<28} done")

    if verbose:
        print("GOLD    business star schema")
    contracts.create_gold_tables(con)
    for step in GOLD_STEPS:
        run_sql_file(con, step)
        if verbose:
            table = step.stem.split("_", 2)[-1]
            n = scalar(con, f"SELECT COUNT(*) FROM gold.{table}")
            print(f"  {step.name:<34} gold.{table:<22} {n:>8,} rows")

    violations = con.execute("PRAGMA gold.foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"Foreign key violations in gold: {violations[:5]}")
    if verbose:
        print(f"Finished in {time.time() - t0:.1f}s. Foreign keys in gold: all resolved.")
    return con


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full-refresh", action="store_true", help="drop all layers and rebuild from raw files")
    run(full_refresh=ap.parse_args().full_refresh)


if __name__ == "__main__":
    main()
