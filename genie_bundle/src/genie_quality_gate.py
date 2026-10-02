"""Quality gate for the FreshCart Genie space. Runs as the task of the bundle job `genie_quality_gate`.

    mode=gate   run every benchmark of the space (Genie benchmark evaluation) and fail unless
                  - accuracy on the automatically graded benchmarks is >= min_accuracy (default 60%),
                  - at least min_graded benchmarks could be graded automatically, and
                  - at most max_bad benchmarks are answered incorrectly (-1, the default: no separate limit)
    mode=smoke  ask one question; fail unless Genie answers with SQL

A failed check raises SystemExit(1), so the job run fails and `databricks bundle run` exits non-zero,
which stops the release pipeline. Authentication is the job's identity (run_as) inside Databricks;
run locally, it uses the standard Databricks environment variables or profile.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta

from databricks.sdk import WorkspaceClient


def _value(x):
    return getattr(x, "value", x)


def _response(r) -> str:
    """The SQL (or text) of an evaluation response, or "" when the service did not return one."""
    text = getattr(r, "response", None)
    return text if isinstance(text, str) else ""


def run_benchmarks(w: WorkspaceClient, space_id: str, timeout_s: int = 1800, poll_s: int = 10) -> dict:
    run = w.genie.genie_create_eval_run(space_id)
    deadline = time.time() + timeout_s
    while _value(run.eval_run_status) in (None, "NOT_STARTED", "RUNNING"):
        if time.time() > deadline:
            raise TimeoutError(f"benchmark run {run.eval_run_id} did not finish within {timeout_s}s")
        time.sleep(poll_s)
        run = w.genie.genie_get_eval_run(space_id, run.eval_run_id)
    results, token = [], None
    while True:
        page = w.genie.genie_list_eval_results(space_id, run.eval_run_id, page_size=100, page_token=token)
        for r in page.eval_results or []:
            d = w.genie.genie_get_eval_result_details(space_id, run.eval_run_id, r.result_id)
            reasons = "; ".join(str(_value(x)) for x in (d.assessment_reasons or []))
            results.append({"question": r.question, "assessment": _value(d.assessment) or "NEEDS_REVIEW", "reason": reasons,
                            "genie_sql": _response(d.actual_response), "expected_sql": _response(d.expected_response)})
        token = page.next_page_token
        if not token:
            break
    return {"eval_run_id": run.eval_run_id, "status": _value(run.eval_run_status),
            "num_questions": run.num_questions or len(results), "num_correct": run.num_correct or 0,
            "num_needs_review": run.num_needs_review or 0,
            "num_bad": sum(1 for r in results if r["assessment"] == "BAD"), "results": results}


def evaluate(res: dict, *, min_accuracy: float, max_bad: int, min_graded: int) -> dict:
    graded = res["num_questions"] - res["num_needs_review"]
    accuracy = res["num_correct"] / graded if graded else 0.0
    reasons = []
    if res["status"] != "DONE":
        reasons.append(f"the benchmark run ended with status {res['status']}")
    if 0 <= max_bad < res["num_bad"]:
        reasons.append(f"{res['num_bad']} benchmark(s) answered incorrectly (allowed {max_bad})")
    if graded < min_graded:
        reasons.append(f"only {graded} benchmark(s) could be graded (need {min_graded})")
    if accuracy < min_accuracy:
        reasons.append(f"accuracy {accuracy:.0%} is below {min_accuracy:.0%}")
    return {**res, "graded": graded, "accuracy": accuracy, "passed": not reasons, "reasons": reasons}


def gate(w, space_id, *, min_accuracy, max_bad, min_graded) -> dict:
    res = evaluate(run_benchmarks(w, space_id), min_accuracy=min_accuracy, max_bad=max_bad, min_graded=min_graded)
    print(f"benchmark run {res['eval_run_id']}: {res['num_correct']}/{res['graded']} graded correct, "
          f"{res['num_bad']} bad, {res['num_needs_review']} need review")
    for r in res["results"]:
        if r["assessment"] == "BAD":
            print(f"  BAD  {r['question']}\n       {r['reason']}")
            if r.get("genie_sql"):                      # what Genie wrote: the starting point for a fix
                print("       Genie's SQL:\n" + "\n".join("         " + line for line in r["genie_sql"].splitlines()))
    print("GATE PASSED" if res["passed"] else "GATE FAILED: " + "; ".join(res["reasons"]))
    return res


def smoke(w, space_id, question, timeout_s: int = 600) -> dict:
    msg = w.genie.start_conversation_and_wait(space_id, question, timeout=timedelta(seconds=timeout_s))
    sql = next((a.query.query for a in (msg.attachments or []) if a.query and a.query.query), None)
    status = _value(msg.status)
    passed = status == "COMPLETED" and bool(sql)
    print(f"smoke test: {question!r} -> {status}{' with SQL' if sql else ' without SQL'}")
    if sql:
        print("  " + sql.replace("\n", "\n  "))
    print("SMOKE TEST PASSED" if passed else "SMOKE TEST FAILED")
    return {"status": status, "sql": sql, "passed": passed}


def main(argv=None, client=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--space-id", required=True)
    ap.add_argument("--mode", choices=["gate", "smoke"], default="gate")
    ap.add_argument("--min-accuracy", type=float, default=0.60)
    ap.add_argument("--max-bad", type=int, default=-1, help="-1: no separate limit on wrong answers")
    ap.add_argument("--min-graded", type=int, default=1)
    ap.add_argument("--smoke-question", default="What were net sales and margin by region last week?")
    args = ap.parse_args(argv)
    w = client or WorkspaceClient()
    if args.mode == "gate":
        ok = gate(w, args.space_id, min_accuracy=args.min_accuracy, max_bad=args.max_bad, min_graded=args.min_graded)["passed"]
    else:
        ok = smoke(w, args.space_id, args.smoke_question)["passed"]
    return 0 if ok else 1


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)        # a non-zero exit fails the job task, and with it `databricks bundle run`
