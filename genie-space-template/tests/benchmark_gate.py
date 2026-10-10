"""
Benchmark gate for the Genie space: run every benchmark question of a deployed space
(Genie benchmark evaluation) and fail unless enough of them are answered correctly.

Benchmarks Genie cannot grade automatically come back as NEEDS_REVIEW; they are reported but do not
count towards the accuracy. The gate fails when:
  - the evaluation does not finish (or finishes with an error), or
  - no benchmark could be graded, or
  - accuracy on the graded benchmarks is below ACCURACY_THRESHOLD (default 0.60).

Usage:
    export GENIE_SPACE_ID="$(databricks bundle summary -t qa -o json \
        | jq -r '.resources.genie_spaces.genie_space.id')"
    python tests/benchmark_gate.py

Auth comes from your active Databricks CLI profile / DATABRICKS_* env vars,
exactly like the bundle commands. Requires: pip install databricks-sdk
"""

import os
import sys
import time

from databricks.sdk import WorkspaceClient

TIMEOUT_S = 1800
POLL_S = 10


def _value(x):
    """SDK enums -> their string value ("DONE", "GOOD", ...)."""
    return getattr(x, "value", x)


def run_benchmarks(w: WorkspaceClient, space_id: str):
    run = w.genie.genie_create_eval_run(space_id)
    print(f"Eval run started: {run.eval_run_id}")
    deadline = time.time() + TIMEOUT_S
    while _value(run.eval_run_status) in (None, "NOT_STARTED", "RUNNING"):
        if time.time() > deadline:
            sys.exit(f"GATE FAILED: the benchmark run did not finish within {TIMEOUT_S}s")
        time.sleep(POLL_S)
        run = w.genie.genie_get_eval_run(space_id, run.eval_run_id)
    status = _value(run.eval_run_status)
    if status != "DONE":
        sys.exit(f"GATE FAILED: the benchmark run ended with status {status}")

    # The list holds one entry per question; the grade is in each result's details.
    results, token = [], None
    while True:
        page = w.genie.genie_list_eval_results(space_id, run.eval_run_id, page_size=100, page_token=token)
        for r in page.eval_results or []:
            d = w.genie.genie_get_eval_result_details(space_id, run.eval_run_id, r.result_id)
            reasons = ", ".join(str(_value(x)) for x in (d.assessment_reasons or []))
            results.append((r.question, _value(d.assessment) or "NEEDS_REVIEW", reasons))
        token = page.next_page_token
        if not token:
            return results


def main() -> None:
    space_id = os.environ.get("GENIE_SPACE_ID")
    if not space_id:
        sys.exit("GENIE_SPACE_ID is not set (see the usage at the top of this file).")
    threshold = float(os.environ.get("ACCURACY_THRESHOLD", "0.60"))

    results = run_benchmarks(WorkspaceClient(), space_id)
    good = sum(1 for _, a, _ in results if a == "GOOD")
    bad = sum(1 for _, a, _ in results if a == "BAD")
    graded = good + bad
    accuracy = good / graded if graded else 0.0

    icons = {"GOOD": "✅", "BAD": "❌", "NEEDS_REVIEW": "❔"}
    print(f"\nBenchmark results: {good}/{graded} graded correct ({accuracy:.0%}), "
          f"{len(results) - graded} need review")
    for question, assessment, reasons in results:
        print(f"  {icons.get(assessment, '?')} {question}" + (f"  [{reasons}]" if assessment == "BAD" and reasons else ""))

    if not graded:
        sys.exit("GATE FAILED: no benchmark could be graded automatically")
    if accuracy < threshold:
        sys.exit(f"GATE FAILED: accuracy {accuracy:.0%} < threshold {threshold:.0%}")
    print("\nGate passed.")


if __name__ == "__main__":
    main()
