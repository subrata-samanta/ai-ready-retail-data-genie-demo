"""Central configuration for the FreshCart demo.

Everything that affects the numbers lives here so a run is fully reproducible.
"""
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Where things live
RAW_DIR = ROOT / "data" / "raw"            # the "landing zone": files exactly as source systems deliver them
WAREHOUSE_DIR = ROOT / "warehouse"          # local SQLite databases, one per layer (not committed)
EXPORT_DIR = ROOT / "data" / "exports"      # CSV snapshots of silver and gold (committed for browsing)
PIPELINE_DIR = ROOT / "pipeline"
CONTRACTS_DIR = ROOT / "contracts"
SEMANTIC_DIR = ROOT / "semantic"
GENIE_BUNDLE_DIR = ROOT / "genie_bundle"   # the Genie space (Declarative Automation Bundle)
DOCS_SRC_DIR = ROOT / "docs_src"
DOCS_DIR = ROOT / "docs"

LAYERS = ["bronze", "silver", "gold"]

# The business date the pipeline treats as "today".
# In production this is current_date(); pinning it keeps every number in the docs reproducible.
AS_OF_DATE = date(2026, 9, 27)              # a Sunday: the last completed fiscal week is FY2026 week 34

# Synthetic data range: all of FY2025 and FY2026 up to the as-of date
SALES_START = date(2025, 2, 2)              # first day of FY2025
SALES_END = date(2026, 9, 26)               # yesterday relative to AS_OF_DATE
INVENTORY_START = date(2026, 6, 28)         # last 13 fiscal weeks of snapshots
RANDOM_SEED = 42

# Unity Catalog names used by the Databricks version. The local engine maps
# freshcart.<layer>.<table> to the attached SQLite database <layer>.<table>.
CATALOG = "freshcart"
