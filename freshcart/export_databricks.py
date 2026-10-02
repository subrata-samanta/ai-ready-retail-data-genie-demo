"""Generate the Databricks DDL that is derived from files in this repo.

  contracts/gold/*.yml  -> databricks/gold/00_create_gold_tables.sql
  semantic/*.yaml       -> databricks/semantic/02_metric_views.sql

    python -m freshcart.export_databricks
"""
from __future__ import annotations

import re

from . import config as C
from . import contracts


def write_metric_views() -> None:
    parts = ["-- GENERATED from semantic/*.yaml by `python -m freshcart.export_databricks`. Do not edit by hand.\n"
             "-- Creates the Unity Catalog metric views. Requires Databricks Runtime 16.4+ (YAML spec 1.1 for\n"
             "-- synonyms, display names and formats). The local engine reads the very same YAML.\n"]
    for name in ["sales_metrics", "inventory_metrics"]:
        yml = (C.SEMANTIC_DIR / f"{name}.yaml").read_text(encoding="utf-8")
        body = "\n".join(line for line in yml.splitlines() if not line.startswith("#"))
        body = re.sub(rf"\b{C.CATALOG}\.(?=(bronze|silver|gold|semantic)\.)", f"{C.DATABRICKS_CATALOG}.", body)
        parts.append(f"CREATE OR REPLACE VIEW {C.DATABRICKS_CATALOG}.semantic.{name}\nWITH METRICS\nLANGUAGE YAML\nAS $$\n"
                     f"{body.strip()}\n$$;\n")
    parts.append(f"GRANT SELECT ON SCHEMA {C.DATABRICKS_CATALOG}.semantic TO `freshcart-business-users`;\n")
    path = C.ROOT / "databricks" / "semantic" / "02_metric_views.sql"
    path.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {path.relative_to(C.ROOT)}")


def main():
    contracts.write_databricks_ddl()
    write_metric_views()


if __name__ == "__main__":
    main()
