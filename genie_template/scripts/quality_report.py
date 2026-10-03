"""Show the output of the latest run of a bundle job (by default genie_quality) of a target, in the GitHub job summary.

    python scripts/quality_report.py -t qa                 the qa gate: accuracy, wrong answers with Genie's SQL
    python scripts/quality_report.py -t prod --job genie_usage

`databricks bundle run` only says whether the job passed. The approver of a prod deployment needs to see *how* qa
did, so the release runs this after the gate (pass or fail) and writes the job's log into the run summary.
Uses the Databricks CLI (DATABRICKS_CLI, default `databricks`) with the environment's authentication.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genie_tools as T                                        # noqa: E402

CLI = os.environ.get("DATABRICKS_CLI", "databricks")


def cli_json(*args, cwd=T.PROJECT_DIR):
    p = subprocess.run([CLI, *args, "-o", "json"], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"`databricks {' '.join(args)}` failed:\n{p.stderr.strip()}")
    out = p.stdout
    start = min([i for i in (out.find("{"), out.find("[")) if i >= 0], default=0)
    return json.loads(out[start:])


def latest_run_logs(target: str, job: str) -> tuple[dict, str]:
    jobs = cli_json("bundle", "summary", "-t", target).get("resources", {}).get("jobs", {})
    job_id = (jobs.get(job) or {}).get("id")
    if not job_id:
        raise SystemExit(f"job {job} is not deployed to {target}")
    runs = cli_json("jobs", "list-runs", "--job-id", str(job_id), "--limit", "1")
    runs = runs.get("runs", []) if isinstance(runs, dict) else runs
    if not runs:
        return {}, "(the job has not run yet)"
    run = cli_json("jobs", "get-run", str(runs[0]["run_id"]))
    logs = []
    for task in run.get("tasks") or [run]:
        out = cli_json("jobs", "get-run-output", str(task["run_id"]))
        logs.append((out.get("logs") or "") + (f"\nERROR: {out['error']}" if out.get("error") else ""))
    return run, "\n".join(logs).strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", "--target", required=True)
    ap.add_argument("--job", default="genie_quality")
    args = ap.parse_args(argv)
    run, logs = latest_run_logs(args.target, args.job)
    state = (run.get("state") or {}).get("result_state", "?")
    url = run.get("run_page_url", "")
    print(f"{args.job} in {args.target}: {state}  {url}\n{logs}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### {args.job} in {args.target}: {state}\n\n[job run]({url})\n\n```text\n{logs[-60000:]}\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
