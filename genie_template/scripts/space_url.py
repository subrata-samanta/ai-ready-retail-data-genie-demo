"""Print `url=<link to the deployed Genie space>` for a target (a GitHub Actions step output).

    python scripts/space_url.py -t prod >> "$GITHUB_OUTPUT"

The release uses it as each GitHub environment's URL, so the deployment page links to the live space.
Uses the Databricks CLI (DATABRICKS_CLI, default `databricks`) and DATABRICKS_HOST.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def space_url(target: str) -> str:
    p = subprocess.run([os.environ.get("DATABRICKS_CLI", "databricks"), "bundle", "summary", "-t", target, "-o", "json"],
                       cwd=PROJECT_DIR, capture_output=True, text=True)
    if p.returncode != 0:
        return ""
    summary = json.loads(p.stdout[p.stdout.find("{"):])
    sid = ((summary.get("resources", {}).get("genie_spaces", {}) or {}).get("genie_space") or {}).get("id")
    host = (summary.get("workspace", {}).get("host") or os.environ.get("DATABRICKS_HOST", "")).rstrip("/")
    return f"{host}/genie/rooms/{sid}" if sid and host else ""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", "--target", required=True)
    print(f"url={space_url(ap.parse_args().target)}")
    sys.exit(0)
